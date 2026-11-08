"""端到端：真 Xvfb + 真窗口管理器 + 假微信 + 合成加密库，走完整发送流程。没有 Xvfb、fluxbox 或 tkinter 时跳过。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_weixin.py"
PYTHON_TK = os.environ.get("FAKE_PYTHON") or sys.executable
DISPLAY_NUMBER = 171

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("fluxbox")
         and subprocess.run([PYTHON_TK, "-c", "import tkinter"], capture_output=True).returncode == 0),
    reason="需要 Xvfb、fluxbox 和 tkinter")


@pytest.fixture(scope="module")
def display():
    name = f":{DISPLAY_NUMBER}"
    xvfb = subprocess.Popen(["Xvfb", name, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    wm = subprocess.Popen(["fluxbox", "-display", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    try:
        yield name
    finally:
        wm.terminate()
        xvfb.terminate()
        wm.wait(timeout=10)
        xvfb.wait(timeout=10)


class World:
    def __init__(self, base: Path, display: str, monkeypatch):
        from wechat_export_send import synthetic
        self.synthetic = synthetic
        self.base = base
        self.display = display
        self.home = base / "instances"
        self.dir = self.home / "main"
        self.dir.mkdir(parents=True, mode=0o700)
        synthetic.build_account(self.dir)
        calibration = json.loads((ROOT / "config" / "ui-calibration.json").read_text(encoding="utf-8"))
        calibration.update(display=display, search_delay_seconds=0.3, switch_delay_seconds=0.4, paste_settle_seconds=0.4,
                           send_settle_seconds=0.3, confirmation_timeout_seconds=8, clipboard_wait_seconds=4,
                           image_ready_timeout_seconds=5, allowed_visible_window_titles=["Fluxbox", "tk", "fake_weixin.py"])
        self.calibration = calibration
        (self.dir / "calibration").mkdir(mode=0o700)
        (self.dir / "calibration" / "ui-calibration.json").write_text(json.dumps(calibration), encoding="utf-8")
        (self.dir / "config.json").write_text(json.dumps({
            "instance": "main", "send": {"mode": "direct", "timeout_seconds": 60, "lock_wait_seconds": 5}}), encoding="utf-8")
        monkeypatch.setenv("WES_HOME", str(self.home))
        monkeypatch.setenv("DISPLAY", display)
        self.log = base / "fake.log"
        self.log.write_text("", encoding="utf-8")
        self.process = None

    def start(self, **env) -> None:
        environment = {**os.environ, "DISPLAY": self.display, "FAKE_ROOT": str(self.dir), "FAKE_LOG": str(self.log),
                       "FAKE_CALIBRATION": json.dumps(self.calibration), **env}
        self.process = subprocess.Popen([PYTHON_TK, str(FAKE)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if '"ready"' in self.log.read_text(encoding="utf-8"):
                time.sleep(1)
                return
            if self.process.poll() is not None:
                raise RuntimeError(self.process.stderr.read().decode())
            time.sleep(0.1)
        raise RuntimeError("fake weixin did not start")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        time.sleep(0.5)

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def clicks(self) -> list[dict]:
        return [event for event in self.events() if event["event"] == "click_send"]

    def send(self, *arguments: str) -> tuple[int, dict]:
        from wechat_export_send.send import host
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = host.main(list(arguments) + ["--json"])
        return code, json.loads(buffer.getvalue())

    def calibrate(self, to: str) -> None:
        code, result = self.send("--to", to, "--calibrate")
        assert code == 0, result
        assert result["status"] == "calibrated"


GROUP_NAME = "合成示例群（虚构）"


@pytest.fixture
def world(tmp_path, display, monkeypatch):
    item = World(tmp_path, display, monkeypatch)
    yield item
    item.stop()


def test_text_sent_confirmed_and_idempotent(world):
    world.start()
    world.calibrate(GROUP_NAME)
    code, result = world.send("--to", GROUP_NAME, "--text", "周六上午见 ✅", "--request-id", "r1")
    assert code == 0 and result["status"] == "sent", result
    assert result["schema"] == "chat-send/v1" and result["request_id"] == "r1"
    assert result["conversation_id"].startswith("g_") and result["submitted_at"]
    assert result["extra"]["receipt"]["server_id"]
    assert [event["text"] for event in world.clicks()] == ["周六上午见 ✅"]
    code, again = world.send("--to", GROUP_NAME, "--text", "周六上午见 ✅", "--request-id", "r1")
    assert code == 0 and again["status"] == "sent"
    assert "没有重发" in again["detail"]
    assert len(world.clicks()) == 1  # 同一个 request-id 绝不再点


def test_dry_run_never_clicks_send(world):
    world.start()
    world.calibrate(GROUP_NAME)
    code, result = world.send("--to", GROUP_NAME, "--text", "不会发出去", "--dry-run")
    assert code == 0 and result["status"] == "dry_run", result
    assert world.clicks() == []


def test_click_without_receipt_is_unknown_and_blocks_retry(world):
    world.start(FAKE_NO_RECEIPT="1")
    world.calibrate(GROUP_NAME)
    code, result = world.send("--to", GROUP_NAME, "--text", "确认不到", "--request-id", "r2", "--timeout", "40")
    assert code == 2 and result["status"] == "unknown", result
    assert len(world.clicks()) == 1
    code, again = world.send("--to", GROUP_NAME, "--text", "确认不到", "--request-id", "r2", "--timeout", "40")
    assert code == 2 and again["status"] == "unknown"
    assert len(world.clicks()) == 1  # unknown 之后也不许自动重发


def test_existing_draft_is_failed_and_not_touched(world):
    world.start()
    world.calibrate(GROUP_NAME)
    world.stop()
    world.log.write_text("", encoding="utf-8")
    world.start(FAKE_DRAFT="用户自己写了一半的话")
    code, result = world.send("--to", GROUP_NAME, "--text", "x", "--request-id", "r3")
    assert code == 1 and result["status"] == "failed", result
    assert result["extra"]["code"] == "draft_not_empty"
    assert world.clicks() == []


def test_wrong_header_is_failed_before_click(world):
    world.start()
    world.calibrate(GROUP_NAME)
    world.stop()
    world.log.write_text("", encoding="utf-8")
    world.start(FAKE_HEADER="#ff00ff")
    code, result = world.send("--to", GROUP_NAME, "--text", "x")
    assert code == 1 and result["status"] == "failed", result
    assert result["extra"]["code"] == "header_mismatch"
    assert world.clicks() == []


def test_uncalibrated_target_is_failed(world):
    world.start()
    code, result = world.send("--to", GROUP_NAME, "--text", "x")
    assert code == 1 and result["extra"]["code"] == "header_not_calibrated"
    assert world.clicks() == []


def test_unknown_target_is_failed(world):
    world.start()
    code, result = world.send("--to", "不存在的群", "--text", "x")
    assert code == 1 and result["extra"]["code"] == "target_not_found"


def test_window_drift_is_restored_then_sent(world):
    world.start()
    world.calibrate(GROUP_NAME)
    world.stop()
    world.log.write_text("", encoding="utf-8")
    world.start(FAKE_START_SIZE="915x667")
    code, result = world.send("--to", GROUP_NAME, "--text", "窗口漂移后照样发")
    assert code == 0 and result["status"] == "sent", result
    assert len(world.clicks()) == 1


def test_image_sent(world):
    from wechat_export_send.synthetic import _png
    world.start()
    world.calibrate(GROUP_NAME)
    picture = world.base / "picture.png"
    picture.write_bytes(_png(40, 30))
    code, result = world.send("--to", GROUP_NAME, "--image", str(picture))
    assert code == 0 and result["status"] == "sent", result
    assert world.clicks()[-1]["image"] is True
