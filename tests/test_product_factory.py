import json
import tempfile
import unittest
from pathlib import Path

from scripts import product_factory


class ProductFactoryTests(unittest.TestCase):
    def test_completion_is_review_only_and_never_published(self):
        report = product_factory.build_completion_report(
            {"product_id": "fixture", "pages": [{"page": 1}]}, "run", "qwen3-vl:30b",
            {"candidates": {"1": {"attempts": [{"file": "page.png"}]}}},
            Path("deliverables"), {"etsy_upload_files": [{"name": "print.pdf"}], "pdf_render_checks": []})
        self.assertEqual(report["status"], "READY_FOR_FINAL_HUMAN_REVIEW")
        self.assertTrue(report["human_approval_required"])
        self.assertFalse(report["published"])

    def test_assembly_rejects_unreviewed_pages_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "product"
            (source / "pages").mkdir(parents=True)
            (source / "prompts.json").write_text(json.dumps({"product_id": "fixture"}), encoding="utf-8")
            product = {"product_id": "fixture", "pages": [{"page": 1}]}
            with self.assertRaisesRegex(RuntimeError, "not accepted"):
                product_factory.make_assembly(source, root / "run", product, {"candidates": {"1": {"status": "manual_review"}}})

    def test_read_config_surfaces_missing_vision_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            result = product_factory.read_config(Path(temp))
        self.assertFalse(result["ready"])
        self.assertIn("vision endpoint and model", result["reason"])


if __name__ == "__main__":
    unittest.main()
