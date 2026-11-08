"""增量导出成 chat-export/v1（格式见 docs/CONTRACT.md）。

源数据库只读：先把数据库和 WAL 复制到实例私有的临时目录，校验并解密后再读，读完就删。
游标存在 export-root/state/（消费方不读）和实例 state/ 下。崩溃时宁可多写一行：
消息行先落盘，游标后更新；消费方本来就按 (conversation_id, id) 去重。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__, media as media_mod, message_parse, sqlcipher, wcdb
from .config import IdMapper, Instance, atomic_write, conversation_kind

SCHEMA = "chat-export/v1"
FLUSH_LINES = 5000  # 攒这么多行就落盘，库再大也不会把消息全留在内存里
_DEFERRED = "deferred"


class ExportBusy(RuntimeError):
    pass


def _repair_jsonl_tail(path: Path) -> None:
    """上次写到一半崩了就把残缺的最后一行截掉，否则下一行会粘在它后面。"""
    if not path.is_file():
        return
    with path.open("r+b") as stream:
        size = stream.seek(0, os.SEEK_END)
        if not size:
            return
        stream.seek(size - 1)
        if stream.read(1) == b"\n":
            return
        position = size
        while position > 0:
            step = min(65536, position)
            position -= step
            stream.seek(position)
            block = stream.read(step)
            cut = block.rfind(b"\n")
            if cut >= 0:
                stream.truncate(position + cut + 1)
                return
        stream.truncate(0)


class Source:
    """把一个账号目录抽象出来，demo 和真实实例走同一条路。"""

    def __init__(self, db_storage: Path, keys: dict, workdir: Path):
        self.root = Path(db_storage)
        self.keys = keys
        self.work = Path(workdir)
        self.account_dir = self.root.parent

    def decrypt(self, relative: str) -> Path | None:
        info = sqlcipher.key_for_rel(self.keys, relative)
        source = self.root / relative
        if not info or not source.is_file():
            return None
        target = self.work / hashlib.sha256(relative.encode()).hexdigest()
        return sqlcipher.decrypted_snapshot(source, info, target)

    def shards(self) -> list[str]:
        found = []
        for path in sorted((self.root / "message").glob("message_*.db")):
            if path.stem.removeprefix("message_").isdigit():
                found.append(path.relative_to(self.root).as_posix())
        return found

    def metadata(self) -> tuple[dict, dict, dict]:
        contacts, sessions, rooms = {}, {}, {}
        for relative in ("contact/contact.db", "session/session.db"):
            plain = self.decrypt(relative)
            if plain is None:
                continue
            contacts.update(wcdb.load_contacts(plain))
            sessions.update(wcdb.load_sessions(plain))
            rooms.update(wcdb.load_rooms(plain))
            plain.unlink(missing_ok=True)
        return contacts, sessions, rooms


class Exporter:
    def __init__(self, source: Source, export_root: Path, settings: dict, mapper: IdMapper,
                 *, account_label: str, image_keys: tuple | None = None, state_root: Path | None = None,
                 now: float | None = None):
        self.source = source
        self.root = Path(export_root)
        self.settings = settings
        self.mapper = mapper
        self.account_label = account_label
        self.image_keys = image_keys
        self.state_root = Path(state_root) if state_root else self.root / "state"
        self.now = now
        self._buffered = 0
        self._known_senders: set[str] = set()
        self.stats = {"conversations": 0, "new_lines": 0, "rewritten_lines": 0, "media_ready": 0, "media_pending": 0,
                      "media_missing": 0, "shards_scanned": 0, "shards_skipped": 0, "errors": []}

    # ---- 状态 ----
    def _load_state(self) -> dict:
        try:
            state = json.loads((self.state_root / "cursors.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        return {"shards": state.get("shards", {}), "conversations": state.get("conversations", {})}

    def _save_state(self, state: dict) -> None:
        atomic_write(self.state_root / "cursors.json", json.dumps(state, ensure_ascii=False, separators=(",", ":")))

    # ---- 主流程 ----
    def run(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for sub in ("conversations", "members", "media", "state"):
            (self.root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_root / "export.lock"
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ExportBusy("另一个导出正在运行") from None
            return self._run_locked()

    def _run_locked(self) -> dict:
        state = self._load_state()
        contacts, sessions, rooms = self.source.metadata()
        names = dict(contacts)
        known = set(contacts) | set(sessions) | set(rooms)
        now = int(self.now if self.now is not None else time.time())
        since = int(self.settings.get("since") or 0)
        lookback = int(self.settings.get("lookback_seconds") or 0)
        resources = self._resource_map()
        batches: dict[str, list[dict]] = {}
        for relative in self.source.shards():
            shard_state = state["shards"].setdefault(relative, {"tables": {}})
            signature = sqlcipher.snapshot_signature(self.source.root / relative)
            has_pending = any(item.get("shard") == relative
                              for conv in state["conversations"].values() for item in conv.get("pending", []))
            if shard_state.get("signature") == signature and not has_pending:
                self.stats["shards_skipped"] += 1
                continue
            plain = self.source.decrypt(relative)
            if plain is None:
                self.stats["errors"].append({"shard": relative, "error": "no_verified_key"})
                continue
            try:
                self._scan_shard(relative, plain, shard_state, state, known, names, rooms, resources,
                                 since, lookback, now, batches)
                shard_state["signature"] = signature
                self.stats["shards_scanned"] += 1
            finally:
                plain.unlink(missing_ok=True)
        self._write_batches(batches, state)
        self._write_members(state, rooms, names)
        self._write_manifest(state, names, now)
        self._save_state(state)
        return self.stats

    def _resource_map(self) -> dict:
        if not self.settings.get("media", {}).get("enabled"):
            return {}
        plain = self.source.decrypt("message/message_resource.db")
        if plain is None:
            return {}
        try:
            return media_mod.load_resource_map(plain)
        finally:
            plain.unlink(missing_ok=True)

    def _wanted(self, username: str, display: str, kind: str) -> bool:
        if kind not in self.settings.get("kinds", []):
            return False
        include = self.settings.get("include") or []
        exclude = self.settings.get("exclude") or []
        identifiers = {username, display, self.mapper.conversation(username, kind)}
        if include and not identifiers.intersection(include):
            return False
        return not identifiers.intersection(exclude)

    def _scan_shard(self, relative, plain, shard_state, state, known, names, rooms, resources,
                    since, lookback, now, batches) -> None:
        connection = wcdb.open_ro(plain)
        try:
            tables = wcdb.table_names(connection)
            sender_ids = wcdb.load_name2id(connection)
            self._known_senders = set(sender_ids.values()) | set(names)
            by_table = {wcdb.message_table(name): name for name in set(known) | set(sender_ids.values())}
            for table in sorted(name for name in tables if name.startswith("Msg_")):
                username = by_table.get(table)
                if username is None:
                    # 对不上账号的表说明元数据不全，不猜它是谁
                    self.stats["errors"].append({"shard": relative, "table": table, "error": "unmapped_conversation"})
                    continue
                kind = conversation_kind(username)
                display = names.get(username, {}).get("display_name") or ""
                if not self._wanted(username, display, kind):
                    continue
                conversation_id = self.mapper.conversation(username, kind)
                table_state = shard_state["tables"].setdefault(table, {"local_id": 0, "recent": {}})
                self._scan_table(connection, relative, table, username, kind, display, conversation_id,
                                 table_state, state, sender_ids, names, rooms, resources, since, lookback, now, batches)
        finally:
            connection.close()

    def _scan_table(self, connection, relative, table, username, kind, display, conversation_id, table_state,
                    state, sender_ids, names, rooms, resources, since, lookback, now, batches) -> None:
        # floor 在这一轮里不能变：真实库里 local_id 和时间顺序不一致（同步历史时会逆序），
        # 边扫边用推进后的游标判断，会把时间更晚、ID 更小的行跳过
        floor = int(table_state.get("local_id") or 0)
        cursor = floor
        recent: dict = table_state.get("recent") or {}
        window = max(since, now - lookback) if floor else since
        rows = wcdb.read_messages(connection, table, after_local_id=floor, since_ts=window)
        conversation = state["conversations"].setdefault(conversation_id, {
            "kind": kind, "name": display,
            "file": f"conversations/{conversation_id}.jsonl", "message_count": 0, "last_ts": 0, "pending": []})
        conversation.update(kind=kind, name=display)
        if kind == "group":
            conversation["members_file"] = f"members/{conversation_id}.json"
        attach_index = None
        kept_recent: dict = {}
        room = rooms.get(username, {})
        for row in rows:
            created = int(row.get("create_time") or 0)
            local_id = int(row.get("local_id") or 0)
            if created < since:
                cursor = max(cursor, local_id)
                continue
            fingerprint = _row_fingerprint(row)
            key = str(local_id)
            if created >= now - lookback:
                kept_recent[key] = fingerprint
            if recent.get(key) == fingerprint or (local_id <= floor and key not in recent):
                # 已经写过且没变；或者比游标旧又不在回看表里（升级后第一次运行时的旧行）
                continue
            is_self = int(row.get("origin_source") or 0) == 1
            if is_self and not int(row.get("server_id") or 0) and created >= now - lookback:
                # 自己发的消息还没拿到服务器 ID，等下一轮，免得同一条消息换 ID 出现两次。
                # 记进回看表，游标被后面的行推过去也不会漏掉它
                kept_recent[key] = _DEFERRED
                continue
            if attach_index is None and self.settings.get("media", {}).get("enabled"):
                attach_index = media_mod.index_attach_dir(self.source.account_dir, username)
            message = self._build_message(row, relative, username, kind, sender_ids, names, room, resources, attach_index or {})
            rewritten = key in recent and recent[key] != _DEFERRED
            batches.setdefault(conversation_id, []).append(message)
            self._buffered += 1
            if self._buffered >= FLUSH_LINES:
                self._write_batches(batches, state)
                batches.clear()
                self._buffered = 0
            self.stats["rewritten_lines" if rewritten else "new_lines"] += 1
            if not rewritten:
                conversation["message_count"] = int(conversation.get("message_count") or 0) + 1
            conversation["last_ts"] = max(int(conversation.get("last_ts") or 0), created)
            cursor = max(cursor, local_id)
            self._track_pending(conversation, message, row, relative, local_id)
        self._recheck_pending(conversation, relative, username, resources, attach_index, batches, now)
        table_state["local_id"] = cursor
        table_state["recent"] = kept_recent

    # ---- 单条消息 ----
    def _build_message(self, row, relative, username, kind, sender_ids, names, room, resources, attach_index) -> dict:
        base_type, subtype = wcdb.split_msg_type(row.get("local_type"))
        body = wcdb.decompress_content(row.get("message_content"), row.get("WCDB_CT_message_content"))
        packed = wcdb.decompress_content(row.get("compress_content"), None)
        parsed = message_parse.parse_message(base_type, subtype, body if body is not None else packed,
                                             sender_known=lambda value: kind == "group" and value in self._known_senders)
        server_id = int(row.get("server_id") or 0)
        local_id = int(row.get("local_id") or 0)
        shard_no = Path(relative).stem.removeprefix("message_")
        message_id = str(server_id) if server_id else f"l{shard_no}_{local_id}"
        is_self = int(row.get("origin_source") or 0) == 1
        message: dict = {"id": message_id, "ts": int(row.get("create_time") or 0), "type": parsed["type"]}
        if parsed["type"] in {"system", "revoke"}:
            message["sender_id"] = "system"
        else:
            account = parsed.get("group_sender") or sender_ids.get(int(row.get("real_sender_id") or 0), "")
            if not account and kind == "private" and not is_self:
                account = username
            message["sender_id"] = self.mapper.sender(account) if account else "unknown"
            name = (room.get("nicknames") or {}).get(account) or names.get(account, {}).get("display_name")
            if name:
                message["sender_name"] = name
        if is_self:
            message["is_self"] = True
        if parsed.get("text"):
            message["text"] = parsed["text"]
        for field in ("reply_to", "forward", "link"):
            if parsed.get(field):
                message[field] = parsed[field]
        mentions = self._mentions(row)
        if mentions:
            message["mentions"] = mentions
        message["raw_type"] = parsed["raw_type"]
        media_items = self._media_for(parsed, body, row, username, resources, attach_index)
        if media_items:
            message["media"] = media_items
        return message

    def _mentions(self, row) -> list[str]:
        try:
            source_xml = wcdb.decompress_content(row.get("source"), None)
        except ValueError:
            return []
        found = message_parse.parse_mentions(source_xml)
        return ["@all" if item == "@all" else self.mapper.sender(item) for item in found]

    def _media_for(self, parsed, body, row, username, resources, attach_index) -> list[dict]:
        settings = self.settings.get("media", {})
        if parsed["type"] == "image":
            return self._image_item(body, row, username, resources, attach_index, settings)
        if parsed["type"] == "file":
            return self._file_item(parsed, settings)
        if parsed["type"] in {"voice", "video", "sticker"}:
            self.stats["media_missing"] += 1
            return [{"kind": parsed["type"], "status": "missing"}]
        return []

    def _resolved_image_keys(self) -> tuple:
        if self.image_keys is None:
            try:
                self.image_keys = media_mod.image_key(self.source.root)
            except (OSError, ValueError, KeyError):
                self.image_keys = (None, None)
        return self.image_keys

    def _image_item(self, body, row, username, resources, attach_index, settings) -> list[dict]:
        _, content = message_parse.split_group_sender(body)
        attributes = message_parse.parse_image_attrs(content)
        advertised = bool(attributes.get("cdnbigimgurl") or attributes.get("cdnmidimgurl"))
        fallback = {"kind": "image", "status": "pending" if advertised else "missing"}
        if not settings.get("enabled"):
            return [fallback]
        aliases = self._image_aliases(row, username, resources, attributes)
        candidates = [path for alias in aliases for path in attach_index.get(alias, [])]
        aes, xor = self._resolved_image_keys()
        found = media_mod.best_cached_image(candidates, attributes, aes, xor, allow_thumbnail=False) if candidates else None
        items = []
        if found:
            items.append(self._stored_image(found))
        else:
            self.stats["media_pending" if advertised else "media_missing"] += 1
            items.append(fallback)
            if settings.get("thumbnails") and candidates:
                # 缩略图单独成一项并明确标记，绝不顶替原图
                thumb = media_mod.best_cached_image(candidates, attributes, aes, xor, allow_thumbnail=True)
                if thumb:
                    items.append(self._stored_image(thumb))
        return items

    @staticmethod
    def _image_aliases(row, username, resources, attributes) -> set[str]:
        aliases = media_mod.image_aliases(resources.get((username, int(row.get("local_id") or 0))), attributes)
        packed = wcdb.packed_resource_md5(row.get("packed_info_data"))
        if packed:
            aliases.add(packed)
        return aliases

    def _stored_image(self, found: dict) -> dict:
        fmt = found["format"]
        ext = media_mod.EXTENSIONS.get(fmt, fmt)
        relative, digest = media_mod.store_content(self.root, found["data"], ext)
        self.stats["media_ready"] += 1
        return {"kind": "image", "path": relative, "sha256": digest, "mime": media_mod.MIME_TYPES.get(fmt, "application/octet-stream"),
                "bytes": len(found["data"]), "status": "ready", "is_thumbnail": bool(found["is_thumbnail"]),
                "source_quality": found["quality"]}

    def _file_item(self, parsed, settings) -> list[dict]:
        name = parsed.get("file_name") or ""
        item: dict = {"kind": "file", "status": "missing"}
        if name:
            item["name"] = name
        if parsed.get("file_size"):
            item["bytes"] = parsed["file_size"]
        if not settings.get("enabled") or not settings.get("files") or not name or "/" in name or name in {".", ".."}:
            self.stats["media_missing"] += 1
            return [item]
        expected = parsed.get("file_size")
        for path in sorted((self.source.account_dir / "msg" / "file").glob(f"*/{_glob_escape(name)}")):
            try:
                size = path.stat().st_size
                if (expected and size != expected) or size > int(settings.get("max_bytes") or 0):
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            ext = "".join(c for c in Path(name).suffix.lstrip(".").lower() if c.isalnum())[:12] or "bin"
            relative, digest = media_mod.store_content(self.root, data, ext)
            self.stats["media_ready"] += 1
            item.update(path=relative, sha256=digest, bytes=len(data), status="ready", mime="application/octet-stream", is_thumbnail=False)
            return [item]
        self.stats["media_missing"] += 1
        return [item]

    # ---- 还没下载到的图片：之后再看，有了就用同一个 id 重写一行 ----
    def _track_pending(self, conversation, message, row, relative, local_id) -> None:
        items = message.get("media") or []
        if not any(item.get("status") == "pending" for item in items):
            return
        days = int(self.settings.get("media", {}).get("pending_days") or 0)
        now = int(self.now if self.now is not None else time.time())
        if days <= 0 or int(message.get("ts") or 0) < now - days * 86400:
            return  # 太旧的图不再等，否则第一次导出全部历史时待办表会撑爆
        body = wcdb.decompress_content(row.get("message_content"), row.get("WCDB_CT_message_content"))
        _, content = message_parse.split_group_sender(body)
        attributes = message_parse.parse_image_attrs(content)
        kept = {name: attributes[name] for name in ("md5", "length", "hdlength") if name in attributes}
        pending = conversation.setdefault("pending", [])
        pending[:] = [entry for entry in pending if entry["message"]["id"] != message["id"]]
        packed = wcdb.packed_resource_md5(row.get("packed_info_data"))
        pending.append({"message": message, "shard": relative, "local_id": local_id, "attributes": kept, "packed_md5": packed})

    def _recheck_pending(self, conversation, relative, username, resources, attach_index, batches, now) -> None:
        pending = conversation.get("pending") or []
        if not pending:
            return
        days = int(self.settings.get("media", {}).get("pending_days") or 0)
        keep = []
        index = attach_index
        for entry in pending:
            if entry.get("shard") != relative:
                keep.append(entry)
                continue
            message = entry["message"]
            if int(message.get("ts") or 0) < now - days * 86400:
                continue
            if index is None:
                index = media_mod.index_attach_dir(self.source.account_dir, username)
            found = self._find_pending(entry, username, resources, index)
            if not found:
                keep.append(entry)
                continue
            message["media"] = [self._stored_image(found)] + [
                item for item in message.get("media", []) if item.get("is_thumbnail")]
            batches.setdefault(Path(conversation["file"]).stem, []).append(message)
            self.stats["rewritten_lines"] += 1
        conversation["pending"] = keep

    def _find_pending(self, entry, username, resources, index) -> dict | None:
        attributes = entry.get("attributes") or {}
        aliases = media_mod.image_aliases(resources.get((username, int(entry.get("local_id") or 0))), attributes)
        if entry.get("packed_md5"):
            aliases.add(entry["packed_md5"])
        candidates = [path for alias in aliases for path in index.get(alias, [])]
        if not candidates:
            return None
        aes, xor = self._resolved_image_keys()
        return media_mod.best_cached_image(candidates, attributes, aes, xor, allow_thumbnail=False)

    # ---- 落盘 ----
    def _write_batches(self, batches: dict, state: dict) -> None:
        for conversation_id, messages in batches.items():
            path = self.root / "conversations" / f"{conversation_id}.jsonl"
            _repair_jsonl_tail(path)
            messages.sort(key=lambda item: (item["ts"], item["id"]))
            lines = "".join(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n" for message in messages)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(lines)
                out.flush()
                os.fsync(out.fileno())

    def _write_members(self, state: dict, rooms: dict, names: dict) -> None:
        if not self.settings.get("members"):
            return
        for username, room in rooms.items():
            kind = conversation_kind(username)
            conversation_id = self.mapper.conversation(username, kind)
            if conversation_id not in state["conversations"]:
                continue
            members = []
            accounts = set(room.get("members") or []) | set(room.get("nicknames") or {})
            for account in sorted(accounts):
                contact = names.get(account, {})
                entry = {"id": self.mapper.sender(account)}
                nickname = (room.get("nicknames") or {}).get(account)
                if nickname:
                    entry["name"] = nickname
                if contact.get("display_name"):
                    entry["display_name"] = contact["display_name"]
                    entry.setdefault("name", contact["display_name"])
                members.append(entry)
            if not members:
                continue
            payload = {"conversation_id": conversation_id,
                       "updated_at": int(self.now if self.now is not None else time.time()), "members": members}
            atomic_write(self.root / "members" / f"{conversation_id}.json", json.dumps(payload, ensure_ascii=False, indent=1) + "\n")

    def _write_manifest(self, state: dict, names: dict, now: int) -> None:
        zone = self.settings.get("timezone") or "UTC"
        conversations = []
        for conversation_id, item in sorted(state["conversations"].items()):
            entry = {"id": conversation_id, "kind": item["kind"], "name": item.get("name") or "",
                     "file": item["file"], "last_ts": int(item.get("last_ts") or 0),
                     "message_count": int(item.get("message_count") or 0)}
            members_file = item.get("members_file")
            if members_file and (self.root / members_file).is_file():
                entry["members_file"] = members_file
            conversations.append(entry)
        self.stats["conversations"] = len(conversations)
        manifest = {"schema": SCHEMA, "producer": {"name": "wechat-export-send", "version": __version__},
                    "source": {"platform": "wechat", "account_id": self.mapper.account(self.account_label)},
                    "generated_at": datetime.fromtimestamp(now, ZoneInfo(zone)).isoformat(timespec="seconds"),
                    "timezone": zone, "conversations": conversations}
        atomic_write(self.root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _glob_escape(name: str) -> str:
    return "".join(f"[{c}]" if c in "*?[" else c for c in name)


def _row_fingerprint(row: dict) -> str:
    digest = hashlib.sha256()
    for key in ("local_type", "server_id", "message_content", "compress_content"):
        value = row.get(key)
        if isinstance(value, memoryview):
            value = value.tobytes()
        data = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:16]


def load_image_keys(instance: Instance) -> tuple | None:
    """优先读 scan-image-key 存下的密钥；没有就让导出时从客户端配置里算。"""
    try:
        value = json.loads(instance.image_key_file.read_text(encoding="utf-8"))
        return value["image_aes_key"], int(value["image_xor_key"])
    except (OSError, KeyError, ValueError):
        return None


def export_instance(instance: Instance) -> dict:
    db_storage = instance.db_storage()
    keys = sqlcipher.load_keys(instance.keys_file)
    if not any(rel.startswith("message/message_") for rel in keys):
        raise RuntimeError("keys/all_keys.json 里没有通过校验的消息库密钥；先运行 keys capture")
    instance.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = tempfile.mkdtemp(prefix="export-", dir=instance.state)
    try:
        os.chmod(temporary, 0o700)
        source = Source(db_storage, keys, Path(temporary))
        mapper = IdMapper(instance.id_salt(), instance.config["export"]["id_mode"])
        exporter = Exporter(source, instance.export_root, instance.config["export"], mapper,
                            account_label=db_storage.parent.name, image_keys=load_image_keys(instance),
                            state_root=instance.state / "export")
        return exporter.run()
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


__all__ = ["Exporter", "Source", "ExportBusy", "export_instance", "SCHEMA"]
