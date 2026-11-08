"""解码本地图片缓存（.dat）并按内容寻址存进导出目录。

V1/V2 .dat 的版式改写自 boujuan/extract-wechat-messages-linux 的 wxextract/images.py
（Juan Boullosa，MIT），声明见 THIRD_PARTY_NOTICES.md 和 vendor/wxextract/LICENSE。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sqlite3
import struct
import tempfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"
FORMAT_KEY = b"xwechat_crypt_ke"
_XOR_MAGICS = ((b"\x89PNG", "png"), (b"GIF8", "gif"), (b"II*\x00", "tif"), (b"RIFF", "webp"), (b"\xff\xd8\xff", "jpg"))
MIME_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
              "bmp": "image/bmp", "tif": "image/tiff", "heic": "image/heic", "hevc": "application/x-wxgf"}
EXTENSIONS = {"hevc": "wxgf"}


def _ecb_decrypt(key: bytes, data: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        from . import aes_fallback
        return aes_fallback.ecb_decrypt(key, data)
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def _unpad(data: bytes) -> bytes:
    if not data or not 1 <= data[-1] <= 16 or data[-data[-1]:] != bytes([data[-1]]) * data[-1]:
        raise ValueError("bad PKCS#7 padding")
    return data[:-data[-1]]


def detect_image_format(data: bytes) -> str | None:
    if not data:
        return None
    checks = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF87a", "gif"), (b"GIF89a", "gif"),
              (b"RIFF", "webp"), (b"BM", "bmp"), (b"II*\x00", "tif"), (b"MM\x00*", "tif"), (b"wxgf", "hevc"))
    for magic, name in checks:
        if data.startswith(magic):
            if name == "webp" and data[8:12] != b"WEBP":
                return None
            return name
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1"):
        return "heic"
    return None


def _complete_image(data: bytes, fmt: str) -> bool:
    if fmt == "hevc":
        return data.startswith(b"wxgf")  # 微信私有的 HEVC 容器，Pillow 解不了
    if fmt == "jpg" and b"\xff\xd9" not in data:
        return False
    try:
        from PIL import Image, ImageFile, ImageSequence
    except ImportError:
        if fmt == "png":
            return data.endswith(b"IEND\xaeB`\x82")
        return True  # 没有 Pillow 时只做到文件头和尾部标记这一层
    old = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        if fmt == "heic":
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                return False
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            for frame in ImageSequence.Iterator(image):
                frame.load()
        return True
    except Exception:
        return False
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = old


def _valid_result(data: bytes) -> tuple[bytes | None, str | None]:
    fmt = detect_image_format(data)
    if fmt is None or not _complete_image(data, fmt):
        return None, None
    return data, fmt


def _aes_key_bytes(key) -> bytes:
    if isinstance(key, bytes):
        return key
    if not isinstance(key, str):
        return b""
    if len(key) == 16:
        return key.encode("ascii", "strict")
    try:
        return bytes.fromhex(key)
    except ValueError:
        return key.encode("ascii", "strict")


def decrypt_v2(data: bytes, key, xor_key: int = 0x88) -> tuple[bytes | None, str | None]:
    if len(data) < 15:
        return None, None
    signature = data[:6]
    if signature not in (V2_MAGIC, V1_MAGIC):
        return None, None
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    aligned = (aes_size // 16 + 1) * 16  # PKCS#7：已对齐时也多一整块
    offset = 15
    if not aes_size or offset + aligned > len(data) or xor_size > len(data) - offset - aligned:
        return None, None
    key_bytes = V1_AES_KEY if signature == V1_MAGIC else _aes_key_bytes(key)
    if len(key_bytes) < 16:
        return None, None
    try:
        prefix = _unpad(_ecb_decrypt(key_bytes[:16], data[offset:offset + aligned]))
    except (ValueError, TypeError):
        return None, None
    if len(prefix) != aes_size:
        return None, None
    offset += aligned
    raw_end = len(data) - xor_size
    if raw_end < offset:
        return None, None
    xor = int(xor_key) & 255
    return _valid_result(prefix + data[offset:raw_end] + bytes(value ^ xor for value in data[raw_end:]))


def decrypt_dat(path: Path, aes_key=None, xor_key=None) -> tuple[bytes | None, str | None]:
    data = Path(path).read_bytes()
    if data[:6] in (V2_MAGIC, V1_MAGIC):
        return decrypt_v2(data, aes_key, 0x88 if xor_key is None else xor_key)
    if len(data) < 4:
        return None, None
    key = None
    for magic, _ in _XOR_MAGICS:
        if len(data) >= len(magic):
            candidate = data[0] ^ magic[0]
            if all((data[i] ^ candidate) == magic[i] for i in range(1, len(magic))):
                key = candidate
    if key is None and len(data) >= 14:
        candidate = data[0] ^ ord("B")
        header = bytes(value ^ candidate for value in data[:14])
        if header[:2] == b"BM":
            try:
                file_size, _, _, image_offset = struct.unpack_from("<IHHI", header, 2)
                if abs(file_size - len(data)) < 1024 and 14 <= image_offset <= 1078:
                    key = candidate
            except struct.error:
                pass
    if key is None:
        return None, None
    return _valid_result(bytes(value ^ key for value in data))


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("Truncated MMKV value")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << (index * 7)
        if not byte & 128:
            return value, offset
    raise ValueError("Invalid MMKV varint")


def _span(data: bytes, offset: int) -> tuple[bytes, int]:
    size, offset = _varint(data, offset)
    end = offset + size
    if end > len(data):
        raise ValueError("Truncated MMKV span")
    return data[offset:end], end


def image_key(db_storage: Path) -> tuple[str, int]:
    """从客户端本地配置算出 V2 图片的 AES 和 XOR 密钥，不碰客户端进程。"""
    db_storage = Path(db_storage)
    config = db_storage.parent.parent / "all_users/config"
    data = (config / "global_config").read_bytes()
    metadata = (config / "global_config.crc").read_bytes()
    if len(metadata) < 40 or struct.unpack_from("<I", metadata, 4)[0] != 4:
        raise ValueError("Unsupported MMKV identity metadata")
    size = struct.unpack_from("<I", metadata, 28)[0]
    encrypted = data[4:4 + size]
    if len(encrypted) != size or zlib.crc32(encrypted) & 0xFFFFFFFF != struct.unpack_from("<I", metadata)[0]:
        raise ValueError("MMKV identity snapshot CRC mismatch")
    try:
        from Crypto.Cipher import AES
        plain = AES.new(FORMAT_KEY, AES.MODE_CFB, iv=metadata[12:28], segment_size=128).decrypt(encrypted)
    except ImportError:
        from . import aes_fallback
        plain = aes_fallback.cfb128_decrypt(FORMAT_KEY, metadata[12:28], encrypted)
    _, at = _varint(plain, 0)
    entries: dict[str, bytes] = {}
    while at < len(plain):
        key, at = _span(plain, at)
        value, at = _span(plain, at)
        if value:
            entries[key.decode()] = value
        else:
            entries.pop(key.decode(), None)
    username_raw, _ = _span(entries["mmkv_key_user_name"], 0)
    username = username_raw.decode()
    uin, end = _varint(entries["mmkv_key_latest_login_uin"], 0)
    if end != len(entries["mmkv_key_latest_login_uin"]) or not uin or uin > 0xFFFFFFFF:
        raise ValueError("Invalid image identity")
    account = db_storage.parent.name
    if not (account == username or account.startswith(username + "_")):
        raise ValueError("Image identity does not match the selected account")
    aes = hashlib.md5((str(uin) + username).encode()).hexdigest()[:16]
    xor = next(((uin >> shift) & 255 for shift in range(4) if (uin >> shift) & 255), 0)
    return aes, xor


def load_resource_map(database: Path) -> dict[tuple[str, int], str]:
    """从解密后的资源库里找 (会话, local_id) -> md5。表结构随版本变化，认不出就返回空。"""
    uri = Path(database).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                chat = next((c for c in columns if c.lower() in ("username", "chatid", "conversationid", "talker")), None)
                local = next((c for c in columns if c.lower() in ("localid", "msgid", "messageid")), None)
                md5 = next((c for c in columns if c.lower() in ("md5", "filemd5", "resourceid")), None)
                if chat and local and md5:
                    return {(str(a), int(b)): str(c).lower() for a, b, c in connection.execute(
                        f'SELECT "{chat}","{local}","{md5}" FROM "{table}"')
                        if a is not None and b is not None and c}
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        pass
    return {}


def index_attach_dir(account_dir: Path, username: str) -> dict[str, list[Path]]:
    """按文件名索引这个会话的图片缓存：别名（md5）-> 文件。优先只扫这个会话的目录。"""
    account_dir = Path(account_dir)
    conversation = account_dir / "msg" / "attach" / hashlib.md5(username.encode("utf-8")).hexdigest()
    base = conversation if conversation.is_dir() else account_dir / "msg" / "attach"
    index: dict[str, list[Path]] = {}
    if not base.is_dir():
        return index
    try:
        for path in base.rglob("*.dat"):
            stem = path.stem
            alias = stem.split("_", 1)[0].lower()
            index.setdefault(alias, []).append(path)
    except OSError:
        return index
    return index


def image_aliases(resource_md5: str | None, attributes: dict) -> set[str]:
    aliases = {resource_md5.lower()} if resource_md5 else set()
    original = attributes.get("md5")
    if isinstance(original, str) and len(original) == 32 and all(c in "0123456789abcdefABCDEF" for c in original):
        aliases.add(original.lower())
    return aliases


def _quality(path: Path, data: bytes, attributes: dict) -> str:
    expected = int(attributes.get("hdlength") or 0) or int(attributes.get("length") or 0)
    if expected == len(data) and str(attributes.get("md5") or "").lower() == hashlib.md5(data).hexdigest():
        return "original"
    return "client_full"


def best_cached_image(candidates: list[Path], attributes: dict, aes_key, xor_key,
                      *, allow_thumbnail: bool) -> dict | None:
    """在缓存文件里挑最好的一份。缩略图绝不算原图，只有明确允许时才返回并标记。"""
    full, thumbnails = [], []
    for path in candidates:
        suffix = path.stem.split("_", 1)[1].lower() if "_" in path.stem else ""
        (thumbnails if suffix == "t" else full).append(path)
    choices = []
    for path in full:
        data, fmt = decrypt_dat(path, aes_key, xor_key)
        if data:
            quality = _quality(path, data, attributes)
            choices.append((quality == "original", path.stem.endswith("_h"), len(data), data, fmt, quality))
    if choices:
        _, _, _, data, fmt, quality = max(choices, key=lambda item: item[:3])
        return {"data": data, "format": fmt, "quality": quality, "is_thumbnail": False}
    if allow_thumbnail:
        for path in thumbnails:
            data, fmt = decrypt_dat(path, aes_key, xor_key)
            if data:
                return {"data": data, "format": fmt, "quality": "thumbnail", "is_thumbnail": True}
    return None


def store_content(export_root: Path, data: bytes, ext: str) -> tuple[str, str]:
    """按内容寻址写进 media/<前两位>/<sha256>.<ext>，返回 (相对路径, sha256)。"""
    digest = hashlib.sha256(data).hexdigest()
    relative = f"media/{digest[:2]}/{digest}.{ext}"
    target = Path(export_root) / relative
    if target.is_file() and target.stat().st_size == len(data):
        return relative, digest
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".media-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return relative, digest


def _xml_root(raw) -> ET.Element | None:
    if not isinstance(raw, str):
        return None
    start = raw.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(raw[start:])
    except ET.ParseError:
        return None


def record_image_md5s(appmsg_xml: str) -> list[str]:
    """合并转发里图片条目的 md5，用来和本地缓存对应。"""
    root = _xml_root(appmsg_xml)
    found = []
    if root is None:
        return found
    for item in root.findall(".//recorditem"):
        nested = _xml_root(item.text) or item.find("recordinfo")
        if nested is None:
            continue
        for node in nested.findall(".//dataitem"):
            if node.get("datatype") == "2":
                value = (node.findtext("fullmd5") or node.get("fullmd5") or "").strip().lower()
                if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                    found.append(value)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解码一个本地图片缓存文件")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--aes-key", help="V2 图片需要的 16 字节密钥或其十六进制")
    parser.add_argument("--xor-key", type=lambda value: int(value, 0), default=0x88)
    args = parser.parse_args(argv)
    payload, fmt = decrypt_dat(args.source, args.aes_key, args.xor_key)
    if payload is None:
        raise SystemExit("unrecognized, incomplete, or invalid local image data")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".media-", dir=args.output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
        os.replace(temporary, args.output)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    print(f"wrote {len(payload)} bytes ({fmt})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
