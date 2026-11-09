#!/bin/sh
# 安装向导。只碰这个目录：不改系统、不需要 root。
#   ./install.sh                检查环境 → 装依赖 → 在终端里一步步问（只跑 demo，还是接真实账号）
#   ./install.sh --config FILE  用答案文件回答向导，不提问（格式见 install.env.example）
#   ./install.sh --non-interactive  不提问：只装依赖（和旧版 ./install.sh 一样）
#   ./install.sh --no-deps      不装依赖，只检查（离线 demo 用纯 Python AES 也能跑）
#   ./install.sh --images       额外构建工具镜像和桌面镜像（需要 Docker）
#   ./install.sh --tools        只构建工具镜像（对接已有容器时不需要桌面镜像）
#   ./install.sh --link DIR     把两个命令软链到 DIR（例如 /usr/local/bin）
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
deps=1 images=0 tools=0 link="" ask=auto answers="" legacy=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-deps) deps=0; legacy=1 ;;
        --images) images=1; legacy=1 ;;
        --tools) tools=1; legacy=1 ;;
        --link) shift; link=${1:?--link 需要一个目录}; legacy=1 ;;
        --config|--answers) shift; answers=${1:?--config 需要一个答案文件}; ask=no ;;
        --non-interactive) ask=no ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done
command -v python3 >/dev/null 2>&1 || { echo "找不到 python3。Debian/Ubuntu：apt-get update && apt-get install -y python3 python3-venv（不是 root 就在前面加 sudo）" >&2; exit 1; }
python3 - <<'PY' || { echo "需要 Python 3.10 或更新版本" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
chmod +x bin/wechat-export-send bin/wechat-send 2>/dev/null || true
if [ -n "$answers" ]; then
    [ -f "$answers" ] || { echo "找不到答案文件 $answers" >&2; exit 1; }
    answers=$(cd "$(dirname "$answers")" && pwd)/$(basename "$answers")
fi
if [ "$ask" = auto ]; then
    if [ -t 0 ] && [ -t 1 ]; then ask=yes; else ask=no; fi
fi
if [ "$legacy" = 0 ]; then
    python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from wechat_export_send.setup_wizard import check_main; raise SystemExit(check_main())' "$ROOT" || exit 1
    echo "==> 安装 Python 依赖"
fi
if [ "$deps" = 1 ]; then
    if python3 -m venv .venv >/dev/null 2>&1 && [ -x .venv/bin/pip ]; then
        .venv/bin/pip install --disable-pip-version-check -q -r requirements.txt
        echo "依赖已装进 .venv"
    elif python3 -m pip --version >/dev/null 2>&1; then
        rm -rf .venv
        python3 -m pip install --disable-pip-version-check -q --target .deps -r requirements.txt
        echo "没有 venv 模块，依赖已装进 .deps"
    else
        rm -rf .venv
        echo "警告：既没有 venv 也没有 pip，没装依赖。离线 demo 能跑，真实数据库请先装好：" >&2
        echo "  Debian/Ubuntu：apt-get install -y python3-venv，然后重新运行 ./install.sh" >&2
    fi
fi
if [ "$tools" = 1 ] && [ "$images" = 0 ]; then
    docker build -t wechat-export-send-tools:local -f docker/tools/Dockerfile .
fi
if [ "$images" = 1 ]; then
    version=$(cat VERSION)
    docker build -t wechat-export-send-tools:local -f docker/tools/Dockerfile .
    docker build --platform linux/amd64 -t "wechat-export-send-desktop:$version" docker/desktop
fi
if [ -n "$link" ]; then
    ln -sf "$ROOT/bin/wechat-export-send" "$link/wechat-export-send"
    ln -sf "$ROOT/bin/wechat-send" "$link/wechat-send"
    echo "已链接到 $link"
fi
if [ "$legacy" = 0 ]; then
    if [ -n "$answers" ]; then
        exec ./bin/wechat-export-send setup --answers "$answers"
    elif [ "$ask" = yes ]; then
        exec ./bin/wechat-export-send setup
    fi
    echo "==> 不提问（没有终端，或者用了 --non-interactive），只装了依赖。要自动回答向导：./install.sh --config 答案文件（格式见 install.env.example）"
fi
echo "完成。试试：./bin/wechat-export-send demo  和  ./bin/wechat-export-send doctor"
