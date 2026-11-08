#!/usr/bin/env python3
"""可选：只监听 127.0.0.1 的只读 HTTP 服务，凭 token 提供导出目录里按内容寻址的媒体文件。"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

IMAGE_PATH = re.compile(r"/media/[0-9a-f]{2}/[0-9a-f]{64}\.(?:png|jpg|jpeg|gif|webp)\Z")
MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
CHUNK_SIZE = 64 * 1024


class ConfigError(ValueError):
    pass


class LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class ImageOriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ImageOrigin"
    sys_version = ""

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def do_POST(self):
        self._error(405, {"Allow": "GET, HEAD"})

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_OPTIONS = do_POST
    do_TRACE = do_POST
    do_CONNECT = do_POST

    def _error(self, status, headers=None):
        body = b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        try:
            peer = ipaddress.ip_address(self.client_address[0].split("%", 1)[0])
        except ValueError:
            return False
        if not peer.is_loopback:
            return False
        expected = ("Bearer " + self.server.origin_config["token"]).encode("utf-8")
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        return hmac.compare_digest(expected, supplied)

    def _serve(self, head):
        if not self._authorized():
            self._error(403)
            return
        path = urlsplit(self.path).path
        if not IMAGE_PATH.fullmatch(path):
            self._error(404)
            return
        _, prefix, name = path.lstrip("/").split("/")
        root_fd = sub_fd = file_fd = None
        headers_sent = False
        try:
            root_fd = os.open(self.server.origin_config["assets_dir"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            media_fd = os.open("media", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                sub_fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=media_fd)
            finally:
                os.close(media_fd)
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=sub_fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                self._error(404)
                return
            start, end, status = 0, info.st_size - 1, 200
            range_header = self.headers.get("Range")
            if range_header is not None:
                parsed = self._parse_range(range_header, info.st_size)
                if parsed is None:
                    self._error(416, {"Content-Range": f"bytes */{info.st_size}"})
                    return
                start, end = parsed
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", MIME_TYPES[Path(name).suffix.lower()])
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{info.st_size}")
            self.end_headers()
            headers_sent = True
            if not head:
                os.lseek(file_fd, start, os.SEEK_SET)
                remaining = length
                while remaining:
                    data = os.read(file_fd, min(CHUNK_SIZE, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (OSError, ValueError):
            if headers_sent:
                self.close_connection = True
            elif not self.wfile.closed:
                try:
                    self._error(404)
                except (OSError, ConnectionError):
                    pass
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if sub_fd is not None:
                os.close(sub_fd)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _parse_range(value, size):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
        if not match or size == 0 or (not match.group(1) and not match.group(2)):
            return None
        first, last = match.groups()
        if not first:
            suffix = int(last)
            if suffix <= 0:
                return None
            return max(0, size - suffix), size - 1
        start = int(first)
        end = int(last) if last else size - 1
        if start >= size or end < start:
            return None
        return start, min(end, size - 1)


def serve(config):
    server = LoopbackThreadingHTTPServer(("127.0.0.1", config["port"]), ImageOriginHandler)
    server.origin_config = config
    try:
        server.serve_forever()
    finally:
        server.server_close()


def config_for_instance(instance) -> dict:
    settings = instance.config["media_origin"]
    if not settings["enabled"]:
        raise ConfigError("media_origin.enabled 是 false")
    if not settings["token"]:
        raise ConfigError("media_origin.token 不能为空")
    assets = instance.export_root
    if not assets.is_dir():
        raise ConfigError("导出目录还不存在；先运行一次 export")
    return {"assets_dir": assets, "token": settings["token"], "port": int(settings["port"]), "bind": "127.0.0.1"}
