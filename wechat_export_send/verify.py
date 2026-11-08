"""拿解密后的数据库核对导出结果。只输出计数和哈希过的会话 ID，绝不输出消息内容。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile

from . import sqlcipher, wcdb
from .config import IdMapper, Instance, conversation_kind
from .exporter import Exporter, Source


def _exported(export_root: Path, item: dict) -> dict:
    """最后一行为准：id -> 消息。文件里的总行数也留着，用来看重写了多少。"""
    latest: dict[str, dict] = {}
    total = 0
    bad = 0
    path = export_root / item["file"]
    if not path.is_file():
        return {"latest": latest, "total": 0, "bad": 0}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            total += 1
            try:
                message = json.loads(line)
                latest[message["id"]] = message
            except (ValueError, KeyError):
                bad += 1
    return {"latest": latest, "total": total, "bad": bad}


def compare(source: Source, export_root: Path, settings: dict, mapper: IdMapper) -> dict:
    manifest = json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))
    until = int(datetime.fromisoformat(manifest["generated_at"]).timestamp())
    since = int(settings.get("since") or 0)
    helper = Exporter(source, export_root, settings, mapper, account_label="", image_keys=(None, None))
    contacts, sessions, rooms = source.metadata()
    known = set(contacts) | set(sessions) | set(rooms)
    expected: Counter = Counter()
    unmapped = 0
    for relative in source.shards():
        plain = source.decrypt(relative)
        if plain is None:
            continue
        try:
            connection = wcdb.open_ro(plain)
            try:
                by_table = {wcdb.message_table(name): name for name in known | set(wcdb.load_name2id(connection).values())}
                for table in wcdb.table_names(connection):
                    if not table.startswith("Msg_"):
                        continue
                    username = by_table.get(table)
                    if username is None:
                        unmapped += connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                        continue
                    kind = conversation_kind(username)
                    display = contacts.get(username, {}).get("display_name") or ""
                    if not helper._wanted(username, display, kind):
                        continue
                    count = connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE create_time >= ? AND create_time <= ?',
                                               (since, until)).fetchone()[0]
                    expected[mapper.conversation(username, kind)] += count
            finally:
                connection.close()
        finally:
            plain.unlink(missing_ok=True)
    conversations = {item["id"]: item for item in manifest["conversations"]}
    types: Counter = Counter()
    media_status: Counter = Counter()
    media_quality: Counter = Counter()
    mismatches = []
    total_lines = bad_lines = rows = exported = empty_text = no_sender = missing_files = 0
    for conversation_id in sorted(set(expected) | set(conversations)):
        item = conversations.get(conversation_id)
        data = _exported(export_root, item) if item else {"latest": {}, "total": 0, "bad": 0}
        total_lines += data["total"]
        bad_lines += data["bad"]
        within = 0
        for message in data["latest"].values():
            if int(message.get("ts") or 0) <= until:
                within += 1
            types[message.get("type", "?")] += 1
            if message.get("type") == "text" and not message.get("text"):
                empty_text += 1
            if message.get("sender_id") in {"", "unknown", None}:
                no_sender += 1
            for media in message.get("media", []):
                media_status[f"{media.get('kind')}:{media.get('status')}"] += 1
                if media.get("source_quality"):
                    media_quality[media["source_quality"]] += 1
                if media.get("status") == "ready" and not (export_root / str(media.get("path"))).is_file():
                    missing_files += 1
        rows += expected[conversation_id]
        exported += within
        if expected[conversation_id] != within:
            mismatches.append({"conversation_id": conversation_id, "kind": item["kind"] if item else "",
                               "db_rows": expected[conversation_id], "exported": within})
    return {"ok": not mismatches and not bad_lines and not missing_files, "conversations": len(conversations),
            "db_rows": rows, "exported_unique": exported, "file_lines": total_lines, "bad_lines": bad_lines,
            "unmapped_rows": unmapped, "mismatches": mismatches[:50], "mismatch_count": len(mismatches),
            "types": dict(types), "media": dict(media_status), "media_quality": dict(media_quality),
            "empty_text_messages": empty_text, "messages_without_sender": no_sender,
            "ready_media_missing_on_disk": missing_files, "until": until}


def verify_instance(instance: Instance) -> dict:
    keys = sqlcipher.load_keys(instance.keys_file)
    temporary = tempfile.mkdtemp(prefix="verify-", dir=instance.state)
    try:
        source = Source(instance.db_storage(), keys, Path(temporary))
        mapper = IdMapper(instance.id_salt(), instance.config["export"]["id_mode"])
        return compare(source, instance.export_root, instance.config["export"], mapper)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
