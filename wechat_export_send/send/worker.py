"""发送 worker：真正操作微信窗口的进程，通常跑在桌面容器的网络命名空间里。

只做一件事：读 job.json，定位会话，校验，粘贴，点发送，到数据库里找回执。每个阶段写进
progress.json；点击发送之前一定先把 submitting 落盘，所以宿主即使等不到回执，也能分清
「肯定没点过」和「可能点过」。异常文本可能带聊天内容，一律不写进结果，只写类型和位置。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from .. import sqlcipher, wcdb
from ..config import IdMapper, atomic_write, conversation_kind
from .. import message_parse


class StageError(Exception):
    """已知原因的提交前失败。message 是写给人看的固定文字，不含聊天内容。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def load_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


class Progress:
    def __init__(self, job_dir: Path):
        self.path = Path(job_dir) / "progress.json"
        self.stage = "started"
        self.submitted = False
        self._write()

    def _write(self) -> None:
        atomic_write(self.path, json.dumps({"stage": self.stage, "submitted": self.submitted, "at": time.time()}))

    def set(self, stage: str) -> None:
        self.stage = stage
        self._write()

    def submit_state(self, started: bool) -> None:
        # True 在按下鼠标之前写；False 只有 XTest 明确拒绝按下时才写
        self.submitted = started
        self.stage = "submitting" if started else "submit_rejected"
        self._write()


def resolve_target(to: str, contacts: dict, mapper: IdMapper) -> dict:
    """把 --to 解析成唯一的会话；重名、搜索词能撞上别的会话都算失败，绝不猜。"""
    exact_id = [u for u in contacts if u == to or mapper.conversation(u, conversation_kind(u)) == to]
    if len(exact_id) == 1:
        username = exact_id[0]
    else:
        matches = [u for u, item in contacts.items()
                   if to and to in {item.get("display_name"), item.get("remark"), item.get("nick_name")}]
        if not matches:
            raise StageError("target_not_found", "找不到这个会话；--to 要写完整的会话名或 conversation_id")
        if len(matches) > 1:
            raise StageError("target_ambiguous", f"有 {len(matches)} 个会话同名；改用 conversation_id")
        username = matches[0]
    item = contacts[username]
    term = item.get("display_name") or username
    collisions = [u for u, other in contacts.items() if u != username and any(
        term.casefold() in str(other.get(field) or "").casefold() for field in ("display_name", "remark", "nick_name"))]
    if collisions:
        raise StageError("search_ambiguous", f"搜索词还会匹配另外 {len(collisions)} 个联系人或群；不能确定点到的是目标")
    return {"username": username, "term": term, "conversation_id": mapper.conversation(username, conversation_kind(username))}


def message_databases(root: Path, keys: dict) -> list[str]:
    found = []
    for path in sorted((root / "message").glob("message_*.db")):
        if path.stem.removeprefix("message_").isdigit():
            relative = path.relative_to(root).as_posix()
            if sqlcipher.key_for_rel(keys, relative):
                found.append(relative)
    if not found:
        raise StageError("no_message_db", "没有带校验密钥的消息库；先运行 keys capture")
    return found


def query_rows(database: Path, table: str, floor: int | None, start_epoch: int | None,
               *, message_type: int, include_content: bool) -> list[dict]:
    uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tables:
            return []
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        needed = {"local_id", "server_id", "local_type", "create_time", "origin_source"}
        if not needed <= columns:
            raise RuntimeError("message schema missing columns")
        if floor is None:
            maximum = connection.execute(f'SELECT MAX(local_id) FROM "{table}"').fetchone()[0]
            return [] if maximum is None else [{"local_id": int(maximum)}]
        names = ["local_id", "server_id", "local_type", "create_time", "origin_source"]
        if include_content:
            names += ["message_content", "compress_content", "WCDB_CT_message_content"]
        sql = (f'SELECT {", ".join(names)} FROM "{table}" WHERE local_id>? AND COALESCE(server_id,0)<>0 '
               "AND COALESCE(create_time,0)>=? AND COALESCE(origin_source,0)=1 "
               f"AND (local_type & 4294967295)={int(message_type)} ORDER BY create_time DESC, local_id DESC")
        return [dict(zip(names, row)) for row in connection.execute(sql, (floor, start_epoch))]
    finally:
        connection.close()


class _CachedShard:
    def __init__(self, main_sig, wal_sig, base_plain: Path, salt_file: Path, enc_key: bytes):
        self.main_sig = main_sig
        self.wal_sig = wal_sig
        self.base_plain = base_plain
        self.salt_file = salt_file
        self.enc_key = enc_key


class ShardCache:
    """复用未变化的主库解密副本，仅增量重解 WAL 并跳过未变更分片。"""

    def __init__(self, temp: Path):
        self.temp = Path(temp)
        self.temp.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.entries: dict[str, _CachedShard] = {}

    def _snapshot_work_db(self, src: Path, relative: str, key_info: dict, *, skip_if_unchanged: bool) -> Path | None:
        src_wal = Path(str(src) + "-wal")
        cur_main = sqlcipher._signature(src)
        cur_wal = sqlcipher._signature(src_wal)
        cached = self.entries.get(relative)
        if skip_if_unchanged and cached is not None and (cur_main, cur_wal) == (cached.main_sig, cached.wal_sig):
            return None

        stem = hashlib.sha256(relative.encode()).hexdigest()
        work_db = self.temp / f"{stem}.work.db"
        wal_copy = self.temp / f"{stem}.work.db-wal"

        if cached is not None and cur_main is not None and cur_main == cached.main_sig:
            stable = False
            for _ in range(3):
                before = (sqlcipher._signature(src), sqlcipher._signature(src_wal))
                if before[0] != cached.main_sig:
                    break
                if before[1] is not None:
                    shutil.copyfile(src_wal, wal_copy)
                    wal_copy.chmod(0o600)
                else:
                    wal_copy.unlink(missing_ok=True)
                if before == (sqlcipher._signature(src), sqlcipher._signature(src_wal)):
                    cur_wal = before[1]
                    stable = True
                    break
            if stable:
                try:
                    shutil.copyfile(cached.base_plain, work_db)
                    work_db.chmod(0o600)
                    if cur_wal is not None:
                        sqlcipher.patch_wal(wal_copy, work_db, cached.enc_key, source=cached.salt_file)
                    cached.wal_sig = cur_wal
                    return work_db
                except Exception:
                    work_db.unlink(missing_ok=True)
                    self.entries.pop(relative, None)
                finally:
                    wal_copy.unlink(missing_ok=True)

        enc_copy = self.temp / f"{stem}.encrypted"
        base_plain = self.temp / f"{stem}.base.db"
        salt_file = self.temp / f"{stem}.salt"
        stable = False
        before = (None, None)
        for _ in range(3):
            before = (sqlcipher._signature(src), sqlcipher._signature(src_wal))
            if before[0] is None:
                raise RuntimeError(f"source database disappeared during snapshot: {src.name}")
            shutil.copyfile(src, enc_copy)
            enc_copy.chmod(0o600)
            if before[1] is not None:
                shutil.copyfile(src_wal, wal_copy)
                wal_copy.chmod(0o600)
            else:
                wal_copy.unlink(missing_ok=True)
            if before == (sqlcipher._signature(src), sqlcipher._signature(src_wal)):
                stable = True
                break
        if not stable:
            enc_copy.unlink(missing_ok=True)
            wal_copy.unlink(missing_ok=True)
            raise RuntimeError(f"source database changed during all snapshot attempts: {src.name}")

        try:
            enc_key = bytes.fromhex(key_info["enc_key"])
            with enc_copy.open("rb") as inp:
                salt = inp.read(16)
            salt_file.write_bytes(salt)
            salt_file.chmod(0o600)
            sqlcipher.full_decrypt(enc_copy, base_plain, enc_key)
            shutil.copyfile(base_plain, work_db)
            work_db.chmod(0o600)
            if before[1] is not None:
                sqlcipher.patch_wal(wal_copy, work_db, enc_key, source=salt_file)
            self.entries[relative] = _CachedShard(before[0], before[1], base_plain, salt_file, enc_key)
            return work_db
        except Exception:
            work_db.unlink(missing_ok=True)
            base_plain.unlink(missing_ok=True)
            salt_file.unlink(missing_ok=True)
            self.entries.pop(relative, None)
            raise
        finally:
            enc_copy.unlink(missing_ok=True)
            wal_copy.unlink(missing_ok=True)

    def query(self, root: Path, keys: dict, relative: str, table: str, floor: int | None, start_epoch: int | None,
              *, message_type: int = 1, include_content: bool = False, skip_if_unchanged: bool = False) -> list[dict] | None:
        work_db = self._snapshot_work_db(root / relative, relative, sqlcipher.key_for_rel(keys, relative),
                                         skip_if_unchanged=skip_if_unchanged)
        if work_db is None:
            return None
        try:
            return query_rows(work_db, table, floor, start_epoch, message_type=message_type, include_content=include_content)
        finally:
            work_db.unlink(missing_ok=True)


def scan_all(root: Path, keys: dict, shards: list[str], table: str, temp: Path,
             floors: dict | None = None, start_epoch: int | None = None,
             *, message_type: int = 1, include_content: bool = False,
             cache: ShardCache | None = None, skip_if_unchanged: bool = False) -> dict[str, list[dict]]:
    result = {}
    if cache is not None:
        for relative in shards:
            rows = cache.query(
                root, keys, relative, table, None if floors is None else floors.get(relative, 0),
                start_epoch, message_type=message_type, include_content=include_content,
                skip_if_unchanged=skip_if_unchanged,
            )
            if rows is not None:
                result[relative] = rows
        return result
    for relative in shards:
        target = temp / (hashlib.sha256(relative.encode()).hexdigest() + ".db")
        plain = sqlcipher.decrypted_snapshot(root / relative, sqlcipher.key_for_rel(keys, relative), target, verify=False)
        try:
            result[relative] = query_rows(plain, table, None if floors is None else floors.get(relative, 0),
                                          start_epoch, message_type=message_type, include_content=include_content)
        finally:
            plain.unlink(missing_ok=True)
    return result


def highwaters(rows_by_shard: dict) -> dict[str, int]:
    return {shard: max((int(row.get("local_id") or 0) for row in rows), default=0) for shard, rows in rows_by_shard.items()}


def locate_new_self_text(rows_by_shard: dict, baseline: dict, start_epoch: int, expected_hash: str) -> dict | None:
    candidates = []
    for shard, rows in rows_by_shard.items():
        for row in rows:
            if int(row.get("local_id") or 0) <= baseline.get(shard, 0):
                continue
            if not int(row.get("server_id") or 0) or int(row.get("create_time") or 0) < start_epoch:
                continue
            if int(row.get("origin_source") or 0) != 1 or (int(row.get("local_type") or 0) & 0xFFFFFFFF) != 1:
                continue
            body = wcdb.decompress_content(row.get("message_content"), row.get("WCDB_CT_message_content"))
            packed = wcdb.decompress_content(row.get("compress_content"), None)
            decoded = body if body is not None else packed
            _, text = message_parse.split_group_sender(decoded)
            # 发的文字本身可能长得像「账号:\\n正文」，所以剥离前后都比
            forms = {text or "", decoded or ""}
            if any(hashlib.sha256(form.encode("utf-8")).hexdigest() == expected_hash for form in forms):
                candidates.append({**row, "source_db": shard})
    return candidates[0] if len(candidates) == 1 else None


def locate_new_self_image(rows_by_shard: dict, baseline: dict, start_epoch: int) -> dict | None:
    candidates = []
    for shard, rows in rows_by_shard.items():
        floor = baseline.get(shard, 0)
        for row in rows:
            if (int(row.get("local_id") or 0) > floor and int(row.get("server_id") or 0) and
                    int(row.get("create_time") or 0) >= start_epoch and
                    (int(row.get("local_type") or 0) & 0xFFFFFFFF) == 3 and int(row.get("origin_source") or 0) == 1):
                candidates.append({**row, "source_db": shard})
    return candidates[0] if len(candidates) == 1 else None


def capture_private(x11, window: dict, target: Path, redact_rect: list | None = None):
    image = x11.screenshot(window)
    if redact_rect:
        from PIL import ImageDraw
        x, y, width, height = map(int, redact_rect)
        ImageDraw.Draw(image).rectangle((x, y, x + width - 1, y + height - 1), fill=(0, 0, 0))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    image.save(target, format="PNG")
    os.chmod(target, 0o600)
    return image


def _select_conversation(x11, window: dict, calibration: dict, term: str) -> None:
    """在搜索框里输入完整名字，点第一条结果。非 ASCII 名字走剪贴板粘贴。"""
    x11.activate(window)
    x11.click(window, calibration["search_box"])
    time.sleep(0.2)
    x11.chord("Control_L", "a")
    x11.press("BackSpace")
    if all(32 <= ord(char) <= 126 for char in term):
        x11.type_ascii(term)
    else:
        x11.start_text_clipboard(term)
        x11.chord("Control_L", "v")
        time.sleep(float(calibration.get("paste_settle_seconds", 1.0)))
        x11.stop_clipboard()
    time.sleep(float(calibration.get("search_delay_seconds", 0.8)))
    x11.click(window, calibration["search_result"])
    time.sleep(float(calibration.get("switch_delay_seconds", 1.2)))


def header_is_blank(screen, calibration: dict) -> bool:
    """标题区域接近纯色时说明没有打开任何会话。这种模板绝不能存，也不算匹配。"""
    x, y, width, height = map(int, calibration["header_region"])
    crop = screen.crop((x, y, x + width, y + height)).convert("L")
    low, high = crop.getextrema()
    return high - low < int(calibration.get("header_min_contrast", 40))


def _header_ok(x11_mod, screen, calibration: dict, template: Path) -> bool:
    if header_is_blank(screen, calibration):
        return False
    ok, _ = x11_mod.image_match(screen, calibration["header_region"], template,
                                int(calibration.get("header_pixel_threshold", 18)),
                                float(calibration.get("header_max_bad_fraction", 0.001)))
    return ok


def _safe_reason(exc: Exception) -> str:
    """x11.py 自己抛的 RuntimeError 是写死的文字，不含聊天内容，可以带出去；别处的异常一律不带。"""
    trace = exc.__traceback__
    last = None
    while trace is not None:
        last = trace
        trace = trace.tb_next
    if last is not None and isinstance(exc, RuntimeError) and Path(last.tb_frame.f_code.co_filename).name == "x11.py":
        return str(exc)[:200]
    return ""


def run_job(job_dir: Path) -> dict:
    job_dir = Path(job_dir)
    job = load_json(job_dir / "job.json")
    progress = Progress(job_dir)
    result: dict = {"status": "failed", "stage": "preparing", "to": job.get("to", ""), "conversation_id": ""}
    from . import x11 as x11_mod
    display_x11 = None
    os.umask(0o077)
    temp = Path(tempfile.mkdtemp(prefix="wechat-send-", dir=job.get("temp_dir") or None))
    os.chmod(temp, 0o700)
    started = time.monotonic()
    budget = float(job.get("budget_seconds", 165))
    try:
        calibration = load_json(Path(job["calibration_file"]))
        if calibration.get("version") != 1:
            raise StageError("calibration_version", "ui-calibration.json 的 version 必须是 1")
        x11_mod._calibrated_window(calibration)
        root = Path(job["db_storage"])
        keys = sqlcipher.load_keys(Path(job["keys_file"]))
        mapper = IdMapper(bytes.fromhex(job["id_salt"]), job.get("id_mode", "hash"))
        contact_info = sqlcipher.key_for_rel(keys, "contact/contact.db")
        if not contact_info:
            raise StageError("no_contact_key", "没有联系人库的密钥；先运行 keys capture")

        progress.set("resolve_target")
        contact_plain = sqlcipher.decrypted_snapshot(root / "contact/contact.db", contact_info, temp / "contact.db", verify=False)
        contacts = wcdb.load_contacts(contact_plain)
        contact_plain.unlink(missing_ok=True)
        target = resolve_target(job["to"], contacts, mapper)
        result["conversation_id"] = target["conversation_id"]
        table = wcdb.message_table(target["username"])
        shards = message_databases(root, keys)

        mode = job["mode"]  # text、image、dry_run、calibrate
        template = Path(job["headers_dir"]) / (target["conversation_id"] + ".png")
        if mode != "calibrate" and not template.is_file():
            raise StageError("header_not_calibrated", "这个会话还没校准标题模板；先运行 wechat-send calibrate --to <会话>")

        progress.set("window")
        display_x11 = x11_mod.X11(str(calibration.get("display", ":99")))
        window = display_x11.find_window(calibration)

        progress.set("select_conversation")
        screen = display_x11.screenshot(window)
        current_only = bool(job.get("current"))
        if current_only and mode != "calibrate":
            raise StageError("current_only_calibrate", "--current 只能和 --calibrate 一起用")
        already_there = mode != "calibrate" and template.is_file() and _header_ok(x11_mod, screen, calibration, template)
        if not already_there and not current_only:
            # 当前打开的已经是目标会话时不搜索：少一次界面操作，也支持搜不到的会话（比如文件传输助手）
            _select_conversation(display_x11, window, calibration, target["term"])
            screen = display_x11.screenshot(window)

        if mode == "calibrate":
            if header_is_blank(screen, calibration):
                raise StageError("header_blank", "标题区域是空白的，没有选中会话，没有保存模板。在 noVNC 里打开目标会话后用 --calibrate --current")
            x, y, width, height = map(int, calibration["header_region"])
            template.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            screen.crop((x, y, x + width, y + height)).save(template, format="PNG")
            os.chmod(template, 0o600)
            check = template.with_suffix(".check.png")
            screen.save(check, format="PNG")
            os.chmod(check, 0o600)
            result.update(status="calibrated", stage="calibrate", detail="已保存标题模板；请打开 .check.png 或 noVNC 确认选中的确实是目标会话")
            return result

        progress.set("verify_header")
        if not _header_ok(x11_mod, screen, calibration, template):
            raise StageError("header_mismatch", "选中的会话和校准的标题模板不一致，没有提交")
        progress.set("verify_empty_draft")
        if not display_x11.composer_empty(window, calibration["compose"]):
            raise StageError("draft_not_empty", "输入框里已有内容；为了不动用户的草稿，没有提交")

        if mode == "dry_run":
            result.update(status="dry_run", stage="dry_run", detail="已定位会话并通过校验，没有提交")
            return result

        payload = Path(job["payload"]).read_bytes()
        payload_hash = hashlib.sha256(payload).hexdigest()
        if payload_hash != job["payload_sha256"]:
            raise StageError("payload_changed", "待发送内容在校验后变了，没有提交")
        message_type = 1 if mode == "text" else 3
        redact_y = int(calibration["header_region"][1]) + int(calibration["header_region"][3])
        redact = [0, redact_y, int(calibration["window"]["width"]), int(calibration["window"]["height"]) - redact_y]
        evidence = Path(job["evidence_dir"]) if job.get("evidence_dir") else None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

        shard_cache = ShardCache(temp)
        progress.set("baseline")
        baseline = highwaters(scan_all(root, keys, shards, table, temp, cache=shard_cache))
        progress.set("paste")
        if mode == "text":
            text = payload.decode("utf-8", "strict")
            display_x11.start_text_clipboard(text)
            display_x11.click(window, calibration["compose"])
            display_x11.chord("Control_L", "v")
            time.sleep(float(calibration.get("paste_settle_seconds", 1.0)))
            display_x11.stop_clipboard()
            if not display_x11.verify_text_draft(window, calibration["compose"], text):
                raise StageError("draft_mismatch", "粘贴进输入框的文字和要发的不完全一致，没有提交")
        else:
            display_x11.start_png_clipboard(payload)
            display_x11.click(window, calibration["compose"])
            display_x11.chord("Control_L", "v")
            if not display_x11._image_requested.wait(timeout=float(calibration.get("clipboard_wait_seconds", 5.0))):
                raise StageError("paste_not_requested", "微信没有从剪贴板取图片，没有提交")
            ready_deadline = time.monotonic() + float(calibration.get("image_ready_timeout_seconds", 45))
            while True:
                pasted = display_x11.screenshot(window)
                if x11_mod.send_button_ready(pasted, calibration["send_button_region"]):
                    break
                if time.monotonic() >= ready_deadline:
                    raise StageError("paste_not_ready", "图片粘贴后发送按钮一直没有就绪，没有提交")
                time.sleep(0.25)
        if display_x11._selection_error:
            raise StageError("clipboard_error", "剪贴板服务出错，没有提交")

        progress.set("final_checks")
        draft = display_x11.screenshot(window)
        if not _header_ok(x11_mod, draft, calibration, template):
            raise StageError("header_changed", "校验草稿时会话标题变了，没有提交")
        if evidence is not None:
            capture_private(display_x11, window, evidence / f"{stamp}-before-submit.png", redact)
        display_x11.click(window, calibration["compose"])
        display_x11.activate(window)
        if not display_x11.focus_within(window):
            raise StageError("focus_lost", "微信窗口拿不到输入焦点，没有提交")
        final_screen = display_x11.screenshot(window)
        if not _header_ok(x11_mod, final_screen, calibration, template):
            raise StageError("header_changed", "提交前最后一刻会话标题变了，没有提交")

        # 确认窗口：大图上传更久，等太短会把成功的发送误判成 unknown
        base = float(calibration.get("confirmation_timeout_seconds", job.get("confirm_base_seconds", 35)))
        confirm = base if mode == "text" else min(max(100.0, base), base + 3 * len(payload) / (1 << 20))
        remaining = budget - (time.monotonic() - started)
        if remaining < 15:
            raise StageError("deadline_before_submit", "--timeout 剩余时间不够确认回执，没有提交")
        confirm = min(confirm, remaining - 5)

        start_epoch = int(time.time())
        result["submitted_at"] = start_epoch
        display_x11.click(window, calibration["send_button"], on_submit_state=progress.submit_state)
        # ---- 从这里起可能已经发出去了：再出任何问题都只能报 unknown ----
        time.sleep(float(calibration.get("send_settle_seconds", 0.8)))
        progress.set("confirm")
        progress.submitted = True
        deadline = time.monotonic() + confirm
        receipt = None
        while time.monotonic() < deadline:
            rows = scan_all(root, keys, shards, table, temp, baseline, start_epoch,
                            message_type=message_type, include_content=mode == "text",
                            cache=shard_cache, skip_if_unchanged=True)
            receipt = locate_new_self_text(rows, baseline, start_epoch, payload_hash) if mode == "text" else \
                locate_new_self_image(rows, baseline, start_epoch)
            if receipt:
                break
            time.sleep(0.5)
        if evidence is not None:
            try:
                capture_private(display_x11, window, evidence / f"{stamp}-after-submit.png", redact)
            except Exception:
                pass
        if not receipt:
            result.update(status="unknown", stage="confirm", code="receipt_unconfirmed",
                          detail="已点击发送，但没有在数据库里确认到这条新消息；不要自动重发，请人工查看")
            return result
        result.update(status="sent", stage="done", detail="已提交，并在目标会话的数据库里确认到新消息",
                      receipt={"server_id": str(receipt["server_id"]), "local_id": int(receipt["local_id"]),
                               "create_time": int(receipt["create_time"]), "source_db": receipt["source_db"]})
        return result
    except StageError as exc:
        if progress.submitted:
            result.update(status="unknown", stage=progress.stage, code=exc.code, detail=exc.message)
        else:
            result.update(status="failed", stage=progress.stage, code=exc.code, detail=exc.message)
        return result
    except Exception as exc:
        # 异常文本可能带私密内容，只留类型
        status = "unknown" if progress.submitted else "failed"
        detail = "提交后出错；不要自动重发，请人工查看" if progress.submitted else "提交前出错，确定没有发出去"
        result.update(status=status, stage=progress.stage, code=type(exc).__name__, detail=detail)
        reason = _safe_reason(exc)
        if reason:
            result["reason"] = reason
        return result
    finally:
        result["submitted"] = progress.submitted
        if display_x11 is not None:
            try:
                if not progress.submitted and result.get("status") == "failed":
                    display_x11.dismiss_popups({})  # 失败时别把搜索下拉框留在屏幕上，否则下一次会被当成意外窗口
            except Exception:
                pass
            try:
                display_x11.stop_clipboard()
                display_x11.close()
            except Exception:
                pass
        shutil.rmtree(temp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wechat-send 的内部 worker，不要直接调用")
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_job(args.job)
    atomic_write(args.job / "result.json", json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
