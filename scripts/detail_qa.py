#!/usr/bin/env python3
"""Create full-resolution quadrant QA sheets for generated coloring pages.

Each output is a JPEG containing four native-resolution quadrants from one
page. This makes fine linework inspectable through repository previews when
original PNG blobs are too large for the GitHub connector's image decoder.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def resolve(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/little-worlds-style-lock-5")
    parser.add_argument(
        "--output",
        default="test_outputs/little_worlds_style_lock_5_details",
    )
    parser.add_argument("--quality", type=int, default=90)
    args = parser.parse_args()

    product_root = resolve(args.product)
    pages = sorted((product_root / "pages").glob("page_*.png"))
    if not pages:
        raise SystemExit(f"No pages found under {product_root / 'pages'}")

    out_root = resolve(args.output)
    out_root.mkdir(parents=True, exist_ok=True)
    margin, label_h, title_h = 18, 34, 52
    saved = 0

    for page_path in pages:
        with Image.open(page_path) as source:
            image = source.convert("RGB")
        width, height = image.size
        mx, my = width // 2, height // 2
        regions = [
            ("TOP LEFT", (0, 0, mx, my)),
            ("TOP RIGHT", (mx, 0, width, my)),
            ("BOTTOM LEFT", (0, my, mx, height)),
            ("BOTTOM RIGHT", (mx, my, width, height)),
        ]
        crops = [(label, image.crop(bounds)) for label, bounds in regions]
        crop_w = max(crop.width for _, crop in crops)
        crop_h = max(crop.height for _, crop in crops)
        cell_w = crop_w
        cell_h = crop_h + label_h
        canvas = Image.new(
            "RGB",
            (margin * 3 + cell_w * 2, margin * 3 + title_h + cell_h * 2),
            "white",
        )
        draw = ImageDraw.Draw(canvas)
        title = f"{page_path.stem} — full-resolution detail inspection ({width}x{height})"
        draw.text((margin, margin), title, fill="black")
        for index, (label, crop) in enumerate(crops):
            col, row = index % 2, index // 2
            x = margin + col * (cell_w + margin)
            y = margin + title_h + row * (cell_h + margin)
            canvas.paste(crop, (x, y))
            draw.text((x, y + crop_h + 8), label, fill="black")
        destination = out_root / f"{page_path.stem}_detail.jpg"
        canvas.save(destination, "JPEG", quality=args.quality, optimize=True, subsampling=0)
        print(f"{destination} | {canvas.width}x{canvas.height}")
        saved += 1

    print(f"Created {saved} detail sheets in {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
