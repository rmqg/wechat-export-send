"""实例和配置。一个实例就是一个微信账号：一个容器、一个目录、一份配置。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from . import ROOT

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

DEFAULTS: dict = {
    "instance": "",
    "account_dir": "",
    "gui_dir": "",
    "desktop": {"managed": True, "container": "", "docker_host": "", "network": ""},
    "export": {
        "root": "export",
        "timezone": "Asia/Shanghai",
        "id_mode": "hash",
        "kinds": ["group", "private"],
        "include": [],
        "exclude": [],
        "since": 0,
        "lookback_seconds": 3600,
        "interval_seconds": 300,
        "runner": "host",
        "container_memory_mb": 1024,
        "members": True,
        "avatars": True,
        "media": {"enabled": True, "max_bytes": 104857600, "thumbnails": False, "files": True, "pending_days": 7},
    },
    "send": {
        "mode": "docker",
        "tools_image": "wechat-export-send-tools:local",
        "calibration": "",
        "timeout_seconds": 180,
        "lock_wait_seconds": 60,
        "confirm_base_seconds": 35,
        "max_image_bytes": 26214383,
        "max_text_bytes": 16384,
        "evidence": True,
        "memory_mb": 768,
        "tmpfs_mb": 384,
    },
    "originals": {"enabled": False, "client_version_code": -227271398, "max_per_run": 50, "timeout_seconds": 600},
    "media_origin": {"enabled": False, "port": 8099, "token": ""},
    "watchdog": {"bark_url": "", "group": "wechat", "fail_level": "timeSensitive", "recovery_level": "active"},
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, extra: dict, path: str = "") -> dict:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        where = f"{path}{key}"
        if key not in base:
            raise ConfigError(f"未知配置项 {where}，请对照 docs/CONFIGURATION.md")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"配置项 {where} 应该是对象")
            result[key] = _merge(base[key], value, where + ".")
        else:
            result[key] = value
    return result


def instances_root() -> Path:
    return Path(os.environ.get("WES_HOME") or ROOT / "instances").resolve()


def atomic_write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class Instance:
    """实例目录：gui/（客户端数据）keys/ export/ state/ calibration/ config.json instance.env"""

    def __init__(self, directory: Path, config: dict):
        self.dir = Path(directory)
        self.config = config
        self.name = config.get("instance") or self.dir.name

    @classmethod
    def load(cls, name_or_path: str) -> "Instance":
        path = Path(name_or_path)
        directory = path if (path / "config.json").is_file() else instances_root() / name_or_path
        config_path = directory / "config.json"
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"实例不存在：{name_or_path}。先运行 wechat-export-send instance create {name_or_path}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{config_path} 不是合法的 JSON：第 {exc.lineno} 行") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{config_path} 应该是 JSON 对象")
        return cls(directory, validate(_merge(DEFAULTS, raw)))

    @property
    def gui(self) -> Path:
        configured = self.config.get("gui_dir")
        return Path(configured) if configured else self.dir / "gui"

    def env(self) -> dict:
        values = {}
        try:
            for line in (self.dir / "instance.env").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
        except OSError:
            pass
        return values

    def owner(self) -> tuple[int, int]:
        """容器里用哪个用户：和读得了 gui/ 的用户一致。"""
        env = self.env()
        return int(env.get("PUID") or os.getuid()), int(env.get("PGID") or os.getgid())

    def give_to_owner(self, path: Path, *, recursive: bool = False) -> None:
        """宿主用 root 运行时，把新建的东西交给容器用户，否则容器里写不进去。"""
        if os.getuid() != 0:
            return
        uid, gid = self.owner()
        path = Path(path)
        os.chown(path, uid, gid)
        if recursive and path.is_dir():
            for parent, directories, files in os.walk(path):
                for name in directories + files:
                    os.chown(os.path.join(parent, name), uid, gid, follow_symlinks=False)

    def docker(self) -> list[str]:
        """查看桌面容器用的 docker 命令；桌面可能在另一个 dockerd 里。"""
        host = self.config["desktop"]["docker_host"]
        return ["docker", "-H", host] if host else ["docker"]

    def worker_network(self) -> str:
        return self.config["desktop"]["network"] or f"container:{self.container}"

    @property
    def keys_file(self) -> Path:
        return self.dir / "keys" / "all_keys.json"

    @property
    def image_key_file(self) -> Path:
        return self.dir / "keys" / "image_key.json"

    @property
    def state(self) -> Path:
        return self.dir / "state"

    @property
    def export_root(self) -> Path:
        root = Path(self.config["export"]["root"])
        return root if root.is_absolute() else self.dir / root

    @property
    def calibration_dir(self) -> Path:
        return self.dir / "calibration"

    @property
    def container(self) -> str:
        return self.config["desktop"]["container"] or f"wes-{self.name}-desktop"

    def calibration_file(self) -> Path:
        configured = self.config["send"]["calibration"]
        if configured:
            path = Path(configured)
            return path if path.is_absolute() else self.dir / path
        local = self.calibration_dir / "ui-calibration.json"
        return local if local.is_file() else ROOT / "config" / "ui-calibration.json"

    def account_candidates(self) -> list[Path]:
        found: set[Path] = set()
        for pattern in ("xwechat_files/*/db_storage", "Documents/xwechat_files/*/db_storage", "文档/xwechat_files/*/db_storage"):
            found.update(path for path in self.gui.glob(pattern) if path.is_dir())
        return sorted(found)

    def db_storage(self) -> Path:
        """当前账号的 db_storage。有多个账号目录时必须在配置里写明 account_dir，绝不猜。"""
        candidates = self.account_candidates()
        wanted = self.config.get("account_dir")
        if wanted:
            candidates = [path for path in candidates if path.parent.name == wanted]
        if len(candidates) != 1:
            raise ConfigError(f"需要唯一确定一个账号目录，现在找到 {len(candidates)} 个；在 config.json 里设置 account_dir")
        return candidates[0]

    def id_salt(self) -> bytes:
        """ID 哈希用的盐：实例第一次用到时生成，之后不变，导出和发送共用。"""
        path = self.state / "id-salt"
        try:
            return bytes.fromhex(path.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            pass
        salt = secrets.token_hex(16)
        if not self.state.exists():
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.give_to_owner(self.state)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return bytes.fromhex(path.read_text(encoding="ascii").strip())
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(salt + "\n")
        self.give_to_owner(path)
        return bytes.fromhex(salt)


def validate(config: dict) -> dict:
    export = config["export"]
    if export["id_mode"] not in {"hash", "raw"}:
        raise ConfigError("export.id_mode 只能是 hash 或 raw")
    unknown = set(export["kinds"]) - {"group", "private", "channel", "other"}
    if unknown:
        raise ConfigError(f"export.kinds 里有未知取值：{sorted(unknown)}")
    for key in ("lookback_seconds", "interval_seconds", "since"):
        if not isinstance(export[key], int) or export[key] < 0:
            raise ConfigError(f"export.{key} 应该是非负整数")
    if export["runner"] not in {"host", "container"}:
        raise ConfigError("export.runner 只能是 host 或 container")
    send = config["send"]
    if send["mode"] not in {"docker", "direct"}:
        raise ConfigError("send.mode 只能是 docker 或 direct")
    if not isinstance(send["timeout_seconds"], int) or send["timeout_seconds"] < 30:
        raise ConfigError("send.timeout_seconds 应该是不小于 30 的整数")
    return config


class IdMapper:
    """把平台账号变成导出里的稳定 ID。同一个账号每次都得到同一个 ID。"""

    def __init__(self, salt: bytes, mode: str = "hash"):
        self.salt = salt
        self.mode = mode

    def _digest(self, value: str, length: int = 16) -> str:
        return hashlib.sha256(self.salt + value.encode("utf-8")).hexdigest()[:length]

    def conversation(self, username: str, kind: str) -> str:
        if self.mode == "raw":
            cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", username)
            return cleaned
        prefix = {"group": "g", "private": "p", "channel": "c"}.get(kind, "o")
        return f"{prefix}_{self._digest(username)}"

    def sender(self, username: str) -> str:
        if self.mode == "raw":
            return username
        return f"u_{self._digest(username)}"

    def account(self, name: str) -> str:
        return f"a_{self._digest(name, 24)}"


def conversation_kind(username: str) -> str:
    if username.endswith("@chatroom"):
        return "group"
    if username.startswith("gh_") or username.endswith("@openim"):
        return "channel"
    if username in {"filehelper", "floatbottle", "medianote", "newsapp", "fmessage", "notifymessage"} or username.startswith("@"):
        return "other"
    return "private"
