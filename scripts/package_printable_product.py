#!/usr/bin/env python3
"""Package a finished Little Worlds coloring product for Etsy validation.

Creates monochrome PNG pages, US Letter/A4 PDFs, three Etsy-sized PNG ZIPs,
listing preview images, a listing-copy draft, and a package QA report.
Source ComfyUI pages are preserved unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import textwrap
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
ETSY_LIMIT = 20 * 1024 * 1024
SAFE_LIMIT = 19 * 1024 * 1024
PAPER = "#F7F3EA"
INK = "#26352F"
ACCENT = "#7B9B82"
MUTED = "#746F65"
WHITE = "#FFFFFF"


def resolve(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_font(size: int, bold: bool = False):
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/arial.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                pass
    return ImageFont.load_default()


def load_manifest(product_root: Path) -> dict:
    path = product_root / "prompts.json"
    if not path.is_file():
        raise SystemExit(f"Missing product manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def make_binary(source_path: Path, output_path: Path, threshold: int) -> dict:
    with Image.open(source_path) as loaded:
        gray = ImageOps.grayscale(loaded)
        width, height = gray.size
        hist = gray.histogram()
        total = width * height
        binary = gray.point(lambda value: 0 if value < threshold else 255, mode="L")
        binary.save(output_path, optimize=True)
    black = sum(hist[:threshold])
    white = total - black
    return {
        "size": [width, height],
        "threshold": threshold,
        "source_midtone_percent": round(100 * sum(hist[50:240]) / total, 3),
        "output_black_percent": round(100 * black / total, 3),
        "output_white_percent": round(100 * white / total, 3),
        "source_unique_grayscale_levels": sum(n > 0 for n in hist),
        "output_file": str(output_path),
    }


def make_pdf(pages: list[Path], output: Path, page_size, title: str) -> None:
    doc = canvas.Canvas(str(output), pagesize=page_size, pageCompression=1, invariant=1)
    doc.setTitle(title)
    doc.setAuthor("Little Worlds")
    page_w, page_h = page_size
    margin = 18 if page_size == letter else 28.35
    available_w, available_h = page_w - 2 * margin, page_h - 2 * margin
    for path in pages:
        with Image.open(path) as loaded:
            img_w, img_h = loaded.size
        scale = min(available_w / img_w, available_h / img_h)
        draw_w, draw_h = img_w * scale, img_h * scale
        x, y = (page_w - draw_w) / 2, (page_h - draw_h) / 2
        doc.drawImage(ImageReader(str(path)), x, y, width=draw_w, height=draw_h)
        doc.showPage()
    doc.save()


def make_contact_sheet(
    page_paths: list[Path],
    labels: dict[int, str],
    output: Path,
    title: str,
    columns: int = 5,
    tile_w: int = 250,
) -> None:
    margin, gap, header_h, label_h = 22, 14, 65, 31
    tile_h = round(tile_w * 1984 / 1536)
    rows = math.ceil(len(page_paths) / columns)
    sheet = Image.new(
        "RGB",
        (margin * 2 + columns * tile_w + (columns - 1) * gap,
         margin * 2 + header_h + rows * (tile_h + label_h) + (rows - 1) * gap),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    draw.text((margin, margin), title, fill=INK, font=load_font(26, True))
    draw.text((margin, margin + 37), f"{len(page_paths)} pages · print-ready black outlines · threshold 150", fill=MUTED, font=load_font(15))
    for idx, path in enumerate(page_paths):
        row, col = divmod(idx, columns)
        x = margin + col * (tile_w + gap)
        y = margin + header_h + row * (tile_h + label_h + gap)
        with Image.open(path) as loaded:
            thumb = ImageOps.contain(loaded.convert("RGB"), (tile_w - 8, tile_h - 8))
        sheet.paste(thumb, (x + (tile_w - thumb.width) // 2, y + (tile_h - thumb.height) // 2))
        page_num = int(path.stem.split("_")[-1])
        label = labels.get(page_num, path.stem)
        draw.text((x, y + tile_h + 5), label[:35], fill=INK, font=load_font(13))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, "JPEG", quality=91, optimize=True, subsampling=0)


def add_preview_card(canvas_img: Image.Image, page: Image.Image, box: tuple[int, int, int, int]) -> None:
    draw = ImageDraw.Draw(canvas_img)
    x, y, w, h = box
    draw.rounded_rectangle((x + 10, y + 12, x + w + 10, y + h + 12), radius=20, fill="#DDD7CB")
    draw.rounded_rectangle((x, y, x + w, y + h), radius=20, fill=WHITE, outline="#D5CDBE", width=3)
    thumb = ImageOps.contain(page.convert("RGB"), (w - 30, h - 30))
    canvas_img.paste(thumb, (x + (w - thumb.width) // 2, y + (h - thumb.height) // 2))


def draw_center(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill=INK) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor="mm")


def make_listing_images(final_pages: list[Path], preview_dir: Path, manifest: dict | None = None) -> list[Path]:
    manifest = manifest or {}
    listing = manifest.get("listing", {})
    brand = str(manifest.get("brand", "LITTLE WORLDS"))
    subtitle = str(manifest.get("subtitle", manifest.get("title", "Printable Coloring Pages")))
    count = len(final_pages)
    preview_dir.mkdir(parents=True, exist_ok=True)
    loaded_pages = []
    for path in final_pages:
        with Image.open(path) as image:
            loaded_pages.append(image.convert("RGB").copy())
    width = height = 2400
    made: list[Path] = []

    # Image 1: hero/cover using real page previews.
    img = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((48, 48, width - 48, height - 48), radius=38, outline=INK, width=5)
    draw_center(draw, (width // 2, 165), brand, load_font(92, True))
    draw_center(draw, (width // 2, 285), subtitle.upper(), load_font(43, True), ACCENT)
    draw_center(draw, (width // 2, 380), f"{count} PRINTABLE COLORING PAGES", load_font(38, True))
    sample_ids = [max(1, min(count, int(n))) - 1 for n in listing.get("hero_page_numbers", [1, 2, 5, 9, 14, count])][:6]
    card_w, card_h, gap = 520, 690, 55
    left = (width - (3 * card_w + 2 * gap)) // 2
    top = 500
    for idx, page_id in enumerate(sample_ids):
        row, col = divmod(idx, 3)
        x = left + col * (card_w + gap)
        y = top + row * (card_h + 45)
        add_preview_card(img, loaded_pages[page_id], (x, y, card_w, card_h))
    draw_center(draw, (width // 2, 2045), "US LETTER + A4 PDFs  •  INDIVIDUAL PNG PAGES", load_font(32, True))
    draw_center(draw, (width // 2, 2120), "Instant digital download · No physical item shipped", load_font(29), MUTED)
    draw_center(draw, (width // 2, 2210), str(listing.get("tagline", "Imaginative coloring pages for all ages")), load_font(30, True), INK)
    path = preview_dir / "listing_01_hero.jpg"
    img.save(path, "JPEG", quality=89, optimize=True, subsampling=0)
    made.append(path)

    # Image 2: eight-page gallery.
    img = Image.new("RGB", (width, height), "#FBFAF6")
    draw = ImageDraw.Draw(img)
    draw_center(draw, (width // 2, 125), "A PEEK INSIDE", load_font(86, True))
    draw_center(draw, (width // 2, 230), "A selection of pages included in your download", load_font(35), MUTED)
    card_w, card_h, gap_x, gap_y = 485, 555, 65, 40
    left, top = (width - 3 * card_w - 2 * gap_x) // 2, 335
    sample_ids = [max(1, min(count, int(n))) - 1 for n in listing.get("gallery_page_numbers", list(range(1, min(count, 9) + 1)))][:9]
    for idx, page_id in enumerate(sample_ids):
        row, col = divmod(idx, 3)
        add_preview_card(img, loaded_pages[page_id], (left + col * (card_w + gap_x), top + row * (card_h + gap_y), card_w, card_h))
    draw_center(draw, (width // 2, 2245), f"{count} DISTINCT SCENES  •  CLEAN BLACK-AND-WHITE LINE ART", load_font(31, True), ACCENT)
    path = preview_dir / "listing_02_gallery.jpg"
    img.save(path, "JPEG", quality=89, optimize=True, subsampling=0)
    made.append(path)

    # Image 3: what is included.
    img = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(img)
    draw_center(draw, (width // 2, 170), "WHAT'S INCLUDED", load_font(82, True))
    blocks = [
        (str(count), "unique coloring pages", f"{count} individual printable illustrations"),
        ("2", "print-ready PDFs", "US Letter and A4 paper sizes"),
        (str(count), "individual PNGs", "High-resolution black-and-white pages"),
        ("0", "physical items", "Digital download only"),
    ]
    y = 360
    for number, heading, detail in blocks:
        draw.rounded_rectangle((210, y, 2190, y + 330), radius=36, fill=WHITE, outline="#D8D0C1", width=3)
        draw.text((270, y + 55), number, font=load_font(100, True), fill=ACCENT)
        draw.text((540, y + 70), heading, font=load_font(48, True), fill=INK)
        draw.text((540, y + 160), detail, font=load_font(32), fill=MUTED)
        y += 385
    draw_center(draw, (width // 2, 2075), "Print at home or at a local print shop", load_font(38, True))
    draw_center(draw, (width // 2, 2160), "AI-assisted artwork; see listing description for disclosure.", load_font(25), MUTED)
    path = preview_dir / "listing_03_included.jpg"
    img.save(path, "JPEG", quality=89, optimize=True, subsampling=0)
    made.append(path)

    # Image 4: how it works.
    img = Image.new("RGB", (width, height), "#FBFAF6")
    draw = ImageDraw.Draw(img)
    draw_center(draw, (width // 2, 190), "DOWNLOAD. PRINT. COLOR.", load_font(76, True))
    steps = [
        ("01", "Download", "Your Etsy order contains print-ready files."),
        ("02", "Choose a format", "Open the US Letter or A4 PDF, or print a PNG."),
        ("03", "Print and enjoy", "Use plain paper and select Fit or Actual Size."),
    ]
    y = 430
    for num, title, detail in steps:
        draw.rounded_rectangle((180, y, 2220, y + 420), radius=34, fill=WHITE, outline="#D8D0C1", width=3)
        draw.text((245, y + 55), num, font=load_font(88, True), fill=ACCENT)
        draw.text((500, y + 65), title, font=load_font(55, True), fill=INK)
        draw.multiline_text((500, y + 160), textwrap.fill(detail, 50), font=load_font(32), fill=MUTED, spacing=12)
        y += 500
    draw_center(draw, (width // 2, 2070), "For personal coloring use; no physical item is shipped.", load_font(30, True))
    path = preview_dir / "listing_04_how_it_works.jpg"
    img.save(path, "JPEG", quality=89, optimize=True, subsampling=0)
    made.append(path)
    return made


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/little-worlds-whimsical-fantasy-homes-30")
    parser.add_argument("--threshold", type=int, default=150)
    parser.add_argument("--etsy-limit-mb", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.threshold <= 254:
        raise SystemExit("--threshold must be between 1 and 254.")

    product_root = resolve(args.product)
    manifest = load_manifest(product_root)
    page_specs = manifest.get("pages", [])
    expected = len(page_specs)
    if expected < 1:
        raise SystemExit("Product manifest contains no pages.")
    source_root = product_root / "pages"
    sources = [source_root / f"page_{int(item['page']):03d}.png" for item in page_specs]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise SystemExit(
            f"Cannot package: {len(missing)} page(s) are missing. Generate the full product first.\n"
            + "\n".join(missing[:10])
        )

    generation_manifest = product_root / "qa" / "generation_manifest.json"
    if generation_manifest.exists():
        gm = json.loads(generation_manifest.read_text(encoding="utf-8"))
        if gm.get("summary", {}).get("passed") != expected or gm.get("summary", {}).get("failed") != 0:
            raise SystemExit(f"Generation QA is not fully green: {gm.get('summary')}")

    final_root = product_root / "final"
    # Rebuild deterministically so stale archives from a previous run cannot leak into delivery.
    shutil.rmtree(final_root, ignore_errors=True)
    pages_out = final_root / "pages"
    upload_root = final_root / "etsy_uploads"
    listing_root = final_root / "listing_images"
    for directory in (pages_out, upload_root, listing_root):
        directory.mkdir(parents=True, exist_ok=True)

    page_labels = {
        int(item["page"]): str(item.get("label", f"Page {int(item['page']):02d}"))
        for item in page_specs
    }
    output_pages: list[Path] = []
    page_metrics: list[dict] = []
    for source, item in zip(sources, page_specs):
        page_num = int(item["page"])
        output = pages_out / f"page_{page_num:03d}.png"
        metric = make_binary(source, output, args.threshold)
        metric.update({"page": page_num, "label": page_labels[page_num]})
        if metric["size"][0] < 1200 or metric["size"][1] < 1600:
            raise SystemExit(f"Page {page_num} is below print resolution: {metric['size']}")
        page_metrics.append(metric)
        output_pages.append(output)

    file_prefix = str(manifest.get("file_prefix", "Little-Worlds"))
    letter_pdf = upload_root / f"{file_prefix}-Letter.pdf"
    a4_pdf = upload_root / f"{file_prefix}-A4.pdf"
    make_pdf(output_pages, letter_pdf, letter, f"{manifest.get('title', 'Little Worlds')} — US Letter")
    make_pdf(output_pages, a4_pdf, A4, f"{manifest.get('title', 'Little Worlds')} — A4")

    readme_text = (
        f"{manifest.get('title', 'Printable Coloring Pages')}\n"
        f"{expected} printable coloring pages\n\n"
        "FILES\n"
        f"- {letter_pdf.name}: US Letter print-ready PDF\n"
        f"- {a4_pdf.name}: A4 print-ready PDF\n"
        f"- {file_prefix}-PNGs-XX.zip: individual high-resolution PNG pages\n\n"
        "PRINTING\n"
        "1. Open the PDF for your paper size, or extract a PNG ZIP to print a single page.\n"
        "2. Select Actual Size or Fit to Page in your printer dialog.\n"
        "3. For coloring, plain paper and single-sided printing are recommended.\n"
        "4. Printer margins vary; use your printer's fit-to-page option if needed.\n\n"
        "DIGITAL PRODUCT\n"
        "This is an instant digital download. No physical product will be shipped.\n"
        "Artwork was created with AI-assisted image generation based on original scene prompts, "
        "then selected and prepared for this printable collection.\n\n"
        "PERSONAL USE\n"
        "You may print copies for your own personal coloring use. Do not resell, redistribute, "
        "share, or repackage the digital files.\n"
    )
    (final_root / "READ_ME_FIRST.txt").write_text(readme_text, encoding="utf-8")

    # PDFs are delivered as two files, PNGs as three archives of ten pages.
    png_archives: list[Path] = []
    pages_per_archive = 10
    for start in range(0, len(output_pages), pages_per_archive):
        part = start // pages_per_archive + 1
        archive_path = upload_root / f"{file_prefix}-PNGs-{part:02d}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("READ_ME_FIRST.txt", readme_text)
            for page_path in output_pages[start:start + pages_per_archive]:
                archive.write(page_path, f"PNG_Pages/{page_path.name}")
        png_archives.append(archive_path)

    previews = make_listing_images(output_pages, listing_root, manifest)
    original_sheet = final_root / "preview_original_pages.jpg"
    final_sheet = final_root / "preview_print_ready_pages.jpg"
    make_contact_sheet(sources, page_labels, original_sheet, f"{manifest.get('title', 'Coloring Pages')} — Original Generated Pages")
    make_contact_sheet(output_pages, page_labels, final_sheet, f"{manifest.get('title', 'Coloring Pages')} — Print-Ready Monochrome Pages")

    listing = manifest.get("listing", {})
    listing_title = str(listing.get("title", f"{manifest.get('title', 'Coloring Pages')} — {expected} Printable PDF and PNG Pages"))
    description = str(listing.get("description", f"Enjoy this original collection of {expected} printable coloring pages.")) + (
        "\n\nYOU WILL RECEIVE\n"
        f"• One US Letter PDF with all {expected} pages\n"
        f"• One A4 PDF with all {expected} pages\n"
        f"• {expected} individual high-resolution PNG pages in {len(png_archives)} ZIP file(s)\n\n"
        "HOW TO USE\n"
        "Download the files, open the PDF for your paper size, and print at home or at a local print shop. "
        "You can also extract the PNG files to print individual designs. Printer margins vary, so choose "
        "Actual Size or Fit to Page as appropriate.\n\n"
        "PLEASE NOTE\n"
        "This is a digital product; no physical book or printed pages will be shipped. Colors and print "
        "appearance can vary by screen, printer, and paper. For personal coloring use.\n\n"
        "AI DISCLOSURE\n"
        "The illustrations were created with AI-assisted image generation using original scene prompts, "
        "then curated and prepared into this printable collection.\n"
    )
    tags = listing.get("tags", [
        "printable coloring", "coloring pages", "instant download",
        "cute coloring pages", "adult coloring", "kids coloring pages",
        "printable PDF"
    ])
    listing_draft = (
        f"# Etsy Listing Draft\n\n## Title\n{listing_title}\n\n"
        f"## Description\n{description}\n\n## Suggested tags\n"
        + "\n".join(f"- {tag}" for tag in tags)
        + "\n\n## Price test\nStart around $3.49–$4.49 for the initial validation listing, then adjust based on views, clicks, favorites, "
          "and purchases.\n\n## Before publishing\nReview every full-resolution page, confirm all files open and print correctly, "
          "verify the AI disclosure remains in the description, and check current Etsy listing requirements.\n"
    )    (final_root / "etsy_listing_draft.md").write_text(listing_draft, encoding="utf-8")

    upload_files = [letter_pdf, a4_pdf, *png_archives]
    limit_bytes = args.etsy_limit_mb * 1024 * 1024
    file_entries = []
    for path in upload_files:
        size = path.stat().st_size
        file_entries.append({
            "name": path.name,
            "path": str(path),
            "bytes": size,
            "megabytes": round(size / (1024 * 1024), 3),
            "under_configured_limit": size < limit_bytes,
            "under_safe_19mb": size < SAFE_LIMIT,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    if len(upload_files) > 5:
        raise SystemExit(f"Etsy delivery needs {len(upload_files)} files; Etsy currently allows at most five.")
    too_large = [f for f in file_entries if f["bytes"] >= min(limit_bytes, SAFE_LIMIT)]
    if too_large:
        raise SystemExit(
            "At least one Etsy upload file is too large; split/compress before publishing: "
            + ", ".join(f"{f['name']}={f['megabytes']}MB" for f in too_large)
        )

    qa = {
        "product_id": manifest["product_id"],
        "title": manifest.get("title"),
        "expected_pages": expected,
        "prepared_pages": len(output_pages),
        "threshold": args.threshold,
        "method": "Grayscale threshold: pixels below threshold are black; all others are white.",
        "page_metrics": page_metrics,
        "etsy_upload_files": file_entries,
        "etsy_file_count": len(upload_files),
        "etsy_size_limit_mb": args.etsy_limit_mb,
        "listing_images": [{"name": p.name, "bytes": p.stat().st_size} for p in previews],
        "artwork_note": "Thresholding removes gray tones but does not correct malformed illustration details or all solid-black regions. Human review is still required.",
        "status": "PACKAGE_BUILT_REVIEW_REQUIRED",
    }
    (final_root / "package_qa.json").write_text(json.dumps(qa, indent=2), encoding="utf-8")

    print(f"Product: {manifest['product_id']}")
    print(f"Prepared pages: {len(output_pages)}")
    print(f"US Letter PDF: {letter_pdf} ({letter_pdf.stat().st_size / 1024 / 1024:.2f} MB)")
    print(f"A4 PDF: {a4_pdf} ({a4_pdf.stat().st_size / 1024 / 1024:.2f} MB)")
    print("Etsy upload files:")
    for item in file_entries:
        print(f"  {item['name']} — {item['megabytes']:.2f} MB")
    print(f"Listing images: {listing_root}")
    print(f"Listing draft: {final_root / 'etsy_listing_draft.md'}")
    print(f"QA report: {final_root / 'package_qa.json'}")
    print("Package built; page art still requires visual review before listing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
