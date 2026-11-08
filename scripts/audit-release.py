#!/usr/bin/env python3
"""发布前的隐私检查：扫已跟踪文件里不该出现的东西。CI 和本地都跑。"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "微信账号 ID": r"wxid_[a-z0-9]{6,}",
    "Windows SID": r"S-1-5-21-\d+",
    "邮箱": r"[A-Za-z0-9._%+-]+@(gmail|qq|163|outlook|hotmail)\.com",
    "CGNAT 地址（Tailscale 等）": r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b",
    "内网地址": r"\b(192\.168|10\.\d+|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+\b",
    "群 ID": r"\b\d{9,}@chatroom\b",
    "私有服务路径": r"/mnt/[a-z0-9_-]+/",
}
ALLOW = {
    "群 ID": {"90000000001@chatroom"},
}
# 第三方文件原样收录，不改
SKIP = ("vendor/", "examples/synthetic/account/")


def main() -> int:
    # -z：中文文件名不会被加引号转义
    files = [name for name in subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\0") if name]
    problems = []
    for name in files:
        if name.startswith(SKIP) or name == "scripts/audit-release.py":
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                if match.group(0) in ALLOW.get(label, ()):
                    continue
                problems.append(f"{name}: {label}")
    # 发布包里不能有这些文件
    for name in files:
        if re.search(r"(^|/)(instances|runtime|keys|decrypted_db|gui)/", name) and not name.startswith("examples/synthetic/"):
            problems.append(f"{name}: 不该进仓库的目录")
        if name.endswith((".db", ".sqlite")) and not name.startswith("examples/synthetic/"):
            problems.append(f"{name}: 数据库文件")
    for line in sorted(set(problems)):
        print(line)
    print(f"检查了 {len(files)} 个文件，{len(set(problems))} 处问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
