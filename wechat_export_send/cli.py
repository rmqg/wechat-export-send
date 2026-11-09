"""wechat-export-send 命令行。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import time

from . import ROOT, __version__
from .config import DEFAULTS, NAME_RE, ConfigError, Instance, atomic_write, instances_root


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=1))


def client_root() -> Path:
    return Path(os.environ.get("WES_CLIENT_DIR") or ROOT / "client").resolve()


# ---- 实例 ----
def _free_port(start: int) -> int:
    used = set()
    root = instances_root()
    if root.is_dir():
        for path in root.iterdir():
            try:
                for line in (path / "instance.env").read_text(encoding="utf-8").splitlines():
                    if line.startswith("VNC_WEB_PORT="):
                        used.add(int(line.split("=", 1)[1]))
            except (OSError, ValueError):
                pass
    port = start
    while port in used or _port_busy(port):
        port += 1
    return port


def _port_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _current_client() -> dict:
    try:
        return json.loads((client_root() / "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_env(instance_dir: Path, name: str, port: int) -> None:
    client = _current_client()
    lines = [f"INSTANCE={name}", f"INSTANCE_DIR={instance_dir}", f"VNC_WEB_PORT={port}", "VNC_BIND=127.0.0.1",
             f"PUID={os.getuid()}", f"PGID={os.getgid()}", "TZ=Asia/Shanghai",
             f"CLIENT_ROOT={client.get('root', '')}", f"WECHAT_BINARY_SHA256={client.get('executable_sha256', '')}",
             f"DESKTOP_IMAGE=wechat-export-send-desktop:{__version__}", "DESKTOP_MEM_LIMIT=2g", "DESKTOP_CPUS=1.5", ""]
    atomic_write(instance_dir / "instance.env", "\n".join(lines))


def instance_create(args) -> int:
    if not NAME_RE.fullmatch(args.name):
        raise ConfigError("实例名只能用小写字母、数字、- 和 _，以字母或数字开头，不超过 31 个字符")
    directory = instances_root() / args.name
    if (directory / "config.json").exists():
        raise ConfigError(f"实例已存在：{args.name}")
    for sub in ("gui", "keys", "export", "state", "calibration/headers"):
        (directory / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(instances_root(), 0o700)
    os.chmod(directory, 0o700)
    atomic_write(directory / "config.json", json.dumps({"instance": args.name}, ensure_ascii=False, indent=2) + "\n")
    port = args.vnc_port or _free_port(17900)
    _write_env(directory, args.name, port)
    print(f"已创建实例 {args.name}：{directory}\nnoVNC 端口（只绑 127.0.0.1）：{port}")
    if not _current_client():
        print("还没有安装微信客户端：wechat-export-send client install <WeChatLinux_x86_64.deb>")
    print(f"下一步：wechat-export-send instance up {args.name}")
    return 0


def instance_attach(args) -> int:
    """对接一个已经在跑、已经登录的微信容器。容器和它的数据都不动，只读挂载；导出和状态放在实例自己的目录。"""
    if not NAME_RE.fullmatch(args.name):
        raise ConfigError("实例名只能用小写字母、数字、- 和 _，以字母或数字开头，不超过 31 个字符")
    directory = instances_root() / args.name
    if (directory / "config.json").exists():
        raise ConfigError(f"实例已存在：{args.name}")
    gui = Path(args.gui).resolve()
    keys = Path(args.keys).resolve()
    if not gui.is_dir():
        raise ConfigError(f"找不到客户端数据目录：{gui}")
    if not keys.is_file():
        raise ConfigError(f"找不到密钥文件：{keys}")
    info = gui.stat()
    uid = args.puid if args.puid is not None else info.st_uid
    gid = args.pgid if args.pgid is not None else info.st_gid
    for sub in ("keys", "export", "state", "calibration/headers"):
        (directory / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(instances_root(), 0o711 if os.getuid() == 0 else 0o700)
    os.chmod(directory, 0o700)
    config = {"instance": args.name, "gui_dir": str(gui),
              "desktop": {"managed": False, "container": args.container, "docker_host": args.docker_host or "", "network": args.network or ""}}
    if args.account_dir:
        config["account_dir"] = args.account_dir
    if args.runner:
        config["export"] = {"runner": args.runner}
    atomic_write(directory / "config.json", json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    atomic_write(directory / "instance.env", f"INSTANCE={args.name}\nINSTANCE_DIR={directory}\nPUID={uid}\nPGID={gid}\n")
    atomic_write(directory / "keys" / "all_keys.json", keys.read_bytes())
    if args.image_key:
        atomic_write(directory / "keys" / "image_key.json", Path(args.image_key).read_bytes())
    calibration = json.loads((ROOT / "config" / "ui-calibration.json").read_text(encoding="utf-8"))
    if args.display:
        calibration["display"] = args.display
    atomic_write(directory / "calibration" / "ui-calibration.json", json.dumps(calibration, ensure_ascii=False, indent=2) + "\n")
    instance = Instance.load(args.name)
    instance.give_to_owner(directory, recursive=True)
    print(f"已对接实例 {args.name}：{directory}\n容器 {args.container} 由它原来的服务管理，这里不会启动或停止它。")
    return 0


def run_in_tools(instance: Instance, arguments: list[str]) -> int:
    """在工具镜像里跑一次导出类任务：无网络、内存有上限。宿主没有依赖或者内存紧时用它。"""
    export = instance.config["export"]
    uid, gid = instance.owner()
    home = str(instances_root())
    state = instance.state
    if not state.exists():
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        instance.give_to_owner(state)
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--memory", f"{int(export['container_memory_mb'])}m", "--cpus", "1", "--user", f"{uid}:{gid}",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--env", "PYTHONPATH=/app", "--env", f"WES_HOME={home}",
               "--env", f"SQLITE_TMPDIR={state}", "--env", "TZ=UTC",
               "--mount", f"type=bind,src={instance.dir},dst={instance.dir}"]
    if not instance.gui.is_relative_to(instance.dir):
        command += ["--mount", f"type=bind,src={instance.gui},dst={instance.gui},readonly"]
    if not instance.export_root.is_relative_to(instance.dir):
        command += ["--mount", f"type=bind,src={instance.export_root},dst={instance.export_root}"]
    command += [instance.config["send"]["tools_image"], "-m", "wechat_export_send.cli", *arguments]
    return subprocess.call(command)


def _compose(instance: Instance, *arguments: str) -> list[str]:
    return ["docker", "compose", "--project-name", f"wes-{instance.name}", "--project-directory", str(ROOT),
            "--env-file", str(instance.dir / "instance.env"), "-f", str(ROOT / "compose.yaml"), *arguments]


def instance_compose(args, *arguments: str) -> int:
    instance = Instance.load(args.name)
    if not instance.config["desktop"]["managed"]:
        raise ConfigError(f"实例 {instance.name} 对接的是已有容器 {instance.container}，由它原来的服务管理，这里不启停")
    env = instance.env()
    if arguments[0] == "up" and not env.get("CLIENT_ROOT"):
        current = _current_client()
        if not current:
            raise ConfigError("还没有安装微信客户端：wechat-export-send client install <deb>")
        _write_env(instance.dir, instance.name, int(env.get("VNC_WEB_PORT") or _free_port(17900)))
    return subprocess.call(_compose(instance, *arguments))


def instance_list(args) -> int:
    root = instances_root()
    names = sorted(path.name for path in root.iterdir() if (path / "config.json").is_file()) if root.is_dir() else []
    for name in names:
        print(name)
    return 0


# ---- 客户端 ----
def _read_ar(path: Path):
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ConfigError("这不是 deb 文件")
        while True:
            header = stream.read(60)
            if len(header) < 60:
                return
            name = header[:16].decode("ascii").strip().rstrip("/")
            size = int(header[48:58].decode("ascii").strip())
            yield name, stream.read(size)
            if size % 2:
                stream.read(1)


def _open_member(name: str, data: bytes) -> tarfile.TarFile:
    if name.endswith(".xz"):
        data = lzma.decompress(data)
    elif name.endswith(".zst"):
        try:
            import zstandard
        except ImportError:
            raise ConfigError("这个 deb 用 zstd 压缩，需要 zstandard：运行 ./install.sh") from None
        data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
    return tarfile.open(fileobj=io.BytesIO(data))


def _safe_extract(archive: tarfile.TarFile, target: Path) -> None:
    """解压时拒绝路径穿越和指向目录外面的链接。Python 3.11.4 以前的 tarfile 没有 filter 参数。"""
    try:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(target, filter="data")
            return
        root = target.resolve()

        def inside(path: Path) -> bool:
            return path == root or root in path.parents

        for member in archive.getmembers():
            destination = (root / member.name).resolve()
            if member.name.startswith("/") or not inside(destination):
                raise ConfigError("deb 里有指向目录外面的路径，拒绝解开")
            if member.isdev():
                raise ConfigError("deb 里有设备文件，拒绝解开")
            if member.issym() and not inside((destination.parent / member.linkname).resolve()):
                raise ConfigError("deb 里有指向目录外面的链接，拒绝解开")
            if member.islnk() and not inside((root / member.linkname).resolve()):
                raise ConfigError("deb 里有指向目录外面的链接，拒绝解开")
        archive.extractall(target)
    except tarfile.TarError as exc:
        raise ConfigError(f"deb 内容不安全或已损坏（{type(exc).__name__}），拒绝解开") from None


def client_install(args) -> int:
    """解开官方 deb（不执行里面的任何脚本），放到 client/<版本>/，记录哈希。"""
    deb = Path(args.deb).resolve()
    digest = hashlib.sha256()
    with deb.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    version = ""
    root = client_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    extracted = False
    try:
        for name, data in _read_ar(deb):
            if name.startswith("control.tar"):
                with _open_member(name, data) as archive:
                    member = next(item for item in archive.getmembers() if item.name.lstrip("./") == "control")
                    control = archive.extractfile(member).read().decode("utf-8", "replace")
                version = next((line.split(":", 1)[1].strip() for line in control.splitlines() if line.startswith("Version:")), "")
            elif name.startswith("data.tar"):
                with _open_member(name, data) as archive:
                    _safe_extract(archive, staging)
                extracted = True
        binary = staging / "opt" / "wechat" / "wechat"
        if not extracted or not binary.is_file():
            raise ConfigError("deb 里没有 opt/wechat/wechat；确认这是官方 Linux 版微信")
        executable = hashlib.sha256(binary.read_bytes()).hexdigest()
        version = version or executable[:12]
        target = root / version
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    info = {"version": version, "root": str(target), "deb_sha256": digest.hexdigest(), "executable_sha256": executable}
    atomic_write(root / "current.json", json.dumps(info, indent=1) + "\n", mode=0o644)
    from .doctor import tested_versions
    tested = {item["executable_sha256"] for item in tested_versions().get("clients", [])}
    _print_json({**info, "tested": executable in tested})
    if executable not in tested:
        print("注意：这不是实测过的版本（见 docs/COMPATIBILITY.md）。捕获可能失败，界面坐标可能要重新校准。", file=sys.stderr)
    return 0


# ---- 密钥 ----
def keys_check(args) -> int:
    from . import capture
    return capture.host_run(Instance.load(args.instance), "gdb", timeout=120, check=True)


def keys_capture(args) -> int:
    from . import capture
    instance = Instance.load(args.instance)
    if not args.i_own_this_account:
        raise ConfigError("捕获要读微信进程内存。确认这是你自己的账号后加 --i-own-this-account")
    if args.method == "native":
        capture.stop_client(instance)
    return capture.host_run(instance, args.method, timeout=args.timeout)


def keys_image(args) -> int:
    from . import capture
    instance = Instance.load(args.instance)
    if not args.i_own_this_account:
        raise ConfigError("这一步要读微信进程内存。确认这是你自己的账号后加 --i-own-this-account")
    return capture.host_run(instance, "image-key", timeout=args.timeout)


# ---- 导出 ----
def _avatar_download(instance: Instance, *, limit: int | None = None, max_seconds: float | None = None) -> int:
    """下载头像。容器模式下用单独的容器，只挂导出目录，拿不到密钥和数据库。输出走 stderr，stdout 留给 export 的 JSON。"""
    from . import avatars

    settings = instance.config["export"]["avatar_download"]
    state_root = instance.state / "export"
    arguments = ["--export-root", str(instance.export_root), "--state-root", str(state_root),
                 "--limit", str(settings["per_run"] if limit is None else limit),
                 "--delay", str(settings["delay_seconds"]),
                 "--max-seconds", str(settings["max_seconds"] if max_seconds is None else max_seconds),
                 "--timeout", str(settings["timeout_seconds"])]
    if instance.config["export"]["runner"] != "container":
        return avatars.main(arguments)
    uid, gid = instance.owner()
    return subprocess.call(
        ["docker", "run", "--rm", "--pull", "never", "--network", settings["network"], "--read-only",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "256m", "--cpus", "0.5",
         "--user", f"{uid}:{gid}", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m", "--env", "PYTHONPATH=/app", "--env", "TZ=UTC",
         "--mount", f"type=bind,src={instance.export_root},dst={instance.export_root}",
         "--mount", f"type=bind,src={state_root},dst={state_root}",
         instance.config["send"]["tools_image"], "-m", "wechat_export_send.avatars", *arguments],
        stdout=sys.stderr)


def _download_avatars_quietly(instance: Instance) -> None:
    """导出之后顺手下载头像。失败不影响导出的结果。"""
    from . import avatars

    export = instance.config["export"]
    if not export.get("avatars") or not export["avatar_download"]["enabled"]:
        return
    try:
        if avatars.has_due(instance.state / "export"):
            _avatar_download(instance)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"头像下载没有完成：{type(exc).__name__}", file=sys.stderr)


def fetch_avatars(args) -> int:
    return _avatar_download(Instance.load(args.instance), limit=args.limit, max_seconds=args.max_seconds)


def export(args) -> int:
    instance = Instance.load(args.instance)
    if not args.in_process and (args.container or instance.config["export"]["runner"] == "container"):
        while True:
            code = run_in_tools(instance, ["export", "--instance", instance.name, "--in-process"])
            _download_avatars_quietly(instance)
            if not args.watch:
                return code
            time.sleep(int(instance.config["export"]["interval_seconds"]))
    from . import exporter
    while True:
        try:
            stats = exporter.export_instance(instance)
        except exporter.ExportBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        _print_json({"time": int(time.time()), **stats})
        if not args.in_process:
            _download_avatars_quietly(instance)
        if not args.watch:
            return 1 if stats["errors"] and not stats["new_lines"] and not stats["conversations"] else 0
        time.sleep(int(instance.config["export"]["interval_seconds"]))


def verify_export(args) -> int:
    instance = Instance.load(args.instance)
    if not args.in_process and (args.container or instance.config["export"]["runner"] == "container"):
        return run_in_tools(instance, ["verify-export", "--instance", instance.name, "--in-process"])
    from . import verify
    result = verify.verify_instance(instance)
    _print_json(result)
    return 0 if result["ok"] else 1


def conversations(args) -> int:
    instance = Instance.load(args.instance)
    manifest = instance.export_root / "manifest.json"
    if not manifest.is_file():
        raise ConfigError("还没有导出过；先运行 export")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    for item in value["conversations"]:
        print(f"{item['id']}\t{item['kind']}\t{item['message_count']}\t{item['name']}")
    return 0


def fetch_originals(args) -> int:
    from . import originals
    _print_json(originals.fetch_pending(Instance.load(args.instance), limit=args.limit))
    return 0


def serve_media(args) -> int:
    from . import media_origin
    media_origin.serve(media_origin.config_for_instance(Instance.load(args.instance)))
    return 0


def watchdog_tick(args) -> int:
    from . import watchdog
    instance = Instance.load(args.instance)
    if args.test_notify:
        sent = watchdog.notify_bark(instance.config["watchdog"], f"微信 {instance.name} 测试通知", "这是测试，不代表发生故障。",
                                    instance.config["watchdog"].get("recovery_level", "active"))
        _print_json({"status": "test", "notified": sent})
        return 0 if sent else 1
    _print_json(watchdog.tick(instance))
    return 0


def doctor(args) -> int:
    from . import doctor as doctor_module
    return doctor_module.report(doctor_module.run(args.instance), args.json)


def demo(args) -> int:
    from . import demo as demo_module
    output = Path(args.output) if args.output else Path.cwd() / "demo-output"
    result = demo_module.run(output)
    if args.json:
        _print_json(result)
    else:
        for item in result["checks"]:
            print(("  ok  " if item["passed"] else "失败  ") + item["check"])
        print(f"\n导出结果在：{result['output']}（全部是虚构数据）")
        if not result["fast_aes"]:
            print("提示：没装 PyCryptodome，这次用的是很慢的纯 Python AES。真实数据库请先运行 ./install.sh。")
    return 0 if result["ok"] else 1


def show_defaults(args) -> int:
    _print_json(DEFAULTS)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechat-export-send", description="在 Linux 服务器上用容器挂微信、导出聊天记录")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("demo", help="离线 demo：不用账号、不联网")
    item.add_argument("--output", help="输出目录，默认 ./demo-output，必须为空")
    item.add_argument("--json", action="store_true")
    item.set_defaults(func=demo)

    item = sub.add_parser("doctor", help="自检依赖、权限、配置、连通性")
    item.add_argument("--instance")
    item.add_argument("--json", action="store_true")
    item.set_defaults(func=doctor)

    item = sub.add_parser("setup", help="安装向导：检查环境，一步步建实例、构建镜像、装客户端、启动桌面")
    item.add_argument("--answers", help="答案文件（见 install.env.example），给了就不提问")
    item.add_argument("--non-interactive", action="store_true")
    item.add_argument("--check-only", action="store_true", help="只检查环境")
    item.set_defaults(func=_setup)

    item = sub.add_parser("instance", help="管理实例（一个实例一个账号、一个容器）")
    actions = item.add_subparsers(dest="action", required=True)
    action = actions.add_parser("create")
    action.add_argument("name")
    action.add_argument("--vnc-port", type=int)
    action.set_defaults(func=instance_create)
    action = actions.add_parser("attach", help="对接已经在跑、已经登录的微信容器，不启停它")
    action.add_argument("name")
    action.add_argument("--container", required=True, help="桌面容器的名字")
    action.add_argument("--gui", required=True, help="容器的 HOME 在宿主上的目录（含 xwechat_files），只读挂载")
    action.add_argument("--keys", required=True, help="all_keys.json，复制一份进实例目录")
    action.add_argument("--docker-host", help="桌面容器不在默认 dockerd 时填，例如 unix:///run/other.sock")
    action.add_argument("--network", help="worker 的网络，默认 container:<容器>；容器用 host 网络时填 host")
    action.add_argument("--image-key", help="可选：image_key.json。没有时从客户端配置里算")
    action.add_argument("--display", help="X 显示，默认 :99")
    action.add_argument("--account-dir", help="有多个账号目录时指定")
    action.add_argument("--puid", type=int, help="容器用户，默认取 gui 目录的属主")
    action.add_argument("--pgid", type=int)
    action.add_argument("--runner", choices=["host", "container"], help="导出跑在宿主还是工具容器；宿主没有依赖时选 container")
    action.set_defaults(func=instance_attach)
    actions.add_parser("list").set_defaults(func=instance_list)
    for verb, arguments in (("up", ("up", "-d")), ("down", ("down",)), ("status", ("ps",)), ("logs", ("logs", "--tail", "100"))):
        action = actions.add_parser(verb)
        action.add_argument("name")
        action.set_defaults(func=lambda a, compose_args=arguments: instance_compose(a, *compose_args))

    item = sub.add_parser("client", help="安装官方 Linux 版微信 deb（自己下载，本项目不分发）")
    actions = item.add_subparsers(dest="action", required=True)
    action = actions.add_parser("install")
    action.add_argument("deb")
    action.set_defaults(func=client_install)

    item = sub.add_parser("keys", help="检查捕获点、捕获数据库和图片密钥")
    actions = item.add_subparsers(dest="action", required=True)
    action = actions.add_parser("check", help="只检查捕获点，不附加调试器")
    action.add_argument("--instance", required=True)
    action.set_defaults(func=keys_check)
    action = actions.add_parser("capture", help="登录时捕获；要在 noVNC 里配合手机确认")
    action.add_argument("--instance", required=True)
    action.add_argument("--method", choices=["gdb", "native"], default="gdb")
    action.add_argument("--timeout", type=int, default=300)
    action.add_argument("--i-own-this-account", action="store_true")
    action.set_defaults(func=keys_capture)
    action = actions.add_parser("image-key", help="客户端配置算不出图片密钥时，从内存里找")
    action.add_argument("--instance", required=True)
    action.add_argument("--timeout", type=int, default=300)
    action.add_argument("--i-own-this-account", action="store_true")
    action.set_defaults(func=keys_image)

    item = sub.add_parser("export", help="增量导出成 chat-export/v1")
    item.add_argument("--instance", required=True)
    item.add_argument("--watch", action="store_true", help="一直运行，间隔见 export.interval_seconds")
    item.add_argument("--container", action="store_true", help="在工具容器里跑（无网络，内存上限 export.container_memory_mb）")
    item.add_argument("--in-process", action="store_true", help=argparse.SUPPRESS)
    item.set_defaults(func=export)

    item = sub.add_parser("verify-export", help="拿解密后的数据库核对导出的条数，只输出计数，不输出内容")
    item.add_argument("--instance", required=True)
    item.add_argument("--container", action="store_true")
    item.add_argument("--in-process", action="store_true", help=argparse.SUPPRESS)
    item.set_defaults(func=verify_export)

    item = sub.add_parser("conversations", help="列出已导出的会话：id、类型、条数、名字")
    item.add_argument("--instance", required=True)
    item.set_defaults(func=conversations)

    item = sub.add_parser("fetch-avatars", help="下载本地没有缓存的头像（导出之后也会自动少量下载）")
    item.add_argument("--instance", default="main")
    item.add_argument("--limit", type=int, help="这次最多下载几个")
    item.add_argument("--max-seconds", type=float, help="这次最多运行多少秒")
    item.set_defaults(func=fetch_avatars)

    item = sub.add_parser("fetch-originals", help="可选：从腾讯 CDN 下载 pending 的原图（默认关闭）")
    item.add_argument("--instance", required=True)
    item.add_argument("--limit", type=int)
    item.set_defaults(func=fetch_originals)

    item = sub.add_parser("serve-media", help="可选：在 127.0.0.1 上凭 token 提供导出的媒体文件")
    item.add_argument("--instance", required=True)
    item.set_defaults(func=serve_media)

    item = sub.add_parser("watchdog", help="跑一次登录看门狗；建议每分钟由 cron 或 timer 调用")
    item.add_argument("--instance", required=True)
    item.add_argument("--test-notify", action="store_true")
    item.set_defaults(func=watchdog_tick)

    sub.add_parser("defaults", help="打印所有配置项的默认值").set_defaults(func=show_defaults)
    return parser


def _setup(args) -> int:
    from .setup_wizard import main as setup_main
    return setup_main((["--answers", args.answers] if args.answers else []) + (["--non-interactive"] if args.non_interactive else [])
                      + (["--check-only"] if args.check_only else []))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except PermissionError as exc:
        print(f"错误：没有权限（{exc.filename or exc}）。实例目录属于谁就用谁运行；用 root 建的实例要用 root（sudo）运行。"
              "\n  先运行 ./bin/wechat-export-send doctor 看具体是哪一项。", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"错误：找不到 {exc.filename or exc}。确认路径，或先运行 ./bin/wechat-export-send doctor。", file=sys.stderr)
        return 1
    except Exception as exc:  # 最后一道：不让新手只看到英文异常栈
        if os.environ.get("WES_DEBUG"):
            raise
        text = str(exc) if re.search(r"[\u4e00-\u9fff]", str(exc)) else f"{type(exc).__name__}: {exc}"
        print(f"错误：{text}\n  先运行 ./bin/wechat-export-send doctor，它会列出每个问题和修复命令；"
              "\n  还不行就加 WES_DEBUG=1 重跑这条命令看完整信息，再按 docs/TROUBLESHOOTING.md 排查。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
