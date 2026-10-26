"""Build local KDP review files and social launch creatives from selected pages.

These outputs are review drafts. They never contact marketplaces or mark a
channel published. Existing source illustrations are read only.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import textwrap
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


PAPER = "#F6F1E7"
INK = "#263B32"
MOSS = "#63816C"
PUMPKIN = "#C9794D"
LILAC = "#9C8AA8"
WHITE = "#FFFFFF"
TRIM_W, TRIM_H = 8.5, 11.0
BLEED = 0.125
SPINE_PER_PAGE_WHITE_BW = 0.002252


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    paths = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf"]
        if bold else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"]
    )
    for path in paths:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _register_pdf_fonts() -> tuple[str, str]:
    normal = Path("C:/Windows/Fonts/arial.ttf")
    bold = Path("C:/Windows/Fonts/arialbd.ttf")
    if normal.is_file() and bold.is_file():
        for name, path in (("HauntsSans", normal), ("HauntsSansBold", bold)):
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(path)))
        return "HauntsSans", "HauntsSansBold"
    return "Helvetica", "Helvetica-Bold"


def _centered_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                   font: ImageFont.ImageFont, fill: str) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor="mm")


def _wrap_pixel(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
                max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), trial, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _draw_wrapped(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
                  font: ImageFont.ImageFont, fill: str, width: int,
                  line_gap: int) -> int:
    for line in _wrap_pixel(draw, text, font, width):
        draw.text((x, y), line, font=font, fill=fill)
        y += line_gap
    return y


def _make_cover(first_art: Path, labels: list[str], title: str, page_count: int,
                target: Path) -> dict[str, Any]:
    spine = page_count * SPINE_PER_PAGE_WHITE_BW
    cover_w_in = 2 * BLEED + 2 * TRIM_W + spine
    cover_h_in = TRIM_H + 2 * BLEED
    dpi = 300
    width, height = round(cover_w_in * dpi), round(cover_h_in * dpi)
    scale = dpi
    bleed_px = round(BLEED * scale)
    trim_w, trim_h = round(TRIM_W * scale), round(TRIM_H * scale)
    back_x = bleed_px
    spine_x = back_x + trim_w
    front_x = round((BLEED + TRIM_W + spine) * scale)

    image = Image.new("RGB", (width, height), INK)
    draw = ImageDraw.Draw(image)
    draw.rectangle((back_x, bleed_px, back_x + trim_w - 1, bleed_px + trim_h - 1), fill=PAPER)
    draw.rectangle((front_x, bleed_px, front_x + trim_w - 1, bleed_px + trim_h - 1), fill="#E8E4D5")

    inset = round(0.34 * scale)
    draw.rounded_rectangle((back_x + inset, bleed_px + inset,
                            back_x + trim_w - inset, bleed_px + trim_h - inset),
                           radius=round(0.24 * scale), outline=INK, width=7)
    draw.rounded_rectangle((front_x + inset, bleed_px + inset,
                            front_x + trim_w - inset, bleed_px + trim_h - inset),
                           radius=round(0.24 * scale), outline=INK, width=7)

    body = _font(57)
    body_bold = _font(68, True)
    small = _font(48)
    title_font = _font(155, True)
    subtitle_font = _font(78, True)

    # Front cover: large title plus one existing illustration in a clean paper panel.
    front_mid = front_x + trim_w // 2
    _centered_text(draw, (front_mid, bleed_px + round(0.76 * scale)), "LITTLE HAUNTS CLUB",
                   body_bold, INK)
    _centered_text(draw, (front_mid, bleed_px + round(1.43 * scale)), "A VERY ODD",
                   title_font, INK)
    _centered_text(draw, (front_mid, bleed_px + round(2.05 * scale)), "HALLOWEEN",
                   title_font, INK)
    _centered_text(draw, (front_mid, bleed_px + round(2.65 * scale)),
                   f"{page_count} cozy-spooky coloring scenes", subtitle_font, MOSS)

    with Image.open(first_art) as art:
        art = ImageOps.grayscale(art).convert("RGB")
        art.thumbnail((round(6.65 * scale), round(6.35 * scale)), Image.Resampling.LANCZOS)
        panel_w, panel_h = round(6.9 * scale), round(6.65 * scale)
        px, py = front_mid - panel_w // 2, bleed_px + round(3.0 * scale)
        draw.rounded_rectangle((px, py, px + panel_w, py + panel_h), radius=34,
                               fill=WHITE, outline="#CFC9BC", width=5)
        image.paste(art, (front_mid - art.width // 2, py + (panel_h - art.height) // 2))
    _centered_text(draw, (front_mid, bleed_px + round(10.0 * scale)),
                   "Printable storybook line art", body_bold, INK)
    _centered_text(draw, (front_mid, bleed_px + round(10.42 * scale)),
                   "Single-sided pages with blank backs", small, MOSS)

    # Back copy and sample scenes; deliberately reserve a blank barcode area.
    back_mid = back_x + trim_w // 2
    _centered_text(draw, (back_mid, bleed_px + round(0.9 * scale)), "A LITTLE ODD.",
                   subtitle_font, INK)
    _centered_text(draw, (back_mid, bleed_px + round(1.45 * scale)), "A LOT OF COZY.",
                   subtitle_font, PUMPKIN)
    text = (f"Meet {page_count} friendly spooky neighbors in the Little Haunts Club. "
            "From a ghostly tailor and a sky-mapping skeleton to a zombie chef, each page "
            "holds one small story, crisp outlines, and roomy spaces to color.")
    y = _draw_wrapped(draw, back_x + round(0.65 * scale), bleed_px + round(2.05 * scale),
                      text, body, INK, trim_w - round(1.3 * scale), round(0.45 * scale))
    y += round(0.28 * scale)
    for bullet in ("17 original Halloween scenes", "Single-sided coloring pages",
                   "Blank reverse sides for coloring comfort", "Whimsical art for cozy-spooky fans"):
        draw.text((back_x + round(0.7 * scale), y), f"•  {bullet}", font=small, fill=INK)
        y += round(0.5 * scale)
    # Small thumbnail row above the reserved barcode zone.
    sample_x = back_x + round(0.7 * scale)
    sample_y = max(y + round(0.1 * scale), bleed_px + round(5.1 * scale))
    row_w = trim_w - round(1.4 * scale)
    thumb_w, thumb_h, gap = round(1.38 * scale), round(1.78 * scale), round(0.3 * scale)
    for idx, label in enumerate(labels[1:5]):
        try:
            page_path = Path(label.split("\u241f", 1)[1])
        except (IndexError, OSError):
            continue
        if not page_path.is_file():
            continue
        with Image.open(page_path) as art:
            art = ImageOps.grayscale(art).convert("RGB")
            art.thumbnail((thumb_w - 18, thumb_h - 18), Image.Resampling.LANCZOS)
            x = sample_x + idx * (thumb_w + gap)
            draw.rounded_rectangle((x, sample_y, x + thumb_w, sample_y + thumb_h),
                                   radius=18, fill=WHITE, outline="#CFC9BC", width=4)
            image.paste(art, (x + (thumb_w-art.width)//2, sample_y + (thumb_h-art.height)//2))
    # Empty white barcode reserve: 2.25 x 1.25 inches, outside all back-cover copy.
    bx = back_x + trim_w - round(2.55 * scale)
    by = bleed_px + trim_h - round(1.55 * scale)
    draw.rectangle((bx, by, bx + round(2.25 * scale), by + round(1.25 * scale)), fill=WHITE)

    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=94, optimize=True, dpi=(300, 300), subsampling=0)
    return {"trim_size_inches": [TRIM_W, TRIM_H], "page_count": page_count,
            "paper": "white", "ink": "black", "spine_width_inches": round(spine, 6),
            "bleed_inches": BLEED, "cover_pdf_size_inches": [round(cover_w_in, 6), cover_h_in],
            "cover_raster_pixels": [width, height], "cover_raster_dpi": 300,
            "spine_text": False, "barcode_reserve_inches": [2.25, 1.25]}


def _draw_vector_cover_decorations(c: canvas.Canvas, page_w: float, page_h: float,
                                   font_bold: str) -> None:
    c.setFillColorRGB(0.15, 0.23, 0.2)
    c.setFont(font_bold, 22)
    c.drawCentredString(page_w / 2, page_h - 142, "LITTLE HAUNTS CLUB")


def build_kdp_files(pages: list[Path], labels: list[str], manifest: dict[str, Any],
                    output_root: Path, approvals: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a 36-page KDP review edition for 17 illustrations and report hard checks."""
    if len(pages) != 17:
        return {"status": "not_prepared", "reason": "The Halloween paperback is scoped to the 17-page edition."}

    target = output_root / "kdp_paperback"
    target.mkdir(parents=True, exist_ok=True)
    font, bold_font = _register_pdf_fonts()
    title = "Little Haunts Club: A Very Odd Halloween"
    subtitle = "17 Cozy-Spooky Coloring Scenes"
    interior_path = target / "Little-Haunts-Club-Halloween-Interior-8.5x11.pdf"
    cover_preview = target / "cover_wrap_preview.jpg"
    cover_pdf = target / "Little-Haunts-Club-Halloween-Cover-Wrap.pdf"
    page_count = 2 + 2 * len(pages)

    c = canvas.Canvas(str(interior_path), pagesize=(TRIM_W * 72, TRIM_H * 72), pageCompression=1, invariant=1)
    c.setTitle(title)
    c.setAuthor("Little Haunts Club")
    c.setSubject("Black-and-white Halloween coloring paperback interior review draft")
    w, h = TRIM_W * 72, TRIM_H * 72
    c.setFillColorRGB(0.96, 0.95, 0.92)
    c.rect(0, 0, w, h, fill=1, stroke=0)
    c.setStrokeColorRGB(0.15, 0.23, 0.2)
    c.setLineWidth(2)
    c.roundRect(44, 44, w - 88, h - 88, 22, stroke=1, fill=0)
    c.setFillColorRGB(0.15, 0.23, 0.2)
    c.setFont(bold_font, 18)
    c.drawCentredString(w / 2, h - 120, "LITTLE HAUNTS CLUB")
    c.setFont(bold_font, 38)
    c.drawCentredString(w / 2, h - 220, "A VERY ODD")
    c.drawCentredString(w / 2, h - 272, "HALLOWEEN")
    c.setFont(font, 17)
    c.drawCentredString(w / 2, h - 322, subtitle)
    c.setFont(font, 14)
    c.setFillColorRGB(0.39, 0.51, 0.42)
    c.drawCentredString(w / 2, 115, "A coloring book for cozy-spooky afternoons")
    c.showPage()

    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, w, h, fill=1, stroke=0)
    c.setFillColorRGB(0.15, 0.23, 0.2)
    c.setFont(bold_font, 24)
    c.drawString(62, h - 85, "A little note before you color")
    c.setFont(font, 13)
    c.setFillColorRGB(0.18, 0.2, 0.18)
    instructions = [
        "These 17 original Little Haunts Club scenes were selected for this Halloween edition.",
        "Each illustration is printed on one side of the page. The reverse side is intentionally blank",
        "to give your coloring a clean surface and help limit show-through.",
        "Colored pencils, crayons, or markers designed for coloring books work well. If you use markers,",
        "place a spare sheet behind the page and test your colors first.",
        "Take your time, choose any colors you like, and enjoy meeting each odd little neighbor.",
        "",
        "AI content disclosure for KDP title setup: the interior illustrations and the book text were",
        "created with generative AI tools, then reviewed and prepared for this edition.",
    ]
    y = h - 132
    for line in instructions:
        if line:
            c.drawString(62, y, line)
        y -= 26
    c.setFont(font, 10)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(62, 62, "Halloween 2026 Edition · Little Haunts Club")
    c.showPage()

    raster_sizes: list[list[int]] = []
    for page_index, path in enumerate(pages, 1):
        with Image.open(path) as source:
            gray = ImageOps.grayscale(source)
            # 7.75 x 10 inches at 300 DPI, leaving >=0.375 inch on every side.
            art = gray.resize((2325, 3000), Image.Resampling.LANCZOS).point(lambda v: 0 if v < 180 else 255, mode="L")
            raster_sizes.append(list(art.size))
            buf = BytesIO()
            art.save(buf, format="PNG", optimize=True, dpi=(300, 300))
            buf.seek(0)
            c.drawImage(ImageReader(buf), 0.375 * 72, 0.5 * 72,
                        width=7.75 * 72, height=10 * 72, preserveAspectRatio=False, mask=None)
        c.showPage()
        c.showPage()  # intentionally blank reverse side
    c.save()

    cover_info = _make_cover(pages[0], [f"{label}\u241f{path}" for label, path in zip(labels, pages)],
                             title, len(pages), cover_preview)
    with Image.open(cover_preview) as cover_img:
        cw, ch = cover_img.size
    pdf_cover_w, pdf_cover_h = cover_info["cover_pdf_size_inches"]
    cv = canvas.Canvas(str(cover_pdf), pagesize=(pdf_cover_w * 72, pdf_cover_h * 72), pageCompression=1, invariant=1)
    cv.setTitle(f"{title} - paperback cover wrap")
    cv.setAuthor("Little Haunts Club")
    cv.drawImage(str(cover_preview), 0, 0, width=pdf_cover_w * 72, height=pdf_cover_h * 72)
    cv.showPage()
    cv.save()

    # Verify page count, exact trim, single-page art placement, blank backs, cover box and images.
    doc = fitz.open(interior_path)
    page_sizes = [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in doc]
    art_page_numbers = [3 + 2 * i for i in range(len(pages))]
    for number in art_page_numbers:
        page = doc[number - 1]
        images = page.get_images(full=True)
        if len(images) != 1:
            doc.close()
            raise RuntimeError(f"KDP interior page {number} should contain one image; got {len(images)}")
        rects = page.get_image_rects(images[0][0])
        if len(rects) != 1 or rects[0].x0 < 27 - 0.1 or rects[0].y0 < 36 - 0.1 or rects[0].x1 > w - 27 + 0.1 or rects[0].y1 > h - 36 + 0.1:
            doc.close()
            raise RuntimeError(f"KDP interior page {number} image exceeds the safe margin")
    blank_even_pages = [4 + 2 * i for i in range(len(pages))]
    for number in blank_even_pages:
        if doc[number - 1].get_images(full=True):
            doc.close()
            raise RuntimeError(f"KDP interior reverse page {number} is not blank")
    doc.close()
    if len(page_sizes) != page_count:
        raise RuntimeError(f"KDP interior must be {page_count} pages, found {len(page_sizes)}")
    cover_doc = fitz.open(cover_pdf)
    cover_points = [round(cover_doc[0].rect.width, 3), round(cover_doc[0].rect.height, 3)]
    if len(cover_doc) != 1 or abs(cover_points[0] - pdf_cover_w * 72) > 1 or abs(cover_points[1] - pdf_cover_h * 72) > 1:
        cover_doc.close()
        raise RuntimeError("KDP cover wrap PDF dimensions do not match its calculated cover size")
    cover_doc.close()

    selection = approvals or {}
    approval_rows = selection.get("pages", [])
    approved_count = sum(bool(item.get("human_approved")) for item in approval_rows)
    if len(approval_rows) != len(pages):
        approved_count = 0
    metadata = {
        "title": title, "subtitle": subtitle, "author": "Little Haunts Club",
        "language": "English", "edition": "Halloween 2026", "trim_size": "8.5 x 11 in",
        "interior": "Black ink on white paper, no bleed, left-to-right, single-sided artwork",
        "page_count": page_count, "coloring_illustration_count": len(pages),
        "ai_generated_content_disclosure": {"images": True, "text": True, "cover": True,
            "seller_entry_required_during_kdp_title_setup": True},
        "description": ("Step into the Little Haunts Club, where friendly spooky neighbors make "
            "Halloween feel cozy and curious. Meet a ghostly tailor, a skeleton astronomer, a zombie "
            "chef, and more in 17 original storybook scenes. Each black-and-white illustration has "
            "room to color, and every page is printed single-sided with a blank reverse for a more "
            "comfortable coloring experience. This paperback contains 17 coloring illustrations "
            "plus a title page and a short how-to-use page. No claim of hand-drawn artwork is made."),
        "keywords": ["cute spooky halloween coloring", "cozy halloween coloring book", "friendly monster coloring pages",
                     "skeleton and ghost coloring", "single sided coloring book", "storybook halloween activity",
                     "whimsical spooky coloring book"],
        "categories_to_review_in_kdp": ["Juvenile Nonfiction / Activity Books / Coloring", "Games & Activities / Coloring Books"],
        "suggested_price": "Set after checking KDP's current marketplace print-cost calculator; no price is preselected.",
        "isbn": "Use a KDP-provided free ISBN if desired; do not print a blank or invented ISBN on the cover.",
        "artwork_approval": {"approved_pages": approved_count, "total_pages": len(pages),
            "status": "approved" if approved_count == len(pages) else "review required"},
        "page_order": [{"book_page": 3 + index * 2, "illustration": index + 1, "label": labels[index],
                         "reverse_page_blank": 4 + index * 2} for index in range(len(pages))],
        "cover": cover_info,
        "source_raster": {"dimensions": [1536, 1984], "native_effective_dpi_at_7.75x10": [198.2, 198.4],
            "submitted_raster": [2325, 3000], "submitted_dpi": 300,
            "note": "Submitted art rasters are interpolated to 300 DPI from the 1536x1984 source. Upsampling does not add source detail; inspect the KDP Print Previewer before upload."},
        "direct_links": {"bookshelf": "https://kdp.amazon.com/en_US/bookshelf",
            "cover_calculator": "https://kdp.amazon.com/cover-templates?language=en_US",
            "print_cost_calculator": "https://kdp.amazon.com/en_US/royalty-calculator"},
    }
    (target / "kdp_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    readme = f"""# KDP paperback review draft

These files are prepared from the selected Halloween artwork. Current human
approval is {approved_count}/{len(pages)} pages; this is a review draft and is
not cleared for publication or upload yet.

## Files

- `{interior_path.name}`: {page_count}-page US Letter (8.5 x 11 in) black-ink, white-paper, no-bleed interior.
- `{cover_pdf.name}`: one-page full cover wrap, 0.125 in bleed, 300 DPI raster, no spine text (under 79 pages), with an empty barcode reserve.
- `{cover_preview.name}`: raster preview of that wrap.
- `kdp_metadata.json`: title, description, keyword suggestions, page map, dimensions, and KDP disclosure notes.

## Interior structure

Pages 1-2 contain a title page and useful coloring/printing guidance. The 17
illustrations occupy odd-numbered pages 3-35. Each reverse side is blank to
reduce show-through; page 36 is also blank. No decorative filler pages are used.

## Remaining KDP checks

1. Approve all 17 illustrations in the local dashboard and rebuild.
2. Review the cover and manuscript in KDP's Print Previewer. Confirm trim,
   title, page order, barcode placement, categories, keywords, and pricing.
3. Disclose AI-generated interior and cover images, and AI-generated text, in
   KDP's content questionnaire. The cover includes no spine text because it is
   under 79 pages.
4. Enter marketplace, ink, paper, and territories, then use KDP's live print
   cost calculator to choose a price. The product has not been uploaded.

The line art source is 1536 x 1984 pixels (about 198 DPI at its placed size).
The print PDF embeds interpolated 2325 x 3000 images at 300 DPI. Upsampling
does not create new detail; inspect the KDP Print Previewer for the final print
result before approving the paperback.
"""
    (target / "KDP_README.md").write_text(readme, encoding="utf-8")
    qa = {"status": "PASS_WITH_REVIEW_GATES" if approved_count < len(pages) else "PASS_AWAITING_KDP_PREVIEWER",
          "page_count": page_count, "page_size_points": page_sizes[0], "page_size_inches": [TRIM_W, TRIM_H],
          "minimum_page_count_24": page_count >= 24, "art_pages": art_page_numbers,
          "blank_reverse_pages": blank_even_pages, "ordered_art_pages": len(art_page_numbers) == len(pages),
          "embedded_art_raster_pixels": sorted(set(tuple(size) for size in raster_sizes)),
          "source_native_effective_dpi": 198.2, "interpolated_to_300dpi": True,
          "image_margin_inches": {"inside": 0.375, "outside": 0.375, "top": 0.5, "bottom": 0.5},
          "cover_page_size_points": cover_points, "cover_calculated_inches": cover_info["cover_pdf_size_inches"],
          "cover_dpi": 300, "cover_has_spine_text": False, "barcode_reserve_empty": True,
          "approved_pages": approved_count, "total_illustrations": len(pages),
          "print_previewer_live_check": False, "uploaded": False, "published": False}
    (target / "kdp_qa.json").write_text(json.dumps(qa, indent=2), encoding="utf-8")
    return {"status": qa["status"], "directory": str(target), "qa": qa}


def _pin_art(source: Path, label: str, output: Path, index: int, *, linked: bool = False) -> None:
    width, height = 1000, 1500
    palette = [("#EEE8DC", INK, MOSS), ("#EBE4EE", INK, LILAC), ("#F0E3D6", INK, PUMPKIN), ("#E5EDE4", INK, MOSS)]
    bg, ink, accent = palette[(index - 1) % len(palette)]
    canvas = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((28, 28, 972, 1472), radius=40, outline=ink, width=5)
    _centered_text(d, (500, 112), "LITTLE HAUNTS CLUB", _font(35, True), ink)
    scene = label.split("·", 1)[-1].strip()
    lines = textwrap.wrap(scene, width=26)[:2]
    for line_no, line in enumerate(lines):
        _centered_text(d, (500, 205 + line_no * 58), line, _font(46, True), ink)
    d.rounded_rectangle((105, 345, 895, 1235), radius=26, fill=WHITE, outline="#D6D0C5", width=4)
    with Image.open(source) as art:
        art = ImageOps.grayscale(art).convert("RGB")
        art.thumbnail((735, 835), Image.Resampling.LANCZOS)
        canvas.paste(art, (500 - art.width // 2, 790 - art.height // 2))
    _centered_text(d, (500, 1304), "17 STORYBOOK SCENES", _font(42, True), accent)
    _centered_text(d, (500, 1366), "PRINTABLE PDF + PNG BUNDLE", _font(28, True), ink)
    _centered_text(d, (500, 1422), "Shop link in Pin" if linked else "Add link when listed", _font(23), ink)
    canvas.save(output, "JPEG", quality=93, optimize=True, subsampling=0)


def build_social_files(pages: list[Path], labels: list[str], manifest: dict[str, Any],
                       output_root: Path, listing_url: str | None = None) -> dict[str, Any]:
    target = output_root / "social_promotion"
    pinterest = target / "pinterest"
    shorts = target / "shorts_stills"
    pinterest.mkdir(parents=True, exist_ok=True)
    shorts.mkdir(parents=True, exist_ok=True)
    selection = [0, 4, 10, 16]
    destination = listing_url or "{PUBLIC_LISTING_URL_AFTER_LAUNCH}"
    pins = []
    captions = [
        ("Meet the Little Haunts Club", "Meet 17 friendly spooky neighbors in this cozy Halloween coloring collection. Downloadable print-at-home pages include US Letter and A4 PDFs plus individual PNGs. Add the live shop link after launch."),
        ("A Cozy-Spooky Coloring Book", "A ghostly tailor, a sky-mapping skeleton, a zombie chef, and more share 17 original Halloween scenes with crisp black outlines and open spaces to color. The live listing link will be added after launch."),
        ("Printable Halloween Pages", "Choose colored pencils and settle in with 17 odd little Halloween stories. Digital bundle includes print-ready US Letter and A4 PDFs and individual page PNGs. Link to the shop will be added after launch."),
        ("The Zombie Chef Is Cooking", "The zombie chef stirs harvest soup in one of 17 cozy-spooky Little Haunts Club scenes. Get the printable PDF + PNG set from the live listing once it is published."),
    ]
    for i, page_index in enumerate(selection, 1):
        out = pinterest / f"pinterest_pin_{i:02d}.jpg"
        _pin_art(pages[page_index], labels[page_index], out, i, linked=bool(listing_url))
        pins.append({"file": out.name, "destination_url": destination,
                     "title": captions[i - 1][0], "description": captions[i - 1][1] + f"\n\nShop link: {destination}",
                     "dimensions": [1000, 1500], "aspect_ratio": "2:3"})

    # Three portrait stills can be uploaded directly as Instagram stories, TikTok
    # photo posts, or Shorts source cards without requiring a new model run.
    vertical = []
    for i, page_index in enumerate([0, 10, 16], 1):
        w, h = 1080, 1920
        card = Image.new("RGB", (w, h), ["#EEE8DC", "#EBE4EE", "#F0E3D6"][i-1])
        d = ImageDraw.Draw(card)
        d.rounded_rectangle((30, 30, w-30, h-30), radius=48, outline=INK, width=6)
        _centered_text(d, (w//2, 158), "LITTLE HAUNTS CLUB", _font(43, True), INK)
        _centered_text(d, (w//2, 275), "A VERY ODD HALLOWEEN", _font(50, True), MOSS)
        with Image.open(pages[page_index]) as art:
            art = ImageOps.grayscale(art).convert("RGB")
            art.thumbnail((880, 1200), Image.Resampling.LANCZOS)
            card.paste(art, (w//2-art.width//2, 950-art.height//2))
        scene_lines = textwrap.wrap(labels[page_index].split("·", 1)[-1].strip(), width=29)[:2]
        for line_no, line in enumerate(scene_lines):
            _centered_text(d, (w//2, 1640 + line_no*62), line, _font(42, True), INK)
        _centered_text(d, (w//2, 1782), "17 PRINTABLE COLORING SCENES", _font(34, True), PUMPKIN)
        _centered_text(d, (w//2, 1845), "Shop link in post" if listing_url else "Add link when listed", _font(27), INK)
        out = shorts / f"vertical_promo_{i:02d}.jpg"
        card.save(out, "JPEG", quality=92, optimize=True, subsampling=0)
        vertical.append(out)

    # A single 9-second local MP4, made only from existing stills, is convenient
    # to repurpose. The caller may use it if FFmpeg is installed.
    shop_tail = f" Shop: {destination}" if listing_url else " Shop link added after launch."
    captions_md = ["# Halloween promotional copy", "",
                   (f"Verified product destination: {destination}" if listing_url else
                    "The listing URL is intentionally blank until a public product URL is verified."),
                   "", "## Pinterest Pins", ""]
    for i, item in enumerate(pins, 1):
        captions_md.extend([f"### Pin {i}: {item['title']}", "", item["description"], "",
                            f"Image: pinterest/{item['file']}", f"Destination: {destination}", ""])
    captions_md.extend(["## Instagram", "", "Caption: Meet the Little Haunts Club: 17 friendly spooky neighbors, one cozy Halloween coloring page at a time. Printable US Letter and A4 PDFs plus individual PNGs." + shop_tail,
                        "", "## TikTok", "", "Caption: A VERY ODD HALLOWEEN 🎃 Meet the ghost tailor, skeleton astronomer, zombie chef, and their cozy-spooky neighbors. 17 printable coloring scenes." + shop_tail,
                        "", "## YouTube Shorts", "", "Title: 17 Cozy-Spooky Halloween Coloring Pages | Little Haunts Club", "Description: A quick look inside Little Haunts Club: A Very Odd Halloween. 17 original storybook coloring scenes, supplied as printable PDFs and individual PNGs." + shop_tail,
                        "", "## Use", "",
                        ("Use the verified public product URL as the Pinterest destination and replace the link on each social platform. These files have not been uploaded or posted."
                         if listing_url else
                         "Replace {PUBLIC_LISTING_URL_AFTER_LAUNCH} only after the dashboard verifies the live public URL. These files have not been uploaded or posted."), ""])
    (target / "social_captions.md").write_text("\n".join(captions_md), encoding="utf-8")
    video = target / "shorts_promo.mp4"
    ffmpeg = shutil.which("ffmpeg")
    video_status = "not_built_ffmpeg_unavailable"
    if ffmpeg:
        inputs = []
        for path in vertical:
            inputs.extend(["-loop", "1", "-t", "3", "-i", str(path)])
        filters = []
        for i in range(len(vertical)):
            filters.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
        concat = "".join(f"[v{i}]" for i in range(len(vertical))) + f"concat=n={len(vertical)}:v=1:a=0[outv]"
        try:
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs,
                "-filter_complex", ";".join([*filters, concat]), "-map", "[outv]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(video)], check=True, timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            video_status = "ready_for_manual_upload"
        except (subprocess.SubprocessError, OSError):
            video.unlink(missing_ok=True)
            video_status = "not_built_ffmpeg_failed"
    info = {"status": "ready_for_manual_upload", "published": False, "pinterest_pins": pins,
            "portrait_stills": [{"file": p.name, "dimensions": [1080, 1920]} for p in vertical],
            "video": {"file": video.name if video.is_file() else None, "status": video_status,
                      "duration_seconds": 9 if video.is_file() else None, "published": False},
            "listing_url_placeholder": None if listing_url else "{PUBLIC_LISTING_URL_AFTER_LAUNCH}",
            "destination_url": listing_url}
    (target / "social_assets.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return {"status": "ready_for_manual_upload", "directory": str(target), "pin_count": len(pins), "vertical_stills": len(vertical)}


def refresh_social_listing_link(package_root: Path, listing_url: str) -> dict[str, Any]:
    """Refresh promotion files with a URL only after the dashboard verifies it."""
    pages = sorted((package_root / "pages").glob("page_*.png"))
    metadata_path = package_root / "etsy_listing_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    order = metadata.get("page_order", [])
    if len(pages) != 17 or len(order) != 17:
        raise ValueError("The verified destination can only be added to the 17-page Halloween promotion kit")
    labels = [str(item.get("label", f"Page {index:02d}")) for index, item in enumerate(order, 1)]
    return build_social_files(pages, labels, {"product_id": metadata.get("product_id")}, package_root, listing_url)


def build_distribution_assets(pages: list[Path], page_specs: list[dict[str, Any]],
                              manifest: dict[str, Any], output_root: Path,
                              approval_state: dict[str, Any] | None = None) -> dict[str, Any]:
    labels = [str(spec.get("label", f"Page {i:02d}")) for i, spec in enumerate(page_specs, 1)]
    approvals = approval_state or {}
    kdp = build_kdp_files(pages, labels, manifest, output_root, approvals)
    social = build_social_files(pages, labels, manifest, output_root)
    result = {"product_id": manifest.get("product_id"), "page_count": len(pages),
              "kdp": kdp, "social": social, "etsy": {"status": "ready_after_all_artwork_approved",
                  "digital_files": len(list((output_root / "etsy_uploads").glob("*"))),
                  "max_files": 5, "max_each_mb": 20, "listing_images": len(list((output_root / "listing_images").glob("*.jpg"))),
                  "proposed_price_usd": "4.99", "published": False}}
    (output_root / "distribution_qa.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
