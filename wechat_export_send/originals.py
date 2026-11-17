"""可选：用官方客户端缓存的 CDN 线路下载消息绑定的完整图片。默认关闭（originals.enabled=false）。

协议格式参考 thisnick/agent-wechat 的 wechat-protocol/media.rs 和 large_cdn_config.rs（MIT，
见 THIRD_PARTY_NOTICES.md）；这里是独立实现。不登录、不加载客户端、不改客户端文件、不用界面坐标。
CDN 凭据只留在内存里。这一步会把请求发到腾讯的 CDN；只在你明确需要原图时打开。
"""
from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import struct
import shutil
import tempfile
import time
import urllib.error
import urllib.request

from . import media, message_parse, sqlcipher, wcdb
from .config import Instance, atomic_write

DEFAULT_CLIENT_VERSION = -227271398  # Linux 4.1.13.26 的协议版本号
MAX_IMAGE_BYTES = 26_214_383


def protobuf(data):
    if len(data) > 512 * 1024:
        raise ValueError("CDN configuration exceeds the supported bound")
    result, at = [], 0
    while at < len(data):
        tag, at = media._varint(data, at)
        number, wire = tag >> 3, tag & 7
        if not number or number > 10000:
            raise ValueError("Invalid CDN configuration tag")
        if wire == 0:
            value, at = media._varint(data, at)
        elif wire == 2:
            value, at = media._span(data, at)
        else:
            raise ValueError("Unsupported CDN configuration layout")
        result.append((number, wire, value))
        if len(result) > 10000:
            raise ValueError("Too many CDN configuration fields")
    return result


def one(fields, number, wire, default=None):
    values = [v for n, w, v in fields if n == number and w == wire]
    if not values and default is not None:
        return default
    if len(values) != 1:
        raise ValueError("Missing or ambiguous CDN configuration field")
    return values[0]


def image_route(source, filetype, config_root=Path("/config")):
    image_key, _ = media.image_key(source)
    usernames = {source.parent.name, source.parent.name.rsplit("_", 1)[0]}
    cache = (Path(config_root) / ".xwechat/net/cdncomm/cdninfo_new.cache").read_bytes()
    bound = []
    for number, wire, raw in protobuf(cache):
        if number != 1 or wire != 2:
            continue
        record = protobuf(raw)
        table = protobuf(one(record, 7, 2))
        uin = one(table, 2, 0)
        if any(hashlib.md5((str(uin) + user).encode()).hexdigest()[:16] == image_key for user in usernames):
            bound.append(table)
    if len(bound) != 1:
        raise ValueError("CDN cache is not uniquely bound to the selected account")
    table = bound[0]
    rules = []
    for number, wire, raw in table:
        if number != 8 or wire != 2:
            continue
        rule = protobuf(raw)
        if any(n not in range(1, 7) or w != 0 for n, w, _ in rule):
            raise ValueError("Unsupported CDN routing predicate")
        if all(one(rule, n, 0, 0) in (0, actual) for n, actual in ((2, 1), (3, 1), (4, filetype), (5, 2))):
            sequence = one(rule, 1, 0) & 0xffffffff
            if sequence & (1 << 31):
                sequence -= 1 << 32
            rules.append((sequence, one(rule, 6, 0)))
    rules.sort()
    if not rules or sum(rank == rules[0][0] for rank, _ in rules) != 1:
        raise ValueError("Missing or ambiguous original-image CDN route")
    configs = [protobuf(raw) for n, w, raw in table if n == 7 and w == 2]
    configs = [c for c in configs if one(c, 1, 0) == rules[0][1]]
    if len(configs) != 1:
        raise ValueError("CDN route target is ambiguous")
    config = configs[0]
    auth = one(config, 8, 2)
    ips = []
    for n, w, value in config:
        if n != 3 or w != 0:
            continue
        low = value & 0xffffffff
        signed = low if low < (1 << 31) else low + 0xffffffff00000000
        if value not in (low, signed):
            raise ValueError("Invalid CDN endpoint integer")
        ip = ipaddress.IPv4Address(struct.pack("<I", low))
        if not ip.is_global:
            raise ValueError("CDN endpoint is not public IPv4")
        ips.append(str(ip))
    if not ips or len(ips) > 32 or not 0 < len(auth) <= 65536 or 443 not in [v for n, w, v in config if n == 7 and w == 0]:
        raise ValueError("Invalid CDN route credentials or endpoint")
    return {"version": one(table, 1, 0), "uin": one(table, 2, 0), "auth": auth, "server_ips": ips}


def pack_fields(fields):
    return b"".join(struct.pack(">I", len(part)) + part
                    for key, value in sorted(fields.items()) for part in (key.encode("ascii"), value))


def unpack_fields(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("CDN range response exceeds the supported bound")
    fields, at = {}, 0
    while at < len(raw):
        parts = []
        for _ in range(2):
            if at + 4 > len(raw):
                raise ValueError("Truncated CDN field length")
            size = struct.unpack_from(">I", raw, at)[0]
            at += 4
            if size > len(raw) - at:
                raise ValueError("Truncated CDN field")
            parts.append(raw[at:at + size])
            at += size
        key = parts[0].decode("ascii")
        if not key or len(key) > 128 or not all(c.isalnum() or c in "_-" for c in key) or key in fields:
            raise ValueError("Invalid or duplicate CDN field")
        fields[key] = parts[1]
        if len(fields) > 256:
            raise ValueError("Too many CDN response fields")
    return fields


def number(fields, key):
    value = fields.get(key, b"")
    if not value or len(value) > 20 or not value.lstrip(b"-").isdigit():
        raise ValueError("Missing or invalid CDN response number")
    return int(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def download(route, descriptor, deadline, client_version=DEFAULT_CLIENT_VERSION):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    size = descriptor["expected_length"]
    expected_ciphertext = (size // 16 + 1) * 16
    original = descriptor['filetype'] == 1
    total = expected_ciphertext if original else None
    representation = None
    server_size = None
    server_md5 = None
    fields = {k: str(v).encode() for k, v in {
        "ver": route["version"], "weixinnum": route["uin"], "clientversion": client_version & 0xffffffff,
        "nettype": 1, "acceptdupack": 1, "safeproto": 1, "filetype": descriptor["filetype"],
        "wxchattype": 0, "lastretcode": 0, "ipseq": 0, "largesvideo": 0,
        # Standard images may be a different representation from the original.
        "sourceflag": 0, "wxautostart": 0, "downpicformat": 4,
    }.items()}
    fields.update(clientostype=b"UnifiedPCLinux", authkey=route["auth"], wxmsgflag=b"", fileid=descriptor["fileid"].encode())
    encrypted = bytearray()
    opener = urllib.request.build_opener(NoRedirect())
    sequence = 0
    while total is None or len(encrypted) < total:
        sequence += 1
        if sequence > ((MAX_IMAGE_BYTES // 16 + 1) * 16 + 65535) // 65536:
            raise ValueError('CDN transfer exceeds the supported bound')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Original-image download deadline exceeded")
        start = len(encrypted)
        end = start + 65535 if total is None else min(start + 65535, total - 1)
        fields.update(seq=str(sequence).encode(), rangestart=str(start).encode(), rangeend=str(end).encode())
        body = pack_fields(fields)
        failure = None
        for endpoint in tuple(route["server_ips"]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Original-image download deadline exceeded")
            request = urllib.request.Request("http://" + endpoint + ":443/download", data=body,
                headers={"Accept": "*/*", "Content-Type": "application/octet-stream", "User-Agent": "MicroMessenger Client",
                         "Connection": "close", "C-Seq": str(sequence)}, method="POST")
            try:
                with opener.open(request, timeout=min(15, remaining)) as response:
                    if response.status != 200:
                        raise RuntimeError(f"CDN HTTP status {response.status}")
                    reply = unpack_fields(response.read(2 * 1024 * 1024 + 1))
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"CDN HTTP status {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                failure = exc
                continue
            route["server_ips"].remove(endpoint)
            route["server_ips"].insert(0, endpoint)
            break
        else:
            reason = failure.reason if isinstance(failure, urllib.error.URLError) else failure
            raise RuntimeError(f"All configured CDN endpoints failed ({type(reason).__name__})") from None
        code = number(reply, "retcode")
        if code:
            raise RuntimeError(f"CDN original-image download returned {code}")
        block = reply.get('filedata', b'')
        returned_total = number(reply, 'totalsize')
        returned_end = number(reply, 'rangeend')
        if (number(reply, 'seq') != sequence or number(reply, 'rangestart') != start
                or not block or not 0 < returned_total <= (MAX_IMAGE_BYTES // 16 + 1) * 16
                or returned_total % 16 or returned_end != start + len(block) - 1
                or returned_end > end or returned_end >= returned_total
                or total is not None and total != returned_total):
            raise ValueError('CDN range correlation or size changed')
        total = returned_total
        current_representation = {key: number(reply, key) if key in reply else None
                                  for key in ('substituteftype', 'rsppicformat')}
        if representation is not None and representation != current_representation:
            raise ValueError('CDN representation changed during transfer')
        representation = current_representation
        if not original and representation['substituteftype'] not in (2, 12):
            raise ValueError('CDN did not return a complete standard image')
        if 'rawfilesize' in reply:
            value = number(reply, 'rawfilesize')
            if server_size is not None and value != server_size:
                raise ValueError('CDN source size changed during transfer')
            server_size = value
        if reply.get('rawfilemd5'):
            value = reply['rawfilemd5'].decode('ascii').lower()
            if len(value) != 32 or any(c not in '0123456789abcdef' for c in value) or server_md5 is not None and value != server_md5:
                raise ValueError('CDN source checksum changed during transfer')
            server_md5 = value
        encrypted.extend(block)
    if total is None or len(encrypted) != total:
        raise ValueError('CDN transfer is incomplete')
    plain = unpad(AES.new(descriptor['key'], AES.MODE_ECB).decrypt(encrypted), 16)
    digest = hashlib.md5(plain).hexdigest()
    original_matches = len(plain) == size and digest == descriptor['expected_md5']
    if not 0 < len(plain) <= MAX_IMAGE_BYTES or original and not original_matches:
        raise ValueError('Downloaded original failed message length or MD5 verification')
    if original and ((server_size is not None and len(plain) != server_size) or (server_md5 is not None and digest != server_md5)):
        raise ValueError('Downloaded image failed server integrity verification')
    return plain, {'ciphertext_bytes': total, 'requested_filetype': descriptor['filetype'],
                   **representation, 'original_md5_matches': original_matches,
                   'reported_source_bytes': server_size, 'reported_source_md5': server_md5}


def _descriptor(attributes: dict) -> dict:
    original = bool(attributes.get("cdnbigimgurl"))
    fileid = attributes.get("cdnbigimgurl") or attributes.get("cdnmidimgurl")
    size = int(attributes.get("hdlength") or 0) if original else int(attributes.get("length") or 0)
    md5 = str(attributes.get("md5") or "").lower()
    key = bytes.fromhex(attributes.get("aeskey", ""))
    if not fileid or len(fileid) > 8192 or len(key) != 16 or not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError("Unsupported or incomplete full-image descriptor")
    if len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5):
        raise ValueError("Image message has no valid source checksum")
    return {"fileid": fileid, "key": key, "expected_length": size, "expected_md5": md5,
            "filetype": 1 if original else 2, "quality": "original" if original else "client_full"}


def _lock_read_cursors(lock_path: Path, state_path: Path) -> dict:
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return json.loads(state_path.read_text(encoding="utf-8"))


def fetch_pending(instance: Instance, *, limit: int | None = None) -> dict:
    """处理导出状态里 status=pending 的图片：下载、校验、入库，再用同一个 id 重写一行。"""
    config = instance.config["originals"]
    if not config["enabled"]:
        raise RuntimeError("originals.enabled 是 false；这一步会访问腾讯 CDN，确实需要时再打开")
    state_root = instance.state / "export"
    state_path = state_root / "cursors.json"
    lock_path = state_root / "export.lock"
    state = _lock_read_cursors(lock_path, state_path)

    now = int(time.time())
    pending_days = int(instance.config["export"].get("media", {}).get("pending_days") or 7)
    max_attempts = max(1, int(config.get("max_attempts") or 5))
    expired_keys: set[tuple[str, str]] = set()
    eligible: list[tuple[int, int, str, dict]] = []
    for conversation_id, conversation in state.get("conversations", {}).items():
        for entry in conversation.get("pending", []):
            msg = entry.get("message") or {}
            msg_id = str(msg.get("id") or "")
            ts = int(msg.get("ts") or 0)
            attempts = int(entry.get("attempts") or 0)
            if (pending_days > 0 and ts and ts < now - pending_days * 86400) or attempts >= max_attempts:
                expired_keys.add((conversation_id, msg_id))
                continue
            if now >= int(entry.get("next_retry_at") or 0):
                eligible.append((attempts, -ts, conversation_id, entry))
    eligible.sort(key=lambda item: (item[0], item[1]))
    max_items = int(limit or config["max_per_run"])
    eligible = eligible[:max_items]

    stats = {"requested": 0, "fetched": 0, "failed": 0, "expired": len(expired_keys)}
    if not eligible and not expired_keys:
        return stats

    fetched_by_conv: dict[str, dict[str, dict]] = {}
    retry_updates: dict[tuple[str, str], dict] = {}
    if eligible:
        storage = instance.db_storage()
        keys = sqlcipher.load_keys(instance.keys_file)
        deadline = time.monotonic() + max(5, int(config["timeout_seconds"]) - 10)
        temp = Path(tempfile.mkdtemp(prefix="originals-", dir=instance.state))
        shard_cache: dict[str, tuple[sqlite3.Connection, list[str]]] = {}
        routes: dict[int, dict] = {}
        try:
            for _, _, conversation_id, entry in eligible:
                if time.monotonic() >= deadline:
                    break
                stats["requested"] += 1
                msg_id = str(entry.get("message", {}).get("id") or "")
                relative = entry["shard"]
                try:
                    cached = shard_cache.get(relative)
                    if cached is None:
                        key_info = sqlcipher.key_for_rel(keys, relative)
                        if not key_info:
                            raise RuntimeError(f"missing database key for {relative}")
                        plain = sqlcipher.decrypted_snapshot(
                            storage / relative,
                            key_info,
                            temp / (hashlib.sha256(relative.encode()).hexdigest() + ".db"),
                            verify=False,
                        )
                        connection = wcdb.open_ro(plain)
                        tables = [t for t in wcdb.table_names(connection) if t.startswith("Msg_")]
                        cached = shard_cache[relative] = (connection, tables)
                    connection, tables = cached
                    message = _fetch_one_from_connection(
                        instance, storage, connection, tables, entry, routes, config, deadline
                    )
                except Exception:
                    stats["failed"] += 1
                    attempts = int(entry.get("attempts") or 0) + 1
                    if attempts >= max_attempts:
                        expired_keys.add((conversation_id, msg_id))
                        stats["expired"] += 1
                    else:
                        delay = min(300 * (2 ** (attempts - 1)), 21600)
                        retry_updates[(conversation_id, msg_id)] = {
                            "attempts": attempts,
                            "next_retry_at": int(time.time()) + delay,
                        }
                    continue
                stats["fetched"] += 1
                fetched_by_conv.setdefault(conversation_id, {})[msg_id] = message
        finally:
            for connection, _ in shard_cache.values():
                try:
                    connection.close()
                except Exception:
                    pass
            shutil.rmtree(temp, ignore_errors=True)

    if fetched_by_conv or retry_updates or expired_keys:
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            latest_state = json.loads(state_path.read_text(encoding="utf-8"))
            for conversation_id, conversation in latest_state.get("conversations", {}).items():
                conv_fetched = fetched_by_conv.get(conversation_id, {})
                keep = []
                out_lines = []
                for entry in conversation.get("pending", []):
                    msg_id = str(entry.get("message", {}).get("id") or "")
                    key = (conversation_id, msg_id)
                    if msg_id in conv_fetched:
                        out_lines.append(
                            json.dumps(conv_fetched[msg_id], ensure_ascii=False, separators=(",", ":")) + "\n"
                        )
                        continue
                    if key in expired_keys:
                        continue
                    if key in retry_updates:
                        entry = {**entry, **retry_updates[key]}
                    keep.append(entry)
                conversation["pending"] = keep
                if out_lines:
                    path = instance.export_root / conversation["file"]
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write("".join(out_lines))
                        stream.flush()
                        os.fsync(stream.fileno())
                    instance.give_to_owner(path)
            atomic_write(state_path, json.dumps(latest_state, ensure_ascii=False, separators=(",", ":")))
            # Host cron runs fetch-originals as root, while export runs as the
            # instance UID in a container. mkstemp+replace creates a root-owned
            # 0600 inode: return it to the configured owner before releasing
            # export.lock, otherwise the next incremental export cannot read it.
            instance.give_to_owner(state_path)
    return stats


def _fetch_one_from_connection(instance, storage, connection, msg_tables, entry, routes, config, deadline) -> dict:
    row = None
    for table in msg_tables:
        found = connection.execute(
            f'SELECT message_content, WCDB_CT_message_content, server_id FROM "{table}" WHERE local_id=?',
            (entry["local_id"],),
        ).fetchone()
        if found is not None and str(found["server_id"]) == entry["message"]["id"]:
            row = dict(found)
            break
    if row is None:
        raise ValueError("message row disappeared")
    body = wcdb.decompress_content(row["message_content"], row["WCDB_CT_message_content"])
    _, content = message_parse.split_group_sender(body)
    descriptor = _descriptor(message_parse.parse_image_attrs(content))
    route = routes.get(descriptor["filetype"])
    if route is None:
        route = routes[descriptor["filetype"]] = image_route(storage, descriptor["filetype"], instance.gui)
    raw, transfer = download(route, descriptor, deadline, int(config["client_version_code"]))
    fmt = media.detect_image_format(raw)
    if fmt not in media.MIME_TYPES:
        raise ValueError("Downloaded image format is not supported")
    relative_path, digest = media.store_content(instance.export_root, raw, media.EXTENSIONS.get(fmt, fmt))
    message = dict(entry["message"])
    message["media"] = [{"kind": "image", "path": relative_path, "sha256": digest, "mime": media.MIME_TYPES[fmt], "bytes": len(raw),
                         "status": "ready", "is_thumbnail": False, "source_quality": descriptor["quality"],
                         "md5_verified": transfer["original_md5_matches"]}]
    return message


def _fetch_one(instance, storage, keys, entry, routes, config, deadline, temp) -> dict:
    relative = entry["shard"]
    plain = sqlcipher.decrypted_snapshot(
        storage / relative, sqlcipher.key_for_rel(keys, relative),
        temp / hashlib.sha256(relative.encode()).hexdigest(), verify=False,
    )
    try:
        connection = wcdb.open_ro(plain)
        try:
            tables = [t for t in wcdb.table_names(connection) if t.startswith("Msg_")]
            return _fetch_one_from_connection(instance, storage, connection, tables, entry, routes, config, deadline)
        finally:
            connection.close()
    finally:
        plain.unlink(missing_ok=True)
