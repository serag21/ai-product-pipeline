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

    def test_assembly_refreshes_existing_selected_page_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, run = root / "product", root / "run"
            (source / "pages").mkdir(parents=True)
            (source / "prompts.json").write_text("{}", encoding="utf-8")
            first, selected = root / "first.png", root / "selected.png"
            first.write_bytes(b"old")
            selected.write_bytes(b"new")
            assembly_page = run / "assembly_source/pages/page_001.png"
            assembly_page.parent.mkdir(parents=True)
            assembly_page.write_bytes(b"stale")
            product = {"product_id": "fixture", "pages": [{"page": 1}]}
            state = {"candidates": {"1": {"status": "accepted_by_vision", "best_candidate": str(selected),
                "attempts": [{"file": str(selected), "qa": {"pass": True}, "review_policy_version": product_factory.REVIEW_POLICY_VERSION,
                    "review": {"decision": "accept"}}]}}}
            product_factory.make_assembly(source, run, product, state)
            self.assertEqual(assembly_page.read_bytes(), b"new")

    def test_quality_change_report_lists_only_policy_rejections_that_were_regenerated(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            product = {"pages": [{"page": 1}, {"page": 2}]}
            state = {"candidates": {
                "1": {"best_candidate": "new.png", "attempts": [
                    {"file": "old.png", "review_history": [{"decision": "accept"}],
                     "review": {"decision": "retry", "defects": ["doorway is solid black"]}},
                    {"file": "new.png", "review": {"decision": "accept"}},
                ]},
                "2": {"best_candidate": "same.png", "attempts": [{"file": "same.png", "review": {"decision": "accept"}}]},
            }}
            product_factory.write_quality_change_report(product, state, output)
            report = (output / "QUALITY_REVIEW_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("Page 1: regenerated", report)
            self.assertIn("doorway is solid black", report)
            self.assertIn("Pages retained unchanged: 2", report)


if __name__ == "__main__":
    unittest.main()
