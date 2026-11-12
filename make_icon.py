#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the NS2 Presence icon (PNG + ICO) used by the tray and the exe."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
ASSETS = BASE / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

RED = (230, 0, 18, 255)
WHITE = (255, 255, 255, 255)
DARK = (28, 28, 30, 255)


def font(size: int):
    for name in ("arialbd.ttf", "segoeuib.ttf", "calibrib.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build(size: int = 256) -> Image.Image:
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = int(big * 0.05)
    draw.rounded_rectangle(
        (margin, margin, big - margin, big - margin),
        radius=int(big * 0.22),
        fill=RED,
    )

    # Joy-Con like white marks
    bar_w = int(big * 0.055)
    bar_h = int(big * 0.42)
    top = int(big * 0.16)
    left_x = int(big * 0.34)
    right_x = int(big * 0.60)
    draw.rounded_rectangle(
        (left_x, top, left_x + bar_w, top + bar_h),
        radius=bar_w // 2,
        fill=WHITE,
    )
    draw.rounded_rectangle(
        (right_x, top, right_x + bar_w, top + bar_h),
        radius=bar_w // 2,
        fill=WHITE,
    )
    dot = bar_w // 2
    draw.ellipse(
        (left_x + bar_w // 2 - dot, top + int(bar_h * 0.12), left_x + bar_w // 2 + dot, top + int(bar_h * 0.12) + 2 * dot),
        fill=RED,
    )

    # "2"
    f2 = font(int(big * 0.42))
    text = "2"
    bbox = draw.textbbox((0, 0), text, font=f2)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((big - tw) / 2 - bbox[0], int(big * 0.50) - bbox[1]),
        text,
        font=f2,
        fill=WHITE,
    )

    # small "NS" label
    fns = font(int(big * 0.13))
    label = "NS"
    bbox = draw.textbbox((0, 0), label, font=fns)
    lw = bbox[2] - bbox[0]
    draw.text(
        ((big - lw) / 2 - bbox[0], int(big * 0.075) - bbox[1]),
        label,
        font=fns,
        fill=DARK,
    )

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    img = build(256)
    img.save(ASSETS / "icon.png")
    img.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("icons written to", ASSETS)


if __name__ == "__main__":
    main()
