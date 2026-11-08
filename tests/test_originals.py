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
