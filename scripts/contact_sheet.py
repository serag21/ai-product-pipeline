#!/usr/bin/env python3
"""Create a visual QA sheet from generated product pages."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
THUMB_W = 200
THUMB_H = 258
GAP = 12
LABEL_H = 34
COLS = 6


def resolve_product_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def load_labels(product_root: Path) -> dict[int, str]:
    manifest_path = product_root / "prompts.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        int(item["page"]): item.get("label", f"page_{int(item['page']):03d}")
        for item in data.get("pages", [])
        if "page" in item
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/construction-vehicles-toddler-30")
    args = parser.parse_args()

    product_root = resolve_product_root(args.product)
    pages = product_root / "pages"
    out = product_root / "qa" / "contact_sheet.jpg"
    labels = load_labels(product_root)

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
        label = labels.get(int(path.stem.split("_")[-1]), path.stem)
        draw.text((x, y + THUMB_H + 7), label, fill="black")

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=94, subsampling=0)
    print(out)


if __name__ == "__main__":
    main()
