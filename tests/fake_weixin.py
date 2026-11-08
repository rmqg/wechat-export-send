"""测试用的假微信：Tk 画一个和校准文件坐标一致的窗口，点发送时往合成加密库写一行。

不是微信，也不连任何网络。行为由环境变量控制：
  FAKE_ROOT       合成账号目录（synthetic.build_account 的输出）
  FAKE_LOG        写动作日志的文件（每行一个 JSON），测试靠它判断发送按钮有没有被点过
  FAKE_NO_RECEIPT 点了发送但不写库，模拟发出去了却确认不到
  FAKE_DRAFT      启动时输入框里已有草稿
  FAKE_START_SIZE 启动时的窗口尺寸，如 915x667，模拟窗口漂移
  FAKE_HEADER     选中会话后标题的颜色，默认按搜索词算；用它模拟点错会话
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechat_export_send import synthetic  # noqa: E402

ROOT_DIR = Path(os.environ["FAKE_ROOT"])
LOG = Path(os.environ["FAKE_LOG"])
SELF_ROWID = 1
CALIBRATION = json.loads(os.environ["FAKE_CALIBRATION"])
state = {"server_id": 700000, "image_draft": False, "conversation": ""}


def log(event: str, **details) -> None:
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, "at": time.time(), **details}, ensure_ascii=False) + "\n")


root = tk.Tk()
root.title("Weixin")
width, height = CALIBRATION["window"]["width"], CALIBRATION["window"]["height"]
start_size = os.environ.get("FAKE_START_SIZE") or f"{width}x{height}"
root.geometry(f"{start_size}+{CALIBRATION['window']['x']}+{CALIBRATION['window']['y']}")
root.resizable(True, True)

search = tk.Entry(root)
sx, sy = CALIBRATION["search_box"]
search.place(x=sx - 100, y=sy - 12, width=200, height=24)
result = tk.Label(root, text="", anchor="w", bg="#eeeeee")
rx, ry = CALIBRATION["search_result"]
result.place(x=rx - 100, y=ry - 20, width=240, height=40)
hx, hy, hw, hh = CALIBRATION["header_region"]
header = tk.Frame(root, width=hw, height=hh, bg="#ffffff")
header.place(x=hx, y=hy, width=hw, height=hh)
cx, cy = CALIBRATION["compose"]
compose = tk.Text(root, undo=False)
compose.place(x=cx - 170, y=cy - 50, width=500, height=100)
if os.environ.get("FAKE_DRAFT"):
    compose.insert("1.0", os.environ["FAKE_DRAFT"])
bx, by, bw, bh = CALIBRATION["send_button_region"]
send_button = tk.Label(root, text="发送", bg="#cccccc")
send_button.place(x=bx, y=by, width=bw, height=bh)


def on_search_key(_event=None):
    result.configure(text=search.get())


search.bind("<KeyRelease>", on_search_key)
search.bind("<<Paste>>", lambda _e: root.after(50, on_search_key))


def select_all(event):
    if event.widget is compose:
        compose.tag_add("sel", "1.0", "end-1c")
    else:
        event.widget.select_range(0, "end")
    return "break"


root.bind_class("Text", "<Control-a>", select_all)
root.bind_class("Entry", "<Control-a>", select_all)


def header_color(name: str) -> str:
    fixed = os.environ.get("FAKE_HEADER")
    if fixed:
        return fixed
    digest = hashlib.sha256(name.encode()).hexdigest()
    return "#" + digest[:6]


def click_result(_event=None):
    state["conversation"] = result.cget("text")
    header.configure(bg=header_color(state["conversation"]))
    log("select", conversation=state["conversation"])


result.bind("<Button-1>", click_result)


def on_paste(event):
    """先看剪贴板有什么：有图片就要 image/png，这一步会触发发送器的选择服务。"""
    if event.widget is not compose:
        return None
    try:
        targets = root.tk.call("selection", "get", "-selection", "CLIPBOARD", "-type", "TARGETS")
    except tk.TclError:
        targets = ""
    if "image/png" in str(targets):
        try:
            root.tk.call("selection", "get", "-selection", "CLIPBOARD", "-type", "image/png")
        except tk.TclError:
            return "break"
        state["image_draft"] = True
        send_button.configure(bg="#07c160")
        log("paste_image")
        return "break"
    return None


root.bind_class("Text", "<Control-v>", on_paste, add=False)
root.bind_class("Text", "<<Paste>>", lambda e: None)


def plain_paste(event):
    try:
        event.widget.insert("insert", root.clipboard_get())
    except tk.TclError:
        pass
    return "break"


def control_v(event):
    handled = on_paste(event)
    if handled is None:
        return plain_paste(event)
    return handled


compose.bind("<Control-v>", control_v)


def click_send(_event=None):
    text = compose.get("1.0", "end-1c")
    log("click_send", conversation=state["conversation"], text=text, image=state["image_draft"])
    if not text and not state["image_draft"]:
        return
    state["server_id"] += 1
    if not os.environ.get("FAKE_NO_RECEIPT"):
        if state["image_draft"]:
            synthetic.append_group_message(ROOT_DIR, server_id=state["server_id"], text="<msg><img md5=\"0\"/></msg>",
                                           sender_rowid=SELF_ROWID, created=int(time.time()) + 1, origin=1, local_type=3)
        else:
            synthetic.append_group_message(ROOT_DIR, server_id=state["server_id"], text=text, sender_rowid=SELF_ROWID,
                                           created=int(time.time()) + 1, origin=1, local_type=1)
    compose.delete("1.0", "end")
    state["image_draft"] = False
    send_button.configure(bg="#cccccc")


send_button.bind("<Button-1>", click_send)
compose.bind("<KeyRelease>", lambda _e: send_button.configure(bg="#07c160" if compose.get("1.0", "end-1c") else "#cccccc"))
log("ready", size=start_size)
root.mainloop()
