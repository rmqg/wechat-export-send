from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from wechat_export_send import avatars, exporter, sqlcipher, synthetic, wcdb
from wechat_export_send.config import DEFAULTS, IdMapper, _merge

NOW = synthetic.START + 30000


@pytest.fixture
def account(tmp_path):
    info = synthetic.build_account(tmp_path)
    return tmp_path, info


def run(root: Path, info: dict, *, now=NOW, settings=None, mode="hash"):
    config = _merge(DEFAULTS, {})["export"]
    config.update(settings or {})
    keys = sqlcipher.load_keys(root / "keys" / "all_keys.json")
    work = root / "work"
    work.mkdir(exist_ok=True)
    source = exporter.Source(Path(info["storage"]), keys, work)
    return exporter.Exporter(source, root / "export", config, IdMapper(b"test-salt", mode), account_label=info["account_dir"],
                             image_keys=(synthetic.PUBLIC_IMAGE_AES, synthetic.PUBLIC_IMAGE_XOR), now=now).run()


def lines(root: Path, kind: str = "group") -> list[dict]:
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    item = next(entry for entry in manifest["conversations"] if entry["kind"] == kind)
    return [json.loads(line) for line in (root / "export" / item["file"]).read_text(encoding="utf-8").splitlines()]


def test_contract_shape(account):
    root, info = account
    run(root, info)
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "chat-export/v1" and manifest["source"]["platform"] == "wechat"
    assert manifest["producer"]["name"] == "wechat-export-send"
    allowed_types = {"text", "image", "file", "link", "voice", "video", "sticker", "quote", "forward", "system", "revoke", "other"}
    for item in manifest["conversations"]:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", item["id"])
        assert item["kind"] in {"group", "private", "channel", "other"}
        for message in [json.loads(line) for line in (root / "export" / item["file"]).read_text(encoding="utf-8").splitlines()]:
            assert isinstance(message["id"], str) and isinstance(message["ts"], int) and isinstance(message["sender_id"], str)
            assert message["type"] in allowed_types
            for media in message.get("media", []):
                assert media["status"] in {"ready", "pending", "missing"}
                if media["status"] == "ready":
                    data = (root / "export" / media["path"]).read_bytes()
                    assert len(data) == media["bytes"]
                    import hashlib
                    assert hashlib.sha256(data).hexdigest() == media["sha256"]
    assert (root / "export").stat().st_mode & 0o077 == 0
    assert (root / "export" / "members" / (manifest["conversations"][0]["id"] + ".json")).is_file()


def _group_export(root: Path) -> tuple[dict, dict]:
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    group = next(item for item in manifest["conversations"] if item["kind"] == "group")
    members = json.loads((root / "export" / group["members_file"]).read_text(encoding="utf-8"))["members"]
    return group, {member["id"]: member for member in members}


def test_avatars_exported_for_members_and_group(account):
    root, info = account
    stats = run(root, info)
    group, members = _group_export(root)
    mapper = IdMapper(b"test-salt", "hash")
    expected = {synthetic.ALICE: 40, synthetic.BOB: 120}
    for person, tint in expected.items():
        relative = members[mapper.sender(person)]["avatar"]
        assert re.fullmatch(r"media/[0-9a-f]{2}/[0-9a-f]{64}\.png", relative)
        assert (root / "export" / relative).read_bytes() == synthetic._png(32, 32, tint)
    assert "avatar" not in members[mapper.sender(synthetic.CEN)]  # 本地没有缓存，不猜
    assert (root / "export" / group["avatar"]).read_bytes() == synthetic._png(32, 32, 200)
    assert stats["avatars"] == 4 and not stats["errors"]  # 两个成员 + 群 + 其中一人的单聊


def test_avatars_can_be_turned_off(account):
    root, info = account
    run(root, info, settings={"avatars": False})
    group, members = _group_export(root)
    assert "avatar" not in group and all("avatar" not in member for member in members.values())


def test_avatars_survive_when_the_client_cache_goes_away(account):
    root, info = account
    run(root, info)
    before = _group_export(root)
    (Path(info["storage"]) / "head_image" / "head_image.db").unlink()
    stats = run(root, info, now=NOW + 300)
    assert _group_export(root) == before and not stats["errors"]


AVATAR_URL = "https://wx.qlogo.cn/mmhead/synthetic/0"


def _with_contact_urls(monkeypatch, urls: dict):
    original = wcdb.load_contacts

    def patched(path):
        result = original(path)
        for username, url in urls.items():
            if username in result:
                result[username]["head_urls"] = [url]
        return result

    monkeypatch.setattr(wcdb, "load_contacts", patched)


def test_avatar_missing_locally_is_queued_then_merged(account, monkeypatch):
    root, info = account
    _with_contact_urls(monkeypatch, {synthetic.CEN: AVATAR_URL})
    stats = run(root, info)
    state = root / "export" / "state"
    queue = json.loads((state / avatars.QUEUE_FILE).read_text(encoding="utf-8"))
    assert [item["urls"] for item in queue.values()] == [[AVATAR_URL]] and stats["avatars_queued"] == 1
    assert "avatar" not in _group_export(root)[1][IdMapper(b"test-salt", "hash").sender(synthetic.CEN)]

    calls = []
    picture = synthetic._png(32, 32, 77)
    result = avatars.download_pending(root / "export", state, fetcher=lambda url, timeout: calls.append(url) or picture)
    assert result["downloaded"] == 1 and calls == [AVATAR_URL]
    assert not avatars.has_due(state)  # 下载过的不会重复下载

    stats = run(root, info, now=NOW + 300)
    member = _group_export(root)[1][IdMapper(b"test-salt", "hash").sender(synthetic.CEN)]
    assert (root / "export" / member["avatar"]).read_bytes() == picture and stats["avatars_queued"] == 0


def test_avatar_url_outside_wechat_is_never_queued(account, monkeypatch):
    root, info = account
    _with_contact_urls(monkeypatch, {synthetic.CEN: "https://example.com/a.png"})
    run(root, info)
    assert json.loads((root / "export" / "state" / avatars.QUEUE_FILE).read_text(encoding="utf-8")) == {}


def _set_contact_url(monkeypatch, username: str, url: str):
    _with_contact_urls(monkeypatch, {username: url})


def test_changed_avatar_url_is_refreshed_and_old_kept_until_then(account, monkeypatch):
    """模拟换头像：链接变了就优先下载，下载成功前用旧头像，成功后换成新的。"""
    root, info = account
    state = root / "export" / "state"
    sender = IdMapper(b"test-salt", "hash").sender(synthetic.CEN)
    old_picture, new_picture = synthetic._png(32, 32, 11), synthetic._png(32, 32, 222)
    _set_contact_url(monkeypatch, synthetic.CEN, AVATAR_URL + "/old")
    run(root, info)
    avatars.download_pending(root / "export", state, fetcher=lambda u, t: old_picture)
    run(root, info, now=NOW + 120)
    first = _group_export(root)[1][sender]["avatar"]
    assert (root / "export" / first).read_bytes() == old_picture

    monkeypatch.undo()
    _set_contact_url(monkeypatch, synthetic.CEN, AVATAR_URL + "/new")
    stats = run(root, info, now=NOW + 240)
    assert stats["avatars_queued"] == 1
    assert _group_export(root)[1][sender]["avatar"] == first  # 新头像还没下载到，先用旧的
    queue = json.loads((state / avatars.QUEUE_FILE).read_text(encoding="utf-8"))
    assert [item["changed"] for item in queue.values() if AVATAR_URL + "/new" in item["urls"]] == [True]

    avatars.download_pending(root / "export", state, fetcher=lambda u, t: new_picture)
    run(root, info, now=NOW + 360)
    current = _group_export(root)[1][sender]["avatar"]
    assert current != first and (root / "export" / current).read_bytes() == new_picture


def test_changed_avatar_jumps_the_queue(tmp_path):
    export, state = _queue(tmp_path, 5)
    queue = json.loads((state / avatars.QUEUE_FILE).read_text(encoding="utf-8"))
    queue["fp4"]["changed"] = True
    (state / avatars.QUEUE_FILE).write_text(json.dumps(queue), encoding="utf-8")
    calls = []
    avatars.download_pending(export, state, limit=1, fetcher=lambda u, t: calls.append(u) or synthetic._png(8, 8, 1))
    assert calls == [f"{AVATAR_URL}/4"]


def test_downloaded_link_beats_stale_local_cache(account, monkeypatch):
    """本地缓存更旧时不会盖住按新链接下载到的头像；本地更新得比下载晚，才用本地的。"""
    root, info = account
    state = root / "export" / "state"
    sender = IdMapper(b"test-salt", "hash").sender(synthetic.ALICE)
    fresh = synthetic._png(32, 32, 123)
    _set_contact_url(monkeypatch, synthetic.ALICE, AVATAR_URL + "/fresh")
    run(root, info)
    avatars.download_pending(root / "export", state, fetcher=lambda u, t: fresh)
    run(root, info, now=NOW + 120)  # 合成库里本地头像 update_time 是 0，比下载旧
    assert (root / "export" / _group_export(root)[1][sender]["avatar"]).read_bytes() == fresh

    original = wcdb.load_head_images
    newer = synthetic._png(32, 32, 88)
    monkeypatch.setattr(wcdb, "load_head_images", lambda path: {**original(path),
                        synthetic.ALICE: {"data": newer, "update_time": int(time.time()) + 100}})
    run(root, info, now=NOW + 240)
    assert (root / "export" / _group_export(root)[1][sender]["avatar"]).read_bytes() == newer


def test_forced_refresh_replaces_avatar_and_failure_keeps_old(tmp_path):
    export, state = _queue(tmp_path, 2)
    old, new = synthetic._png(8, 8, 5), synthetic._png(8, 8, 200)
    avatars.download_pending(export, state, fetcher=lambda u, t: old, sleeper=lambda s: None)
    before = json.loads((state / avatars.DONE_FILE).read_text(encoding="utf-8"))
    failed = avatars.download_pending(export, state, force={"u0"}, fetcher=lambda u, t: None, sleeper=lambda s: None)
    assert failed["refresh_failed"] == 1 and json.loads((state / avatars.DONE_FILE).read_text(encoding="utf-8")) == before
    calls = []
    result = avatars.download_pending(export, state, force={"u0", "not-a-member"}, limit=0,
                                      fetcher=lambda u, t: calls.append(u) or new, sleeper=lambda s: None)
    assert result["refreshed"] == 1 and calls == [f"{AVATAR_URL}/0"]
    entry = json.loads((state / avatars.DONE_FILE).read_text(encoding="utf-8"))["fp0"]
    assert (export / entry["path"]).read_bytes() == new and entry["at"] > 0


def _queue(tmp_path: Path, count: int = 1) -> tuple[Path, Path]:
    export, state = tmp_path / "export", tmp_path / "state"
    export.mkdir()
    state.mkdir()
    queue = {f"fp{index}": {"urls": [f"{AVATAR_URL}/{index}"], "targets": [f"u{index}"]} for index in range(count)}
    (state / avatars.QUEUE_FILE).write_text(json.dumps(queue), encoding="utf-8")
    return export, state


def test_avatar_download_failure_backs_off_then_retries(tmp_path):
    export, state = _queue(tmp_path)
    calls = []
    assert avatars.download_pending(export, state, now=1000, fetcher=lambda u, t: calls.append(u))["failed"] == 1
    assert avatars.download_pending(export, state, now=1000 + 3599, fetcher=lambda u, t: calls.append(u))["waiting"] == 1
    assert len(calls) == 1 and not avatars.has_due(state, now=1000 + 3599)
    assert avatars.download_pending(export, state, now=1000 + 3601, fetcher=lambda u, t: calls.append(u))["failed"] == 1
    assert len(calls) == 2  # 第二次失败后退避翻倍
    assert not avatars.has_due(state, now=1000 + 3601 + 7199) and avatars.has_due(state, now=1000 + 3601 + 7201)


def test_avatar_download_rejects_bad_images_and_respects_limits(tmp_path):
    export, state = _queue(tmp_path, 5)
    assert avatars.download_pending(export, state, limit=2, sleeper=lambda s: None,
                                    fetcher=lambda u, t: b"not an image")["failed"] == 2
    assert not list(export.rglob("*.png"))
    delays = []
    ticks = iter([0, 0, 100, 100, 100, 100, 100, 100, 100])
    result = avatars.download_pending(export, state, now=time.time() + 10**7, max_seconds=60, delay=0.5, sleeper=delays.append,
                                      clock=lambda: next(ticks), fetcher=lambda u, t: synthetic._png(32, 32, 9))
    assert result["downloaded"] == 1 and delays == []  # 超时后不再下载


@pytest.mark.parametrize("url", ["http://wx.qlogo.cn/a", "https://example.com/a", "https://wx.qlogo.cn.evil.test/a",
                                 "https://user@wx.qlogo.cn/a", "https://wx.qlogo.cn:8443/a", "", "https://wx.qlogo.cn/" + "a" * 5000])
def test_avatar_url_allowlist(url):
    assert not avatars.url_allowed(url) and avatars.fetch(url, 1) is None


def test_load_contacts_reads_head_urls(tmp_path):
    path = tmp_path / "contact.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, big_head_url TEXT, small_head_url TEXT, head_img_md5 TEXT)")
    connection.execute("INSERT INTO contact VALUES ('a','甲','',?,?,'m')", (AVATAR_URL, AVATAR_URL + "/s"))
    connection.commit()
    connection.close()
    assert wcdb.load_contacts(path)["a"]["head_urls"] == [AVATAR_URL, AVATAR_URL + "/s"]


def test_no_raw_account_ids_in_hash_mode(account):
    root, info = account
    run(root, info)
    blob = "".join(path.read_text(encoding="utf-8") for path in (root / "export").rglob("*.json*"))
    for raw in (synthetic.ALICE, synthetic.BOB, synthetic.GROUP, synthetic.SELF, info["account_dir"]):
        assert raw not in blob


def test_raw_mode_keeps_safe_ids(account):
    root, info = account
    run(root, info, mode="raw")
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", item["id"]) for item in manifest["conversations"])


def test_incremental_only_appends_new(account):
    root, info = account
    first = run(root, info)
    before = lines(root)
    assert run(root, info)["new_lines"] == 0
    synthetic.append_group_message(root, server_id=910001, text="新消息", created=synthetic.START + 25000)
    second = run(root, info)
    assert second["new_lines"] == 1 and first["new_lines"] > 1
    after = lines(root)
    assert after[:len(before)] == before and after[-1]["text"] == "新消息"
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["conversations"] if item["kind"] == "group")
    assert entry["message_count"] == len(after) and entry["last_ts"] == synthetic.START + 25000


def test_text_that_looks_like_prefix_is_not_eaten(account):
    root, info = account
    run(root, info)
    synthetic.append_group_message(root, server_id=910010, text="note:\n第二行", created=synthetic.START + 25000)
    synthetic.append_group_message(root, server_id=910011, text=f"{synthetic.BOB}:\n真前缀", created=synthetic.START + 25001, sender_rowid=4)
    run(root, info)
    latest = {message["id"]: message for message in lines(root)}
    assert latest["910010"]["text"] == "note:\n第二行"
    assert latest["910011"]["text"] == "真前缀"
    assert latest["910011"]["sender_name"] == "周禾"


def test_local_id_out_of_time_order_is_not_skipped(account):
    """真实库里同步历史时 local_id 和时间顺序不一致：时间更晚、ID 更小的行不能漏。"""
    root, info = account
    from wechat_export_send import verify
    # 新一批行：先插入的时间晚，后插入的时间早
    synthetic.append_group_message(root, server_id=910020, text="时间晚但 ID 小", created=synthetic.START + 12900)
    synthetic.append_group_message(root, server_id=910021, text="时间早但 ID 大", created=synthetic.START + 12800)
    stats = run(root, info)
    assert stats["new_lines"] == 18
    assert {"910020", "910021"} <= {message["id"] for message in lines(root)}
    keys = sqlcipher.load_keys(root / "keys" / "all_keys.json")
    work = root / "verify-work"
    work.mkdir()
    settings = _merge(DEFAULTS, {})["export"]
    result = verify.compare(exporter.Source(Path(info["storage"]), keys, work), root / "export", settings, IdMapper(b"test-salt", "hash"))
    assert result["mismatch_count"] == 0, result


def test_image_found_through_packed_info_md5(account):
    """真实的 .dat 文件名不是 XML 里的 md5，而是 packed_info_data 里的。"""
    root, info = account
    from wechat_export_send import wcdb
    picture = synthetic._png(24, 24)
    alias = hashlib_md5(b"resource name")
    directory = Path(info["storage"]).parent / "msg" / "attach" / hashlib_md5(synthetic.GROUP.encode()) / "2026-10" / "Img"
    (directory / f"{alias}_h.dat").write_bytes(synthetic._xor_dat(picture, 0x5A))
    inner = b"\x22\x20" + alias.encode()
    packed = b"\x1a" + bytes([len(inner)]) + inner
    assert wcdb.packed_resource_md5(packed) == alias
    keys = json.loads((root / "keys" / "all_keys.json").read_text(encoding="utf-8"))
    synthetic._rewrite(Path(info["storage"]) / "message" / "message_0.db", keys["message/message_0.db"], [
        (f"UPDATE {wcdb.message_table(synthetic.GROUP)} SET packed_info_data=? WHERE server_id=900007", (packed,))])
    run(root, info)
    message = next(item for item in lines(root) if item["id"] == "900007")
    assert message["media"][0]["status"] == "ready" and message["media"][0]["is_thumbnail"] is False


def test_timestamped_thumbnail_is_still_a_thumbnail(tmp_path):
    from wechat_export_send import media
    picture = synthetic._png(8, 8)
    thumb = tmp_path / ("a" * 32 + "_1793951284_t.dat")
    thumb.write_bytes(synthetic._xor_dat(picture, 1))
    assert media.best_cached_image([thumb], {}, None, None, allow_thumbnail=False) is None
    assert media.best_cached_image([thumb], {}, None, None, allow_thumbnail=True)["is_thumbnail"] is True
    (tmp_path / "msg" / "attach" / "x" / "Img").mkdir(parents=True)
    (tmp_path / "msg" / "attach" / "x" / "Img" / thumb.name).write_bytes(b"x")
    assert "a" * 32 in media.index_attach_dir(tmp_path, "nobody")


def test_unchanged_shard_skipped_when_nothing_pending(account):
    root, info = account
    settings = {"media": {"enabled": False}}
    run(root, info, settings=settings)
    assert run(root, info, settings=settings)["shards_skipped"] == 2


def test_revoke_within_lookback_rewrites_same_id(account):
    root, info = account
    now = synthetic.START + 11030
    run(root, info, now=now)
    synthetic.update_group_message(root, server_id=900014, text="你撤回了一条消息", local_type=10002)
    stats = run(root, info, now=now + 60)
    assert stats["rewritten_lines"] == 1 and stats["new_lines"] == 0
    latest = {message["id"]: message for message in lines(root)}
    assert latest["900014"]["type"] == "revoke"
    assert sum(1 for message in lines(root) if message["id"] == "900014") == 2  # 只追加，消费方取最后一行


def test_old_rows_outside_lookback_not_rescanned(account):
    root, info = account
    run(root, info, now=NOW)
    synthetic.update_group_message(root, server_id=900001, text="改了很久以前的")
    assert run(root, info, now=NOW + 60)["rewritten_lines"] == 0


def test_self_message_waits_for_server_id_then_exports_once(account):
    root, info = account
    now = synthetic.START + 25060
    run(root, info, now=now)
    synthetic.append_group_message(root, server_id=0, text="发送中", sender_rowid=1, created=synthetic.START + 25000, origin=1)
    # 后面又来一条，把游标推过去；等服务器 ID 的那条不能丢
    synthetic.append_group_message(root, server_id=910002, text="别人的新消息", created=synthetic.START + 25010)
    stats = run(root, info, now=now)
    assert stats["new_lines"] == 1
    connection_rows = lines(root)
    assert not any(message.get("text") == "发送中" for message in connection_rows)
    keys = json.loads((root / "keys" / "all_keys.json").read_text(encoding="utf-8"))
    from wechat_export_send import wcdb
    synthetic._rewrite(Path(info["storage"]) / "message" / "message_0.db", keys["message/message_0.db"], [
        (f"UPDATE {wcdb.message_table(synthetic.GROUP)} SET server_id=910003 WHERE message_content='发送中'", ())])
    assert run(root, info, now=now + 60)["new_lines"] == 1
    assert [message["id"] for message in lines(root) if message.get("text") == "发送中"] == ["910003"]


def test_pending_image_rewritten_when_cache_arrives(account):
    root, info = account
    run(root, info)
    pending = [message for message in lines(root) if message["id"] == "900008"]
    assert pending[0]["media"][0]["status"] == "pending"
    from wechat_export_send import media
    picture = synthetic._png(20, 20)
    md5 = hashlib_md5(b"synthetic thumb only")
    directory = Path(info["storage"]).parent / "msg" / "attach" / hashlib_md5(synthetic.GROUP.encode()) / "2026-10" / "Img"
    (directory / f"{md5}.dat").write_bytes(synthetic._xor_dat(picture, 0x5A))
    stats = run(root, info)
    assert stats["rewritten_lines"] == 1
    latest = [message for message in lines(root) if message["id"] == "900008"][-1]
    assert latest["media"][0]["status"] == "ready" and latest["media"][0]["is_thumbnail"] is False
    assert run(root, info)["rewritten_lines"] == 0


def hashlib_md5(data: bytes) -> str:
    import hashlib
    return hashlib.md5(data).hexdigest()


def test_thumbnail_only_when_enabled_and_marked(account):
    root, info = account
    settings = {"media": {"enabled": True, "thumbnails": True, "max_bytes": 1 << 20, "files": True, "pending_days": 7}}
    run(root, info, settings=settings)
    message = next(item for item in lines(root) if item["id"] == "900008")
    assert message["media"][0]["status"] == "pending"
    assert message["media"][1]["is_thumbnail"] is True and message["media"][1]["status"] == "ready"


def test_filters(account):
    root, info = account
    run(root, info, settings={"kinds": ["private"]})
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    assert {item["kind"] for item in manifest["conversations"]} == {"private"}


def test_exclude_by_name(account):
    root, info = account
    run(root, info, settings={"exclude": ["合成示例群（虚构）"]})
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    assert {item["kind"] for item in manifest["conversations"]} == {"private"}


def test_since_cutoff(account):
    root, info = account
    run(root, info, settings={"since": synthetic.START + 12030})
    assert [message["id"] for message in lines(root, "private")] == ["800002"]


def test_partial_last_line_repaired(account):
    root, info = account
    run(root, info)
    manifest = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    item = next(entry for entry in manifest["conversations"] if entry["kind"] == "group")
    path = root / "export" / item["file"]
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"id":"half')
    synthetic.append_group_message(root, server_id=910005, text="崩溃之后", created=synthetic.START + 26000)
    run(root, info)
    assert [json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()][-1] == "910005"


def test_source_files_unchanged_by_export(account):
    root, info = account
    storage = Path(info["storage"])
    before = {path: path.read_bytes() for path in storage.rglob("*.db")}
    run(root, info)
    assert {path: path.read_bytes() for path in storage.rglob("*.db")} == before


def test_concurrent_export_refused(account):
    import fcntl
    root, info = account
    state = root / "export" / "state"
    state.mkdir(parents=True)
    with (state / "export.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(exporter.ExportBusy):
            run(root, info)


def test_ids_stable_across_runs(account):
    root, info = account
    run(root, info)
    first = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    import shutil
    shutil.rmtree(root / "export")
    run(root, info)
    second = json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in first["conversations"]] == [item["id"] for item in second["conversations"]]
    assert first["source"]["account_id"] == second["source"]["account_id"]
