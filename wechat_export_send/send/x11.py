"""只用 libX11/libXtst（ctypes）操作微信窗口：定位窗口、注入按键和点击、读写剪贴板、截屏。

不操作任何微信内部接口。点击发送按钮时的 on_submit_state 回调标出「可能已经提交」的边界，
上层靠它严格区分 failed 和 unknown。
"""
from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path
import threading
import time


# Xlib ABI structures used by the ctypes-only worker.
class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
        ("height", ctypes.c_int), ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong), ("class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
    ]



class XImageData(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                ("blue_mask", ctypes.c_ulong)]


class XSelectionRequestEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("owner", ctypes.c_ulong),
                ("requestor", ctypes.c_ulong), ("selection", ctypes.c_ulong),
                ("target", ctypes.c_ulong), ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class XSelectionEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("requestor", ctypes.c_ulong),
                ("selection", ctypes.c_ulong), ("target", ctypes.c_ulong),
                ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5), ("pad", ctypes.c_long * 16)]


class XPropertyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("atom", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("state", ctypes.c_int)]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xselectionrequest", XSelectionRequestEvent),
                ("xselection", XSelectionEvent), ("xproperty", XPropertyEvent), ("pad", ctypes.c_long * 24)]


def _calibrated_window(config: dict) -> dict:
    window = config.get("window")
    fields = {"x", "y", "width", "height"}
    if (not isinstance(window, dict) or set(window) != fields or
            any(type(window[field]) is not int for field in fields) or
            window["width"] <= 0 or window["height"] <= 0):
        raise ValueError("UI calibration window must define integer reference x/y and positive template width/height")
    return window


class X11:
    ZPIXMAP = 2
    IS_VIEWABLE = 2
    CURRENT_TIME = 0
    PROP_REPLACE = 0
    SELECTION_REQUEST = 30
    SELECTION_NOTIFY = 31
    PROPERTY_NOTIFY = 28
    PROPERTY_DELETE = 1
    PROPERTY_CHANGE_MASK = 1 << 22
    INCR_CHUNK_BYTES = 1 << 20
    XA_ATOM = 4

    def __init__(self, display_name: str):
        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise RuntimeError("runtime must provide libX11 and libXtst")
        self.x = ctypes.CDLL(x11_path)
        self.xtst = ctypes.CDLL(xtst_path)
        self._bind()
        self.x.XInitThreads()
        self.display = self.x.XOpenDisplay(display_name.encode())
        if not self.display:
            raise RuntimeError(f"cannot connect to X11 DISPLAY={display_name}")


        self.root = self.x.XDefaultRootWindow(self.display)
        self._delivery_config: dict | None = None
        self._delivery_window: dict | None = None
        self._selection_thread: threading.Thread | None = None
        self._selection_stop = threading.Event()
        self._selection_error: list[str] = []
        self._image_requested = threading.Event()
        self._png_buffer = None
        self._owner_window = 0

    def _bind(self) -> None:
        x = self.x
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]; x.XOpenDisplay.restype = ctypes.c_void_p
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]; x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                 ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                                 ctypes.POINTER(ctypes.c_uint)]; x.XQueryTree.restype = ctypes.c_int
        x.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]; x.XFetchName.restype = ctypes.c_int
        x.XFree.argtypes = [ctypes.c_void_p]; x.XFree.restype = ctypes.c_int
        x.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes)]; x.XGetWindowAttributes.restype = ctypes.c_int
        x.XTranslateCoordinates.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                            ctypes.POINTER(ctypes.c_ulong)]; x.XTranslateCoordinates.restype = ctypes.c_int
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]; x.XInternAtom.restype = ctypes.c_ulong
        x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                                         ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        x.XGetWindowProperty.restype = ctypes.c_int
        x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; x.XRaiseWindow.restype = ctypes.c_int
        x.XResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_uint]; x.XResizeWindow.restype = ctypes.c_int
        x.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]; x.XSelectInput.restype = ctypes.c_int
        x.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        x.XGrabServer.argtypes = [ctypes.c_void_p]; x.XGrabServer.restype = ctypes.c_int
        x.XUngrabServer.argtypes = [ctypes.c_void_p]; x.XUngrabServer.restype = ctypes.c_int
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]; x.XCloseDisplay.restype = ctypes.c_int
        x.XExtendedMaxRequestSize.argtypes = [ctypes.c_void_p]; x.XExtendedMaxRequestSize.restype = ctypes.c_long
        x.XMaxRequestSize.argtypes = [ctypes.c_void_p]; x.XMaxRequestSize.restype = ctypes.c_long
        x.XPending.argtypes = [ctypes.c_void_p]; x.XPending.restype = ctypes.c_int
        x.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        x.XCreateSimpleWindow.restype = ctypes.c_ulong
        x.XSetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        x.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; x.XGetSelectionOwner.restype = ctypes.c_ulong
        x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
        x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(XEvent)]
        x.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        x.XConvertSelection.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        x.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
        x.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        x.XFlush.argtypes = [ctypes.c_void_p]; x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]; x.XGetImage.restype = ctypes.c_void_p
        x.XDestroyImage.argtypes = [ctypes.c_void_p]
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; x.XKeysymToKeycode.restype = ctypes.c_uint
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]; x.XStringToKeysym.restype = ctypes.c_ulong
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]

    def atom(self, name: str) -> int:
        return int(self.x.XInternAtom(self.display, name.encode(), 0))

    def _window_title(self, window: int) -> str:
        atom = self.atom("_NET_WM_NAME")
        actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
        item_count, bytes_after = ctypes.c_ulong(), ctypes.c_ulong()
        property_data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(self.display, window, atom, 0, 1024, 0, 0,
                                           ctypes.byref(actual_type), ctypes.byref(actual_format),
                                           ctypes.byref(item_count), ctypes.byref(bytes_after),
                                           ctypes.byref(property_data))
        if status == 0 and property_data and actual_format.value == 8 and item_count.value:
            try:
                return ctypes.string_at(property_data, item_count.value).decode("utf-8", "replace")
            finally:
                self.x.XFree(property_data)
        if property_data:
            self.x.XFree(property_data)
        result = ctypes.c_void_p()
        if not self.x.XFetchName(self.display, window, ctypes.byref(result)) or not result.value:
            return ""
        try:
            return ctypes.string_at(result.value).decode("utf-8", "replace")
        finally:
            self.x.XFree(result)

    def _children(self, parent: int) -> list[int]:
        root_ret, parent_ret = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        if not self.x.XQueryTree(self.display, parent, ctypes.byref(root_ret), ctypes.byref(parent_ret),
                                 ctypes.byref(children), ctypes.byref(count)):
            raise RuntimeError(f"XQueryTree failed for window {parent:#x}")
        try:
            return [int(children[i]) for i in range(count.value)]
        finally:
            if children:
                self.x.XFree(children)

    def _client_windows(self) -> list[int]:
        # The window manager lists application windows, not short-lived clipboard owners.
        actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
        count, remaining = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(
            self.display, self.root, self.atom('_NET_CLIENT_LIST'), 0, 4096, 0, 33,
            ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(count),
            ctypes.byref(remaining), ctypes.byref(data))
        try:
            if (status != 0 or actual_type.value != 33 or actual_format.value != 32 or remaining.value or
                    (count.value and not data)):
                raise RuntimeError('window manager did not provide its application window list')
            if not count.value:
                return []
            return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:count.value])
        finally:
            if data:
                self.x.XFree(data)

    def _target_windows(self, config: dict) -> tuple[list[dict], list[tuple[int, str]]]:
        pattern = str(config.get("window_title_substring", "Weixin")).casefold()
        matches = []
        visible_titles = []
        for candidate in self._client_windows():
            attrs = XWindowAttributes()
            if not self.x.XGetWindowAttributes(self.display, candidate, ctypes.byref(attrs)):
                continue
            title = self._window_title(candidate)
            if attrs.map_state == self.IS_VIEWABLE:
                visible_titles.append((candidate, title))
            if pattern not in title.casefold():
                continue
            rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            if not self.x.XTranslateCoordinates(self.display, candidate, self.root, 0, 0,
                                                ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child)):
                raise RuntimeError("cannot read root position of Weixin client window")
            matches.append({"window_id": int(candidate), "title": title, "x": rx.value, "y": ry.value,
                            "width": attrs.width, "height": attrs.height, "visual": attrs.visual,
                            "map_state": attrs.map_state})
        return matches, visible_titles
    def _override_redirect_windows(self, main: dict) -> list[tuple[int, str]]:
        # Reparenting window managers wrap the client in their own override-redirect
        # frame. That ancestor is not an independent dialog or an occluding surface.
        ancestors = set()
        for client in self._client_windows():
            attrs = XWindowAttributes()
            if (not self.x.XGetWindowAttributes(self.display, client, ctypes.byref(attrs)) or
                    attrs.map_state != self.IS_VIEWABLE):
                continue
            current = int(client)
            chain = {current}
            while current != self.root:
                root, parent, count = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_uint()
                children = ctypes.POINTER(ctypes.c_ulong)()
                try:
                    if not self.x.XQueryTree(self.display, current, ctypes.byref(root), ctypes.byref(parent),
                                             ctypes.byref(children), ctypes.byref(count)):
                        raise RuntimeError("cannot verify managed window ancestry")
                finally:
                    if children:
                        self.x.XFree(children)
                current = int(parent.value)
                if not current or current in chain:
                    raise RuntimeError("invalid managed window ancestry")
                chain.add(current)
            ancestors.update(chain)
        candidates = list(self._children(self.root))
        pending = list(self._children(main["window_id"]))
        seen = set(candidates)
        while pending:
            candidate = pending.pop()
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
            pending.extend(self._children(candidate))
        visible = []
        for candidate in candidates:
            if candidate in ancestors:
                continue
            attrs = XWindowAttributes()
            if (not self.x.XGetWindowAttributes(self.display, candidate, ctypes.byref(attrs)) or
                    not attrs.override_redirect or attrs.map_state != self.IS_VIEWABLE):
                continue
            rx, ry, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            if not self.x.XTranslateCoordinates(self.display, candidate, self.root, 0, 0,
                                                ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child)):
                raise RuntimeError("cannot verify possible Weixin overlay position")
            # A desktop toolbar wholly outside this client cannot intercept its
            # relative clicks. Unnamed overlays inside the client still block input.
            if (rx.value < main["x"] + main["width"] and rx.value + attrs.width > main["x"] and
                    ry.value < main["y"] + main["height"] and ry.value + attrs.height > main["y"]):
                visible.append((candidate, self._window_title(candidate)))
        return visible



    def _select_window(self, config: dict) -> dict:
        matches, visible_titles = self._target_windows(config)
        if not matches:
            raise RuntimeError("Weixin client window not found")
        if len(matches) != 1:
            raise RuntimeError(f"ambiguous Weixin client windows: {len(matches)} candidates")
        main = matches[0]
        if main["map_state"] != self.IS_VIEWABLE:
            raise RuntimeError("Weixin client window is hidden or unmapped")
        allowed = set(config.get("allowed_visible_window_titles", []))
        visible_titles.extend(self._override_redirect_windows(main))
        unexpected = [(wid, title) for wid, title in visible_titles
                      if wid != main["window_id"] and title not in allowed]
        if unexpected:
            # 标题可能带会话名，只报数量
            raise RuntimeError(f"unexpected visible window/dialog: {len(unexpected)} window(s)")
        return main

    def _screen_size(self) -> tuple[int, int]:
        attrs = XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.display, self.root, ctypes.byref(attrs)):
            raise RuntimeError("cannot read X11 root-screen dimensions")
        return attrs.width, attrs.height

    def _inside_screen(self, window: dict) -> bool:
        screen_width, screen_height = self._screen_size()
        return (window["x"] >= 0 and window["y"] >= 0 and
                window["x"] + window["width"] <= screen_width and
                window["y"] + window["height"] <= screen_height)

    def _restore_template_size(self, config: dict, main: dict, expected_size: tuple[int, int]) -> dict:
        # 微信主窗口可调大小，客户端有时会自己偏离模板几个像素，发送因此卡住。这里只把尺寸调回模板，
        # 之后的会话标题比对、空草稿等校验照旧把关；登录窗口不动。
        actual_size = (main["width"], main["height"])
        login = config.get("login_window")
        if isinstance(login, dict) and actual_size == (login.get("width"), login.get("height")):
            raise RuntimeError(f"Weixin client window has wrong size {actual_size}; expected template size {expected_size}")
        self.x.XResizeWindow(self.display, main["window_id"], expected_size[0], expected_size[1])
        self.x.XSync(self.display, 0)
        deadline = time.monotonic() + float(config.get("window_restore_timeout_seconds", 5))
        while True:
            time.sleep(0.2)
            main = self._select_window(config)
            actual_size = (main["width"], main["height"])
            if actual_size == expected_size:
                time.sleep(float(config.get("window_restore_settle_seconds", 1)))
                return self._select_window(config)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Weixin client window has wrong size {actual_size}; expected template size {expected_size}; resize was not applied")

    def find_window(self, config: dict) -> dict:
        expected = _calibrated_window(config)
        main = self._select_window(config)
        expected_size = (expected["width"], expected["height"])
        if (main["width"], main["height"]) != expected_size:
            main = self._restore_template_size(config, main, expected_size)
        actual_size = (main["width"], main["height"])
        if actual_size != expected_size:
            raise RuntimeError(f"Weixin client window has wrong size {actual_size}; expected template size {expected_size}")
        if not self._inside_screen(main):
            raise RuntimeError("Weixin client window is cropped/offscreen")
        self._delivery_config = {
            "window": dict(expected),
            "window_title_substring": str(config.get("window_title_substring", "Weixin")),
            "allowed_visible_window_titles": list(config.get("allowed_visible_window_titles", [])),
        }
        self._delivery_window = dict(main)
        return main

    def _assert_window_stable(self, window: dict) -> dict:
        baseline = self._delivery_window
        config = self._delivery_config
        if baseline is None or config is None:
            raise RuntimeError("Weixin client window has not been calibrated for this delivery")
        if int(window.get("window_id", -1)) != baseline["window_id"]:
            raise RuntimeError("Weixin target instance changed during delivery")
        current = self._select_window(config)
        if current["window_id"] != baseline["window_id"]:
            raise RuntimeError("Weixin target instance changed during delivery")
        if (current["width"], current["height"]) != (baseline["width"], baseline["height"]):
            raise RuntimeError(f"Weixin window size changed during delivery: {(current['width'], current['height'])!r}")
        if not self._inside_screen(current):
            raise RuntimeError("Weixin window moved outside the X11 screen during delivery")
        if (current["x"], current["y"]) != (baseline["x"], baseline["y"]):
            raise RuntimeError(f"Weixin window moved during delivery: origin {(baseline['x'], baseline['y'])!r} -> {(current['x'], current['y'])!r}")
        return current

    def activate(self, win: dict) -> None:
        current = self._assert_window_stable(win)
        self.x.XRaiseWindow(self.display, current["window_id"])
        self.x.XSetInputFocus(self.display, current["window_id"], 2, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        time.sleep(0.15)
        if not self.focus_within(win):
            raise RuntimeError("Weixin window did not retain input focus")

    def focus_within(self, window: dict) -> bool:
        current = self._assert_window_stable(window)
        focused, revert = ctypes.c_ulong(), ctypes.c_int()
        self.x.XGetInputFocus(self.display, ctypes.byref(focused), ctypes.byref(revert))
        allowed = {int(current["window_id"])}
        stack = [int(current["window_id"])]
        while stack:
            parent = stack.pop()
            children = self._children(parent)
            allowed.update(children)
            stack.extend(children)
        self._assert_window_stable(window)
        return int(focused.value) in allowed

    @staticmethod
    def _mask_component(pixel: int, mask: int) -> int:
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        return ((pixel & mask) >> shift) * 255 // maximum

    def screenshot(self, win: dict):
        from PIL import Image
        current = self._assert_window_stable(win)
        attrs = XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.display, current["window_id"], ctypes.byref(attrs)):
            raise RuntimeError("cannot read calibrated Weixin window attributes")
        current = self._assert_window_stable(win)
        pointer = self.x.XGetImage(self.display, self.root, current["x"], current["y"], attrs.width, attrs.height,
                                   ctypes.c_ulong(-1).value, self.ZPIXMAP)
        if not pointer:
            raise RuntimeError("XGetImage failed for the full Weixin window")
        data = ctypes.cast(pointer, ctypes.POINTER(XImageData)).contents
        try:
            stride = int(data.bytes_per_line)
            bits = int(data.bits_per_pixel)
            if stride <= 0 or bits not in (16, 24, 32):
                raise RuntimeError(f"unsupported XImage layout: stride={stride}, bits_per_pixel={bits}")
            raw = ctypes.string_at(data.data, stride * attrs.height)
            masks = (int(data.red_mask), int(data.green_mask), int(data.blue_mask))
            order = int(data.byte_order)
            mode = None
            if bits == 32 and masks == (0x00FF0000, 0x0000FF00, 0x000000FF):
                mode = "BGRX" if order == 0 else "XRGB"
            elif bits == 32 and masks == (0x000000FF, 0x0000FF00, 0x00FF0000):
                mode = "RGBX" if order == 0 else "XBGR"
            elif bits == 24 and masks == (0x00FF0000, 0x0000FF00, 0x000000FF):
                mode = "BGR" if order == 0 else "RGB"
            elif bits == 24 and masks == (0x000000FF, 0x0000FF00, 0x00FF0000):
                mode = "RGB" if order == 0 else "BGR"
            if mode:
                image = Image.frombuffer("RGB", (attrs.width, attrs.height), raw,
                                         "raw", mode, stride, 1).copy()
            else:
                bytes_per_pixel = (bits + 7) // 8
                pixels = bytearray(attrs.width * attrs.height * 3)
                offset = 0
                endian = "little" if order == 0 else "big"
                for y in range(attrs.height):
                    row_offset = y * stride
                    for x in range(attrs.width):
                        start = row_offset + x * bytes_per_pixel
                        value = int.from_bytes(raw[start:start + bytes_per_pixel], endian)
                        pixels[offset] = self._mask_component(value, masks[0])
                        pixels[offset + 1] = self._mask_component(value, masks[1])
                        pixels[offset + 2] = self._mask_component(value, masks[2])
                        offset += 3
                image = Image.frombytes("RGB", (attrs.width, attrs.height), bytes(pixels))
            self._assert_window_stable(win)
            return image
        finally:
            self.x.XDestroyImage(pointer)

    def key(self, name: str, down: bool) -> None:
        if down:
            window = self._delivery_window or {}
            self._assert_window_stable(window)
            if not self.focus_within(window):
                raise RuntimeError("Weixin client window lost input focus before keyboard input")
        sym = self.x.XStringToKeysym(name.encode("ascii"))
        if not sym:
            raise RuntimeError(f"unknown X11 keysym {name}")
        code = self.x.XKeysymToKeycode(self.display, sym)
        if not code or not self.xtst.XTestFakeKeyEvent(self.display, code, int(down), self.CURRENT_TIME):
            raise RuntimeError(f"XTest could not inject key {name}")

    def press(self, name: str) -> None:
        self.key(name, True)
        try:
            self.x.XFlush(self.display)
            time.sleep(0.035)
        finally:
            self.key(name, False)
            self.x.XFlush(self.display)

    def chord(self, modifier: str, key: str) -> None:
        self.key(modifier, True)
        key_down = False
        try:
            self.key(key, True)
            key_down = True
            self.x.XFlush(self.display)
            time.sleep(0.04)
        finally:
            try:
                if key_down:
                    self.key(key, False)
            finally:
                self.key(modifier, False)
                self.x.XFlush(self.display)

    def type_ascii(self, text: str) -> None:
        for char in text:
            if not 32 <= ord(char) <= 126:
                raise ValueError("only printable ASCII text is permitted in the native search field")
            # Printable Latin-1 keysyms equal their character code. XStringToKeysym
            # expects names such as 'minus', not the literal '-' character.
            code = self.x.XKeysymToKeycode(self.display, ord(char))
            if not code:
                raise RuntimeError(f"unsupported ASCII key {char!r}")
            shifted = char.isupper() or char in '~!@#$%^&*()_+{}|:"<>?'
            if shifted:
                self.key("Shift_L", True)
            key_down = False
            try:
                window = self._delivery_window or {}
                if not self.focus_within(window):
                    raise RuntimeError("Weixin client window lost input focus before typing")
                if not self.xtst.XTestFakeKeyEvent(self.display, code, 1, self.CURRENT_TIME):
                    raise RuntimeError(f"XTest could not inject ASCII key {char!r}")
                key_down = True
                self.x.XFlush(self.display)
            finally:
                try:
                    if key_down and not self.xtst.XTestFakeKeyEvent(self.display, code, 0, self.CURRENT_TIME):
                        raise RuntimeError(f"XTest could not release ASCII key {char!r}")
                finally:
                    if shifted:
                        self.key("Shift_L", False)
            self.x.XFlush(self.display)
            time.sleep(0.018)

    def click(self, window: dict, point: list[int], on_submit_state=None) -> None:
        """Inject a validated click; the optional bool callback marks the uncertain press boundary.

        False is reported only when libXtst explicitly rejects the button-down request.
        Validation failures occur before the callback; errors after True are uncertain.
        """
        if len(point) != 2:
            raise ValueError("calibration coordinates must be [x,y]")
        x, y = map(int, point)
        current = self._assert_window_stable(window)
        if not (0 <= x < current["width"] and 0 <= y < current["height"]):
            raise ValueError("calibration click is outside Weixin window")
        self.x.XGrabServer(self.display)
        button_down = False
        release_ok = True
        try:
            current = self._assert_window_stable(window)
            if not self.xtst.XTestFakeMotionEvent(self.display, -1, current["x"] + x, current["y"] + y, self.CURRENT_TIME):
                raise RuntimeError("XTest mouse motion failed")
            self.x.XSync(self.display, 0)
            self._assert_window_stable(window)
            button_down = True
            if on_submit_state is not None:
                on_submit_state(True)
            if not self.xtst.XTestFakeButtonEvent(self.display, 1, 1, self.CURRENT_TIME):
                button_down = False
                if on_submit_state is not None:
                    on_submit_state(False)
                raise RuntimeError("XTest refused mouse press; no button event was accepted")
            if not self.xtst.XTestFakeButtonEvent(self.display, 1, 0, self.CURRENT_TIME):
                raise RuntimeError("XTest mouse release failed after button-down")
            button_down = False
            self.x.XSync(self.display, 0)
        finally:
            try:
                if button_down:
                    release_ok = bool(self.xtst.XTestFakeButtonEvent(self.display, 1, 0, self.CURRENT_TIME))
            finally:
                try:
                    self.x.XUngrabServer(self.display)
                finally:
                    self.x.XSync(self.display, 0)
            if not release_ok:
                raise RuntimeError("XTest could not release mouse button after button-down")
    def dismiss_popups(self, config: dict) -> int:
        """关掉搜索下拉框之类的小窗口。不能用 Esc：微信主窗口收到 Esc 会被收起来，只能靠重新启动唤醒。"""
        closed = 0
        for candidate in self._client_windows():
            attrs = XWindowAttributes()
            if not self.x.XGetWindowAttributes(self.display, candidate, ctypes.byref(attrs)):
                continue
            if self._window_title(candidate) == "wechat" and attrs.width < 400:
                event = XClientMessageEvent(type=33, window=candidate, message_type=self.atom("_NET_CLOSE_WINDOW"), format=32)
                event.data[1] = 2
                self.x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
                self.x.XSendEvent(self.display, self.root, 0, (1 << 20) | (1 << 19), ctypes.byref(event))
                closed += 1
        if closed:
            self.x.XSync(self.display, 0)
        return closed

    def close(self) -> None:
        if self.display:
            self.x.XCloseDisplay(self.display)
            self.display = None

    def composer_empty(self, window: dict, point: list[int]) -> bool:
        self.activate(window)
        # Copy is non-destructive. An empty Qt editor leaves clipboard ownership
        # unchanged; a real text/image draft transfers ownership to Qt.
        probe = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not probe:
            raise RuntimeError("cannot create private draft probe")
        clipboard = self.atom("CLIPBOARD")
        self.x.XSetSelectionOwner(self.display, clipboard, probe, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        self.click(window, point)
        self.chord("Control_L", "a")
        self.chord("Control_L", "c")
        time.sleep(0.2)
        self.x.XSync(self.display, 0)
        empty = int(self.x.XGetSelectionOwner(self.display, clipboard)) == int(probe)
        self.x.XDestroyWindow(self.display, probe)
        self.x.XFlush(self.display)
        return empty

    def start_png_clipboard(self, png: bytes) -> None:
        extended_words = int(self.x.XExtendedMaxRequestSize(self.display))
        if extended_words <= 0:
            extended_words = int(self.x.XMaxRequestSize(self.display))
        # 超过单次 X 请求上限（约 16.8MB）时走 ICCCM INCR 分块，字节和哈希不变；不到上限的一次写入
        use_incr = len(png) + 128 > extended_words * 4
        root = self.root
        owner = self.x.XCreateSimpleWindow(self.display, root, -10, -10, 1, 1, 0, 0, 0)
        if not owner:
            raise RuntimeError("cannot create X11 clipboard owner")
        self._owner_window = int(owner)
        clip = self.atom("CLIPBOARD")
        image_atom = self.atom("image/png")
        targets = self.atom("TARGETS")
        incr = self.atom("INCR")
        self._png_buffer = (ctypes.c_ubyte * len(png)).from_buffer_copy(png)
        self._selection_stop.clear(); self._image_requested.clear(); self._selection_error.clear()
        self.x.XSetSelectionOwner(self.display, clip, owner, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        if int(self.x.XGetSelectionOwner(self.display, clip)) != owner:
            raise RuntimeError("could not own X11 CLIPBOARD selection")

        def send_next_chunk(transfers: dict, window: int, prop: int) -> None:
            offset = transfers[(window, prop)]
            size = min(self.INCR_CHUNK_BYTES, len(png) - offset)
            chunk = ctypes.cast(ctypes.addressof(self._png_buffer) + offset, ctypes.POINTER(ctypes.c_ubyte))
            self.x.XChangeProperty(self.display, window, prop, image_atom, 8, self.PROP_REPLACE, chunk, size)
            if size:
                transfers[(window, prop)] = offset + size
            else:
                # A zero-length chunk ends the transfer.
                del transfers[(window, prop)]
                if not any(key[0] == window for key in transfers):
                    self.x.XSelectInput(self.display, window, 0)
            self.x.XFlush(self.display)

        def serve() -> None:
            transfers: dict[tuple[int, int], int] = {}
            try:
                while not self._selection_stop.is_set():
                    if self.x.XPending(self.display) == 0:
                        time.sleep(0.002 if transfers else 0.025)
                        continue
                    event = XEvent()
                    self.x.XNextEvent(self.display, ctypes.byref(event))
                    if event.type == self.PROPERTY_NOTIFY:
                        changed = event.xproperty
                        if changed.state == self.PROPERTY_DELETE and (int(changed.window), int(changed.atom)) in transfers:
                            send_next_chunk(transfers, int(changed.window), int(changed.atom))
                        continue
                    if event.type != self.SELECTION_REQUEST:
                        continue
                    req = event.xselectionrequest
                    prop = int(req.property or req.target)
                    response = XSelectionEvent(self.SELECTION_NOTIFY, req.serial, 1, req.display,
                                               req.requestor, req.selection, req.target, prop, req.time)
                    if req.target == targets:
                        atom_values = (ctypes.c_ulong * 2)(targets, image_atom)
                        self.x.XChangeProperty(self.display, req.requestor, prop, self.XA_ATOM, 32,
                                               self.PROP_REPLACE, ctypes.cast(atom_values, ctypes.POINTER(ctypes.c_ubyte)), 2)
                    elif req.target == image_atom:
                        if use_incr:
                            # The requestor deleting this property asks for the first chunk.
                            self.x.XSelectInput(self.display, req.requestor, self.PROPERTY_CHANGE_MASK)
                            total = (ctypes.c_ulong * 1)(len(png))
                            self.x.XChangeProperty(self.display, req.requestor, prop, incr, 32, self.PROP_REPLACE,
                                                   ctypes.cast(total, ctypes.POINTER(ctypes.c_ubyte)), 1)
                            transfers[(int(req.requestor), prop)] = 0
                        else:
                            self.x.XChangeProperty(self.display, req.requestor, prop, image_atom, 8,
                                                   self.PROP_REPLACE, self._png_buffer, len(png))
                        self._image_requested.set()
                    else:
                        response.property = 0
                    self.x.XSendEvent(self.display, req.requestor, 0, 0,
                                      ctypes.byref(XEvent(xselection=response)))
                    self.x.XFlush(self.display)
            except Exception as exc:
                self._selection_error.append(f"{type(exc).__name__}: {exc}")
        self._selection_thread = threading.Thread(target=serve, name="x11-png-selection", daemon=True)
        self._selection_thread.start()

    def start_text_clipboard(self, text: str) -> None:
        raw = text.encode("utf-8", "strict")
        if not raw or b"\x00" in raw:
            raise ValueError("text clipboard content must be nonempty UTF-8 without NUL")
        owner = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not owner:
            raise RuntimeError("cannot create X11 clipboard owner")
        self._owner_window = int(owner)
        clip, utf8, targets = self.atom("CLIPBOARD"), self.atom("UTF8_STRING"), self.atom("TARGETS")
        self._text_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        self._selection_stop.clear(); self._selection_error.clear()
        self.x.XSetSelectionOwner(self.display, clip, owner, self.CURRENT_TIME)
        self.x.XSync(self.display, 0)
        if int(self.x.XGetSelectionOwner(self.display, clip)) != owner:
            raise RuntimeError("could not own X11 CLIPBOARD selection")

        def serve() -> None:
            try:
                while not self._selection_stop.is_set():
                    if self.x.XPending(self.display) == 0:
                        time.sleep(0.025); continue
                    event = XEvent(); self.x.XNextEvent(self.display, ctypes.byref(event))
                    if event.type != self.SELECTION_REQUEST:
                        continue
                    req = event.xselectionrequest
                    prop = int(req.property or req.target)
                    response = XSelectionEvent(self.SELECTION_NOTIFY, req.serial, 1, req.display,
                                               req.requestor, req.selection, req.target, prop, req.time)
                    if req.target == targets:
                        atoms = (ctypes.c_ulong * 2)(targets, utf8)
                        self.x.XChangeProperty(self.display, req.requestor, prop, self.XA_ATOM, 32,
                                               self.PROP_REPLACE, ctypes.cast(atoms, ctypes.POINTER(ctypes.c_ubyte)), 2)
                    elif req.target in (utf8, self.atom("TEXT"), self.atom("STRING")):
                        self.x.XChangeProperty(self.display, req.requestor, prop, utf8, 8,
                                               self.PROP_REPLACE, self._text_buffer, len(raw))
                    else:
                        response.property = 0
                    self.x.XSendEvent(self.display, req.requestor, 0, 0, ctypes.byref(XEvent(xselection=response)))
                    self.x.XFlush(self.display)
            except Exception:
                self._selection_error.append("text clipboard selection owner failed")
        self._selection_thread = threading.Thread(target=serve, name="x11-text-selection", daemon=True)
        self._selection_thread.start()

    def read_clipboard_bytes(self, mime: str, timeout: float = 3.0) -> bytes:
        clipboard, target = self.atom('CLIPBOARD'), self.atom(mime)
        prop = self.atom('_WES_TEXT_READBACK')
        window = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not window:
            raise RuntimeError('cannot create private clipboard requestor')
        data = ctypes.POINTER(ctypes.c_ubyte)()
        try:
            self.x.XConvertSelection(self.display, clipboard, target, prop, window, self.CURRENT_TIME)
            self.x.XFlush(self.display)
            deadline = time.monotonic() + timeout
            notified = False
            while time.monotonic() < deadline:
                if not self.x.XPending(self.display):
                    time.sleep(0.01)
                    continue
                event = XEvent()
                self.x.XNextEvent(self.display, ctypes.byref(event))
                if event.type == self.SELECTION_NOTIFY and event.xselection.requestor == window:
                    notified = event.xselection.property == prop
                    break
            if not notified:
                raise RuntimeError('editor text could not be read back from clipboard')
            actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
            count, after = ctypes.c_ulong(), ctypes.c_ulong()
            status = self.x.XGetWindowProperty(self.display, window, prop, 0, 16384, 1, 0,
                                               ctypes.byref(actual_type), ctypes.byref(actual_format),
                                               ctypes.byref(count), ctypes.byref(after), ctypes.byref(data))
            if status != 0 or actual_format.value != 8 or after.value or count.value > 65536:
                raise RuntimeError('editor returned unsupported or incomplete clipboard data')
            return ctypes.string_at(data, count.value)
        finally:
            if data:
                self.x.XFree(data)
            self.x.XDestroyWindow(self.display, window)
            self.x.XFlush(self.display)

    def copy_composer(self, window: dict, point: list[int], targets: tuple[str, ...]) -> dict[str, bytes]:
        self.activate(window)
        self.click(window, point)
        probe = self.x.XCreateSimpleWindow(self.display, self.root, -10, -10, 1, 1, 0, 0, 0)
        if not probe:
            raise RuntimeError('cannot create fresh clipboard probe')
        clipboard = self.atom('CLIPBOARD')
        try:
            self.x.XSetSelectionOwner(self.display, clipboard, probe, self.CURRENT_TIME)
            self.x.XSync(self.display, 0)
            self.chord('Control_L', 'a')
            self.chord('Control_L', 'c')
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                owner = int(self.x.XGetSelectionOwner(self.display, clipboard))
                if owner and owner != probe:
                    return {target: self.read_clipboard_bytes(target) for target in targets}
                time.sleep(0.01)
            return {}
        finally:
            self.x.XDestroyWindow(self.display, probe)
            self.x.XFlush(self.display)
            if self.focus_within(window):
                self.press('Right')

    def read_clipboard_text(self, timeout: float = 3.0) -> str:
        return self.read_clipboard_bytes('UTF8_STRING', timeout).decode('utf-8', 'strict')

    def verify_text_draft(self, window: dict, point: list[int], expected: str) -> bool:
        copied = self.copy_composer(window, point, ('UTF8_STRING',))
        return copied.get('UTF8_STRING', b'').decode('utf-8', 'strict') == expected

    def stop_clipboard(self) -> None:
        if not self._owner_window:
            return
        self._selection_stop.set()
        clip = self.atom("CLIPBOARD")
        if int(self.x.XGetSelectionOwner(self.display, clip)) == self._owner_window:
            self.x.XSetSelectionOwner(self.display, clip, 0, self.CURRENT_TIME)
        self.x.XFlush(self.display)
        if self._selection_thread:
            self._selection_thread.join(timeout=2)
            self._selection_thread = None
        self._owner_window = 0

def image_match(screen, rect: list[int], template_path: Path, threshold: int, max_bad_fraction: float) -> tuple[bool, float]:
    from PIL import Image, ImageChops
    if len(rect) != 4:
        raise ValueError("template region must be [x,y,width,height]")
    x, y, width, height = map(int, rect)
    if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > screen.width or y + height > screen.height:
        raise ValueError(f"calibration region outside window: {rect}")
    with Image.open(template_path) as source:
        template = source.convert("RGB")
    if template.size != (width, height):
        raise ValueError(f"template {template_path} dimensions {template.size} != {(width, height)}")
    crop = screen.crop((x, y, x + width, y + height))
    diff = ImageChops.difference(crop, template).convert("L")
    bad = sum(1 for value in diff.getdata() if value > threshold) / (width * height)
    return bad <= max_bad_fraction, bad


def send_button_ready(screen, rect: list[int]) -> bool:
    x, y, width, height = map(int, rect)
    region = screen.crop((x, y, x + width, y + height)).convert('RGB')
    return sum(g > r + 40 and g > b + 20 for r, g, b in region.getdata()) >= 16
