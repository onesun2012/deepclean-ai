# -*- coding: utf-8 -*-
"""
生成应用图标 assets/icon.ico：品牌绿圆角方块 + 深色 D 字母。

仅构建期依赖 Pillow（pip install pillow），运行时零依赖。
运行: python scripts/make_icon.py
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "icon.ico")
BG = (110, 217, 168, 255)   # 品牌绿 --accent #6ed9a8
FG = (6, 32, 22, 255)       # 深绿 --accent-fg #062016

S = 1024  # 超采样画布，缩到各尺寸边缘更平滑
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)

font = None
for name in ("segoeuib.ttf", "arialbd.ttf", "tahoma.ttf"):
    path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
    if os.path.exists(path):
        font = ImageFont.truetype(path, int(S * 0.62))
        break
if font is None:
    font = ImageFont.load_default()
d.text((S / 2, S * 0.53), "D", font=font, fill=FG, anchor="mm")

master = img.resize((256, 256), Image.LANCZOS)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
master.save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                        (32, 32), (24, 24), (16, 16)])
master.save(OUT.replace(".ico", "_preview.png"))
print("已生成", OUT)
