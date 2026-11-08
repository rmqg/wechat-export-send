# 桌面容器：Xvfb + fluxbox + x11vnc + noVNC。微信客户端不在镜像里，由你提供官方 deb，运行时只读挂载。
FROM debian:12-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       xvfb fluxbox x11vnc novnc websockify supervisor x11-utils procps \
       fonts-noto-cjk ca-certificates dbus \
       libatomic1 libnss3 libnspr4 libgbm1 libasound2 libdbus-1-3 libgl1 libegl1 libglib2.0-0 \
       libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
       libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxcb-util1 libxkbcommon0 libxkbcommon-x11-0 \
       libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxrender1 libxtst6 libxi6 libsm6 libice6 libfontconfig1 \
       libfreetype6 libpulse0 libcups2 libatk1.0-0 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*
COPY supervisord.conf start-wechat.sh entrypoint.sh /deployment/
RUN chmod 755 /deployment/start-wechat.sh /deployment/entrypoint.sh && mkdir -p /opt/wechat
# LANG 不要改成 zh_CN：客户端的窗口标题会从 Weixin 变成「微信」，界面校准和窗口识别都是按前者做的
ENV DISPLAY=:99 HOME=/config LANG=C.UTF-8 XDG_CONFIG_HOME=/config/.config XDG_CACHE_HOME=/config/.cache XDG_DATA_HOME=/config/.local/share
EXPOSE 7900
ENTRYPOINT ["/deployment/entrypoint.sh"]
