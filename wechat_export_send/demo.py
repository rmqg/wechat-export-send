"""离线 demo：不用账号、不联网，把仓库带的合成加密账号导出成 chat-export/v1 并逐项检查。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from . import ROOT, exporter, sqlcipher
from .config import DEFAULTS, IdMapper, _merge

FIXTURE = ROOT / "examples" / "synthetic" / "account"
# 样本里 create_time 最大约 1793157660；固定「现在」让每次结果都一样
FIXED_NOW = 1793175600


def _check(results: list, name: str, passed: bool) -> None:
    results.append({"check": name, "passed": bool(passed)})


def run(output: Path, *, fixture: Path = FIXTURE) -> dict:
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"输出目录不是空的：{output}")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not fixture.is_dir():
        raise SystemExit(f"找不到合成样本：{fixture}")
    account = output / "synthetic-account"
    shutil.copytree(fixture, account)
    storage = next(account.glob("gui/xwechat_files/*/db_storage"))
    keys = sqlcipher.load_keys(account / "keys" / "all_keys.json")
    image = json.loads((account / "keys" / "image_key.json").read_text(encoding="utf-8"))
    settings = _merge(DEFAULTS, {})["export"]
    export_root = output / "export"
    results: list[dict] = []

    def export_once() -> dict:
        with tempfile.TemporaryDirectory(prefix=".demo-", dir=output) as temporary:
            source = exporter.Source(storage, keys, Path(temporary))
            return exporter.Exporter(source, export_root, settings, IdMapper(b"demo-only-salt"), account_label=storage.parent.name,
                                     image_keys=(image["image_aes_key"], image["image_xor_key"]), now=FIXED_NOW).run()

    first = export_once()
    manifest = json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))
    conversations = {item["kind"]: item for item in manifest["conversations"]}
    _check(results, "manifest.schema 是 chat-export/v1", manifest["schema"] == "chat-export/v1")
    _check(results, "导出了一个群和一个私聊", set(conversations) == {"group", "private"})
    lines = [json.loads(line) for line in (export_root / conversations["group"]["file"]).read_text(encoding="utf-8").splitlines()]
    by_type: dict[str, list] = {}
    for line in lines:
        by_type.setdefault(line["type"], []).append(line)
    _check(results, "引用消息带 reply_to", by_type.get("quote") and by_type["quote"][0]["reply_to"].get("id"))
    _check(results, "合并转发带 forward 条目", by_type.get("forward") and len(by_type["forward"][0]["forward"]) == 2)
    images = by_type.get("image", [])
    ready = [item for line in images for item in line["media"] if item["status"] == "ready"]
    pending = [item for line in images for item in line["media"] if item["status"] == "pending"]
    _check(results, "V2 和 XOR 两种图片都解出来了", len(ready) == 2)
    _check(results, "只有缩略图或没有缓存的图标成 pending，不拿缩略图冒充", len(pending) == 2 and not any(item.get("is_thumbnail") for item in ready))
    _check(results, "媒体文件的 sha256 和文件名一致", all(item["path"].endswith(item["sha256"] + ".png") and (export_root / item["path"]).is_file() for item in ready))
    _check(results, "@ 列表含 @all", any("@all" in line.get("mentions", []) for line in lines))
    _check(results, "占位文字没有混进正文", not any(line.get("text") for line in images))
    second = export_once()
    _check(results, "第二次导出没有新增行（增量）", second["new_lines"] == 0 and second["rewritten_lines"] == 0)

    # 错误的密钥和被改过的密文都必须被拒绝
    probe = storage / "message" / "message_0.db"
    wrong_key_rejected = False
    try:
        sqlcipher.full_decrypt(probe, output / "wrong.sqlite", bytes(32))
    except ValueError:
        wrong_key_rejected = True
    _check(results, "错误密钥被 HMAC 拒绝", wrong_key_rejected)
    tampered = output / "tampered.db"
    data = bytearray(probe.read_bytes())
    data[128] ^= 1
    tampered.write_bytes(data)
    tamper_rejected = False
    try:
        sqlcipher.full_decrypt(tampered, output / "tampered.sqlite", bytes.fromhex(keys["message/message_0.db"]["enc_key"]))
    except ValueError:
        tamper_rejected = True
    tampered.unlink()
    _check(results, "被改过的密文被 HMAC 拒绝", tamper_rejected)
    passed = all(item["passed"] for item in results)
    return {"ok": passed, "output": str(export_root), "first_run": first, "second_run": second, "checks": results,
            "fast_aes": sqlcipher.have_fast_aes()}
