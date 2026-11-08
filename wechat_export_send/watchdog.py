"""登录看门狗：每分钟只读看一眼微信窗口，掉线或恢复时最多提醒一次。不操作鼠标键盘，不重启客户端。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from .config import Instance, atomic_write

MAIN_MIN_SIZE = (438, 425)  # 4.1.13.26 主窗口 WM_NORMAL_HINTS 的最小尺寸


def classify_windows(windows: list[dict], main_size: tuple[int, int], login_size: tuple[int, int]) -> str:
    """按窗口判断状态，坐标不算。主窗口可调大小，偏离模板不等于掉线，发送器发送前会自己调回去。"""
    # 标题随语言环境变化：C.UTF-8 下是 Weixin，zh_CN 下是「微信」
    weixin = [w for w in windows if w.get("viewable") is True and w.get("title") in {"Weixin", "微信"}]
    if any((w.get("width"), w.get("height")) == login_size for w in weixin):
        return "offline"
    for window in weixin:
        width, height = window.get("width"), window.get("height")
        if (width, height) == main_size:
            return "online"
        if isinstance(width, int) and isinstance(height, int) and width >= MAIN_MIN_SIZE[0] and height >= MAIN_MIN_SIZE[1]:
            return "online"
    return "unavailable"


def probe(display: str, main_size: tuple[int, int], login_size: tuple[int, int]) -> str:
    import ctypes
    from .send import x11 as x11_mod
    connection = x11_mod.X11(display)
    try:
        windows = []
        for ident in connection._client_windows():
            attrs = x11_mod.XWindowAttributes()
            if connection.x.XGetWindowAttributes(connection.display, ident, ctypes.byref(attrs)):
                windows.append({"title": connection._window_title(ident), "viewable": attrs.map_state == connection.IS_VIEWABLE,
                                "width": attrs.width, "height": attrs.height})
        return classify_windows(windows, main_size, login_size)
    finally:
        connection.close()


def observe(instance: Instance) -> str:
    """在桌面容器的网络命名空间里跑一次探针（X11 抽象 socket 在那里面）。"""
    calibration = json.loads(instance.calibration_file().read_text(encoding="utf-8"))
    main_size = calibration["window"]
    login = calibration["login_window"]
    args = ["--display", str(calibration.get("display", ":99")),
            "--main-width", str(int(main_size["width"])), "--main-height", str(int(main_size["height"])),
            "--login-width", str(int(login["width"])), "--login-height", str(int(login["height"]))]
    if instance.config["send"]["mode"] == "direct":
        value = probe(args[1], (int(main_size["width"]), int(main_size["height"])), (int(login["width"]), int(login["height"])))
        return value
    uid, gid = instance.owner()
    command = ["docker", "run", "--rm", "--pull", "never", "--network", instance.worker_network(),
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "128m",
               "--user", f"{uid}:{gid}", "--tmpfs", "/tmp:rw,nosuid,size=8m", "--env", "PYTHONPATH=/app",
               instance.config["send"]["tools_image"], "-m", "wechat_export_send.watchdog", "--probe", *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    status = json.loads(result.stdout).get("status")
    if status not in ("online", "offline", "unavailable"):
        raise RuntimeError("invalid probe status")
    return status


def notify_bark(config: dict, title: str, body: str, level: str) -> bool:
    url = config.get("bark_url")
    if not url:
        return False
    payload = json.dumps({"title": title, "body": body, "group": config.get("group", "wechat"), "level": level},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read())
            return response.status < 300 and isinstance(result, dict) and result.get("code") == 200
    except Exception:
        return False


def transition(state: dict, observed: str, now: float) -> dict:
    previous = state.get("status")
    if observed == "online":
        if previous != "online":
            was_notified = bool(state.get("failure_notified"))
            state.update(status="online", changed_at=now, pending="recovery" if was_notified else None, failure_notified=False)
    else:
        if previous != observed:
            state.update(status=observed, changed_at=now)
        state["pending"] = None if state.get("failure_notified") else "failure"
    return state


def send_pending(state: dict, instance_name: str, config: dict, notifier) -> bool:
    kind = state.get("pending")
    if kind == "failure":
        if state["status"] == "offline":
            title = f"微信 {instance_name} 掉线了，需要扫码"
            body = "看到了登录窗口。打开这个实例的 noVNC 重新登录。"
        else:
            title = f"微信 {instance_name} 状态无法确认"
            body = "没有找到微信窗口，或者探针出错。检查桌面容器，运行 doctor。"
        level = config.get("fail_level", "timeSensitive")
    elif kind == "recovery":
        title = f"微信 {instance_name} 已恢复"
        body = "主聊天界面就绪。"
        level = config.get("recovery_level", "active")
    else:
        return False
    if notifier(config, title, body, level):
        state["pending"] = None
        state["failure_notified"] = kind == "failure"
        return True
    return False  # 没配 bark_url 或推送失败：状态留着，下一轮再试


def tick(instance: Instance, observer=observe, notifier=notify_bark, now: float | None = None) -> dict:
    directory = instance.state / "watchdog"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = directory / "state.json"
    lock_path = directory / "tick.lock"
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "notified": False, "pending": None}
        stamp = time.time() if now is None else now
        try:
            status = observer(instance)
            if status not in ("online", "offline", "unavailable"):
                status = "unavailable"
        except Exception:
            status = "unavailable"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            state = {}
        state["checked_at"] = stamp
        transition(state, status, stamp)
        notified = send_pending(state, instance.name, instance.config["watchdog"], notifier)
        atomic_write(state_path, json.dumps(state, separators=(",", ":")) + "\n")
        return {"status": status, "notified": notified, "pending": state.get("pending"), "checked_at": stamp}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探针入口（容器内用）；日常用 wechat-export-send watchdog")
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--main-width", type=int, required=True)
    parser.add_argument("--main-height", type=int, required=True)
    parser.add_argument("--login-width", type=int, required=True)
    parser.add_argument("--login-height", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps({"status": probe(args.display, (args.main_width, args.main_height), (args.login_width, args.login_height))}))
        return 0
    except Exception:
        print(json.dumps({"status": "unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
