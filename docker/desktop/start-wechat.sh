#!/bin/sh
# 配了 WECHAT_BINARY_SHA256 时只启动这一个构建，防止客户端被悄悄换掉。
set -eu
umask 077
if [ -n "${WECHAT_BINARY_SHA256:-}" ]; then
    if ! printf '%s  %s\n' "$WECHAT_BINARY_SHA256" /opt/wechat/wechat | sha256sum -c - >/dev/null 2>&1; then
        echo "微信客户端的哈希和 instance.env 里的 WECHAT_BINARY_SHA256 不一致，拒绝启动。" >&2
        exit 78
    fi
fi
until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do sleep 1; done
export QT_QPA_PLATFORM=xcb
export LD_LIBRARY_PATH="/opt/wechat${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec /opt/wechat/wechat --no-sandbox
