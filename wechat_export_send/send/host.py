"""wechat-send：chat-send/v1 命令（接口见 docs/CONTRACT.md）。

退出码：sent=0，failed=1，unknown=2，invalid=3（--dry-run 成功和 calibrate 成功也是 0）。
唯一的硬规矩：凡是可能已经点过「发送」的情况，一律报 unknown，不报 failed。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .. import ROOT
from ..config import ConfigError, Instance, atomic_write, instances_root

SCHEMA = "chat-send/v1"
EXIT_CODES = {"sent": 0, "dry_run": 0, "calibrated": 0, "failed": 1, "unknown": 2, "invalid": 3}
DOCKER_KILL_GRACE = 15


class InvalidArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # 参数错误也要输出合约里的 JSON
        raise InvalidArguments(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="wechat-send", description="往微信会话发一条文字或图片，结果以 JSON 输出（chat-send/v1）")
    parser.add_argument("--to", help="完整的会话名，或导出里的 conversation_id")
    content = parser.add_mutually_exclusive_group()
    content.add_argument("--text", help="要发送的文字")
    content.add_argument("--text-file", type=Path, help="UTF-8 文本文件")
    content.add_argument("--image", type=Path, help="图片文件；PNG 原样发送，JPEG、WebP 先转成 PNG")
    parser.add_argument("--request-id", help="幂等键：同一个键发送成功后再调用不会重发")
    parser.add_argument("--timeout", type=int, help="整次调用的总时限（秒），默认取配置 send.timeout_seconds")
    parser.add_argument("--dry-run", action="store_true", help="只定位会话、校验输入，不提交")
    parser.add_argument("--json", action="store_true", help="输出 JSON（总是输出，留着是为了和接口约定一致）")
    parser.add_argument("--instance", default=os.environ.get("WES_INSTANCE"), help="实例名；只有一个实例时可省略")
    parser.add_argument("--calibrate", action="store_true", help="选中会话并保存标题模板（首次发送前做一次）")
    parser.add_argument("--current", action="store_true", help="和 --calibrate 一起用：不搜索，直接用现在打开的会话。搜不到的会话（如文件传输助手）先在 noVNC 里打开")
    return parser


def _pick_instance(name: str | None) -> Instance:
    if name:
        return Instance.load(name)
    root = instances_root()
    names = sorted(path.name for path in root.iterdir() if (path / "config.json").is_file()) if root.is_dir() else []
    if len(names) != 1:
        raise ConfigError(f"有 {len(names)} 个实例，请用 --instance 或环境变量 WES_INSTANCE 指定")
    return Instance.load(names[0])


def _output(status: str, *, request_id: str = "", to: str = "", conversation_id: str = "",
            submitted_at: int | None = None, detail: str = "", extra: dict | None = None) -> dict:
    result = {"schema": SCHEMA, "status": status, "request_id": request_id, "to": to,
              "conversation_id": conversation_id, "submitted_at": submitted_at, "detail": detail}
    if extra:
        result["extra"] = extra
    return result


def _prepare_payload(args, instance: Instance, job_dir: Path) -> tuple[str, Path, str]:
    """校验输入并写到私有的任务目录。文件不合法算 failed（确定没发）。"""
    send = instance.config["send"]
    payload = job_dir / "payload"
    if args.image is not None:
        raw = args.image.read_bytes()
        if not raw:
            raise ValueError("图片文件是空的")
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            raw = _convert_to_png(raw)
        if len(raw) > int(send["max_image_bytes"]):
            raise ValueError(f"图片超过 send.max_image_bytes（{send['max_image_bytes']} 字节）")
        atomic_write(payload, raw)
        return "image", payload, hashlib.sha256(raw).hexdigest()
    if args.text_file is not None:
        raw = args.text_file.read_bytes()
        text = raw.decode("utf-8", "strict")
    else:
        text = args.text
    text = text.replace("\r\n", "\n")
    raw = text.encode("utf-8")
    if not raw.strip() or b"\x00" in raw:
        raise ValueError("文字不能为空，也不能含 NUL")
    if len(raw) > int(send["max_text_bytes"]):
        raise ValueError(f"文字超过 send.max_text_bytes（{send['max_text_bytes']} 字节）")
    atomic_write(payload, raw)
    return "text", payload, hashlib.sha256(raw).hexdigest()


def _convert_to_png(raw: bytes) -> bytes:
    import io
    try:
        from PIL import Image
    except ImportError:
        raise ValueError("JPEG、WebP 需要 Pillow 才能转成 PNG；请先运行 install.sh 或直接给 PNG") from None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if getattr(image, "is_animated", False):
                raise ValueError("不支持动图")
            if image.format not in {"JPEG", "WEBP", "MPO"}:
                raise ValueError(f"不支持的图片格式：{image.format}")
            buffer = io.BytesIO()
            image.convert("RGBA" if image.mode in {"RGBA", "LA", "P"} else "RGB").save(buffer, format="PNG")
            return buffer.getvalue()
    except ValueError:
        raise
    except Exception:
        raise ValueError("文件不是能识别的图片") from None


class RequestStore:
    """幂等记录：提交之前先落盘 sending，进程崩了也不会被当成没发过。"""

    def __init__(self, instance: Instance, request_id: str):
        self.request_id = request_id
        name = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        self.path = instance.state / "send" / "requests" / f"{name}.json"

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def write(self, record: dict) -> None:
        atomic_write(self.path, json.dumps(record, ensure_ascii=False))


def _worker_paths(instance: Instance, db_storage: Path, job_dir: Path, *, docker: bool) -> dict:
    if not docker:
        return {"db_storage": str(db_storage), "keys_file": str(instance.keys_file),
                "calibration_file": str(instance.calibration_file()),
                "headers_dir": str(instance.calibration_dir / "headers"),
                "evidence_dir": str(instance.state / "send" / "evidence"), "payload": str(job_dir / "payload"),
                "temp_dir": str(instance.state / "send" / "tmp")}
    relative = db_storage.relative_to(instance.gui).as_posix()
    return {"db_storage": f"/config/{relative}", "keys_file": "/keys/all_keys.json",
            "calibration_file": "/calibration/ui-calibration.json", "headers_dir": "/headers",
            "evidence_dir": "/evidence", "payload": "/job/payload", "temp_dir": "/tmp"}


def _docker_command(instance: Instance, job_dir: Path, name: str, *, calibrate: bool) -> list[str]:
    send = instance.config["send"]
    uid, gid = instance.owner()
    headers = instance.calibration_dir / "headers"
    evidence = instance.state / "send" / "evidence"
    headers.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance.give_to_owner(headers, recursive=True)
    instance.give_to_owner(evidence, recursive=True)
    instance.give_to_owner(job_dir, recursive=True)
    command = ["docker", "run", "--rm", "--pull", "never", "--name", name,
               "--network", instance.worker_network(), "--read-only", "--user", f"{uid}:{gid}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--memory", f"{int(send.get('memory_mb', 768))}m", "--cpus", "1",
               "--tmpfs", f"/tmp:rw,nosuid,nodev,size={int(send.get('tmpfs_mb', 384))}m,mode=0700,uid={uid},gid={gid}",
               "--env", "PYTHONPATH=/app", "--env", "TZ=UTC"]
    mounts = [(instance.gui, "/config", True), (instance.keys_file, "/keys/all_keys.json", True),
              (instance.calibration_file(), "/calibration/ui-calibration.json", True),
              (headers, "/headers", not calibrate), (evidence, "/evidence", False), (job_dir, "/job", False)]
    for source, target, readonly in mounts:
        command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    command += [send["tools_image"], "-m", "wechat_export_send.send.worker", "--job", "/job"]
    return command


def _desktop_running(instance: Instance) -> bool:
    try:
        result = subprocess.run([*instance.docker(), "container", "inspect", "--format", "{{.State.Running}}", instance.container],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _stop_worker(process: subprocess.Popen, docker_name: str | None) -> bool:
    """超时后停掉 worker。返回 True 表示确认它已经停了（之后不可能再点发送）。"""
    if docker_name:
        try:
            subprocess.run(["docker", "kill", docker_name], capture_output=True, timeout=DOCKER_KILL_GRACE)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=DOCKER_KILL_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=DOCKER_KILL_GRACE)
        except subprocess.TimeoutExpired:
            return False
    if docker_name:
        try:
            check = subprocess.run(["docker", "container", "inspect", "--format", "{{.State.Running}}", docker_name],
                                   capture_output=True, text=True, timeout=DOCKER_KILL_GRACE)
        except (OSError, subprocess.SubprocessError):
            return False
        if check.returncode == 0 and check.stdout.strip() == "true":
            return False
    return True


def _progress(job_dir: Path) -> dict:
    try:
        return json.loads((job_dir / "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def interpret_worker(returncode: int | None, stdout: str, job_dir: Path, *, timed_out: bool, stopped: bool) -> dict:
    """把 worker 的结局映射成确定的状态。可能点过发送的一律 unknown。"""
    outcome = None
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "status" in value:
            outcome = value
            break
    if outcome is None:
        try:
            outcome = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            outcome = None
    progress = _progress(job_dir)
    clicked = bool(progress.get("submitted"))
    rejected = progress.get("stage") == "submit_rejected"
    if outcome is None or timed_out:
        if not stopped:
            return {"status": "unknown", "code": "worker_not_stopped", "stage": progress.get("stage", ""),
                    "detail": "没能确认 worker 已经停止，它可能还会点击发送；不要自动重发，请人工查看"}
        if clicked and not rejected:
            return {"status": "unknown", "code": "no_receipt", "stage": progress.get("stage", ""),
                    "detail": "已经到了点击发送这一步，但没有拿到确认；不要自动重发，请人工查看"}
        if not progress and returncode not in (0, None):
            return {"status": "failed", "code": "worker_not_started", "stage": "start",
                    "detail": "worker 没有启动（查看 docker 和工具镜像），确定没有发出去"}
        return {"status": "failed", "code": "timeout" if timed_out else "worker_crashed", "stage": progress.get("stage", ""),
                "detail": "worker 在点击发送之前就结束了，确定没有发出去"}
    status = outcome.get("status")
    if clicked and not rejected and status != "sent":
        outcome.update(status="unknown", detail=outcome.get("detail") or "提交后没有拿到确认；不要自动重发")
    elif clicked and not rejected and returncode:
        outcome.update(status="unknown", code="worker_exit_after_receipt", detail="worker 报告后异常退出；不要自动重发")
    elif status not in {"sent", "failed", "unknown", "dry_run", "calibrated"}:
        outcome.update(status="unknown" if clicked else "failed", code="invalid_status", detail="worker 返回了无法识别的状态")
    elif status == "sent" and not clicked:
        outcome.update(status="unknown", code="inconsistent_receipt", detail="worker 报告已发送，但没有提交记录；请人工查看")
    return outcome


def run_worker(instance: Instance, job_dir: Path, *, timeout: int, calibrate: bool) -> dict:
    docker = instance.config["send"]["mode"] == "docker"
    name = "wes-send-" + uuid.uuid4().hex[:12] if docker else None
    if docker:
        command = _docker_command(instance, job_dir, name, calibrate=calibrate)
    else:
        env_path = str(ROOT)
        command = [sys.executable, "-m", "wechat_export_send.send.worker", "--job", str(job_dir)]
    environment = dict(os.environ)
    if not docker:
        environment["PYTHONPATH"] = env_path + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=environment)
    timed_out = False
    stopped = True
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stopped = _stop_worker(process, name)
        try:
            stdout, _ = process.communicate(timeout=1)
        except (subprocess.TimeoutExpired, ValueError):
            stdout = ""
    except BaseException:  # 被 SIGTERM、Ctrl-C 打断：先停 worker，再让上层报 unknown
        _stop_worker(process, name)
        raise
    if name and not timed_out and process.returncode:
        # docker 命令自己挂了时，容器可能还在跑、还可能去点发送，必须确认它停了才能下结论
        stopped = _stop_worker(process, name)
    return interpret_worker(process.returncode, stdout, job_dir, timed_out=timed_out, stopped=stopped)


def _with_lock(instance: Instance, wait_seconds: float):
    path = instance.state / "send" / "send.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a")
    os.chmod(path, 0o600)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.5)


def _prune_evidence(instance: Instance, keep: int = 40) -> None:
    directory = instance.state / "send" / "evidence"
    try:
        files = sorted(directory.glob("*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in files[keep:]:
        path.unlink(missing_ok=True)


def send(args) -> dict:
    request_id = args.request_id or ""
    to = args.to or ""
    instance = _pick_instance(args.instance)
    timeout = int(args.timeout or instance.config["send"]["timeout_seconds"])
    if timeout < 30:
        raise InvalidArguments("--timeout 不能小于 30 秒")
    job_dir = instance.state / "send" / "jobs" / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (instance.state / "send" / "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = None
    try:
        kind, payload_hash = "calibrate", ""
        if not args.calibrate:
            has_content = args.text is not None or args.text_file is not None or args.image is not None
            if has_content:
                try:
                    kind, _, payload_hash = _prepare_payload(args, instance, job_dir)
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    text = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeDecodeError) else type(exc).__name__
                    return _output("failed", request_id=request_id, to=to, detail=f"输入不合法：{text}")
            else:
                kind = "dry_run"

        lock = _with_lock(instance, float(instance.config["send"]["lock_wait_seconds"]))
        if lock is None:
            return _output("failed", request_id=request_id, to=to, detail="同一个实例正在发送另一条消息，等待超时，确定没有发出去")

        store = RequestStore(instance, request_id) if request_id and kind in {"text", "image"} and not args.dry_run else None
        if store is not None:
            record = store.read()
            if record is not None:
                if record.get("payload_sha256") != payload_hash or record.get("to") != to:
                    raise InvalidArguments("这个 --request-id 之前用于不同的内容或目标")
                previous = record.get("status")
                if previous == "sent":
                    result = dict(record.get("result") or {})
                    result["detail"] = "这个 request-id 已经确认发送过，直接返回上次的结果，没有重发"
                    return result
                if previous in {"sending", "unknown"}:
                    return _output("unknown", request_id=request_id, to=to, conversation_id=record.get("conversation_id", ""),
                                   submitted_at=record.get("submitted_at"),
                                   detail="这个 request-id 上次的结果不确定，不会自动重发；请人工确认，确认没发出去后换一个 request-id")
            store.write({"request_id": request_id, "payload_sha256": payload_hash, "to": to, "status": "sending", "at": int(time.time())})

        if instance.config["send"]["mode"] == "docker" and not _desktop_running(instance):
            outcome = {"status": "failed", "code": "desktop_unavailable", "detail": "桌面容器没有运行，确定没有发出去"}
        else:
            try:
                db_storage = instance.db_storage()
                paths = _worker_paths(instance, db_storage, job_dir, docker=instance.config["send"]["mode"] == "docker")
                if not instance.keys_file.is_file():
                    raise ConfigError("还没有密钥文件；先运行 keys capture")
                job = {"mode": "dry_run" if args.dry_run and not args.calibrate else kind, "to": to,
                       "payload_sha256": payload_hash, "budget_seconds": max(timeout - 15, 15), "current": bool(args.current),
                       "confirm_base_seconds": instance.config["send"]["confirm_base_seconds"],
                       "id_salt": instance.id_salt().hex(), "id_mode": instance.config["export"]["id_mode"], **paths}
                if not instance.config["send"]["evidence"]:
                    job["evidence_dir"] = ""
                if instance.config["send"]["mode"] == "direct":
                    (instance.calibration_dir / "headers").mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write(job_dir / "job.json", json.dumps(job, ensure_ascii=False))
            except (ConfigError, OSError) as exc:
                outcome = {"status": "failed", "code": "config", "detail": str(exc) if isinstance(exc, ConfigError) else type(exc).__name__}
            else:
                outcome = run_worker(instance, job_dir, timeout=timeout, calibrate=args.calibrate)

        status = outcome.get("status", "unknown")
        extra = {key: outcome[key] for key in ("code", "stage", "reason", "receipt") if outcome.get(key)}
        result = _output(status, request_id=request_id, to=to, conversation_id=outcome.get("conversation_id", ""),
                         submitted_at=outcome.get("submitted_at"), detail=outcome.get("detail", ""), extra=extra or None)
        if store is not None:
            store.write({"request_id": request_id, "payload_sha256": payload_hash, "to": to, "status": status,
                         "conversation_id": result["conversation_id"], "submitted_at": result["submitted_at"],
                         "at": int(time.time()), "result": result})
        return result
    finally:
        if lock is not None:
            lock.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        _prune_evidence(instance)


def _terminated(signum, frame):
    raise RuntimeError("terminated by signal")


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, _terminated)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if not args.to:
            raise InvalidArguments("必须给 --to")
        has_content = args.text is not None or args.text_file is not None or args.image is not None
        if args.current and not args.calibrate:
            raise InvalidArguments("--current 只能和 --calibrate 一起用")
        if args.calibrate and (has_content or args.dry_run or args.request_id):
            raise InvalidArguments("--calibrate 不能和发送内容、--dry-run、--request-id 一起用")
        if not args.calibrate and not has_content and not args.dry_run:
            raise InvalidArguments("必须给 --text、--text-file 或 --image 之一")
        if args.text is not None and not args.text.strip():
            raise InvalidArguments("--text 不能为空")
        result = send(args)
    except InvalidArguments as exc:
        request_id, to = "", ""
        result = _output("invalid", request_id=request_id, to=to, detail=str(exc))
    except ConfigError as exc:
        result = _output("invalid", detail=str(exc))
    except Exception as exc:  # 走到这里还没动过界面之外的东西：send() 内部把运行 worker 的异常都接住了
        result = _output("unknown", detail=f"发送器内部错误（{type(exc).__name__}）；不确定是否提交过，请人工查看")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return EXIT_CODES.get(result.get("status"), 2)


if __name__ == "__main__":
    raise SystemExit(main())
