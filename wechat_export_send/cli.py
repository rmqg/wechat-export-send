"""wechat-export-send 命令行。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import os
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


def _compose(instance: Instance, *arguments: str) -> list[str]:
    return ["docker", "compose", "--project-name", f"wes-{instance.name}", "--project-directory", str(ROOT),
            "--env-file", str(instance.dir / "instance.env"), "-f", str(ROOT / "compose.yaml"), *arguments]


def instance_compose(args, *arguments: str) -> int:
    instance = Instance.load(args.name)
    env = {}
    for line in (instance.dir / "instance.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
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
def export(args) -> int:
    from . import exporter
    instance = Instance.load(args.instance)
    while True:
        try:
            stats = exporter.export_instance(instance)
        except exporter.ExportBusy as exc:
            print(str(exc), file=sys.stderr)
            return 3
        _print_json({"time": int(time.time()), **stats})
        if not args.watch:
            return 1 if stats["errors"] and not stats["new_lines"] and not stats["conversations"] else 0
        time.sleep(int(instance.config["export"]["interval_seconds"]))


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

    item = sub.add_parser("instance", help="管理实例（一个实例一个账号、一个容器）")
    actions = item.add_subparsers(dest="action", required=True)
    action = actions.add_parser("create")
    action.add_argument("name")
    action.add_argument("--vnc-port", type=int)
    action.set_defaults(func=instance_create)
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
    item.set_defaults(func=export)

    item = sub.add_parser("conversations", help="列出已导出的会话：id、类型、条数、名字")
    item.add_argument("--instance", required=True)
    item.set_defaults(func=conversations)

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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
