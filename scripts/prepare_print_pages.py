#!/usr/bin/env python3
"""Prepare binary black-and-white review candidates without altering originals.

The thresholded files are candidates for visual review, not automatically
approved print assets. Large solid-black regions and lost fine lines still
require human inspection.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]


def resolve(value: str, base: Path = ROOT) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def load_labels(product_root: Path) -> dict[int, str]:
    manifest_path = product_root / "prompts.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        int(item["page"]): str(item.get("label", f"page_{int(item['page']):03d}"))
        for item in data.get("pages", [])
        if "page" in item
    }


def create_comparison_sheet(
    rows: list[dict],
    destination: Path,
    tile_width: int,
    cols: int,
) -> None:
    margin, gap, title_height, label_height = 18, 12, 40, 30
    tile_height = round(tile_width * 1984 / 1536)
    pair_gap = 6
    tile_pair_width = tile_width * 2 + pair_gap
    tile_height_total = tile_height + label_height
    row_count = math.ceil(len(rows) / cols)
    canvas = Image.new(
        "RGB",
        (
            margin * 2 + cols * tile_pair_width + (cols - 1) * gap,
            margin * 2 + title_height + row_count * tile_height_total + (row_count - 1) * gap,
        ),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, margin), "Little Worlds — original (left) vs. binary cleanup (right)", fill="black")
    draw.text((margin, margin + 17), "Review candidate only. Inspect dark fills and fine-line loss before print approval.", fill="black")
    for index, row in enumerate(rows):
        col, row_idx = index % cols, index // cols
        x = margin + col * (tile_pair_width + gap)
        y = margin + title_height + row_idx * (tile_height_total + gap)
        original = ImageOps.contain(row["original"], (tile_width, tile_height))
        cleaned = ImageOps.contain(row["cleaned"], (tile_width, tile_height))
        canvas.paste(original, (x, y))
        canvas.paste(cleaned, (x + tile_width + pair_gap, y))
        label = row["label"]
        draw.text((x, y + tile_height + 5), label[:42], fill="black")
    canvas.save(destination, "JPEG", quality=92, optimize=True, subsampling=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/little-worlds-whimsical-fantasy-homes-30")
    parser.add_argument("--threshold", type=int, default=150)
    parser.add_argument("--output", default=None, help="Output directory; defaults to product QA.")
    parser.add_argument("--tile-width", type=int, default=92)
    parser.add_argument("--columns", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.threshold <= 254:
        raise SystemExit("--threshold must be between 1 and 254.")
    if args.columns < 1 or args.tile_width < 40:
        raise SystemExit("--columns must be positive and --tile-width at least 40.")

    product_root = resolve(args.product)
    source_root = product_root / "pages"
    page_files = sorted(source_root.glob("page_*.png"))
    if not page_files:
        raise SystemExit(f"No source pages found in {source_root}")
    if len(page_files) != 30:
        print(f"NOTE: found {len(page_files)} pages; product manifest may not yet be fully generated.")

    output_root = resolve(args.output, product_root) if args.output else product_root / "qa" / f"print_prep_threshold_{args.threshold}"
    prepared_root = output_root / "pages"
    prepared_root.mkdir(parents=True, exist_ok=True)
    labels = load_labels(product_root)
    metrics = {
        "product_id": product_root.name,
        "purpose": "Thresholded black-and-white review candidates. Originals are preserved; human visual QA is required before sale.",
        "threshold": args.threshold,
        "method": "Global grayscale threshold: source pixels below threshold become black; all other pixels become white.",
        "pages": [],
    }
    overview_rows: list[dict] = []

    for source_path in page_files:
        with Image.open(source_path) as loaded:
            original = loaded.convert("RGB")
            gray = ImageOps.grayscale(loaded)
        histogram = gray.histogram()
        total = gray.width * gray.height
        black_count = sum(histogram[: args.threshold])
        white_count = total - black_count
        candidate = gray.point(lambda value: 0 if value < args.threshold else 255, mode="L")
        out_path = prepared_root / f"{source_path.stem}.png"
        candidate.save(out_path, optimize=True)
        entry = {
            "page": int(source_path.stem.split("_")[-1]),
            "label": labels.get(int(source_path.stem.split("_")[-1]), source_path.stem),
            "source_file": str(source_path.relative_to(ROOT)) if source_path.is_relative_to(ROOT) else str(source_path),
            "candidate_file": str(out_path.relative_to(ROOT)) if out_path.is_relative_to(ROOT) else str(out_path),
            "size": [gray.width, gray.height],
            "threshold": args.threshold,
            "source_midtones_50_to_239_percent": round(100 * sum(histogram[50:240]) / total, 3),
            "candidate_black_percent": round(100 * black_count / total, 3),
            "candidate_white_percent": round(100 * white_count / total, 3),
            "unique_grayscale_levels_source": sum(1 for count in histogram if count),
        }
        metrics["pages"].append(entry)
        overview_rows.append({"label": entry["label"], "original": original, "cleaned": candidate.convert("RGB")})
        print(
            f"{source_path.name}: {gray.width}x{gray.height} | "
            f"black={entry['candidate_black_percent']}% | "
            f"white={entry['candidate_white_percent']}%"
        )

    overview_path = output_root / "comparison_overview.jpg"
    create_comparison_sheet(overview_rows, overview_path, args.tile_width, args.columns)
    metrics_path = output_root / "print_prep_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Threshold: {args.threshold}")
    print(f"Prepared candidates: {prepared_root}")
    print(f"Comparison overview: {overview_path}")
    print(f"Metrics: {metrics_path}")
    print("These are review candidates, not approved final pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
