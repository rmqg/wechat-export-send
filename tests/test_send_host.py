"""wechat-send 的状态语义：不用 X11，直接喂 worker 的各种结局。"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from wechat_export_send import synthetic
from wechat_export_send.config import Instance
from wechat_export_send.send import host


def progress(job_dir: Path, *, submitted: bool, stage: str = "confirm") -> None:
    (job_dir / "progress.json").write_text(json.dumps({"stage": stage, "submitted": submitted}), encoding="utf-8")


@pytest.mark.parametrize("returncode,stdout,marker,timed_out,stopped,expected", [
    (0, '{"status":"sent"}', {"submitted": True}, False, True, "sent"),
    (0, '{"status":"failed","code":"header_mismatch"}', {"submitted": False, "stage": "verify_header"}, False, True, "failed"),
    # 点过发送，无论 worker 怎么说都不能算 failed
    (0, '{"status":"failed"}', {"submitted": True}, False, True, "unknown"),
    (0, '{"status":"unknown"}', {"submitted": True}, False, True, "unknown"),
    # worker 崩溃没有回执
    (1, "", {"submitted": True}, False, True, "unknown"),
    (1, "", {"submitted": False, "stage": "baseline"}, False, True, "failed"),
    (125, "", None, False, True, "failed"),
    # docker 命令挂了但确认不了容器停没停：它可能还会去点
    (125, "", {"submitted": False, "stage": "baseline"}, False, False, "unknown"),
    (125, "", None, False, False, "unknown"),
    # 超时
    (None, "", {"submitted": True}, True, True, "unknown"),
    (None, "", {"submitted": False, "stage": "paste"}, True, True, "failed"),
    (None, "", {"submitted": False, "stage": "paste"}, True, False, "unknown"),
    # XTest 明确拒绝按下：确定没发
    (1, "", {"submitted": False, "stage": "submit_rejected"}, False, True, "failed"),
    # 回执之后 worker 异常退出
    (1, '{"status":"sent"}', {"submitted": True}, False, True, "unknown"),
    # 没点过却说发了：不合常理，不能算 sent
    (0, '{"status":"sent"}', {"submitted": False}, False, True, "unknown"),
    # 乱七八糟的状态
    (0, '{"status":"weird"}', {"submitted": False}, False, True, "failed"),
    (0, '{"status":"weird"}', {"submitted": True}, False, True, "unknown"),
])
def test_interpret_worker(tmp_path, returncode, stdout, marker, timed_out, stopped, expected):
    if marker is not None:
        progress(tmp_path, **marker)
    assert host.interpret_worker(returncode, stdout, tmp_path, timed_out=timed_out, stopped=stopped)["status"] == expected


@pytest.fixture
def instance(tmp_path, monkeypatch):
    home = tmp_path / "instances"
    directory = home / "main"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "gui" / "xwechat_files" / synthetic.ACCOUNT_DIR / "db_storage").mkdir(parents=True)
    (directory / "keys").mkdir(mode=0o700)
    (directory / "keys" / "all_keys.json").write_text("{}", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"instance": "main", "send": {"mode": "direct", "lock_wait_seconds": 1}}), encoding="utf-8")
    monkeypatch.setenv("WES_HOME", str(home))
    return Instance.load("main")


def call(*arguments) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = host.main(list(arguments))
    return code, json.loads(buffer.getvalue())


class Worker:
    def __init__(self, monkeypatch, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        monkeypatch.setattr(host, "run_worker", self)

    def __call__(self, instance, job_dir, *, timeout, calibrate):
        self.calls += 1
        self.job = json.loads((Path(job_dir) / "job.json").read_text(encoding="utf-8"))
        return self.outcomes.pop(0)


def test_arguments_are_invalid_with_exit_3():
    for arguments in ([], ["--to", "x"], ["--text", "a"], ["--to", "x", "--text", "a", "--image", "b"],
                      ["--to", "x", "--text", "   "], ["--to", "x", "--unknown"]):
        code, result = call(*arguments)
        assert code == 3 and result["status"] == "invalid" and result["schema"] == "chat-send/v1", arguments


def test_exit_codes(instance, monkeypatch):
    for status, code in (("sent", 0), ("failed", 1), ("unknown", 2)):
        Worker(monkeypatch, {"status": status, "conversation_id": "g_x"})
        got, result = call("--to", "x", "--text", "你好")
        assert got == code and result["status"] == status and result["conversation_id"] == "g_x"


def test_bad_file_is_failed_not_invalid(instance, monkeypatch, tmp_path):
    worker = Worker(monkeypatch)
    code, result = call("--to", "x", "--image", str(tmp_path / "missing.png"))
    assert code == 1 and result["status"] == "failed"
    not_image = tmp_path / "note.png"
    not_image.write_bytes(b"not an image")
    code, result = call("--to", "x", "--image", str(not_image))
    assert code == 1 and result["status"] == "failed"
    bad_text = tmp_path / "bad.txt"
    bad_text.write_bytes(b"\xff\xfe")
    code, result = call("--to", "x", "--text-file", str(bad_text))
    assert code == 1 and result["status"] == "failed"
    assert worker.calls == 0


def test_request_id_sent_returns_previous_result(instance, monkeypatch):
    worker = Worker(monkeypatch, {"status": "sent", "conversation_id": "g_x", "submitted_at": 123})
    code, first = call("--to", "x", "--text", "你好", "--request-id", "same")
    assert code == 0 and first["submitted_at"] == 123
    code, second = call("--to", "x", "--text", "你好", "--request-id", "same")
    assert code == 0 and second["status"] == "sent" and second["submitted_at"] == 123
    assert worker.calls == 1


def test_request_id_failed_can_retry(instance, monkeypatch):
    worker = Worker(monkeypatch, {"status": "failed"}, {"status": "sent"})
    assert call("--to", "x", "--text", "你好", "--request-id", "retry")[0] == 1
    assert call("--to", "x", "--text", "你好", "--request-id", "retry")[0] == 0
    assert worker.calls == 2


def test_request_id_unknown_never_resends(instance, monkeypatch):
    worker = Worker(monkeypatch, {"status": "unknown"})
    assert call("--to", "x", "--text", "你好", "--request-id", "u")[0] == 2
    assert call("--to", "x", "--text", "你好", "--request-id", "u")[0] == 2
    assert worker.calls == 1


def test_crash_while_sending_blocks_resend(instance, monkeypatch):
    # 上次进程死在提交途中：记录停在 sending
    store = host.RequestStore(instance, "crashed")
    import hashlib
    store.write({"request_id": "crashed", "payload_sha256": hashlib.sha256("你好".encode()).hexdigest(), "to": "x", "status": "sending"})
    worker = Worker(monkeypatch)
    code, result = call("--to", "x", "--text", "你好", "--request-id", "crashed")
    assert code == 2 and result["status"] == "unknown" and worker.calls == 0


def test_request_id_with_different_content_is_invalid(instance, monkeypatch):
    Worker(monkeypatch, {"status": "sent"})
    call("--to", "x", "--text", "你好", "--request-id", "c")
    code, result = call("--to", "x", "--text", "别的内容", "--request-id", "c")
    assert code == 3 and result["status"] == "invalid"
    assert result["to"] == "x" and result["request_id"] == "c"


def test_exception_during_worker_is_unknown(instance, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("private text must not leak")
    monkeypatch.setattr(host, "run_worker", boom)
    code, result = call("--to", "x", "--text", "你好", "--request-id", "e")
    assert code == 2 and result["status"] == "unknown" and "private" not in result["detail"]


def test_lock_busy_is_failed(instance, monkeypatch):
    import fcntl
    worker = Worker(monkeypatch)
    path = instance.state / "send" / "send.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, result = call("--to", "x", "--text", "你好")
    assert code == 1 and result["status"] == "failed" and worker.calls == 0


def test_job_files_are_private_and_removed(instance, monkeypatch):
    modes = {}

    def spy(inst, job_dir, *, timeout, calibrate):
        modes["job"] = stat.S_IMODE((Path(job_dir) / "job.json").stat().st_mode)
        modes["payload"] = stat.S_IMODE((Path(job_dir) / "payload").stat().st_mode)
        modes["dir"] = stat.S_IMODE(Path(job_dir).stat().st_mode)
        return {"status": "sent"}

    monkeypatch.setattr(host, "run_worker", spy)
    call("--to", "x", "--text", "你好")
    assert modes == {"job": 0o600, "payload": 0o600, "dir": 0o700}
    assert list((instance.state / "send" / "jobs").iterdir()) == []


def test_docker_command_is_locked_down(instance):
    command = host._docker_command(instance, instance.state, "wes-send-test", calibrate=False)
    joined = " ".join(command)
    assert "--read-only" in command and "ALL" in command and "no-new-privileges:true" in joined
    assert f"container:{instance.container}" in joined
    assert ",readonly" in [part for part in command if "/config" in part][0]
    assert "SYS_PTRACE" not in joined


def test_cli_entry_runs_as_module():
    result = subprocess.run([sys.executable, "-m", "wechat_export_send.send.host", "--to", "x"], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 3 and json.loads(result.stdout)["status"] == "invalid"


def test_capture_container_can_write_private_keys_dir(instance, monkeypatch):
    """keys/ 归普通用户、权限 700：容器里的 root 必须有 DAC_OVERRIDE，并且只读挂载账号目录。"""
    from wechat_export_send import capture
    seen = {}

    def fake_run(command, timeout):
        seen["command"] = command

        class Result:
            returncode = 0
        return Result()

    storage = instance.db_storage()
    (storage / "x.db").write_bytes(b"x")
    (instance.dir / "instance.env").write_text("CLIENT_ROOT=/client\n", encoding="utf-8")
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    assert capture.host_run(instance, "gdb", check=True) == 0
    joined = " ".join(seen["command"])
    for capability in ("SYS_PTRACE", "DAC_READ_SEARCH", "DAC_OVERRIDE", "CHOWN"):
        assert capability in joined
    assert "--network none" in joined and "no-new-privileges:true" in joined
    assert f"src={instance.gui},dst=/config,readonly" in joined


def test_resolve_target_rules():
    from wechat_export_send.config import IdMapper
    from wechat_export_send.send.worker import StageError, resolve_target
    mapper = IdMapper(b"s")
    contacts = {"1@chatroom": {"display_name": "项目群", "remark": "", "nick_name": "项目群"},
                "2@chatroom": {"display_name": "同名", "remark": "", "nick_name": "同名"},
                "3@chatroom": {"display_name": "同名", "remark": "", "nick_name": "同名"},
                "4@chatroom": {"display_name": "周报", "remark": "", "nick_name": "周报"},
                "5@chatroom": {"display_name": "研发周报群", "remark": "", "nick_name": "研发周报群"},
                "alice": {"display_name": "备注名", "remark": "备注名", "nick_name": "昵称"}}
    assert resolve_target("项目群", contacts, mapper)["username"] == "1@chatroom"
    assert resolve_target("1@chatroom", contacts, mapper)["term"] == "项目群"
    assert resolve_target(mapper.conversation("1@chatroom", "group"), contacts, mapper)["username"] == "1@chatroom"
    assert resolve_target("昵称", contacts, mapper)["term"] == "备注名"
    for to, code in (("不存在", "target_not_found"), ("同名", "target_ambiguous"), ("周报", "search_ambiguous"), ("项", "target_not_found")):
        with pytest.raises(StageError) as caught:
            resolve_target(to, contacts, mapper)
        assert caught.value.code == code


def test_locate_text_receipt_needs_unique_exact_match():
    from wechat_export_send.send.worker import locate_new_self_image, locate_new_self_text
    import hashlib
    digest = hashlib.sha256("你好".encode()).hexdigest()
    row = {"local_id": 5, "server_id": 9, "create_time": 100, "origin_source": 1, "local_type": 1, "message_content": "你好",
           "compress_content": None, "WCDB_CT_message_content": 0}
    assert locate_new_self_text({"s": [row]}, {"s": 4}, 100, digest)["server_id"] == 9
    assert locate_new_self_text({"s": [row]}, {"s": 5}, 100, digest) is None  # 不是新行
    assert locate_new_self_text({"s": [{**row, "server_id": 0}]}, {"s": 4}, 100, digest) is None
    assert locate_new_self_text({"s": [{**row, "origin_source": 2}]}, {"s": 4}, 100, digest) is None
    assert locate_new_self_text({"s": [{**row, "message_content": "别的"}]}, {"s": 4}, 100, digest) is None
    assert locate_new_self_text({"s": [row, {**row, "local_id": 6}]}, {"s": 4}, 100, digest) is None  # 两条就说不清了
    # 文字本身长得像「账号:\n正文」
    prefixed = "note:\n你好"
    assert locate_new_self_text({"s": [{**row, "message_content": prefixed}]}, {"s": 4}, 100, hashlib.sha256(prefixed.encode()).hexdigest())
    image = {"local_id": 5, "server_id": 9, "create_time": 100, "origin_source": 1, "local_type": 3}
    assert locate_new_self_image({"s": [image]}, {"s": 4}, 100)["server_id"] == 9
    assert locate_new_self_image({"s": [image, {**image, "local_id": 6}]}, {"s": 4}, 100) is None


def test_attached_instance_worker_network_and_owner(tmp_path, monkeypatch):
    home = tmp_path / "instances"
    directory = home / "second"
    (directory / "keys").mkdir(parents=True)
    (directory / "keys" / "all_keys.json").write_text("{}", encoding="utf-8")
    (directory / "instance.env").write_text("PUID=1200\nPGID=1201\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"instance": "second", "gui_dir": str(tmp_path / "gui"),
        "desktop": {"managed": False, "container": "other", "network": "host", "docker_host": "unix:///o.sock"}}), encoding="utf-8")
    monkeypatch.setenv("WES_HOME", str(home))
    instance = Instance.load("second")
    command = host._docker_command(instance, instance.state, "n", calibrate=False)
    assert command[command.index("--network") + 1] == "host"
    assert command[command.index("--user") + 1] == "1200:1201"
    assert any(f"src={tmp_path / 'gui'},dst=/config,readonly" in part for part in command)
    calls = []

    class Result:
        returncode = 0
        stdout = "true\n"

    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: calls.append(a[0]) or Result())
    assert host._desktop_running(instance) and calls[0][:3] == ["docker", "-H", "unix:///o.sock"]
