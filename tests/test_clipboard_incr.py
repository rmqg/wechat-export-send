"""真 X11 的剪贴板传输：请求方按 ICCCM 读，每读一块删一次属性。没有 Xvfb 时跳过。"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import time

import pytest

from wechat_export_send.send import x11 as sender

DISPLAY = ":172"
pytestmark = pytest.mark.skipif(not shutil.which("Xvfb"), reason="需要 Xvfb")


@pytest.fixture(scope="module")
def display():
    server = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "800x600x24", "-nolisten", "tcp", "-ac"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    try:
        yield DISPLAY
    finally:
        server.terminate()
        server.wait(timeout=10)


class Requestor:
    def __init__(self, name):
        self.c = sender.X11(name)
        c = self.c
        self.window = c.x.XCreateSimpleWindow(c.display, c.root, 0, 0, 1, 1, 0, 0, 0)
        c.x.XSelectInput(c.display, self.window, c.PROPERTY_CHANGE_MASK)
        self.prop = c.atom("_TEST_CLIPBOARD")

    def _next(self, wanted, deadline):
        event = sender.XEvent()
        while time.monotonic() < deadline:
            if self.c.x.XPending(self.c.display):
                self.c.x.XNextEvent(self.c.display, ctypes.byref(event))
                if wanted(event):
                    return event
            else:
                time.sleep(0.001)
        raise TimeoutError("selection owner stopped answering")

    def _take(self):
        kind, fmt = ctypes.c_ulong(), ctypes.c_int()
        count, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.c.x.XGetWindowProperty(self.c.display, self.window, self.prop, 0, 1 << 26, 1, 0,
                                             ctypes.byref(kind), ctypes.byref(fmt), ctypes.byref(count),
                                             ctypes.byref(after), ctypes.byref(data))
        assert status == 0
        unit = {8: 1, 32: ctypes.sizeof(ctypes.c_ulong)}.get(fmt.value, 1)
        raw = ctypes.string_at(data, count.value * unit) if data else b""
        if data:
            self.c.x.XFree(data)
        self.c.x.XFlush(self.c.display)
        return kind.value, raw

    def read(self, mime, timeout=60.0):
        c = self.c
        target = c.atom(mime)
        deadline = time.monotonic() + timeout
        c.x.XConvertSelection(c.display, c.atom("CLIPBOARD"), target, self.prop, self.window, 0)
        c.x.XFlush(c.display)
        self._next(lambda e: e.type == c.SELECTION_NOTIFY, deadline)
        kind, raw = self._take()
        if kind != c.atom("INCR"):
            return raw, False, 1
        parts, chunks = [], 0
        while True:
            self._next(lambda e: (e.type == c.PROPERTY_NOTIFY and e.xproperty.atom == self.prop
                                  and e.xproperty.state != c.PROPERTY_DELETE), deadline)
            kind, raw = self._take()
            assert kind == target
            if not raw:
                return b"".join(parts), True, chunks
            parts.append(raw)
            chunks += 1


@pytest.mark.parametrize("size,expect_incr,expect_chunks", [
    (2_000_000, False, 1),
    (16_991_505, True, 17),  # 刚超过单次请求上限
    (40_000_000, True, None),
])
def test_png_arrives_byte_identical(display, size, expect_incr, expect_chunks):
    owner = sender.X11(display)
    requestor = Requestor(display)
    payload = os.urandom(size)
    owner.start_png_clipboard(payload)
    try:
        for _ in range(2):  # 工具包可能把同一份内容读两遍
            data, incr, chunks = requestor.read("image/png")
            assert hashlib.sha256(data).hexdigest() == hashlib.sha256(payload).hexdigest()
            assert incr is expect_incr
            if expect_chunks is not None:
                assert chunks == expect_chunks
        assert owner._image_requested.is_set()
        assert owner._selection_error == []
    finally:
        owner.stop_clipboard()
        owner.close()
        requestor.c.close()
