#!/usr/bin/env python3
"""重新生成 examples/synthetic/account（全部是虚构数据和公开测试密钥）。普通用户用不到。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wechat_export_send import synthetic  # noqa: E402

target = ROOT / "examples" / "synthetic" / "account"
shutil.rmtree(target, ignore_errors=True)
info = synthetic.build_account(target)
digests = {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
           for path in sorted(target.rglob("*")) if path.is_file()}
(ROOT / "examples" / "synthetic" / "fixture-integrity.json").write_text(
    json.dumps({"notice": "PUBLIC TEST-ONLY synthetic data", "sha256": digests}, indent=1) + "\n", encoding="utf-8")
print("generated", len(digests), "files")
