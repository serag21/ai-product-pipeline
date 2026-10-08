#!/usr/bin/env python3
"""Create a visual QA sheet from generated product pages."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "products" / "construction-vehicles-toddler-30" / "pages"
OUT = ROOT / "products" / "construction-vehicles-toddler-30" / "qa" / "contact_sheet.jpg"

THUMB_W = 320
THUMB_H = 413
GAP = 24
LABEL_H = 36
COLS = 3


def main() -> None:
    files = sorted(PAGES.glob("page_*.png"))
    if not files:
        raise SystemExit(f"No pages found under {PAGES}")
    rows = math.ceil(len(files) / COLS)
    sheet = Image.new(
        "RGB",
        (
            COLS * THUMB_W + (COLS + 1) * GAP,
            rows * (THUMB_H + LABEL_H) + (rows + 1) * GAP,
        ),
        "white",
    )
    draw = ImageDraw.Draw(sheet)

    for index, path in enumerate(files):
        with Image.open(path) as source:
            image = ImageOps.contain(source.convert("RGB"), (THUMB_W, THUMB_H))
        col = index % COLS
        row = index // COLS
        x = GAP + col * (THUMB_W + GAP)
        y = GAP + row * (THUMB_H + LABEL_H + GAP)
        sheet.paste(image, (x, y))
        draw.text((x, y + THUMB_H + 8), path.stem, fill="black")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT, quality=94, subsampling=0)
    print(OUT)


if __name__ == "__main__":
    main()
