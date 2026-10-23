#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成悬浮胶囊的效果对照图：直接用你当前桌面当背景，一次看多种参数。

用法：python preview_variants.py
输出：variants.png（在脚本同目录）
"""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageGrab

import overlay_win as ow
from overlay_win import LevelMeter, WinOverlay

ROOT = Path(__file__).resolve().parent

VARIANTS = [
    ("A 默认 360x80（更通透 tint=72）", {"width": 360, "height": 80}),
    ("B 小巧 320x72", {"width": 320, "height": 72}),
    ("C 大气 460x88", {"width": 460, "height": 88}),
    ("D 更不透（tint=110，字更清楚）", {"width": 360, "height": 80, "tint": 110}),
    ("E 很透（tint=40，玻璃感最强）", {"width": 360, "height": 80, "tint": 40}),
]


def speech_levels(n=34, seed=21):
    import random

    random.seed(seed)
    out, i = [], 0
    while len(out) < n:
        if i % 7 == 0:
            out.extend([0.004] * 3)
        else:
            base = random.uniform(0.02, 0.22)
            for _ in range(random.randint(2, 4)):
                out.append(base * random.uniform(0.55, 1.0))
        i += 1
    return out[:n]


def main():
    sw, sh = ow.user32.GetSystemMetrics(0), ow.user32.GetSystemMetrics(1)
    W, H = 1000, 360
    x = max(0, (sw - W) // 2)
    y = max(0, sh - H - 120)
    try:
        desk = ImageGrab.grab(bbox=(x, y, x + W, y + H), all_screens=True).convert("RGB")
    except Exception:
        desk = Image.new("RGB", (W, H), (24, 26, 32))

    shots = []
    for label, ov_cfg in VARIANTS:
        meter = LevelMeter()
        for v in speech_levels():
            meter.push(v)
        ov = WinOverlay(meter, {"ui": dict(ov_cfg)})

        # 手动喂背景（和真实运行时同一套处理）
        crop = desk.copy()
        small = crop.resize((max(6, ov.W // 12), max(6, ov.H // 12)),
                            Image.BILINEAR).filter(ImageFilter.GaussianBlur(3.0))
        glass = small.resize((ov.W, ov.H), Image.BICUBIC)
        ov._light_mode = ov.theme != "dark"
        lum = crop.convert("L").resize((16, 8), Image.BILINEAR)
        vals = lum.tobytes()
        ov._eff_white = ov._compute_white(sum(vals) / max(1, len(vals)))
        r, g, b = glass.split()
        lift = lambda v: min(255, int(v * 1.10 + 6))
        ov._backdrop = Image.merge("RGB", (r.point(lift), g.point(lift), b.point(lift)))

        ov._state = "recording"
        ov._state_t = time.time() - 6.0
        img = ov._render(time.time())

        canvas = crop.convert("RGBA")
        # 居中贴到裁好的宽度里
        px = (canvas.width - img.width) // 2
        py = (canvas.height - img.height) // 2
        canvas.alpha_composite(img, (px, py))
        shots.append((label, canvas))

    # 拼成对照图
    pad, label_h = 16, 34
    cw = W
    ch = sum(s.height + label_h + pad for _, s in shots) + pad
    out = Image.new("RGB", (cw, ch), (22, 24, 28))
    d = ImageDraw.Draw(out)
    from overlay_win import _font

    f = _font(22)
    y = pad
    for label, s in shots:
        d.text((pad, y + 4), label, font=f, fill=(230, 232, 238))
        y += label_h
        out.paste(s.convert("RGB"), (0, y))
        y += s.height + pad

    import time as _t
    path = ROOT / f"variants_{_t.strftime('%H%M%S')}.png"
    out.save(path)
    print("已生成对照图:", path, out.size)


if __name__ == "__main__":
    main()
