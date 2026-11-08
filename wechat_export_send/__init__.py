"""wechat-export-send：在 Linux 服务器上导出微信聊天记录、往群里发文字和图片。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    __version__ = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    __version__ = "0+unknown"
