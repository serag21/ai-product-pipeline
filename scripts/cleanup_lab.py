#!/usr/bin/env python3
"""Compare deterministic black-and-white cleanup levels on coloring illustrations.

This is an experiment, not an automatic production decision. It preserves source
files and writes thresholded variants plus overview/detail sheets for review.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
METHODS = [
    ("original", None),
    ("threshold_150", 150),
    ("threshold_180", 180),
    ("threshold_210", 210),
]


def resolve(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def threshold_image(gray: Image.Image, threshold: int | None) -> Image.Image:
    if threshold is None:
        return gray.convert("RGB")
    binary = gray.point(lambda value: 0 if value < threshold else 255, mode="L")
    return binary.convert("RGB")


def pct(count: int, total: int) -> float:
    return round(100.0 * count / max(total, 1), 3)


def make_overview(
    entries: list[dict],
    output: Path,
    tile_size: tuple[int, int] = (250, 323),
) -> None:
    margin, gap, title_h, label_h = 18, 10, 38, 32
    cols, rows = len(METHODS), len(entries)
    tw, th = tile_size
    canvas = Image.new(
        "RGB",
        (2 * margin + cols * tw + (cols - 1) * gap,
         2 * margin + title_h + rows * (th + label_h) + (rows - 1) * gap),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, margin), "Little Worlds — original vs. monochrome cleanup", fill="black")
    for col, (name, _) in enumerate(METHODS):
        x = margin + col * (tw + gap)
        draw.text((x, margin + title_h - 3), name, fill="black")
    for row, entry in enumerate(entries):
        y = margin + title_h + row * (th + label_h + gap)
        for col, (name, _) in enumerate(METHODS):
            image = entry["variants"][name]
            thumb = ImageOps.contain(image, tile_size)
            x = margin + col * (tw + gap)
            canvas.paste(thumb, (x, y))
        draw.text((margin, y + th + 4), entry["page_label"], fill="black")
    canvas.save(output, "JPEG", quality=91, optimize=True, subsampling=0)


def make_detail(entry: dict, output: Path, crop_width: int = 420) -> None:
    source = entry["source"]
    width, height = source.size
    mx, my = width // 2, height // 2
    regions = [
        ("top-left", (0, 0, mx, my)),
        ("top-right", (mx, 0, width, my)),
        ("bottom-left", (0, my, mx, height)),
        ("bottom-right", (mx, my, width, height)),
    ]
    crop_height = round(crop_width * (my / mx))
    label_h, margin, gap, title_h = 25, 14, 8, 40
    cols, rows = len(METHODS), len(regions)
    cell_h = crop_height + label_h
    canvas = Image.new(
        "RGB",
        (2 * margin + cols * crop_width + (cols - 1) * gap,
         2 * margin + title_h + rows * cell_h + (rows - 1) * gap),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, margin), f"{entry['page_label']} — linework detail comparison", fill="black")
    for col, (method, _) in enumerate(METHODS):
        draw.text((margin + col * (crop_width + gap), margin + title_h - 3), method, fill="black")
    for row, (region_label, bounds) in enumerate(regions):
        y = margin + title_h + row * (cell_h + gap)
        for col, (method, _) in enumerate(METHODS):
            crop = entry["variants"][method].crop(bounds)
            crop = crop.resize((crop_width, crop_height), Image.Resampling.LANCZOS)
            x = margin + col * (crop_width + gap)
            canvas.paste(crop, (x, y))
            if col == 0:
                draw.text((x + 3, y + crop_height + 5), region_label, fill="black")
    canvas.save(output, "JPEG", quality=91, optimize=True, subsampling=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/little-worlds-style-lock-5")
    parser.add_argument("--output", default="test_outputs/little_worlds_cleanup_lab")
    args = parser.parse_args()

    product_root = resolve(args.product)
    page_files = sorted((product_root / "pages").glob("page_*.png"))
    if not page_files:
        raise SystemExit(f"No pages found under {product_root / 'pages'}")

    output_root = resolve(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    variant_root = product_root / "qa" / "cleanup_lab"
    entries: list[dict] = []
    metrics: dict = {
        "purpose": "Compare gray originals with binary thresholds; source files are never modified.",
        "methods": {name: {"threshold": value} for name, value in METHODS},
        "pages": [],
    }

    for page_file in page_files:
        with Image.open(page_file) as loaded:
            gray = ImageOps.grayscale(loaded).copy()
        page_id = page_file.stem
        page_variant_root = variant_root / page_id
        page_variant_root.mkdir(parents=True, exist_ok=True)
        variants: dict[str, Image.Image] = {}
        pixels = list(gray.getdata())
        total = len(pixels)
        entry_metrics = {
            "page": page_id,
            "source_size": list(gray.size),
            "source_midtone_50_to_239_percent": pct(sum(50 <= p <= 239 for p in pixels), total),
            "variants": {},
        }
        for name, threshold in METHODS:
            result = threshold_image(gray, threshold)
            variants[name] = result
            if threshold is not None:
                result.save(page_variant_root / f"{name}.png", optimize=True)
            result_gray = ImageOps.grayscale(result)
            result_pixels = list(result_gray.getdata())
            black = sum(p <= 20 for p in result_pixels)
            white = sum(p >= 245 for p in result_pixels)
            mid = total - black - white
            entry_metrics["variants"][name] = {
                "black_pixels_percent": pct(black, total),
                "white_pixels_percent": pct(white, total),
                "midtone_pixels_percent": pct(mid, total),
            }
        metrics["pages"].append(entry_metrics)
        entries.append({"page_label": page_id, "source": gray, "variants": variants})

    make_overview(entries, output_root / "comparison_overview.jpg")
    for entry in entries:
        make_detail(
            entry,
            output_root / f"{entry['page_label']}_detail_comparison.jpg",
        )
    (output_root / "cleanup_metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    print(f"Pages tested: {len(entries)}")
    print(f"Methods compared: {', '.join(name for name, _ in METHODS)}")
    print(f"Overview: {output_root / 'comparison_overview.jpg'}")
    print(f"Detail sheets and metrics: {output_root}")
    print(f"Working variants (ignored by git): {variant_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
