from __future__ import annotations

import hashlib
import sqlite3
import struct

import pytest

from wechat_export_send import aes_fallback, sqlcipher, synthetic


def fixture(tmp_path):
    plain = tmp_path / "plain.db"
    synthetic.reserved_sqlite(plain, ["CREATE TABLE t (v TEXT)", "INSERT INTO t VALUES ('合成')"])
    salt = bytes(range(16))
    key = sqlcipher.derive_enc_key(b"PUBLIC-TEST", salt)
    encrypted = tmp_path / "enc.db"
    sqlcipher.encrypt_sqlite(plain, encrypted, key, salt=salt)
    return encrypted, key, salt


def test_roundtrip(tmp_path):
    encrypted, key, _ = fixture(tmp_path)
    target = tmp_path / "out.db"
    assert sqlcipher.full_decrypt(encrypted, target, key) >= 1
    assert sqlcipher.verify_sqlite(target)
    connection = sqlite3.connect(target)
    assert connection.execute("SELECT v FROM t").fetchone() == ("合成",)
    connection.close()
    assert target.stat().st_mode & 0o077 == 0


def test_wrong_key_and_tamper_rejected(tmp_path):
    encrypted, key, _ = fixture(tmp_path)
    with pytest.raises(ValueError):
        sqlcipher.full_decrypt(encrypted, tmp_path / "wrong.db", bytes(32))
    data = bytearray(encrypted.read_bytes())
    data[128] ^= 1
    encrypted.write_bytes(data)
    with pytest.raises(ValueError):
        sqlcipher.full_decrypt(encrypted, tmp_path / "bad.db", key)
    assert not (tmp_path / "bad.db").exists()


def test_preallocated_empty_pages_at_tail_are_skipped(tmp_path):
    """真实客户端把消息库预分配到 30 MB，尾部全是零页。"""
    encrypted, key, _ = fixture(tmp_path)
    real_pages = encrypted.stat().st_size // sqlcipher.PAGE_SIZE
    with encrypted.open("ab") as stream:
        stream.write(bytes(sqlcipher.PAGE_SIZE * 50))
    target = tmp_path / "out.db"
    assert sqlcipher.full_decrypt(encrypted, target, key) == real_pages
    assert target.stat().st_size == real_pages * sqlcipher.PAGE_SIZE
    assert sqlcipher.verify_sqlite(target)


def test_page_after_empty_pages_rejected(tmp_path):
    encrypted, key, _ = fixture(tmp_path)
    data = encrypted.read_bytes()
    encrypted.write_bytes(data + bytes(sqlcipher.PAGE_SIZE) + data[-sqlcipher.PAGE_SIZE:])
    with pytest.raises(ValueError):
        sqlcipher.full_decrypt(encrypted, tmp_path / "out.db", key)


def test_zeroed_used_pages_fail_quick_check(tmp_path):
    """把用过的页抹成零：数据库头里的页数对不上，快照必须拒绝。"""
    plain = tmp_path / "plain.db"
    statements = ["CREATE TABLE t (v TEXT)"] + [f"INSERT INTO t VALUES ('{'x' * 3000}')" for _ in range(20)]
    synthetic.reserved_sqlite(plain, statements)
    salt = bytes(range(16))
    key = sqlcipher.derive_enc_key(b"PUBLIC-TEST", salt)
    encrypted = tmp_path / "enc.db"
    sqlcipher.encrypt_sqlite(plain, encrypted, key, salt=salt)
    data = bytearray(encrypted.read_bytes())
    pages = len(data) // sqlcipher.PAGE_SIZE
    assert pages > 5
    data[(pages - 3) * sqlcipher.PAGE_SIZE:] = bytes(3 * sqlcipher.PAGE_SIZE)
    encrypted.write_bytes(data)
    with pytest.raises(RuntimeError):
        sqlcipher.decrypted_snapshot(encrypted, {"enc_key": key.hex()}, tmp_path / "snap" / "x.db")
    assert not (tmp_path / "snap" / "x.db").exists()


def test_truncated_rejected(tmp_path):
    encrypted, key, _ = fixture(tmp_path)
    encrypted.write_bytes(encrypted.read_bytes()[:5000])
    with pytest.raises(ValueError):
        sqlcipher.full_decrypt(encrypted, tmp_path / "t.db", key)


def _wal(tmp_path, encrypted, key, salt, page_number, plain_page, *, commit=True):
    """手工造一个带校验和的 WAL：一帧，改第 page_number 页。"""
    enc, mac = sqlcipher._keys(key, salt)
    iv = hashlib.sha256(b"wal").digest()[:16]
    body = plain_page[16:-sqlcipher.RESERVE] if page_number == 1 else plain_page[:-sqlcipher.RESERVE]
    ciphertext = sqlcipher._cbc_encrypt(enc, iv, body)
    import hmac
    tag = hmac.new(mac, ciphertext + iv + struct.pack("<I", page_number), hashlib.sha512).digest()
    page = (salt + ciphertext + iv + tag) if page_number == 1 else (ciphertext + iv + tag)

    def checksum(block, state=(0, 0)):
        s1, s2 = state
        for offset in range(0, len(block), 8):
            x = int.from_bytes(block[offset:offset + 4], "little")
            y = int.from_bytes(block[offset + 4:offset + 8], "little")
            s1 = (s1 + x + s2) & 0xFFFFFFFF
            s2 = (s2 + y + s1) & 0xFFFFFFFF
        return s1, s2

    header = struct.pack(">6I", 0x377F0682, 3007000, sqlcipher.PAGE_SIZE, 0, 11, 22)
    state = checksum(header)
    header += struct.pack(">2I", *state)
    pages = len(encrypted.read_bytes()) // sqlcipher.PAGE_SIZE
    start = struct.pack(">2I", page_number, pages if commit else 0)
    state = checksum(start + page, state)
    frame = start + struct.pack(">2I", 11, 22) + struct.pack(">2I", *state) + page
    wal = tmp_path / (encrypted.name + "-wal")
    wal.write_bytes(header + frame)
    return wal


def test_wal_committed_applied_uncommitted_ignored(tmp_path):
    encrypted, key, salt = fixture(tmp_path)
    target = tmp_path / "out.db"
    sqlcipher.full_decrypt(encrypted, target, key)
    plain = bytearray(target.read_bytes())
    page_two = bytes(plain[sqlcipher.PAGE_SIZE:2 * sqlcipher.PAGE_SIZE])
    changed = page_two.replace("合成".encode(), "更新".encode())
    assert changed != page_two
    wal = _wal(tmp_path, encrypted, key, salt, 2, changed, commit=False)
    assert sqlcipher.patch_wal(wal, target, key, source=encrypted) == 0
    wal = _wal(tmp_path, encrypted, key, salt, 2, changed, commit=True)
    assert sqlcipher.patch_wal(wal, target, key, source=encrypted) == 1
    connection = sqlite3.connect(target)
    assert connection.execute("SELECT v FROM t").fetchone() == ("更新",)
    connection.close()


def test_snapshot_cleans_up_and_never_touches_source(tmp_path):
    encrypted, key, _ = fixture(tmp_path)
    before = encrypted.read_bytes()
    target = tmp_path / "snap" / "x.db"
    sqlcipher.decrypted_snapshot(encrypted, {"enc_key": key.hex()}, target)
    assert encrypted.read_bytes() == before
    assert sorted(path.name for path in target.parent.iterdir()) == ["x.db"]


def test_key_lookup_and_skip_rules():
    keys = {"message/message_0.db": {"enc_key": "aa"}, "contact/contact.db": "bb", "_db_dir": "x"}
    assert sqlcipher.key_for_rel(keys, "message/message_0.db")["enc_key"] == "aa"
    assert sqlcipher.key_for_rel(keys, "contact/contact.db") == {"enc_key": "bb"}
    assert sqlcipher.key_for_rel(keys, "nope.db") is None
    assert sqlcipher.should_skip_source_db("message/message_0_fts.db")
    assert not sqlcipher.should_skip_source_db("message/message_0.db")


def test_aes_fallback_matches_fips197():
    key = bytes(range(32))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
    assert aes_fallback.ecb_encrypt(key, plain) == expected
    assert aes_fallback.ecb_decrypt(key, expected) == plain
    key128 = bytes(range(16))
    assert aes_fallback.ecb_encrypt(key128, plain) == bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_aes_fallback_modes_match_pycryptodome():
    Crypto = pytest.importorskip("Crypto.Cipher.AES")
    key, iv = bytes(range(32)), bytes(range(16, 32))
    data = bytes(range(160))
    assert aes_fallback.cbc_encrypt(key, iv, data) == Crypto.new(key, Crypto.MODE_CBC, iv).encrypt(data)
    assert aes_fallback.cbc_decrypt(key, iv, data) == Crypto.new(key, Crypto.MODE_CBC, iv).decrypt(data)
    assert aes_fallback.cfb128_decrypt(key, iv, data[:37]) == Crypto.new(key, Crypto.MODE_CFB, iv=iv, segment_size=128).decrypt(data[:37])
