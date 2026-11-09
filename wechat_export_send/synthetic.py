"""生成完全虚构的加密样本：目录结构、表结构和加密方式都和真实客户端一样，内容全是编的。

这里出现的密钥、盐都是公开的测试向量，不能用在真实数据上。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import sqlite3
import struct
import zlib
from pathlib import Path

from . import sqlcipher, wcdb

PUBLIC_TEST_PASSPHRASE = b"PUBLIC-TEST-ONLY-not-a-real-WeChat-key"
GROUP = "90000000001@chatroom"
SELF = "synthetic_self"
ALICE = "synthetic_linxiaoman"
BOB = "synthetic_zhouhe"
CEN = "synthetic_chenxing"
START = 1793145600  # 2026-10-28T00:00:00Z
ACCOUNT_DIR = "synthetic_self_0a1b"
PUBLIC_IMAGE_AES = "0123456789abcdef"
PUBLIC_IMAGE_XOR = 0x37
MESSAGE_SCHEMA = ("CREATE TABLE {table} (local_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER, local_type INTEGER, "
                  "sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER, status INTEGER, upload_status INTEGER, "
                  "download_status INTEGER, server_seq INTEGER, origin_source INTEGER, source TEXT, message_content TEXT, "
                  "compress_content TEXT, packed_info_data BLOB, WCDB_CT_message_content INTEGER, WCDB_CT_source INTEGER)")


def reserved_sqlite(path: Path, statements: list[str]) -> None:
    """建一个每页预留 80 字节的 SQLite，这样才能套 SQLCipher 的页格式。"""
    library = ctypes.util.find_library("sqlite3")
    if not library:
        raise RuntimeError("需要系统的 libsqlite3 才能生成样本")
    lib = ctypes.CDLL(library)
    lib.sqlite3_open.argtypes = (ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))
    lib.sqlite3_close.argtypes = (ctypes.c_void_p,)
    lib.sqlite3_file_control.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    lib.sqlite3_exec.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    database = ctypes.c_void_p()
    if lib.sqlite3_open(str(path).encode(), ctypes.byref(database)):
        raise RuntimeError("sqlite3_open failed")
    try:
        lib.sqlite3_exec(database, b"PRAGMA page_size=4096", None, None, None)
        reserve = ctypes.c_int(80)
        if lib.sqlite3_file_control(database, b"main", 38, ctypes.byref(reserve)):
            raise RuntimeError("SQLITE_FCNTL_RESERVE_BYTES failed")
        lib.sqlite3_exec(database, b"VACUUM", None, None, None)
        for statement in statements:
            if lib.sqlite3_exec(database, statement.encode("utf-8"), None, None, None):
                raise RuntimeError(f"sqlite3_exec failed: {statement[:60]}")
    finally:
        lib.sqlite3_close(database)
    header = path.read_bytes()[:100]
    if header[16:18] != b"\x10\x00" or header[20] != 80:
        raise RuntimeError("fixture must use 4096-byte pages with 80 reserved bytes")


def _salt(relative: str) -> bytes:
    return hashlib.sha256(("synthetic:" + relative).encode()).digest()[:16]


def _encrypt(plain: Path, target: Path, relative: str, keys: dict) -> None:
    salt = _salt(relative)
    enc_key = sqlcipher.derive_enc_key(PUBLIC_TEST_PASSPHRASE, salt)
    sqlcipher.encrypt_sqlite(plain, target, enc_key, salt=salt)
    keys[relative] = {"enc_key": enc_key.hex(), "salt": salt.hex()}
    plain.unlink()


def _sql(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _png(width: int = 64, height: int = 48, tint: int = 90) -> bytes:
    """纯标准库画一张小 PNG，生成样本时不依赖 Pillow。"""
    raw = b"".join(b"\x00" + b"".join(bytes((tint, 150 + y % 60, 200 - x % 90)) for x in range(width)) for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _v2_dat(data: bytes, key: str, xor: int) -> bytes:
    """按 V2 版式把图片包起来：前缀 AES-ECB、中段明文、结尾 XOR。"""
    from . import aes_fallback
    prefix, tail = data[:64], data[-32:]
    middle = data[64:-32]
    padding = 16 - len(prefix) % 16
    encrypted = aes_fallback.ecb_encrypt(key.encode("ascii"), prefix + bytes([padding]) * padding)
    return (b"\x07\x08V2\x08\x07" + struct.pack("<II", len(prefix), len(tail)) + b"\x00" +
            encrypted + middle + bytes(value ^ xor for value in tail))


def _xor_dat(data: bytes, xor: int) -> bytes:
    return bytes(value ^ xor for value in data)


def _image_xml(md5: str, size: int, *, advertised: bool = True) -> str:
    url = 'cdnbigimgurl="synthetic-file-id"' if advertised else ""
    return f'<msg><img md5="{md5}" hdlength="{size}" length="{size}" {url}/></msg>'


def _quote_xml(title: str, server_id: int, name: str, quoted: str) -> str:
    return (f"<msg><appmsg><title>{title}</title><type>57</type><refermsg><svrid>{server_id}</svrid>"
            f"<displayname>{name}</displayname><content>{quoted}</content></refermsg></appmsg></msg>")


def _forward_xml() -> str:
    record = ("<recordinfo><datalist>"
              '<dataitem datatype="1"><sourcename>林小满</sourcename><srcMsgCreateTime>1793100000</srcMsgCreateTime>'
              "<datadesc>A 区种香草</datadesc></dataitem>"
              '<dataitem datatype="1"><sourcename>周禾</sourcename><srcMsgCreateTime>1793100060</srcMsgCreateTime>'
              "<datadesc>B 区种花苗</datadesc></dataitem></datalist></recordinfo>")
    escaped = record.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<msg><appmsg><title>社区花园分区安排</title><type>19</type><recorditem>{escaped}</recorditem></appmsg></msg>"


def _link_xml() -> str:
    return ("<msg><appmsg><title>花园种植指南（虚构）</title><des>一篇并不存在的文章</des>"
            "<url>https://example.org/garden-guide</url><type>5</type></appmsg></msg>")


def _file_xml(name: str, size: int) -> str:
    return (f"<msg><appmsg><title>{name}</title><type>6</type><appattach><totallen>{size}</totallen>"
            "<aeskey>00000000000000000000000000000000</aeskey></appattach></appmsg></msg>")


def build_account(output: Path) -> dict:
    """在 output 下生成 gui/xwechat_files/<账号>/ 和 keys.json，返回摘要。"""
    output = Path(output)
    account = output / "gui" / "xwechat_files" / ACCOUNT_DIR
    storage = account / "db_storage"
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    keys: dict = {"_db_dir": "synthetic"}

    # 联系人
    contact_dir = storage / "contact"
    contact_dir.mkdir(exist_ok=True, mode=0o700)
    plain = contact_dir / "plain.db"
    people = [(SELF, "示例账号", ""), (ALICE, "林小满", ""), (BOB, "周禾", "禾哥"), (CEN, "陈星", ""),
              (GROUP, "合成示例群（虚构）", ""), ("gh_synthetic_news", "虚构公众号", "")]
    statements = ["CREATE TABLE contact (username TEXT PRIMARY KEY, nick_name TEXT, remark TEXT, alias TEXT)"]
    statements += [f"INSERT INTO contact VALUES ({_sql(a)},{_sql(b)},{_sql(c)},'')" for a, b, c in people]
    nickname_blob = _room_blob({ALICE: "小满（花园组）", BOB: "周禾", CEN: "陈星"})
    statements += ["CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, owner TEXT, ext_buffer BLOB)",
                   f"INSERT INTO chat_room VALUES (1,{_sql(GROUP)},{_sql(ALICE)},x'{nickname_blob.hex()}')",
                   "CREATE TABLE name2id (username TEXT)",
                   "CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)"]
    for index, person in enumerate((SELF, ALICE, BOB, CEN), 1):
        statements += [f"INSERT INTO name2id(rowid, username) VALUES ({index},{_sql(person)})",
                       f"INSERT INTO chatroom_member VALUES (1,{index})"]
    reserved_sqlite(plain, statements)
    _encrypt(plain, contact_dir / "contact.db", "contact/contact.db", keys)

    # 头像：故意少一个 CEN，覆盖“本地没有缓存头像”的情况
    head_dir = storage / "head_image"
    head_dir.mkdir(exist_ok=True, mode=0o700)
    plain = head_dir / "plain.db"
    statements = ["CREATE TABLE head_image (username TEXT PRIMARY KEY, md5 TEXT, image_buffer BLOB, update_time INTEGER)"]
    for tint, person in ((40, ALICE), (120, BOB), (200, GROUP)):
        statements.append(f"INSERT INTO head_image VALUES ({_sql(person)},'',x'{_png(32, 32, tint).hex()}',0)")
    statements.append(f"INSERT INTO head_image VALUES ('synthetic_empty_avatar','',x'',0)")
    reserved_sqlite(plain, statements)
    _encrypt(plain, head_dir / "head_image.db", "head_image/head_image.db", keys)

    # 会话
    session_dir = storage / "session"
    session_dir.mkdir(exist_ok=True, mode=0o700)
    plain = session_dir / "plain.db"
    reserved_sqlite(plain, ["CREATE TABLE SessionTable (username TEXT PRIMARY KEY, last_timestamp INTEGER)",
                            f"INSERT INTO SessionTable VALUES ({_sql(GROUP)},{START + 11000})",
                            f"INSERT INTO SessionTable VALUES ({_sql(ALICE)},{START + 12000})"])
    _encrypt(plain, session_dir / "session.db", "session/session.db", keys)

    # 媒体缓存
    picture = _png()
    picture_md5 = hashlib.md5(picture).hexdigest()
    xor_picture = _png(32, 32, 20)
    xor_md5 = hashlib.md5(xor_picture).hexdigest()
    attach = account / "msg" / "attach" / hashlib.md5(GROUP.encode()).hexdigest() / "2026-10" / "Img"
    attach.mkdir(parents=True, exist_ok=True, mode=0o700)
    (attach / f"{picture_md5}.dat").write_bytes(_v2_dat(picture, PUBLIC_IMAGE_AES, PUBLIC_IMAGE_XOR))
    (attach / f"{xor_md5}.dat").write_bytes(_xor_dat(xor_picture, 0x5A))
    thumb_only_md5 = hashlib.md5(b"synthetic thumb only").hexdigest()
    (attach / f"{thumb_only_md5}_t.dat").write_bytes(_xor_dat(_png(16, 16, 200), 0x5A))
    file_bytes = "社区花园活动清单（虚构）\n- 工具箱\n- 标签\n".encode("utf-8")
    file_dir = account / "msg" / "file" / "2026-10"
    file_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (file_dir / "活动清单.txt").write_bytes(file_bytes)

    # 消息库 0：群
    message_dir = storage / "message"
    message_dir.mkdir(exist_ok=True, mode=0o700)
    plain = message_dir / "plain0.db"
    table = wcdb.message_table(GROUP)
    statements = ["CREATE TABLE Name2Id (user_name TEXT)"]
    for person in (SELF, ALICE, BOB, CEN, GROUP):
        statements.append(f"INSERT INTO Name2Id VALUES ({_sql(person)})")
    ids = {SELF: 1, ALICE: 2, BOB: 3, CEN: 4}
    statements.append(MESSAGE_SCHEMA.format(table=table))
    at_source = f"<msgsource><atuserlist>{BOB},notify@all</atuserlist></msgsource>"
    rows = [
        (900001, 1, ALICE, 3600, "周末的社区花园活动改到周六上午，入口旁边集合。", None),
        (900002, 1, BOB, 3900, "收到，我会带上工具箱。", None),
        (900003, 49 | (57 << 32), CEN, 4200, _quote_xml("我可以负责准备标签。", 900001, "林小满", "周末的社区花园活动改到周六上午，入口旁边集合。"), None),
        (900004, 49 | (19 << 32), ALICE, 7200, _forward_xml(), None),
        (900005, 3, BOB, 10800, _image_xml(picture_md5, len(picture)), None),
        (900006, 3, CEN, 10900, _image_xml(xor_md5, len(xor_picture)), None),
        (900007, 3, ALICE, 10950, _image_xml(hashlib.md5(b"missing").hexdigest(), 1000), None),
        (900008, 3, ALICE, 10960, _image_xml(thumb_only_md5, 2000), None),
        (900009, 49 | (5 << 32), CEN, 10970, _link_xml(), None),
        (900010, 49 | (6 << 32), ALICE, 10980, _file_xml("活动清单.txt", len(file_bytes)), None),
        (900011, 1, ALICE, 10990, "@周禾 @所有人 记得周六带水", at_source),
        (900012, 34, BOB, 11000, "<msg><voicemsg length=\"1000\"/></msg>", None),
        (900013, 10000, CEN, 11010, "陈星 邀请 示例账号 加入了群聊", None),
        (900014, 1, SELF, 11020, "我也来帮忙。", None),
    ]
    for server_id, local_type, sender, offset, body, source in rows:
        origin = 1 if sender == SELF else 2
        statements.append(
            f"INSERT INTO {table} (server_id, local_type, real_sender_id, create_time, origin_source, source, message_content, "
            f"compress_content, packed_info_data, WCDB_CT_message_content) VALUES ({server_id},{local_type},{ids[sender]},"
            f"{START + offset},{origin},{_sql(source)},{_sql(body)},NULL,NULL,0)")
    reserved_sqlite(plain, statements)
    _encrypt(plain, message_dir / "message_0.db", "message/message_0.db", keys)

    # 消息库 1：私聊，顺便带一条 zstd 风格之外的 zlib 压缩内容
    plain = message_dir / "plain1.db"
    table = wcdb.message_table(ALICE)
    packed = zlib.compress("这条正文在库里是压缩过的。".encode("utf-8"))
    reserved_sqlite(plain, [
        "CREATE TABLE Name2Id (user_name TEXT)",
        f"INSERT INTO Name2Id VALUES ({_sql(SELF)})", f"INSERT INTO Name2Id VALUES ({_sql(ALICE)})",
        MESSAGE_SCHEMA.format(table=table),
        f"INSERT INTO {table} (server_id, local_type, real_sender_id, create_time, origin_source, source, message_content, "
        f"compress_content, packed_info_data, WCDB_CT_message_content) VALUES (800001,1,2,{START + 12000},2,NULL,NULL,x'{packed.hex()}',NULL,0)",
        f"INSERT INTO {table} (server_id, local_type, real_sender_id, create_time, origin_source, source, message_content, "
        f"compress_content, packed_info_data, WCDB_CT_message_content) VALUES (800002,1,1,{START + 12060},1,NULL,'好的，周六见。',NULL,NULL,0)",
    ])
    _encrypt(plain, message_dir / "message_1.db", "message/message_1.db", keys)

    # 资源库：不收录任何映射，测试只靠消息 XML 里的 md5 也能找到图
    plain = message_dir / "plain_resource.db"
    reserved_sqlite(plain, ["CREATE TABLE MessageResourceInfo (message_id INTEGER, packed_info BLOB)"])
    _encrypt(plain, message_dir / "message_resource.db", "message/message_resource.db", keys)

    # 假的 FTS 库，导出必须跳过它
    plain = message_dir / "plain_fts.db"
    reserved_sqlite(plain, ["CREATE TABLE fts_placeholder (body TEXT)"])
    _encrypt(plain, message_dir / "message_0_fts.db", "message/message_0_fts.db", keys)

    keys_path = output / "keys" / "all_keys.json"
    keys_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    keys_path.write_text(json.dumps(keys, indent=1) + "\n", encoding="utf-8")
    (output / "keys" / "image_key.json").write_text(
        json.dumps({"image_aes_key": PUBLIC_IMAGE_AES, "image_xor_key": PUBLIC_IMAGE_XOR}) + "\n", encoding="utf-8")
    return {"account_dir": ACCOUNT_DIR, "storage": str(storage), "keys": str(keys_path),
            "group": GROUP, "picture_md5": picture_md5}


def _room_blob(nicknames: dict[str, str]) -> bytes:
    def varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 127
            value >>= 7
            out.append(byte | (128 if value else 0))
            if not value:
                return bytes(out)

    def field(number: int, payload: bytes) -> bytes:
        return varint(number << 3 | 2) + varint(len(payload)) + payload

    return b"".join(field(1, field(1, name.encode()) + field(2, nick.encode())) for name, nick in nicknames.items())


def append_group_message(account_root: Path, *, server_id: int, text: str, sender_rowid: int = 3,
                         created: int | None = None, origin: int = 2, local_type: int = 1) -> int:
    """测试用：解密、追加一行、再加密，模拟客户端写库。"""
    keys = json.loads((Path(account_root) / "keys" / "all_keys.json").read_text(encoding="utf-8"))
    storage = Path(account_root) / "gui" / "xwechat_files" / ACCOUNT_DIR / "db_storage"
    return _rewrite(storage / "message" / "message_0.db", keys["message/message_0.db"], [
        (f"INSERT INTO {wcdb.message_table(GROUP)} (server_id, local_type, real_sender_id, create_time, origin_source, source, "
         "message_content, compress_content, packed_info_data, WCDB_CT_message_content) VALUES (?,?,?,?,?,NULL,?,NULL,NULL,0)",
         (server_id, local_type, sender_rowid, created if created is not None else START + 20000, origin, text))])


def update_group_message(account_root: Path, *, server_id: int, text: str, local_type: int = 10000) -> int:
    """测试用：原地改一行，模拟撤回。"""
    keys = json.loads((Path(account_root) / "keys" / "all_keys.json").read_text(encoding="utf-8"))
    storage = Path(account_root) / "gui" / "xwechat_files" / ACCOUNT_DIR / "db_storage"
    return _rewrite(storage / "message" / "message_0.db", keys["message/message_0.db"], [
        (f"UPDATE {wcdb.message_table(GROUP)} SET message_content=?, local_type=? WHERE server_id=?",
         (text, local_type, server_id))])


def _rewrite(encrypted: Path, info: dict, statements: list[tuple]) -> int:
    enc_key = bytes.fromhex(info["enc_key"])
    salt = bytes.fromhex(info["salt"])
    plain = encrypted.with_suffix(".rewrite")
    sqlcipher.full_decrypt(encrypted, plain, enc_key)
    connection = sqlite3.connect(plain)
    try:
        for sql, parameters in statements:
            connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()
    count = sqlcipher.encrypt_sqlite(plain, encrypted, enc_key, salt=salt)
    plain.unlink()
    return count
