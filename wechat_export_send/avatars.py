"""下载头像：客户端本地没有缓存的成员，按联系人库里的头像链接取。

解密导出的容器不联网，所以导出只把要下载的链接写进 state/export/avatar-queue.json，下载在这里单独做。
这一步只碰导出目录，拿不到密钥和数据库。只访问微信头像域名，限速，失败退避，时间和单文件大小都有上限。
"""
from __future__ import annotations

import argparse
import fcntl
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import atomic_write

ALLOWED_HOSTS = {"wx.qlogo.cn", "thirdwx.qlogo.cn", "mmhead.hk.wechat.com", "mmhead.c2c.wechat.com"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_URL_BYTES = 4096
IMAGE_FORMATS = {"jpg", "png", "gif", "webp"}
QUEUE_FILE = "avatar-queue.json"
DONE_FILE = "avatar-downloads.json"
BACKOFF_FILE = "avatar-backoff.json"
SEEN_FILE = "avatar-seen.json"
LOCK_FILE = "avatar-download.lock"
BACKOFF_BASE_SECONDS = 3600
BACKOFF_MAX_SECONDS = 7 * 86400


def _public_addresses(host: str, port: int) -> list[str]:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses:
        raise OSError("avatar host has no address")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise OSError("avatar host resolved to a non-public address")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接前已经核对过的公网地址，防止 DNS 把请求指回内网。"""

    def __init__(self, host: str, addresses: list[str], **kwargs):
        super().__init__(host, **kwargs)
        self._addresses = addresses

    def connect(self):
        last_error = None
        for address in self._addresses:
            raw = None
            try:
                raw = socket.create_connection((address, self.port), self.timeout, self.source_address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as error:
                if raw is not None:
                    raw.close()
                self.sock = None
                last_error = error
        raise last_error or OSError("avatar connection failed")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        parsed = urllib.parse.urlsplit(request.full_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
            raise urllib.error.URLError("avatar host is not allowed")
        addresses = _public_addresses(host, parsed.port or 443)
        return self.do_open(lambda h, **kw: _PinnedHTTPSConnection(host, addresses, **kw), request, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def url_allowed(url: str) -> bool:
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_URL_BYTES:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS and port in (None, 443)
            and not parsed.username and not parsed.password and not parsed.fragment)


def fetch(url: str, timeout: float) -> bytes | None:
    if not url_allowed(url):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTPSHandler(), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url or response.status != 200:
                return None
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                return None
            data = response.read(MAX_IMAGE_BYTES + 1)
            return data if len(data) <= MAX_IMAGE_BYTES else None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, ssl.SSLError):
        return None


def _usable(data: bytes | None) -> tuple[bytes, str] | None:
    from . import media as media_mod

    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    fmt = media_mod.detect_image_format(data)
    if fmt not in IMAGE_FORMATS or not media_mod._complete_image(data, fmt):
        return None
    return data, fmt


def _first_usable(item: dict, fetcher, timeout: float) -> tuple[bytes, str] | None:
    for url in item.get("urls") or []:
        found = _usable(fetcher(url, timeout))
        if found:
            return found
    return None


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def done_entry(value) -> dict | None:
    """下载记录：{path, at}。旧版只记了路径，当成很早以前下载的。"""
    if isinstance(value, str):
        return {"path": value, "at": 0}
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return {"path": value["path"], "at": float(value.get("at") or 0)}
    return None


def _due(fingerprint: str, done: dict, backoff: dict, now: float) -> bool:
    if fingerprint in done:
        return False
    entry = backoff.get(fingerprint)
    return not isinstance(entry, dict) or float(entry.get("next") or 0) <= now


def has_due(state_root: Path | str, now: float | None = None) -> bool:
    """这次要不要启动下载。读不了状态文件时返回 True，交给下载步骤自己判断。"""
    root = Path(state_root)
    moment = time.time() if now is None else now
    try:
        queue = json.loads((root / QUEUE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    done, backoff = _load(root / DONE_FILE), _load(root / BACKOFF_FILE)
    return any(_due(fingerprint, done, backoff, moment) for fingerprint in queue)


def download_pending(export_root: Path | str, state_root: Path | str, *, limit: int = 20, delay: float = 0.5,
                     max_seconds: float = 60, timeout: float = 5, now: float | None = None,
                     fetcher=fetch, sleeper=time.sleep, clock=time.monotonic, force: set[str] | None = None) -> dict:
    """下载队列里到期的头像，换了头像的排最前面。失败的按小时翻倍退避，最长 7 天；成功的写进 media/，下一次导出时合并。

    force 里的成员不管下载过没有、在不在退避，都重新下载一次（日报生成前用）。失败时继续用旧头像，也不进退避。"""
    from . import media as media_mod

    export_root, state_root = Path(export_root), Path(state_root)
    stats = {"queued": 0, "downloaded": 0, "failed": 0, "waiting": 0, "busy": False,
             "forced": 0, "refreshed": 0, "refresh_failed": 0}
    lock_path = state_root / LOCK_FILE
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**stats, "busy": True}
        queue, done = _load(state_root / QUEUE_FILE), _load(state_root / DONE_FILE)
        backoff = _load(state_root / BACKOFF_FILE)
        moment = time.time() if now is None else now
        started, tried = clock(), 0
        stats["queued"] = len(queue)
        try:
            wanted = set(force or ())
            forced = sorted(fp for fp, item in queue.items() if wanted & set(item.get("targets") or []))
            for fingerprint in forced:
                if clock() - started >= max_seconds:
                    stats["refresh_failed"] += 1
                    continue
                if stats["forced"]:
                    sleeper(delay)
                stats["forced"] += 1
                found = _first_usable(queue[fingerprint], fetcher, timeout)
                if found is None:
                    stats["refresh_failed"] += 1
                    continue
                relative, _ = media_mod.store_content(export_root, found[0], found[1])
                done[fingerprint] = {"path": relative, "at": moment}
                backoff.pop(fingerprint, None)
                stats["refreshed"] += 1
            for fingerprint, item in sorted(queue.items(), key=lambda pair: (not pair[1].get("changed"), pair[0])):
                if fingerprint in done:
                    continue
                if not _due(fingerprint, done, backoff, moment):
                    stats["waiting"] += 1
                    continue
                if tried >= limit or clock() - started >= max_seconds:
                    stats["waiting"] += 1
                    continue
                if tried or stats["forced"]:
                    sleeper(delay)
                tried += 1
                found = _first_usable(item, fetcher, timeout)
                if found is None:
                    attempts = int((backoff.get(fingerprint) or {}).get("attempts") or 0) + 1
                    wait = min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)
                    backoff[fingerprint] = {"attempts": attempts, "next": moment + wait}
                    stats["failed"] += 1
                    continue
                relative, _ = media_mod.store_content(export_root, found[0], found[1])
                done[fingerprint] = {"path": relative, "at": moment}
                backoff.pop(fingerprint, None)
                stats["downloaded"] += 1
        finally:
            _save(state_root / DONE_FILE, done)
            _save(state_root / BACKOFF_FILE, {key: value for key, value in backoff.items() if key in queue})
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载队列里的头像，只输出计数")
    parser.add_argument("--export-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--force-file", help="每行一个导出 ID：这些成员重新下载一次")
    args = parser.parse_args(argv)
    force = set(Path(args.force_file).read_text(encoding="utf-8").split()) if args.force_file else None
    stats = download_pending(args.export_root, args.state_root, limit=args.limit, delay=args.delay,
                             max_seconds=args.max_seconds, timeout=args.timeout, force=force)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
