#!/usr/bin/env python3
"""Create a visual QA sheet from generated product pages."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]

THUMB_W = 320
THUMB_H = 413
GAP = 24
LABEL_H = 36
COLS = 3


def resolve_product_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--product",
        default="products/construction-vehicles-toddler-30",
        help="Product directory containing the generated pages.",
    )
    args = parser.parse_args()

    product_root = resolve_product_root(args.product)
    pages = product_root / "pages"
    out = product_root / "qa" / "contact_sheet.jpg"

    files = sorted(pages.glob("page_*.png"))
    if not files:
        raise SystemExit(f"No pages found under {pages}")

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

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=94, subsampling=0)
    print(out)


if __name__ == "__main__":
    main()
