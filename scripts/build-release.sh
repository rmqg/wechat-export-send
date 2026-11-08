#!/bin/sh
# 构建 release 包和 SHA256SUMS。本地和 CI 都用这个脚本。
# 用 git archive：文件模式取自 git 索引，不受文件系统影响；gzip -n 让结果可复现。
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
version=$(cat VERSION)
ref=${1:-HEAD}
out=${DIST_DIR:-dist}
name="wechat-export-send-$version"
mkdir -p "$out"
if [ -n "$(git status --porcelain 2>/dev/null)" ] && [ "${ALLOW_DIRTY:-0}" != 1 ]; then
    echo "工作树有未提交的改动，release 包只含已提交的内容。确认没问题时设 ALLOW_DIRTY=1。" >&2
    exit 1
fi
git -c core.fileMode=false archive --format=tar --prefix="$name/" "$ref" | gzip -n -9 > "$out/$name.tar.gz"
cd "$out"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$name.tar.gz" > SHA256SUMS
else
    shasum -a 256 "$name.tar.gz" > SHA256SUMS
fi
cat SHA256SUMS
