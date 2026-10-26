from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw

from scripts.distribution_assets import build_distribution_assets, build_kdp_files, refresh_social_listing_link


class DistributionAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pages_dir = self.root / "pages"
        self.pages_dir.mkdir()
        self.pages = []
        self.labels = []
        for number in range(1, 18):
            path = self.pages_dir / f"page_{number:03d}.png"
            image = Image.new("L", (1536, 1984), 255)
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((130 + number, 120, 1400 - number, 1850), outline=0, width=7)
            draw.ellipse((440 + number, 420, 1080, 1100), outline=0, width=8)
            image.save(path)
            self.pages.append(path)
            self.labels.append(f"{number:02d} · Scene {number}")
        self.output = self.root / "package"
        self.output.mkdir()
        self.manifest = {"product_id": "little-haunts-club-spooky-neighbors-17",
                         "title": "Little Haunts Club: A Very Odd Halloween"}
        self.approvals = {"pages": [{"human_approved": i < 12} for i in range(17)]}

    def tearDown(self):
        self.temp.cleanup()

    def test_kdp_draft_has_36_pages_blank_backs_and_checked_cover_geometry(self):
        result = build_kdp_files(self.pages, self.labels, self.manifest, self.output, self.approvals)
        kdp = self.output / "kdp_paperback"
        interior = fitz.open(kdp / "Little-Haunts-Club-Halloween-Interior-8.5x11.pdf")
        cover = fitz.open(kdp / "Little-Haunts-Club-Halloween-Cover-Wrap.pdf")
        self.assertEqual(len(interior), 36)
        self.assertEqual(len(cover), 1)
        self.assertAlmostEqual(interior[0].rect.width, 612, places=1)
        self.assertAlmostEqual(interior[0].rect.height, 792, places=1)
        art_image_counts = [len(interior[i].get_images(full=True)) for i in range(2, 36, 2)]
        blank_image_counts = [len(interior[i].get_images(full=True)) for i in range(3, 36, 2)]
        self.assertEqual(art_image_counts, [1] * 17)
        self.assertEqual(blank_image_counts, [0] * 17)
        self.assertEqual(result["status"], "PASS_WITH_REVIEW_GATES")
        metadata = json.loads((kdp / "kdp_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["artwork_approval"]["approved_pages"], 12)
        self.assertEqual(metadata["artwork_approval"]["status"], "review required")
        self.assertEqual(metadata["cover"]["spine_text"], False)
        qa = json.loads((kdp / "kdp_qa.json").read_text(encoding="utf-8"))
        self.assertEqual(qa["blank_reverse_pages"], list(range(4, 37, 2)))
        self.assertFalse(qa["uploaded"])
        interior.close(); cover.close()

    def test_social_assets_are_correct_sizes_and_keep_listing_url_as_placeholder(self):
        result = build_distribution_assets(self.pages, [{"page": i + 1, "label": self.labels[i]} for i in range(17)],
            self.manifest, self.output, self.approvals)
        self.assertEqual(result["social"]["pin_count"], 4)
        social = self.output / "social_promotion"
        captions = (social / "social_captions.md").read_text(encoding="utf-8")
        self.assertIn("{PUBLIC_LISTING_URL_AFTER_LAUNCH}", captions)
        self.assertIn("have not been uploaded or posted", captions)
        for image in sorted((social / "pinterest").glob("*.jpg")):
            with Image.open(image) as pin:
                self.assertEqual(pin.size, (1000, 1500))
        for image in sorted((social / "shorts_stills").glob("*.jpg")):
            with Image.open(image) as still:
                self.assertEqual(still.size, (1080, 1920))

    def test_verified_listing_url_is_inserted_into_pin_destinations_and_captions(self):
        final_pages = self.output / "pages"
        final_pages.mkdir()
        for index, source in enumerate(self.pages, 1):
            (final_pages / f"page_{index:03d}.png").write_bytes(source.read_bytes())
        (self.output / "etsy_listing_metadata.json").write_text(json.dumps({
            "product_id": self.manifest["product_id"],
            "page_order": [{"page": i, "label": self.labels[i - 1]} for i in range(1, 18)]
        }), encoding="utf-8")
        listing = "https://www.etsy.com/listing/123456789/little-haunts"

        refresh_social_listing_link(self.output, listing)

        assets = json.loads((self.output / "social_promotion" / "social_assets.json").read_text(encoding="utf-8"))
        self.assertEqual(assets["destination_url"], listing)
        self.assertTrue(all(pin["destination_url"] == listing for pin in assets["pinterest_pins"]))
        captions = (self.output / "social_promotion" / "social_captions.md").read_text(encoding="utf-8")
        self.assertIn(f"Destination: {listing}", captions)
        self.assertIn(f"Shop: {listing}", captions)
        self.assertNotIn("{PUBLIC_LISTING_URL_AFTER_LAUNCH}", captions)


if __name__ == "__main__":
    unittest.main()
