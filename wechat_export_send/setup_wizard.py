"""安装向导：检查环境，一步步问清楚，替你建实例、构建镜像、装客户端、启动桌面。

install.sh 在装依赖之前调用 ``--check-only``（只用标准库），装完依赖后调用向导本体。
以后也可以单独运行：``./bin/wechat-export-send setup``。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from . import ROOT, __version__

ANSWER_KEYS = {"WES_MODE", "WES_INSTANCE", "WES_EXPORT_INCLUDE", "WES_BUILD_IMAGES", "WES_CLIENT_DEB", "WES_START"}
OK, WARN, FAIL = "ok", "warn", "fail"


# ---------------------------------------------------------------- 环境检查

def _os_release() -> dict:
    info: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key] = value.strip().strip('"')
    except OSError:
        pass
    return info


def _run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__


def _sudo() -> str:
    return "" if os.geteuid() == 0 else "sudo "


def docker_install_hint() -> str:
    osr = _os_release()
    if osr.get("ID") == "ubuntu":
        return f"{_sudo()}apt-get update && {_sudo()}apt-get install -y docker.io docker-compose-v2 && {_sudo()}systemctl enable --now docker"
    return f"curl -fsSL https://get.docker.com | {_sudo()}sh   （Docker 官方安装脚本，Engine 和 Compose 插件一起装）"


def compose_install_hint() -> str:
    if _os_release().get("ID") == "ubuntu":
        return f"{_sudo()}apt-get install -y docker-compose-v2"
    return f"{_sudo()}apt-get install -y docker-compose-plugin   （来自 Docker 官方源；没有这个包就用：curl -fsSL https://get.docker.com | {_sudo()}sh）"


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return -1


def environment() -> list[dict]:
    items: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        items.append({"name": name, "status": status, "detail": detail, "fix": fix})

    osr = _os_release()
    add("系统", OK, osr.get("PRETTY_NAME") or platform.system())
    add("内核", OK, platform.release())
    machine = platform.machine()
    add("CPU 架构", OK if machine in {"x86_64", "amd64"} else WARN, machine,
        "" if machine in {"x86_64", "amd64"} else "离线 demo 能跑；接真实账号要 x86_64 机器（Linux 版微信只测过 x86_64）")
    if sys.version_info >= (3, 10):
        add("Python", OK, platform.python_version())
    else:
        add("Python", FAIL, f"{platform.python_version()}，需要 3.10 或更新", "换 Debian 12、Ubuntu 22.04 或更新的系统")
    has_venv = _run([sys.executable, "-c", "import ensurepip, venv"])[0] == 0
    has_pip = _run([sys.executable, "-m", "pip", "--version"])[0] == 0
    if has_venv or has_pip:
        add("Python 虚拟环境", OK, "可以装依赖" + ("（venv）" if has_venv else "（pip）"))
    else:
        add("Python 虚拟环境", WARN, "没有 venv 也没有 pip：装不了依赖，demo 能跑但很慢，真实账号不行",
            f"{_sudo()}apt-get update && {_sudo()}apt-get install -y python3-venv，然后重新运行 ./install.sh")
    docker = shutil.which("docker")
    if docker is None:
        add("Docker", WARN, "没有安装：离线 demo 不需要，接真实账号需要", docker_install_hint())
    else:
        code, text = _run(["docker", "version", "--format", "{{.Server.Version}}"])
        if code == 0:
            add("Docker", OK, f"服务端 {text.splitlines()[0] if text else '?'}")
        elif "permission denied" in text.lower():
            add("Docker", WARN, "当前用户没有权限用 Docker",
                f"sudo usermod -aG docker {os.environ.get('USER', '你的用户名')}，然后退出 SSH 重新登录；或者全程用 root")
        else:
            add("Docker", WARN, "装了 docker 命令，但守护进程没在运行", f"{_sudo()}systemctl enable --now docker")
        code, text = _run(["docker", "compose", "version", "--short"])
        add("Docker Compose 插件", OK if code == 0 else WARN, text.splitlines()[0][:40] if code == 0 and text else "没有",
            "" if code == 0 else compose_install_hint())
    mem = _mem_available_mb()
    if mem >= 0:
        add("可用内存", OK if mem >= 2048 else WARN, f"{mem} MiB", "" if mem >= 2048 else "每个微信实例最多用 2 GB，内存不够时容器会被杀掉；先关掉别的程序")
    try:
        free = shutil.disk_usage(ROOT).free // (1024 * 1024)
        add("磁盘空间", OK if free >= 5120 else WARN, f"{ROOT} 所在分区剩余 {free} MiB",
            "" if free >= 5120 else "镜像约 1 GB，客户端几百 MB，导出要几倍于数据库的临时空间；先清理出 5 GB 以上")
    except OSError:
        pass
    return items


def print_environment(items: list[dict]) -> int:
    marks = {OK: "  ok ", WARN: "提醒 ", FAIL: "缺少 "}
    print("==> 检查环境")
    for item in items:
        print(f"  {marks[item['status']]} {item['name']}：{item['detail']}")
        if item["fix"] and item["status"] != OK:
            print(f"        怎么办：{item['fix']}")
    if any(item["status"] == FAIL for item in items):
        print("\n有必需的东西缺失，先按上面「怎么办」处理，再重新运行 ./install.sh")
        return 1
    return 0


def check_main() -> int:
    return print_environment(environment())


# ---------------------------------------------------------------- 问答

def read_answers(path: str) -> dict:
    """KEY=VALUE；# 开头是注释；值可以加引号。只读取，不执行。"""
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"错误：找不到答案文件 {path}")
    answers: dict[str, str] = {}
    for number, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in ANSWER_KEYS:
            raise SystemExit(f"错误：{path} 第 {number} 行看不懂：{key or raw!r}。可用的键：{', '.join(sorted(ANSWER_KEYS))}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        answers[key] = value
    return answers


class Asker:
    def __init__(self, answers: dict, interactive: bool):
        self.answers, self.interactive = answers, interactive

    def text(self, key: str, prompt: str, default: str = "") -> str:
        if key in self.answers:
            return self.answers[key]
        if not self.interactive:
            return default
        try:
            value = input(f"{prompt}{f' [{default}]' if default else ''}：").strip()
        except EOFError:
            value = ""
        return value or default

    def yes(self, key: str, prompt: str, default: bool) -> bool:
        value = self.text(key, prompt + ("（Y/n）" if default else "（y/N）"), "").strip().lower()
        if not value:
            return default
        return value in ("y", "yes", "1", "true", "是", "好")

    def choose(self, key: str, prompt: str, options: list[tuple[str, str]], default: str) -> str:
        if key in self.answers or not self.interactive:
            value = self.answers.get(key, default)
            if value not in {k for k, _ in options}:
                raise SystemExit(f"错误：{key} 只能是 {'、'.join(k for k, _ in options)}，收到 {value!r}")
            return value
        print(prompt)
        for index, (k, label) in enumerate(options, 1):
            print(f"  {index}) {label}{'  ← 默认' if k == default else ''}")
        while True:
            value = self.text("", "输入编号，直接回车用默认")
            if not value:
                return default
            if value.isdigit() and 1 <= int(value) <= len(options):
                return options[int(value) - 1][0]
            print("  请输入上面列出的编号。")


# ---------------------------------------------------------------- 向导

def _cli(*arguments: str) -> int:
    from .cli import main
    print(f"\n$ ./bin/wechat-export-send {' '.join(arguments)}")
    return main(list(arguments))


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, docker_install_hint()
    code, text = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        if "permission denied" in text.lower():
            return False, f"sudo usermod -aG docker {os.environ.get('USER', '你的用户名')}，退出 SSH 重新登录后再运行 ./install.sh；或者全程用 root"
        return False, f"{_sudo()}systemctl enable --now docker"
    if _run(["docker", "compose", "version", "--short"])[0] != 0:
        return False, compose_install_hint()
    return True, ""


def _deps_ready() -> bool:
    try:
        import Crypto  # noqa: F401
        import zstandard  # noqa: F401
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def _image_exists(tag: str) -> bool:
    return _run(["docker", "image", "inspect", tag])[0] == 0


def wizard(answers: dict, interactive: bool) -> int:
    from .config import ConfigError, Instance, atomic_write, instances_root
    from .cli import _current_client

    ask = Asker(answers, interactive)
    print("\n==> 安装向导（以后可以再运行：./bin/wechat-export-send setup）")
    docker_ok, docker_fix = _docker_ready()
    x86 = platform.machine() in {"x86_64", "amd64"}
    mode = ask.choose("WES_MODE", "\n第 1 步：你想做什么？", [
        ("demo", "只跑离线演示：不用账号、不联网，5 分钟看到导出结果"),
        ("real", "接我自己的微信账号：建实例、构建镜像、装客户端、启动桌面，然后扫码登录"),
    ], "real" if docker_ok and x86 else "demo")
    if mode == "demo":
        print("\n好的，只跑演示。接下来运行：\n  ./bin/wechat-export-send demo\n  ./bin/wechat-export-send doctor\n"
              "想接真实账号时再运行 ./bin/wechat-export-send setup。")
        return 0

    problems = []
    if not x86:
        problems.append(f"这台机器是 {platform.machine()}，Linux 版微信只测过 x86_64：请换一台 x86_64 的服务器")
    if not docker_ok:
        problems.append(f"Docker 还不能用：{docker_fix}")
    if not _deps_ready():
        problems.append(f"Python 依赖没装好（之前运行 ./install.sh 时没能建出 .venv，多半是那时还没装 python3-venv）："
                        f"{_sudo()}apt-get install -y python3-venv && ./install.sh --non-interactive，看到「依赖已装进 .venv」再继续")
    if problems:
        print("\n接真实账号之前，先处理这些：")
        for line in problems:
            print(f"  - {line}")
        print("处理完重新运行 ./install.sh（或 ./bin/wechat-export-send setup），向导会从这里接着走。")
        return 1

    name = ask.text("WES_INSTANCE", "\n第 2 步：实例名（一个微信账号一个实例，小写字母和数字）", "main")
    directory = instances_root() / name
    if (directory / "config.json").exists():
        print(f"  实例 {name} 已经存在，沿用它。")
    elif _cli("instance", "create", name) != 0:
        return 1
    include_text = ask.text("WES_EXPORT_INCLUDE", "\n第 3 步：只导出哪些会话？写群名或好友备注，多个用逗号隔开；直接回车表示全部", "")
    include = [part.strip() for part in re.split(r"[,，]", include_text) if part.strip()]
    if include:
        config_path = directory / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("export", {})["include"] = include
        atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")   # 权限 600
        try:
            Instance.load(name)
        except ConfigError as exc:
            print(f"错误：配置不合法：{exc}", file=sys.stderr)
            return 1
        print(f"  已写入 {config_path}（权限 600）：export.include = {include}")

    tools, desktop = "wechat-export-send-tools:local", f"wechat-export-send-desktop:{__version__}"
    images_ready = _image_exists(tools) and _image_exists(desktop)
    if images_ready:
        print("\n第 4 步：镜像已经构建过了，跳过。")
    elif ask.yes("WES_BUILD_IMAGES", "\n第 4 步：现在构建镜像吗？要下载约 1 GB，通常 5～15 分钟", True):
        for command in (["docker", "build", "-t", tools, "-f", "docker/tools/Dockerfile", "."],
                        ["docker", "build", "--platform", "linux/amd64", "-t", desktop, "docker/desktop"]):
            print(f"\n$ {' '.join(command)}")
            if subprocess.call(command, cwd=ROOT) != 0:
                print("\n镜像构建失败。多半是网络问题：确认服务器能访问 Docker Hub 和 Debian 软件源，然后重新运行 ./install.sh --images。"
                      "\n更多见 docs/TROUBLESHOOTING.md「安装和 demo」。", file=sys.stderr)
                return 1
        images_ready = True
    else:
        print("  跳过。以后运行：./install.sh --images")

    client = _current_client()
    if client:
        print(f"\n第 5 步：微信客户端 {client.get('version', '?')} 已经装好，跳过。")
    else:
        print("\n第 5 步：安装微信客户端。本项目不分发微信：请从微信官网（https://linux.weixin.qq.com/）下载 x86_64 的 deb，"
              "传到这台服务器上（例如 /root/WeChatLinux_x86_64.deb）。")
        deb = ask.text("WES_CLIENT_DEB", "deb 文件的路径（还没下载就直接回车，以后再装）", "")
        if deb:
            deb_path = Path(os.path.expanduser(deb))
            if not deb_path.is_file():
                print(f"错误：找不到 {deb_path}。确认路径后重新运行 ./bin/wechat-export-send setup", file=sys.stderr)
                return 1
            if _cli("client", "install", str(deb_path)) != 0:
                return 1
            client = _current_client()
            # instance.env 记录客户端路径和哈希：实例比客户端先建时要补上
            from .cli import _write_env
            env_path = directory / "instance.env"
            port = re.search(r"^VNC_WEB_PORT=(\d+)", env_path.read_text(encoding="utf-8"), re.M) if env_path.is_file() else None
            if port and "WECHAT_BINARY_SHA256=\n" in env_path.read_text(encoding="utf-8") + "\n":
                _write_env(directory, name, int(port.group(1)))

    port = "17900"
    env_path = directory / "instance.env"
    if env_path.is_file():
        found = re.search(r"^VNC_WEB_PORT=(\d+)", env_path.read_text(encoding="utf-8"), re.M)
        port = found.group(1) if found else port
    started = False
    if client and images_ready:
        if ask.yes("WES_START", "\n第 6 步：现在启动桌面容器吗？", True):
            started = _cli("instance", "up", name) == 0
    print("\n==> 自检")
    _cli("doctor", "--instance", name)
    print("\n==> 下一步做什么（详细步骤和截图见 docs/QUICKSTART.md）")
    steps = []
    if not images_ready:
        steps.append("构建镜像：./install.sh --images")
    if not client:
        steps.append("下载官方 deb，然后：./bin/wechat-export-send client install /root/WeChatLinux_x86_64.deb")
    if not started:
        steps.append(f"启动桌面：./bin/wechat-export-send instance up {name}")
    steps += [f"在你自己的电脑上开隧道：ssh -L {port}:127.0.0.1:{port} 你的用户名@服务器地址",
              f"浏览器打开 http://127.0.0.1:{port}/vnc.html ，点「连接」，用手机微信扫码登录（这一步要你自己做）",
              f"捕获数据库密钥：./bin/wechat-export-send keys capture --instance {name} --i-own-this-account（按提示在网页里退出再登录一次）",
              f"导出：./bin/wechat-export-send export --instance {name}"]
    for index, step in enumerate(steps, 1):
        print(f"  {index}. {step}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wechat-export-send setup", description="环境检查和安装向导")
    parser.add_argument("--answers", help="答案文件（KEY=VALUE，见 install.env.example）；给了就不再提问")
    parser.add_argument("--non-interactive", action="store_true", help="不提问：没写在答案文件里的用默认值")
    parser.add_argument("--check-only", action="store_true", help="只检查环境")
    args = parser.parse_args(argv)
    if args.check_only:
        return check_main()
    answers = read_answers(args.answers) if args.answers else {}
    interactive = not args.non_interactive and not args.answers and sys.stdin.isatty()
    if not interactive and not answers and not args.non_interactive:
        print("没有终端，跳过提问。要自动回答：./install.sh --config 答案文件（格式见 install.env.example）")
        return 0
    return wizard(answers, interactive)


if __name__ == "__main__":
    raise SystemExit(main())
