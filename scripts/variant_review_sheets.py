#!/usr/bin/env python3
"""Create three-up comparison sheets for curated FLUX.2 prompt variants."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]


def resolve(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def font(size: int, bold: bool = False):
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"]
        if bold else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                pass
    return ImageFont.load_default()


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return value[:55] or "scene"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/little-haunts-club-curated-iteration-lab-42")
    parser.add_argument("--output", default="test_outputs/little_haunts_curated_iteration_lab_42")
    args = parser.parse_args()

    product = resolve(args.product)
    manifest = json.loads((product / "prompts.json").read_text(encoding="utf-8"))
    pages = manifest.get("pages", [])
    if not pages:
        raise SystemExit("The product manifest contains no pages.")
    output = resolve(args.output)
    output.mkdir(parents=True, exist_ok=True)

    # One compact three-up sheet per scene makes A/B/C differences easy to compare.
    groups: dict[int, list[dict]] = {}
    for item in pages:
        groups.setdefault(int(item["candidate_scene_slot"]), []).append(item)

    margin, gap, header_h, footer_h = 18, 12, 92, 52
    tile_w, tile_h = 330, 426
    index_rows = ["# Little Haunts Club — 42-candidate comparison", "",
                  "Each concept has three alternatives (A/B/C). The provisional skeleton-mermaid keeper is not regenerated in this lab.", ""]
    group_sheets = []

    for slot, items in sorted(groups.items()):
        items = sorted(items, key=lambda item: item.get("iteration", "A"))
        title = items[0].get("candidate_title", f"Scene {slot:02d}")
        rationale = items[0].get("design_rationale", "")
        canvas = Image.new(
            "RGB",
            (margin * 2 + 3 * tile_w + 2 * gap, margin * 2 + header_h + tile_h + footer_h),
            "white",
        )
        draw = ImageDraw.Draw(canvas)
        draw.text((margin, margin), f"SCENE {slot:02d} — {title}", fill="#24332C", font=font(25, True))
        rationale_text = rationale[:150] + ("..." if len(rationale) > 150 else "")
        draw.text((margin, margin + 38), rationale_text, fill="#676767", font=font(13))
        for col, item in enumerate(items[:3]):
            image_path = product / "pages" / f"page_{int(item['page']):03d}.png"
            if not image_path.is_file():
                raise SystemExit(f"Missing generated candidate: {image_path}")
            with Image.open(image_path) as loaded:
                thumbnail = ImageOps.contain(loaded.convert("RGB"), (tile_w - 10, tile_h - 10))
            x = margin + col * (tile_w + gap)
            y = margin + header_h
            draw.rounded_rectangle((x, y, x + tile_w, y + tile_h), radius=10, outline="#B6B6B6", width=2)
            canvas.paste(thumbnail, (x + (tile_w - thumbnail.width)//2, y + (tile_h - thumbnail.height)//2))
            label = f"{item.get('iteration','?')}  |  page {int(item['page']):02d}"
            draw.text((x + 5, y + tile_h + 8), label, fill="#24332C", font=font(17, True))
        out_path = output / f"scene_{slot:02d}_{slug(title)}.jpg"
        canvas.save(out_path, "JPEG", quality=92, optimize=True, subsampling=0)
        group_sheets.append(out_path)
        index_rows.append(f"- Scene {slot:02d}: **{title}** — {out_path.name}")
        index_rows.append(f"  - Rationale: {rationale}")
        for item in items:
            index_rows.append(f"  - Candidate {item.get('iteration')}: page {int(item['page']):02d}, seed {item.get('seed')}")
        index_rows.append("")

    # Master overview of all variants for rapid scanning.
    cols, thumb_w, thumb_h, label_h = 6, 180, 232, 27
    rows = (len(pages) + cols - 1) // cols
    gap, margin, title_h = 8, 16, 60
    overview = Image.new(
        "RGB",
        (margin*2 + cols*thumb_w + (cols-1)*gap,
         margin*2 + title_h + rows*(thumb_h+label_h) + (rows-1)*gap),
        "white",
    )
    draw = ImageDraw.Draw(overview)
    draw.text((margin, margin), "Little Haunts Club — 42 candidates (A/B/C per scene)", fill="#24332C", font=font(24, True))
    for index, item in enumerate(pages):
        path = product / "pages" / f"page_{int(item['page']):03d}.png"
        if not path.is_file():
            raise SystemExit(f"Missing generated candidate: {path}")
        with Image.open(path) as loaded:
            thumb = ImageOps.contain(loaded.convert("RGB"), (thumb_w - 6, thumb_h - 6))
        col, row = index % cols, index // cols
        x = margin + col * (thumb_w + gap)
        y = margin + title_h + row * (thumb_h + label_h + gap)
        overview.paste(thumb, (x + (thumb_w-thumb.width)//2, y + (thumb_h-thumb.height)//2))
        label = f"{item.get('variant_id','')} | p{int(item['page']):02d}"
        draw.text((x, y + thumb_h + 5), label[:27], fill="#24332C", font=font(11))
    overview_path = output / "overview_42_candidates.jpg"
    overview.save(overview_path, "JPEG", quality=90, optimize=True, subsampling=0)
    (output / "INDEX.md").write_text("\n".join(index_rows) + "\n", encoding="utf-8")

    print(f"Concept groups: {len(groups)}")
    print(f"Candidate images: {len(pages)}")
    print(f"Master overview: {overview_path}")
    print(f"Per-scene three-up sheets: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
