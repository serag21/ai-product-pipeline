import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts import package_printable_product as package


class PackageTests(unittest.TestCase):
    def test_package_creates_and_renders_upload_files(self):
        with tempfile.TemporaryDirectory() as temp:
            product = Path(temp) / "one-page-product"
            (product / "pages").mkdir(parents=True)
            (product / "qa").mkdir()
            image = Image.new("RGB", (1536, 1984), "white")
            ImageDraw.Draw(image).rectangle((100, 100, 1300, 1800), outline="black", width=30)
            image.save(product / "pages/page_001.png")
            manifest = {"product_id": "test-coloring-product", "file_prefix": "Test-Product",
                "title": "Test printable coloring page", "brand": "TEST BRAND", "subtitle": "ONE PAGE",
                "pages": [{"page": 1, "label": "Test page", "prompt": "A simple test page"}],
                "listing": {"title": "Test printable coloring page", "description": "One test page.",
                    "tags": ["coloring page"], "hero_page_numbers": [1], "gallery_page_numbers": [1]}}
            (product / "prompts.json").write_text(json.dumps(manifest), encoding="utf-8")
            (product / "qa/generation_manifest.json").write_text(json.dumps({
                "summary": {"passed": 1, "failed": 0}}), encoding="utf-8")
            output = Path(temp) / "deliverables"
            with patch.object(sys, "argv", ["package_printable_product.py", "--product", str(product), "--output", str(output)]):
                self.assertEqual(package.main(), 0)
            qa = json.loads((output / "package_qa.json").read_text(encoding="utf-8"))
            self.assertEqual(qa["prepared_pages"], 1)
            self.assertEqual(len(qa["etsy_upload_files"]), 3)
            self.assertEqual(len(qa["pdf_render_checks"]), 2)
            self.assertTrue(all(row["render_samples_pass"] for row in qa["pdf_render_checks"]))
            self.assertEqual(qa["duplicate_check"]["exact_duplicates"], [])
            self.assertTrue((output / "etsy_listing_draft.md").is_file())
            self.assertTrue((output / "listing_images/listing_01_hero.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
