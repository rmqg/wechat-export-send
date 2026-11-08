"""按公开的 SQLCipher 4 页格式解密微信数据库的只读副本，每页都先验 HMAC。

页长 4096，每页末尾保留 80 字节（16 字节 IV + 64 字节 HMAC-SHA512），第一页开头
16 字节是盐；AES-256-CBC；MAC 密钥由盐异或 0x3A 后 PBKDF2-HMAC-SHA512 两轮派生。
认证不过一律报错，绝不输出半截数据库。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from pathlib import Path

PAGE_SIZE = 4096
RESERVE = 80
KDF_ITERATIONS = 256000

try:
    from Crypto.Cipher import AES as _AES
except ImportError:  # 没装依赖时只够跑离线 demo
    _AES = None


def have_fast_aes() -> bool:
    return _AES is not None


def _cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if _AES is not None:
        return _AES.new(key, _AES.MODE_CBC, iv).decrypt(data)
    from . import aes_fallback
    return aes_fallback.cbc_decrypt(key, iv, data)


def _cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if _AES is not None:
        return _AES.new(key, _AES.MODE_CBC, iv).encrypt(data)
    from . import aes_fallback
    return aes_fallback.cbc_encrypt(key, iv, data)


def derive_enc_key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITERATIONS, 32)


def _keys(enc_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    if len(enc_key) != 32:
        raise ValueError("enc_key must be the 32-byte SQLCipher-derived key")
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return enc_key, hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, 32)


def _decrypt_page(page: bytes, number: int, enc: bytes, mac: bytes) -> bytes:
    if len(page) != PAGE_SIZE:
        raise ValueError("short encrypted page")
    reserve = page[-RESERVE:]
    ciphertext = page[16:-RESERVE] if number == 1 else page[:-RESERVE]
    iv, tag = reserve[:16], reserve[16:]
    expected = hmac.new(mac, ciphertext + iv + struct.pack("<I", number), hashlib.sha512).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError(f"SQLCipher page {number} authentication failed")
    plain = _cbc_decrypt(enc, iv, ciphertext)
    return (b"SQLite format 3\x00" + plain if number == 1 else plain) + bytes(RESERVE)


def full_decrypt(source: Path, target: Path, key: bytes) -> int:
    """把 source 解密到 target（原子替换，权限 600），返回页数。source 只读。"""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError("source and target must differ")
    with source.open("rb") as inp:
        salt = inp.read(16)
        if len(salt) != 16:
            raise ValueError("encrypted database is too short")
        enc, mac = _keys(bytes(key), salt)
        first = inp.read(PAGE_SIZE - 16)
        if len(first) != PAGE_SIZE - 16:
            raise ValueError("encrypted database has a partial page")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".decrypt-", dir=target.parent)
        pages = 0
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(_decrypt_page(salt + first, 1, enc, mac))
                pages = 1
                while True:
                    page = inp.read(PAGE_SIZE)
                    if not page:
                        break
                    if len(page) != PAGE_SIZE:
                        raise ValueError("encrypted database has a partial page")
                    pages += 1
                    out.write(_decrypt_page(page, pages, enc, mac))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            return pages
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def patch_wal(wal: Path, target: Path, key: bytes, source: Path | None = None) -> int:
    """把 WAL 里完整提交的帧应用到解密副本；未提交的尾帧不用。返回应用的页数。"""
    wal = Path(wal)
    if not wal.exists() or wal.stat().st_size < 32:
        return 0
    if source is None:
        if not wal.name.endswith("-wal"):
            raise ValueError("encrypted database source is required beside WAL")
        source = wal.with_name(wal.name[:-4])
    with Path(source).open("rb") as stream:
        salt = stream.read(16)
    data = wal.read_bytes()
    magic, _, page_size, _, salt1, salt2, _, _ = struct.unpack(">8I", data[:32])
    if magic not in (0x377F0682, 0x377F0683) or page_size != PAGE_SIZE:
        raise ValueError("unsupported SQLite WAL header")
    byteorder = "little" if magic == 0x377F0682 else "big"

    def checksum(block: bytes, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
        s1, s2 = state
        for offset in range(0, len(block), 8):
            x = int.from_bytes(block[offset:offset + 4], byteorder)
            y = int.from_bytes(block[offset + 4:offset + 8], byteorder)
            s1 = (s1 + x + s2) & 0xFFFFFFFF
            s2 = (s2 + y + s1) & 0xFFFFFFFF
        return s1, s2

    state = checksum(data[:24])
    if state != struct.unpack(">2I", data[24:32]):
        raise ValueError("WAL header checksum mismatch")
    enc, mac = _keys(bytes(key), salt)
    frames, pos, committed = [], 32, 0
    while pos + 24 + PAGE_SIZE <= len(data):
        pg, dbsize, fs1, fs2, fc1, fc2 = struct.unpack(">6I", data[pos:pos + 24])
        page = data[pos + 24:pos + 24 + PAGE_SIZE]
        if (fs1, fs2) != (salt1, salt2) or pg == 0:
            break
        state = checksum(data[pos:pos + 8] + page, state)
        if state != (fc1, fc2):
            break
        frames.append((pg, dbsize, page))
        if dbsize:
            committed = len(frames)
        pos += 24 + PAGE_SIZE
    if not committed:
        return 0
    updates = {pg: _decrypt_page(frame, pg, enc, mac) for pg, _, frame in frames[:committed]}
    db_size = frames[committed - 1][1] * PAGE_SIZE
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix=".wal-patch-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(target, temporary)
        os.chmod(temporary, 0o600)
        with open(temporary, "r+b") as out:
            for pg, plain in updates.items():
                out.seek((pg - 1) * PAGE_SIZE)
                out.write(plain)
            out.truncate(db_size)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return len(updates)


def encrypt_sqlite(plain_path: Path, encrypted_path: Path, enc_key: bytes, *, salt: bytes) -> int:
    """只给合成样本和测试用：把预留了 80 字节的 SQLite 加密成 SQLCipher 4 页格式。"""
    plain_path, encrypted_path = Path(plain_path), Path(encrypted_path)
    if len(salt) != 16:
        raise ValueError("salt must be exactly 16 bytes")
    data = plain_path.read_bytes()
    if not data.startswith(b"SQLite format 3\x00") or len(data) % PAGE_SIZE:
        raise ValueError("fixture input must be a complete SQLite database with 4096-byte pages")
    enc, mac = _keys(bytes(enc_key), salt)
    encrypted = bytearray()
    for number, offset in enumerate(range(0, len(data), PAGE_SIZE), 1):
        page = data[offset:offset + PAGE_SIZE]
        body = page[16:-RESERVE] if number == 1 else page[:-RESERVE]
        if len(body) % 16:
            raise ValueError("plain database must have an 80-byte reserved page tail")
        iv = hashlib.sha256(salt + struct.pack("<I", number)).digest()[:16]
        ciphertext = _cbc_encrypt(enc, iv, body)
        tag = hmac.new(mac, ciphertext + iv + struct.pack("<I", number), hashlib.sha512).digest()
        output = (salt + ciphertext + iv + tag) if number == 1 else (ciphertext + iv + tag)
        if len(output) != PAGE_SIZE:
            raise ValueError("database pages must reserve exactly 80 bytes")
        encrypted.extend(output)
    encrypted_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".encrypt-", dir=encrypted_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(encrypted)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, encrypted_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return len(data) // PAGE_SIZE


def key_for_rel(keys: dict, rel: str) -> dict | None:
    rel = Path(rel).as_posix().lstrip("./")
    candidates = keys.get("databases", keys)
    if not isinstance(candidates, dict):
        return None
    found = candidates.get(rel)
    if found is None:
        found = next((v for k, v in candidates.items() if Path(k).as_posix().lstrip("./") == rel), None)
    if isinstance(found, str):
        return {"enc_key": found}
    return found if isinstance(found, dict) and found.get("enc_key") else None


def load_keys(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("keys file must contain a JSON object")
    return {k: v for k, v in value.items() if not str(k).startswith("_")}


def should_skip_source_db(path: Path | str) -> bool:
    name = Path(path).name.lower()
    return name.endswith(("-wal", "-shm", "_fts.db")) or name in {"media.db", "fts.db"}


def verify_sqlite(path: Path) -> bool:
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            return connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return False


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
        return info.st_size, info.st_mtime_ns, info.st_ino
    except FileNotFoundError:
        return None


def snapshot_signature(source: Path) -> list:
    """数据库和 WAL 的 (大小, mtime, inode)，用来判断两次导出之间有没有变化。"""
    # 存进 JSON 再读回来会变成列表，这里统一用列表才能直接比较
    return [list(item) if item else None for item in (_signature(Path(source)), _signature(Path(str(source) + "-wal")))]


def decrypted_snapshot(source: Path, key_info: dict, target: Path) -> Path:
    """客户端还在写时也能用：复制 DB 和 WAL，确认复制前后没变，再解密到私有目录。"""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encrypted_copy = target.with_suffix(target.suffix + ".encrypted")
    wal_copy = Path(str(encrypted_copy) + "-wal")
    src_wal = Path(str(source) + "-wal")
    stable = False
    for _ in range(3):
        before = (_signature(source), _signature(src_wal))
        if before[0] is None:
            raise RuntimeError(f"source database disappeared during snapshot: {source.name}")
        shutil.copyfile(source, encrypted_copy)
        encrypted_copy.chmod(0o600)
        if before[1] is not None:
            shutil.copyfile(src_wal, wal_copy)
            wal_copy.chmod(0o600)
        else:
            wal_copy.unlink(missing_ok=True)
        if before == (_signature(source), _signature(src_wal)):
            stable = True
            break
    if not stable:
        encrypted_copy.unlink(missing_ok=True)
        wal_copy.unlink(missing_ok=True)
        raise RuntimeError(f"source database changed during all snapshot attempts: {source.name}")
    try:
        enc_key = bytes.fromhex(key_info["enc_key"])
        full_decrypt(encrypted_copy, target, enc_key)
        patch_wal(wal_copy, target, enc_key, source=encrypted_copy)
        if not verify_sqlite(target):
            raise RuntimeError(f"decrypted database failed SQLite quick_check: {source.name}")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        encrypted_copy.unlink(missing_ok=True)
        wal_copy.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验并解密一份 SQLCipher 4 数据库副本")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--key", help="十六进制 enc_key（注意：命令行参数对本机其他进程可见）")
    choice.add_argument("--keys", type=Path, help="all_keys.json")
    parser.add_argument("--rel", help="keys 文件里的相对路径，默认用文件名")
    args = parser.parse_args(argv)
    if args.key:
        key = bytes.fromhex(args.key)
    else:
        rel = args.rel or args.source.name
        info = key_for_rel(load_keys(args.keys), rel)
        if not info:
            raise SystemExit(f"no key entry for {rel}")
        key = bytes.fromhex(info["enc_key"])
    count = full_decrypt(args.source, args.target, key)
    patch_wal(Path(str(args.source) + "-wal"), args.target, key, source=args.source)
    if not verify_sqlite(args.target):
        raise SystemExit("decrypted database failed SQLite quick_check")
    print(f"decrypted {count} authenticated pages; sqlite_valid=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
