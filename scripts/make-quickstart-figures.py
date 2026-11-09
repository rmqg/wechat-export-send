#!/usr/bin/env python3
"""画 docs/QUICKSTART.md 用的示意图（合成，不含任何真实账号或二维码）。需要 Pillow 和一个中文字体。

  python3 scripts/make-quickstart-figures.py [字体路径]
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
FONT = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def font(size):
    return ImageFont.truetype(FONT, size)


def browser(url):
    image = Image.new("RGB", (960, 600), "#f3f4f6")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 52), fill="#e5e7eb")
    for i, color in enumerate(("#ef4444", "#f59e0b", "#22c55e")):
        draw.ellipse((16 + i * 22, 20, 28 + i * 22, 32), fill=color)
    draw.rounded_rectangle((100, 12, 940, 40), 14, fill="white", outline="#d1d5db")
    draw.text((118, 15), url, fill="#111827", font=font(16))
    return image, draw


def stamp(draw):
    draw.rounded_rectangle((732, 556, 952, 590), 8, fill="#fef3c7", outline="#f59e0b")
    draw.text((744, 561), "示意图 · 合成，非真实截图", fill="#92400e", font=font(15))


def novnc_connect():
    image, draw = browser("http://127.0.0.1:17900/vnc.html")
    draw.rectangle((0, 52, 960, 600), fill="#3b4252")
    draw.rounded_rectangle((330, 220, 630, 400), 12, fill="#4c566a")
    draw.text((420, 240), "noVNC", fill="white", font=font(34))
    draw.rounded_rectangle((405, 315, 555, 365), 10, fill="#e5e7eb")
    draw.text((428, 324), "连接 Connect", fill="#111827", font=font(20))
    draw.line((575, 400, 520, 360), fill="#f59e0b", width=4)
    draw.text((585, 400), "① 点这里", fill="#fbbf24", font=font(22))
    stamp(draw)
    image.save(OUT / "quickstart-novnc-connect.png", optimize=True)


def wechat_qr():
    image, draw = browser("http://127.0.0.1:17900/vnc.html")
    draw.rectangle((0, 52, 960, 600), fill="#1f2937")
    draw.rectangle((0, 52, 960, 76), fill="#111827")
    draw.text((12, 54), "fluxbox 桌面（容器里的虚拟屏幕）", fill="#9ca3af", font=font(14))
    draw.rectangle((330, 110, 630, 540), fill="white")
    draw.rectangle((330, 110, 630, 140), fill="#f3f4f6")
    draw.text((342, 114), "Weixin", fill="#111827", font=font(16))
    draw.rectangle((390, 175, 570, 355), fill="#d1d5db")
    for x in range(390, 530, 20):
        draw.line((x, 175, x + 40, 355), fill="#e5e7eb", width=6)
    draw.text((413, 252), "二维码（已打码）", fill="#374151", font=font(16))
    draw.text((395, 380), "使用手机微信扫码登录", fill="#111827", font=font(18))
    draw.text((420, 420), "（客户端界面以实物为准）", fill="#6b7280", font=font(14))
    draw.text((660, 250), "② 手机微信 →「扫一扫」\n   扫这个二维码，\n   再在手机上点「登录」", fill="#fbbf24", font=font(20))
    stamp(draw)
    image.save(OUT / "quickstart-wechat-qr.png", optimize=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    novnc_connect()
    wechat_qr()
    print("已生成", *sorted(p.name for p in OUT.glob("quickstart-*.png")))
