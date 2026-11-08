"""读取解密后的微信 4.x 数据库：解压、联系人、会话、消息表。全部只读。"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import sqlite3
import zlib
from pathlib import Path


def _zstd_decompress(raw: bytes) -> bytes:
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=1024 * 1024 * 1024)
    except ImportError:
        library = ctypes.util.find_library("zstd")
        if not library:
            raise RuntimeError("zstd-compressed message requires python-zstandard or libzstd")
        zstd = ctypes.CDLL(library)
        zstd.ZSTD_decompressBound.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        zstd.ZSTD_decompressBound.restype = ctypes.c_ulonglong
        zstd.ZSTD_decompress.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
        zstd.ZSTD_decompress.restype = ctypes.c_size_t
        zstd.ZSTD_isError.argtypes = (ctypes.c_size_t,)
        zstd.ZSTD_isError.restype = ctypes.c_uint
        source = ctypes.create_string_buffer(raw)
        capacity = zstd.ZSTD_decompressBound(source, len(raw))
        if not capacity or capacity > 1024 * 1024 * 1024:
            raise ValueError("invalid or unreasonably large zstd message")
        output = ctypes.create_string_buffer(capacity)
        size = zstd.ZSTD_decompress(output, capacity, source, len(raw))
        if zstd.ZSTD_isError(size):
            raise ValueError("zstd message decompression failed")
        return output.raw[:size]


def decompress_content(value, compression_type=None):
    """微信把正文按 WCDB_CT_* 标记压缩存放：可能是 zstd、zlib，也可能是明文。"""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        return value
    if not raw:
        return ""
    mode = str(compression_type or "").lower()
    if mode in {"1", "zstd", "zstandard"} or raw.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            out = _zstd_decompress(raw)
        except Exception as exc:
            raise ValueError("zstd-compressed message could not be decoded") from exc
        return out.decode("utf-8", "replace")
    if mode in {"0", "none", "uncompressed"}:
        return raw.decode("utf-8", "replace")
    for wbits in (zlib.MAX_WBITS, zlib.MAX_WBITS | 16, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(raw, wbits).decode("utf-8", "replace")
        except zlib.error:
            pass
    return raw.decode("utf-8", "replace")


def open_ro(path: Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(path, sql) -> list[dict]:
    if path is None or not Path(path).is_file():
        return []
    try:
        connection = open_ro(path)
        try:
            return [dict(row) for row in connection.execute(sql)]
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def load_contacts(path: Path | None) -> dict[str, dict]:
    """username -> {nick_name, remark, display_name}。显示名优先用备注，其次昵称。"""
    tables = {row["name"] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    result: dict[str, dict] = {}
    for table in ("contact", "Contact", "contact_info"):
        if table not in tables:
            continue
        columns = [row["name"] for row in _rows(path, f'PRAGMA table_info("{table}")')]
        by_lower = {column.lower(): column for column in columns}
        idcol = next((by_lower[n] for n in ("user_name", "username", "wxid") if n in by_lower), None)
        nickcol = next((by_lower[n] for n in ("nick_name", "nickname") if n in by_lower), None)
        remarkcol = by_lower.get("remark")
        if not idcol:
            continue
        for row in _rows(path, f'SELECT * FROM "{table}"'):
            username = row.get(idcol)
            if not username:
                continue
            nickname = str(row.get(nickcol) or "") if nickcol else ""
            remark = str(row.get(remarkcol) or "") if remarkcol else ""
            result[str(username)] = {"username": str(username), "nick_name": nickname, "remark": remark,
                                     "display_name": remark or nickname}
    return result


def load_sessions(path: Path | None) -> dict[str, dict]:
    tables = {row["name"] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    table = next((name for name in ("SessionTable", "session", "sessions") if name in tables), None)
    if not table:
        return {}
    result = {}
    for row in _rows(path, f'SELECT * FROM "{table}"'):
        columns = {key.lower(): key for key in row}
        idcol = next((columns[n] for n in ("user_name", "username", "strusrname", "usrname", "talker") if n in columns), None)
        if idcol and row.get(idcol):
            result[str(row[idcol])] = dict(row)
    return result


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << (index * 7)
        if not byte & 128:
            return value, offset
    raise ValueError("invalid varint")


def _protobuf_fields(data: bytes):
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            size, offset = _varint(data, offset)
            if offset + size > len(data):
                raise ValueError("truncated field")
            value, offset = data[offset:offset + size], offset + size
        elif wire == 1:
            value, offset = data[offset:offset + 8], offset + 8
        elif wire == 5:
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise ValueError("unsupported wire type")
        yield number, wire, value


def packed_resource_md5(packed_info) -> str | None:
    """消息行 packed_info_data 里缓存文件名用的 md5：protobuf 字段 3 里的字段 4，32 位十六进制。

    这是对 4.1.13.26 观察到的结构（真实库里的图片消息全部能对上文件名），不是公开规范；解不出就返回 None。
    """
    if isinstance(packed_info, memoryview):
        packed_info = packed_info.tobytes()
    if isinstance(packed_info, str):
        packed_info = packed_info.encode("latin-1", "ignore")
    if not isinstance(packed_info, (bytes, bytearray)) or not packed_info:
        return None
    try:
        for number, wire, value in _protobuf_fields(bytes(packed_info)):
            if number != 3 or wire != 2:
                continue
            for inner_number, inner_wire, inner in _protobuf_fields(value):
                if inner_number == 4 and inner_wire == 2 and len(inner) == 32:
                    text = inner.decode("ascii").lower()
                    if all(c in "0123456789abcdef" for c in text):
                        return text
    except (ValueError, UnicodeDecodeError, IndexError):
        return None
    return None


def parse_room_nicknames(ext_buffer) -> dict[str, str]:
    """尽力解析 chat_room.ext_buffer 里的群昵称：成员是字段 1，其中 1=账号、2=群昵称。

    这是观察到的格式，不是公开规范。解析不了就返回空表，调用方退回联系人昵称。
    """
    if isinstance(ext_buffer, memoryview):
        ext_buffer = ext_buffer.tobytes()
    if not isinstance(ext_buffer, (bytes, bytearray)):
        return {}
    result: dict[str, str] = {}
    try:
        for number, wire, value in _protobuf_fields(bytes(ext_buffer)):
            if number != 1 or wire != 2:
                continue
            username, nickname = "", ""
            for inner_number, inner_wire, inner in _protobuf_fields(value):
                if inner_wire != 2:
                    continue
                if inner_number == 1:
                    username = inner.decode("utf-8")
                elif inner_number == 2:
                    nickname = inner.decode("utf-8")
            if username and nickname:
                result[username] = nickname
    except (ValueError, UnicodeDecodeError):
        return {}
    return result


def load_rooms(path: Path | None) -> dict[str, dict]:
    """群 -> {members: [username...], nicknames: {username: 群昵称}}。表不存在时返回空。"""
    tables = {row["name"] for row in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    rooms: dict[str, dict] = {}
    if "chat_room" not in tables:
        return rooms
    columns = {row["name"] for row in _rows(path, 'PRAGMA table_info("chat_room")')}
    if "username" not in columns:
        return rooms
    id_to_room: dict[int, str] = {}
    for row in _rows(path, 'SELECT * FROM "chat_room"'):
        name = row.get("username")
        if not name:
            continue
        rooms[str(name)] = {"members": [], "nicknames": parse_room_nicknames(row.get("ext_buffer"))}
        if row.get("id") is not None:
            id_to_room[int(row["id"])] = str(name)
    if "chatroom_member" in tables and "name2id" in tables:
        names = {int(r["rowid"]): str(r["username"]) for r in _rows(path, 'SELECT rowid, username FROM "name2id"') if r.get("username")}
        for row in _rows(path, 'SELECT room_id, member_id FROM "chatroom_member"'):
            room = id_to_room.get(int(row["room_id"] or 0))
            member = names.get(int(row["member_id"] or 0))
            if room and member:
                rooms[room]["members"].append(member)
    return rooms


def message_table(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def split_msg_type(value) -> tuple[int, int]:
    """local_type 低 32 位是基础类型，高 32 位是应用消息子类型。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0, 0
    number &= 0xFFFFFFFFFFFFFFFF
    return number & 0xFFFFFFFF, (number >> 32) & 0xFFFFFFFF


MESSAGE_COLUMNS = ("local_id", "server_id", "local_type", "real_sender_id", "create_time",
                   "origin_source", "source", "message_content", "compress_content",
                   "packed_info_data", "WCDB_CT_message_content")


def load_name2id(connection: sqlite3.Connection) -> dict[int, str]:
    if "Name2Id" not in table_names(connection):
        return {}
    return {int(row[0]): str(row[1]) for row in connection.execute("SELECT rowid, user_name FROM Name2Id") if row[1]}


def read_messages(connection: sqlite3.Connection, table: str, *, after_local_id: int, since_ts: int):
    """取 local_id 大于游标的新行，以及回看窗口内的旧行（撤回和编辑会原地改旧行）。"""
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    missing = [column for column in MESSAGE_COLUMNS if column not in columns]
    if missing:
        raise RuntimeError(f"unsupported message schema: missing {missing}")
    wanted = ", ".join(f'"{column}"' for column in MESSAGE_COLUMNS)
    sql = (f'SELECT {wanted} FROM "{table}" WHERE local_id > ? OR create_time >= ? '
           "ORDER BY create_time, local_id")
    return (dict(row) for row in connection.execute(sql, (after_local_id, since_ts)))
