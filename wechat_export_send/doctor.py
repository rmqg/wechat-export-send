"""自检：依赖、权限、配置、连通性。每一项都给出状态和修法，不读聊天内容。"""
from __future__ import annotations

import json
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys

from . import ROOT, __version__, sqlcipher
from .config import ConfigError, Instance, instances_root

OK, WARN, FAIL = "ok", "warn", "fail"


def _run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__


def _item(items: list, name: str, status: str, detail: str, fix: str = "") -> None:
    items.append({"check": name, "status": status, "detail": detail, "fix": fix})


def tested_versions() -> dict:
    return json.loads((ROOT / "config" / "versions.json").read_text(encoding="utf-8"))


def general_checks(items: list) -> None:
    version = sys.version_info
    _item(items, "python", OK if version >= (3, 10) else FAIL, platform.python_version(),
          "" if version >= (3, 10) else "需要 Python 3.10 或更新版本")
    _item(items, "cpu", OK if platform.machine() in {"x86_64", "amd64"} else WARN, platform.machine(),
          "" if platform.machine() in {"x86_64", "amd64"} else "demo 和导出代码可以运行，但 Linux 版微信客户端只测过 x86_64；真实账号请用 amd64 机器")
    _item(items, "kernel", OK, platform.release())
    for module, package, why in (("Crypto", "pycryptodome", "解密真实数据库（没有时退回很慢的纯 Python AES，只够跑 demo）"),
                                 ("zstandard", "zstandard", "解压微信 4.x 的压缩正文（也可以用系统 libzstd）"),
                                 ("PIL", "Pillow", "校验图片、比对会话标题")):
        try:
            __import__(module)
            _item(items, f"python 依赖 {package}", OK, "已安装")
        except ImportError:
            _item(items, f"python 依赖 {package}", WARN, f"未安装；用途：{why}", "运行 ./install.sh")
    docker = shutil.which("docker")
    if docker is None:
        _item(items, "docker", WARN, "没有安装；离线 demo 不需要，真实账号需要", "按 docs/INSTALL.md 安装 Docker Engine 和 Compose 插件")
        return
    code, text = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    _item(items, "docker", OK if code == 0 else WARN, text.splitlines()[0][:120] if text else "",
          "" if code == 0 else "Docker 守护进程连不上：检查服务是否启动、当前用户有没有权限")
    code, text = _run(["docker", "compose", "version", "--short"])
    _item(items, "docker compose", OK if code == 0 else WARN, text[:60], "" if code == 0 else "安装 docker-compose-plugin")


def instance_checks(instance: Instance, items: list) -> None:
    prefix = f"[{instance.name}] "
    for path, label in ((instance.dir, "实例目录"), (instance.keys_file.parent, "keys"), (instance.export_root, "导出目录"), (instance.state, "state")):
        if not path.exists():
            _item(items, prefix + label, WARN, "还不存在", "第一次运行相应步骤时会创建")
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        good = mode & 0o077 == 0
        _item(items, prefix + label + " 权限", OK if good else FAIL, oct(mode), "" if good else f"chmod 700 {path}")
    if instance.keys_file.exists():
        mode = stat.S_IMODE(instance.keys_file.stat().st_mode)
        _item(items, prefix + "密钥文件权限", OK if mode & 0o077 == 0 else FAIL, oct(mode), "" if mode & 0o077 == 0 else f"chmod 600 {instance.keys_file}")
    calibration_path = instance.calibration_file()
    try:
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        _item(items, prefix + "界面校准文件", OK if calibration.get("version") == 1 else FAIL, str(calibration_path.name))
    except (OSError, ValueError):
        _item(items, prefix + "界面校准文件", FAIL, str(calibration_path), "检查 send.calibration 配置")
    try:
        storage = instance.db_storage()
    except ConfigError as exc:
        _item(items, prefix + "账号目录", WARN, str(exc), "启动桌面容器，在 noVNC 里扫码登录一次")
        storage = None
    if storage is not None:
        _item(items, prefix + "账号目录", OK, "已确定唯一账号目录")
        if instance.keys_file.is_file():
            _key_checks(instance, storage, items, prefix)
        else:
            _item(items, prefix + "数据库密钥", WARN, "还没有捕获", "运行 wechat-export-send keys capture")
    manifest = instance.export_root / "manifest.json"
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            _item(items, prefix + "导出清单", OK if value.get("schema") == "chat-export/v1" else FAIL,
                  f"{len(value.get('conversations', []))} 个会话，生成于 {value.get('generated_at')}")
        except ValueError:
            _item(items, prefix + "导出清单", FAIL, "manifest.json 损坏", "再运行一次 export")
    if instance.config["send"]["mode"] == "docker" and shutil.which("docker"):
        code, text = _run(["docker", "container", "inspect", "--format", "{{.State.Running}}", instance.container])
        running = code == 0 and text.strip() == "true"
        _item(items, prefix + "桌面容器", OK if running else WARN, instance.container + (" 运行中" if running else " 没有运行"),
              "" if running else f"wechat-export-send instance up {instance.name}")
        code, _ = _run(["docker", "image", "inspect", instance.config["send"]["tools_image"]])
        _item(items, prefix + "工具镜像", OK if code == 0 else WARN, instance.config["send"]["tools_image"],
              "" if code == 0 else "运行 ./install.sh --images")
        if running:
            _client_checks(instance, items, prefix)


def _key_checks(instance: Instance, storage: Path, items: list, prefix: str) -> None:
    try:
        keys = sqlcipher.load_keys(instance.keys_file)
    except (OSError, ValueError):
        _item(items, prefix + "数据库密钥", FAIL, "密钥文件读不了", "重新运行 keys capture")
        return
    checked = bad = 0
    for relative in ("contact/contact.db", "session/session.db", "message/message_0.db"):
        path, info = storage / relative, sqlcipher.key_for_rel(keys, relative)
        if not path.is_file() or not info:
            continue
        checked += 1
        try:
            with path.open("rb") as stream:
                page = stream.read(sqlcipher.PAGE_SIZE)
            enc, mac = sqlcipher._keys(bytes.fromhex(info["enc_key"]), page[:16])
            sqlcipher._decrypt_page(page, 1, enc, mac)
        except (OSError, ValueError):
            bad += 1
    if not checked:
        _item(items, prefix + "数据库密钥", FAIL, "没有一个常用库能对上密钥", "重新运行 keys capture")
    elif bad:
        _item(items, prefix + "数据库密钥", FAIL, f"{bad}/{checked} 个库 HMAC 校验不过（可能换过账号或重建过库）", "重新运行 keys capture")
    else:
        _item(items, prefix + "数据库密钥", OK, f"{checked} 个库第一页 HMAC 校验通过")


def _client_checks(instance: Instance, items: list, prefix: str) -> None:
    tested = tested_versions()
    code, text = _run(["docker", "exec", instance.container, "sha256sum", "/opt/wechat/wechat"], 30)
    digest = text.split()[0] if code == 0 and text else ""
    known = {item["executable_sha256"]: item["version"] for item in tested.get("clients", [])}
    if digest in known:
        _item(items, prefix + "微信客户端", OK, f"{known[digest]}（实测过的版本）")
    else:
        _item(items, prefix + "微信客户端", WARN, "不是实测过的版本；捕获和发送的界面坐标可能不对",
              "先跑 keys check；发送前用 --dry-run 验证，必要时重新校准")
    try:
        from . import watchdog
        status = watchdog.observe(instance)
        _item(items, prefix + "登录状态", OK if status == "online" else WARN, status,
              "" if status == "online" else "打开这个实例的 noVNC 看一眼；offline 表示需要扫码")
    except Exception as exc:
        _item(items, prefix + "登录状态", WARN, f"探针失败（{type(exc).__name__}）", "确认工具镜像已构建")


def run(instance_name: str | None = None) -> list[dict]:
    items: list[dict] = []
    _item(items, "wechat-export-send", OK, __version__)
    general_checks(items)
    names = [instance_name] if instance_name else []
    if not names and instances_root().is_dir():
        names = sorted(path.name for path in instances_root().iterdir() if (path / "config.json").is_file())
    if not names:
        _item(items, "实例", WARN, "还没有实例；离线 demo 不需要", "wechat-export-send instance create main")
    for name in names:
        try:
            instance_checks(Instance.load(name), items)
        except ConfigError as exc:
            _item(items, f"[{name}] 配置", FAIL, str(exc), "对照 docs/CONFIGURATION.md 修改 config.json")
    return items


def report(items: list[dict], as_json: bool) -> int:
    failed = any(item["status"] == FAIL for item in items)
    if as_json:
        print(json.dumps({"ok": not failed, "checks": items}, ensure_ascii=False, indent=1))
        return 1 if failed else 0
    marks = {OK: "  ok ", WARN: "警告 ", FAIL: "失败 "}
    for item in items:
        print(f"{marks[item['status']]} {item['check']}: {item['detail']}")
        if item["fix"]:
            print(f"      修复：{item['fix']}")
    warnings = sum(item["status"] == WARN for item in items)
    failures = sum(item["status"] == FAIL for item in items)
    print(f"\n结果：{failures} 项失败，{warnings} 项警告")
    return 1 if failed else 0

