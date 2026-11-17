import hashlib
import json
import time

import pytest

Crypto = pytest.importorskip("Crypto.Cipher.AES")

from wechat_export_send import originals  # noqa: E402


def test_field_pack_roundtrip():
    fields = {"a": b"1", "b_c": b"\x00\xff"}
    assert originals.unpack_fields(originals.pack_fields(fields)) == fields
    with pytest.raises(ValueError):
        originals.unpack_fields(b"\x00\x00\x00\x09a")


def test_descriptor_validation():
    good = {"cdnbigimgurl": "f", "hdlength": "10", "md5": "a" * 32, "aeskey": "00" * 16}
    assert originals._descriptor(good)["filetype"] == 1
    assert originals._descriptor({**good, "cdnbigimgurl": "", "cdnmidimgurl": "f", "length": "10"})["quality"] == "client_full"
    for broken in ({**good, "aeskey": "00"}, {**good, "md5": "x"}, {**good, "hdlength": "0"}, {**good, "cdnbigimgurl": ""}):
        with pytest.raises(ValueError):
            originals._descriptor(broken)


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _size):
        return self.body


def test_download_verifies_md5_and_decrypts(monkeypatch):
    plain = b"synthetic image bytes" * 10
    key = bytes(range(16))
    padding = 16 - len(plain) % 16
    encrypted = Crypto.new(key, Crypto.MODE_ECB).encrypt(plain + bytes([padding]) * padding)

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://8.8.8.8:443/download"
            sent = originals.unpack_fields(request.data)
            start, end = int(sent["rangestart"]), int(sent["rangeend"])
            block = encrypted[start:end + 1]
            return FakeResponse(originals.pack_fields({
                "retcode": b"0", "seq": sent["seq"], "rangestart": sent["rangestart"], "rangeend": str(start + len(block) - 1).encode(),
                "totalsize": str(len(encrypted)).encode(), "filedata": block,
                "rawfilesize": str(len(plain)).encode(), "rawfilemd5": hashlib.md5(plain).hexdigest().encode()}))

    monkeypatch.setattr(originals.urllib.request, "build_opener", lambda *a: Opener())
    route = {"version": 1, "uin": 2, "auth": b"a", "server_ips": ["8.8.8.8"]}
    descriptor = {"fileid": "f", "key": key, "expected_length": len(plain), "expected_md5": hashlib.md5(plain).hexdigest(), "filetype": 1}
    data, transfer = originals.download(route, descriptor, time.monotonic() + 20)
    assert data == plain and transfer["original_md5_matches"] is True
    descriptor["expected_md5"] = "0" * 32
    with pytest.raises(ValueError):
        originals.download(route, descriptor, time.monotonic() + 20)


def test_disabled_by_default(tmp_path, monkeypatch):
    from wechat_export_send.config import Instance
    directory = tmp_path / "home" / "main"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"instance": "main"}), encoding="utf-8")
    monkeypatch.setenv("WES_HOME", str(tmp_path / "home"))
    with pytest.raises(RuntimeError):
        originals.fetch_pending(Instance.load("main"))


def test_fetch_pending_caches_shard_decrypt_preserves_concurrent_cursors_and_backs_off(tmp_path, monkeypatch):
    from wechat_export_send import cli, sqlcipher, synthetic
    from wechat_export_send.config import Instance

    home = tmp_path / "instances"
    directory = home / "main"
    directory.mkdir(parents=True)
    synthetic.build_account(directory)
    (directory / "config.json").write_text(json.dumps({
        "instance": "main",
        "originals": {"enabled": True, "max_attempts": 3},
    }), encoding="utf-8")
    monkeypatch.setenv("WES_HOME", str(home))
    inst = Instance.load("main")
    inst.export_root.mkdir(parents=True, exist_ok=True)
    conv_file = inst.export_root / "conversations" / "g_1.jsonl"
    conv_file.parent.mkdir(parents=True, exist_ok=True)
    conv_file.write_text("", encoding="utf-8")

    now = int(time.time())
    state_dir = inst.state / "export"
    state_dir.mkdir(parents=True, exist_ok=True)
    cursors_path = state_dir / "cursors.json"
    entry1 = {"message": {"id": "m1", "ts": now}, "shard": "message/message_0.db", "local_id": 1}
    entry2 = {"message": {"id": "m2", "ts": now}, "shard": "message/message_0.db", "local_id": 2}
    entry_concurrent = {"message": {"id": "m3", "ts": now}, "shard": "message/message_0.db", "local_id": 3}
    cursors_path.write_text(json.dumps({
        "shards": {},
        "conversations": {"g_1": {"file": "conversations/g_1.jsonl", "pending": [entry1, entry2]}},
    }), encoding="utf-8")

    decrypt_calls = 0
    real_snapshot = sqlcipher.decrypted_snapshot

    def counting_snapshot(source, key_info, target, **kwargs):
        nonlocal decrypt_calls
        decrypt_calls += 1
        return real_snapshot(source, key_info, target, **kwargs)

    monkeypatch.setattr(sqlcipher, "decrypted_snapshot", counting_snapshot)

    def fake_fetch(instance, storage, connection, tables, entry, routes, config, deadline):
        # 模拟下载期间 export 进程推进了 cursors 并新增了 pending 项
        cur = json.loads(cursors_path.read_text(encoding="utf-8"))
        pending = cur["conversations"]["g_1"]["pending"]
        if not any(item["message"]["id"] == "m3" for item in pending):
            pending.append(entry_concurrent)
            cursors_path.write_text(json.dumps(cur), encoding="utf-8")
        if entry["message"]["id"] == "m2":
            raise RuntimeError("transient cdn failure")
        return {**entry["message"], "media": [{"kind": "image", "status": "ready"}]}

    monkeypatch.setattr(originals, "_fetch_one_from_connection", fake_fetch)

    stats = originals.fetch_pending(inst)
    assert decrypt_calls == 1
    assert stats == {"requested": 2, "fetched": 1, "failed": 1, "expired": 0}
    updated = json.loads(cursors_path.read_text(encoding="utf-8"))
    remaining = updated["conversations"]["g_1"]["pending"]
    assert [item["message"]["id"] for item in remaining] == ["m2", "m3"]
    assert remaining[0]["attempts"] == 1
    assert remaining[0]["next_retry_at"] >= now + 250
    lines = [json.loads(line) for line in conv_file.read_text(encoding="utf-8").splitlines()]
    assert [line["id"] for line in lines] == ["m1"]

    # 容器模式下 fetch-originals 走 run_in_tools 且开启 bridge 网络
    (directory / "config.json").write_text(json.dumps({
        "instance": "main",
        "export": {"runner": "container"},
        "originals": {"enabled": True},
    }), encoding="utf-8")
    captured = {}

    def fake_run_in_tools(instance, arguments, stdout=None, *, network="none"):
        captured["arguments"] = arguments
        captured["network"] = network
        return 0

    monkeypatch.setattr(cli, "run_in_tools", fake_run_in_tools)
    assert cli.main(["fetch-originals", "--instance", "main", "--limit", "15"]) == 0
    assert captured["network"] == "bridge"
    assert captured["arguments"] == ["fetch-originals", "--instance", "main", "--in-process", "--limit", "15"]
