# 工具镜像：短命地跑捕获、发送 worker 和看门狗探针。构建上下文是仓库根目录。
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app DEBUGINFOD_URLS=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdb binutils procps libx11-6 libxtst6 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY VERSION LICENSE THIRD_PARTY_NOTICES.md /app/
COPY wechat_export_send /app/wechat_export_send
COPY vendor /app/vendor
COPY config /app/config
WORKDIR /app
ENTRYPOINT ["python3"]
CMD ["-m", "wechat_export_send.cli", "--version"]
