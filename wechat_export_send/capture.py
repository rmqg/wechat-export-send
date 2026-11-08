"""登录时捕获数据库密钥。只有 Linux 客户端、只对自己的账号；捕获需要读客户端进程内存，是高权限动作。

宿主部分（host_*）用 docker 启动一个短命的工具容器：无网络、只读根文件系统、共享桌面容器的
PID 命名空间。容器里的 worker（gdb_worker、native_worker）只把通过 HMAC 校验的密钥写进
keys/（权限 600），标准输出只有计数，绝不打印密钥。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile

from .config import ConfigError, Instance

SCAN_DIR = Path("/scan")
CLIENT_BINARY = Path("/opt/wechat/wechat")


# ---- 容器内：gdb 硬件断点 ----
def process_state(pid: int) -> dict:
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def capture_address(pid: int, binary: Path, wcdb_tool) -> int:
    with binary.open("rb") as stream:
        kind = struct.unpack_from("<H", stream.read(64), 16)[0]
    mappings = []
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[5] == str(binary):
            start, end = (int(x, 16) for x in fields[0].split("-"))
            mappings.append((start, end, fields[1], int(fields[2], 16)))
    # ELF 虚拟地址相对于加载基址，不是可执行段的起点
    bias = min(start - offset for start, end, perms, offset in mappings) if kind == 3 else 0
    address = bias + wcdb_tool.find_hook_offset(binary)
    segment = next((m for m in mappings if "x" in m[2] and m[0] <= address < m[1]), None)
    if segment is None:
        raise RuntimeError("Capture point is outside executable mappings")
    with binary.open("rb") as stream:
        stream.seek(segment[3] + address - segment[0])
        expected = stream.read(16)
    with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
        memory.seek(address)
        if memory.read(16) != expected:
            raise RuntimeError("Runtime capture point does not match the official binary")
    return address


def release_debugger(process, pid: int) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 硬件断点不会把客户端指令换成 INT3，强杀 gdb 不会留下被改的代码
                process.kill()
                process.wait(timeout=5)
    state = process_state(pid)
    if int(state.get("TracerPid", "0")) != 0:
        raise RuntimeError("Debugger did not detach; client state requires inspection")
    if state.get("State", "").startswith(("T", "t")):
        os.kill(pid, signal.SIGCONT)


def _gdb_script(pid: int, address: int, result_file: Path) -> str:
    return f'''set pagination off
set confirm off
set debuginfod enabled off
set auto-load safe-path /nonexistent
set print thread-events off
handle SIGPIPE nostop noprint pass
handle SIGUSR1 nostop noprint pass
handle SIGUSR2 nostop noprint pass
handle SIGALRM nostop noprint pass
attach {pid}
python
import gdb, json, os
class Capture(gdb.Breakpoint):
    def stop(self):
        candidates = []
        try:
            pointer = int(gdb.parse_and_eval("$rsi"))
            length = int(gdb.parse_and_eval("$rdx"))
            inferior = gdb.selected_inferior()
            if pointer and length == 32:
                candidates.append(inferior.read_memory(pointer, 32).tobytes().hex())
            if pointer:
                try:
                    size = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+16)"))
                    value = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+8)"))
                    if size == 32 and value:
                        candidates.append(inferior.read_memory(value, 32).tobytes().hex())
                except gdb.error:
                    pass
        except gdb.error:
            return False
        if not candidates:
            return False
        fd = os.open({str(result_file)!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(list(dict.fromkeys(candidates)), output)
        return True
Capture("*{address:#x}", type=gdb.BP_HARDWARE_BREAKPOINT)
print("CAPTURE_ARMED", flush=True)
end
continue
detach
quit
'''


def _publish(name: str, value: dict, uid: int, gid: int) -> None:
    destination = SCAN_DIR / name
    staging = destination.with_suffix(".tmp")
    staging.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(staging, 0o600)
    os.chown(staging, uid, gid)
    staging.replace(destination)


def gdb_worker(args) -> int:
    sys.path.insert(0, "/app/vendor/wcdb_key_tool")
    import wcdb_key_tool as tool
    os.umask(0o077)
    uid, gid = int(os.environ.get("WES_UID", "0")), int(os.environ.get("WES_GID", "0"))
    pid = tool._find_wechat_pid()
    state = process_state(pid)
    if int(state.get("TracerPid", "0")) or state.get("State", "").startswith(("T", "t")):
        raise RuntimeError("Client is already traced or stopped; refusing to attach")
    address = capture_address(pid, CLIENT_BINARY, tool)
    if args.check:
        print(json.dumps({"capture_point_located": True, "debugger_attached": False, "binary_matches": True}))
        return 0
    with tempfile.TemporaryDirectory(prefix="capture-", dir=str(SCAN_DIR)) as temporary:
        folder = Path(temporary)
        result_file = folder / "candidates.json"
        script = folder / "capture.gdb"
        script.write_text(_gdb_script(pid, address, result_file), encoding="utf-8")
        print("等待在这个客户端窗口里重新登录（手机确认）；不会发送任何消息。", flush=True)
        with (folder / "gdb.log").open("w+") as log:
            process = subprocess.Popen(["gdb", "-q", "--nx", "--batch", "-x", str(script)], stdout=log, stderr=subprocess.STDOUT)
            try:
                process.wait(timeout=args.timeout)
            finally:
                release_debugger(process, pid)
            if not result_file.exists():
                log.seek(0)
                detail = log.read()
                if "hardware break" in detail.lower() or "Cannot insert" in detail:
                    raise RuntimeError("Hardware capture point was not supported; no software breakpoint fallback used")
                raise RuntimeError("Login-time capture did not complete")
        candidates = json.loads(result_file.read_text())
        db_files, salts = tool.collect_db_files(args.db_dir)
        key_map, valid = {}, None
        for candidate in candidates:
            derived = tool._derive_keys_from_passphrase(bytes.fromhex(candidate), db_files, salts)
            if derived:
                key_map, valid = derived, candidate
                break
        if not key_map:
            raise RuntimeError("Captured candidates did not pass database HMAC verification")
        keys = {rel: {"enc_key": key_map[salt], "salt": salt} for rel, path, size, salt, page in db_files if salt in key_map}
        if not any(rel.startswith("message/message_") and not rel.endswith("_fts.db") for rel in keys):
            raise RuntimeError("No verified message database key; refusing to publish partial setup")
        keys["_db_dir"] = args.db_dir
        _publish("all_keys.json", keys, uid, gid)
        _publish("passphrase.json", {"passphrase": valid}, uid, gid)
        print(json.dumps({"verified_databases": len(keys) - 1, "database_files": len(db_files),
                          "debugger_detached": True, "message_bodies_read": False}), flush=True)
    return 0


# ---- 容器内：ptrace 原生捕获（硬件断点不可用时的备选，需要重启客户端） ----
def native_worker(args) -> int:
    sys.path.insert(0, "/app/vendor")
    import logging
    from wxextract.keys import collect_dbs
    from wxextract.passphrase import capture_passphrase, derive_keys
    os.umask(0o077)
    uid, gid = int(os.environ.get("WES_UID", "0")), int(os.environ.get("WES_GID", "0"))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    db_dir = Path(args.db_dir)
    databases, _ = collect_dbs(db_dir)
    probe = next((db for db in databases if db.rel.replace("\\", "/") == "message/message_0.db"), None)
    if probe is None:
        raise RuntimeError("No existing message database to validate the captured material")
    password = capture_passphrase(CLIENT_BINARY, timeout=args.timeout, probe_page1=probe.page1,
                                  launch_cmd=[sys.executable, "-m", "wechat_export_send.capture", "--start-client"])
    databases, _ = collect_dbs(db_dir)  # 登录后可能有新库或新盐，重新盘点
    raw_keys = derive_keys(password, databases, parallel=False)
    if "message/message_0.db" not in raw_keys:
        raise RuntimeError("No HMAC-verified message key; refusing to publish setup")
    keys = {db.rel.replace("\\", "/"): {"enc_key": raw_keys[db.rel], "salt": db.salt_hex} for db in databases if db.rel in raw_keys}
    keys["_db_dir"] = str(db_dir)
    _publish("all_keys.json", keys, uid, gid)
    _publish("passphrase.json", {"passphrase": password.hex()}, uid, gid)
    print(json.dumps({"verified_databases": len(keys) - 1, "databases": len(databases),
                      "hook_detached": True, "message_bodies_read": False}), flush=True)
    return 0


def start_client_rpc() -> int:
    """经桌面容器里 supervisord 的私有 socket，只启动这个实例自己的微信进程。"""
    import http.client
    import socket
    import xmlrpc.client

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(15)
            self.sock.connect("/config/.supervisor/supervisor.sock")

    class Transport(xmlrpc.client.Transport):
        def make_connection(self, host):
            return Connection(host)

    xmlrpc.client.ServerProxy("http://local", transport=Transport()).supervisor.startProcess("wechat", False)
    return 0


# ---- 容器内：V2 图片密钥（只读内存，不暂停、不附加调试器） ----
def image_key_worker(args) -> int:
    import io
    import re
    from . import media
    from Crypto.Cipher import AES
    from PIL import Image

    def check_key(key: bytes, samples: list[bytes]):
        first = samples[0]
        kind = media.detect_image_format(AES.new(key, AES.MODE_ECB).decrypt(first[15:31]))
        if kind not in {"jpg", "png", "gif", "webp", "hevc"}:
            return None
        xor_values = {0x88}
        if kind == "jpg":
            xor_values.add(first[-1] ^ 0xD9)
        if kind == "png":
            xor_values.add(first[-1] ^ 0x82)
        for xor in xor_values:
            valid = 0
            for sample in samples:
                decoded, fmt = media.decrypt_v2(sample, key, xor)
                if not decoded:
                    continue
                if fmt == "hevc":
                    valid += 1
                    continue
                try:
                    with Image.open(io.BytesIO(decoded)) as image:
                        image.load()
                    valid += 1
                except Exception:
                    pass
            if valid == len(samples):
                return xor
        return None

    os.umask(0o077)
    uid, gid = int(os.environ.get("WES_UID", "0")), int(os.environ.get("WES_GID", "0"))
    samples = []
    for path in sorted(Path(args.account_dir, "msg", "attach").rglob("*.dat")):
        if path.read_bytes()[:6] == media.V2_MAGIC:
            samples.append(path.read_bytes())
        if len(samples) >= 3:
            break
    if not samples or any(len(sample) < 31 for sample in samples):
        raise SystemExit("没有完整的 V2 图片样本；先在客户端里打开几张图片")
    pids = []
    for folder in Path("/proc").iterdir():
        if folder.name.isdigit():
            try:
                if os.readlink(folder / "exe") == str(CLIENT_BINARY):
                    pids.append(int(folder.name))
            except OSError:
                pass
    found: set[bytes] = set()
    pattern = re.compile(rb"[0-9a-f]{16,64}")
    for pid in pids:
        regions = []
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            fields = line.split()
            if "r" not in fields[1] or (len(fields) >= 6 and fields[5].startswith(("/usr/lib/", "/lib/", "/usr/share/"))):
                continue
            start, end = (int(v, 16) for v in fields[0].split("-"))
            if 0 < end - start < 512 * 1024 * 1024:
                regions.append((start, end))
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            for start, end in regions:
                position, carry = start, b""
                while position < end:
                    try:
                        memory.seek(position)
                        block = memory.read(min(8 * 1024 * 1024, end - position))
                    except OSError:
                        break
                    if not block:
                        break
                    data = carry + block
                    for match in pattern.finditer(data):
                        value = match.group(0)
                        for offset in range(0, len(value) - 15, 16):
                            candidate = value[offset:offset + 16]
                            if candidate in found:
                                continue
                            found.add(candidate)
                            xor = check_key(candidate, samples)
                            if xor is not None:
                                _publish("image_key.json", {"image_aes_key": candidate.decode(), "image_xor_key": xor}, uid, gid)
                                print(json.dumps({"validated_image_samples": len(samples), "process_paused": False, "key_printed": False}), flush=True)
                                return 0
                    carry = data[-64:]
                    position += len(block)
    raise SystemExit(f"没有找到能通过 {len(samples)} 张本地样本校验的图片密钥；客户端状态没有改动")


# ---- 宿主 ----
def _client_root(instance: Instance) -> Path:
    root = instance.env().get("CLIENT_ROOT")
    if not root:
        raise ConfigError("instance.env 里没有 CLIENT_ROOT；先运行 client install")
    return Path(root)


def host_run(instance: Instance, mode: str, *, timeout: int = 300, check: bool = False) -> int:
    """mode: gdb、native、image-key。"""
    database = instance.db_storage()
    if not any(database.rglob("*.db")):
        raise ConfigError("客户端还没有建好本地数据库；先在 noVNC 里登录一次")
    keys_dir = instance.keys_file.parent
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(keys_dir, 0o700)
    relative = database.relative_to(instance.gui).as_posix()
    container_database = f"/config/{relative}"
    # DAC_OVERRIDE：keys/ 归普通用户、权限 700，容器里的 root 没有它写不进去
    capabilities = ["SYS_PTRACE", "DAC_READ_SEARCH", "DAC_OVERRIDE"]
    name = f"wes-{instance.name}-capture"
    command = ["docker", "run", "--rm", "--pull", "never", "--name", name, "--network", "none",
               "--pid", f"container:{instance.container}", "--user", "0:0", "--read-only", "--cap-drop", "ALL"]
    for capability in capabilities:
        command += ["--cap-add", capability]
    command += ["--cap-add", "CHOWN", "--security-opt", "no-new-privileges:true", "--memory", "1536m", "--cpus", "1",
                "--env", f"WES_UID={instance.owner()[0]}", "--env", f"WES_GID={instance.owner()[1]}", "--env", "PYTHONPATH=/app",
                "--mount", f"type=bind,src={instance.gui},dst=/config,readonly",
                "--mount", f"type=bind,src={keys_dir},dst=/scan",
                "--mount", f"type=bind,src={_client_root(instance)}/opt/wechat,dst=/opt/wechat,readonly",
                "--tmpfs", "/tmp:rw,nosuid,size=64m", instance.config["send"]["tools_image"],
                "-m", "wechat_export_send.capture", "--worker", mode, "--db-dir", container_database,
                "--account-dir", str(Path(container_database).parent), "--timeout", str(timeout)]
    if check:
        command.append("--check")
    try:
        result = subprocess.run(command, timeout=timeout + 150)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        subprocess.run(["docker", "stop", "--time", "20", name], capture_output=True, timeout=30)
        if mode == "native":
            restart_client(instance)
        raise
    if result.returncode and mode == "native":
        restart_client(instance)  # 软件断点失败时不能留下被改过的指令，从只读镜像重启这一个客户端
    return result.returncode


def restart_client(instance: Instance) -> None:
    subprocess.run(["docker", "exec", instance.container, "supervisorctl", "-c", "/deployment/supervisord.conf", "restart", "wechat"],
                   timeout=45, check=False)


def stop_client(instance: Instance) -> None:
    subprocess.run(["docker", "exec", instance.container, "supervisorctl", "-c", "/deployment/supervisord.conf", "stop", "wechat"],
                   timeout=45, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="容器内的捕获 worker，用 wechat-export-send keys 调用")
    parser.add_argument("--worker", choices=["gdb", "native", "image-key"])
    parser.add_argument("--start-client", action="store_true")
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--account-dir", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.start_client:
        return start_client_rpc()
    if args.worker == "gdb":
        return gdb_worker(args)
    if args.worker == "native":
        return native_worker(args)
    if args.worker == "image-key":
        return image_key_worker(args)
    parser.error("需要 --worker")
    return 2


if __name__ == "__main__":
    def _stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    raise SystemExit(main())
