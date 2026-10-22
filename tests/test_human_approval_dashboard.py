from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts.human_approval_dashboard import Dashboard
from scripts.product_review_worker import REVIEW_POLICY_VERSION


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "product"
        self.run = self.root / "review_runs" / "run1"
        (self.run / "candidates").mkdir(parents=True)
        (self.run / "assembly_source" / "pages").mkdir(parents=True)
        (self.root / "qa").mkdir()
        (self.root / "pages").mkdir()
        self.manifest = {"product_id": "test", "title": "Test Book", "file_prefix": "Test-Book", "pages": [
            {"page": 1, "label": "First scene", "variant_id": "A"},
            {"page": 2, "label": "Second scene", "variant_id": "B"},
        ]}
        (self.root / "prompts.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        config = {"vision": {"endpoint": "http://127.0.0.1:11434/v1", "model": "qwen3-vl:30b", "max_tokens": 100},
                  "generation": {"max_attempts": 14, "accept_score": 80}}
        (self.root / "factory_config.json").write_text(json.dumps(config), encoding="utf-8")
        self.candidates = {}
        for page in (1, 2):
            file = self.run / "candidates" / f"page_{page:03d}_attempt_01.png"
            image = Image.new("RGB", (1536, 1984), "white")
            ImageDraw.Draw(image).rectangle((100, 100, 400, 400), outline="black", width=6)
            ImageDraw.Draw(image).ellipse((500 + page * 50, 200, 800 + page * 50, 500), outline="black", width=8)
            image.save(file)
            self.candidates[str(page)] = {"page": page, "status": "accepted_by_vision", "best_candidate": str(file), "attempts": [
                {"attempt": 1, "file": str(file), "qa": {"pass": True}, "review_policy_version": REVIEW_POLICY_VERSION,
                 "review": {"decision": "accept", "score": 94, "summary": "Clear coloring areas.", "defects": [], "checks": {"coloring_areas_open": True}}}
            ]}
            shutil.copy2(file, self.run / "assembly_source" / "pages" / f"page_{page:03d}.png")
        (self.run / "state.json").write_text(json.dumps({"candidates": self.candidates}), encoding="utf-8")
        self.final = self.run / "final_deliverables"
        self.final.mkdir()
        qa = {"prepared_pages": 2, "etsy_file_count": 4,
              "etsy_upload_files": [{"under_configured_limit": True, "under_safe_19mb": True} for _ in range(4)],
              "listing_images": [{"dimensions": [2400, 2400]} for _ in range(4)],
              "pdf_render_checks": [{"page_count": 2, "ordered_pages_verified": 2,
                  "embedded_page_images_verified": 2, "render_samples_pass": True} for _ in range(2)],
              "duplicate_check": {"exact_duplicates": []}}
        (self.final / "package_qa.json").write_text(json.dumps(qa), encoding="utf-8")
        self.dashboard = Dashboard(self.root, "run1")

    def tearDown(self):
        self.temp.cleanup()

    def test_approval_gate_persistence_and_invalidation(self):
        with self.assertRaisesRegex(ValueError, "Approve all"):
            self.dashboard.approve()
        self.dashboard.decide(1, "keep")
        self.dashboard.decide(2, "keep")
        self.assertEqual(Dashboard(self.root, "run1").data()["approved_pages"], 2)
        approved = self.dashboard.approve()
        self.assertFalse(approved["published"])
        self.dashboard.decide(1, "needs_changes", "Please simplify the background")
        human = self.dashboard.human()
        self.assertIsNone(human["product_approval"])
        self.assertEqual(human["pages"]["1"]["status"], "needs_changes")

    def test_promote_candidate_requires_current_vision_acceptance_and_invalidates_qa(self):
        alternative = self.run / "candidates" / "page_001_attempt_02.png"
        shutil.copy2(self.candidates["1"]["attempts"][0]["file"], alternative)
        state = self.dashboard.load_json(self.dashboard.state_path)
        state["candidates"]["1"]["attempts"].append({"attempt": 2, "file": str(alternative), "qa": {"pass": True},
            "review_policy_version": REVIEW_POLICY_VERSION, "review": {"decision": "accept", "score": 90, "summary": "Alternative", "defects": []}})
        self.dashboard.state_path.write_text(json.dumps(state), encoding="utf-8")
        data = self.dashboard.data()
        promoted = next(c for c in data["pages"][0]["promotable"] if c["candidate_file"] == str(alternative))
        self.assertTrue(promoted["eligible"])
        self.dashboard.decide(1, "replace", candidate_file=str(alternative))
        after = self.dashboard.data()
        self.assertFalse(after["qa_passed"])
        self.assertEqual(after["pages"][0]["human_status"], "needs_review")
        with self.assertRaisesRegex(ValueError, "explicitly approved"):
            self.dashboard.rebuild_package()

    def test_rejected_alternatives_are_bounded_and_explain_defects(self):
        state = self.dashboard.load_json(self.dashboard.state_path)
        for attempt in range(2, 7):
            path = self.run / "candidates" / f"page_001_attempt_{attempt:02}.png"
            shutil.copy2(state["candidates"]["1"]["attempts"][0]["file"], path)
            state["candidates"]["1"]["attempts"].append({"attempt": attempt, "file": str(path), "qa": {"pass": True},
                "review": {"decision": "retry", "score": 80-attempt, "summary": "Large black fill", "defects": ["Doorway is uncolorable"]}})
        self.dashboard.state_path.write_text(json.dumps(state), encoding="utf-8")
        alternatives = self.dashboard.data()["pages"][0]["alternatives"]
        self.assertEqual(len(alternatives), 3)
        self.assertIn("Doorway", alternatives[0]["defects"][0])

    def test_regeneration_queues_only_human_rejected_pages(self):
        self.dashboard.decide(2, "reject", "Please redraw this one")

        class FakeProcess:
            pid = 123
            def poll(self): return None

        with patch("scripts.human_approval_dashboard.subprocess.Popen", return_value=FakeProcess()) as popen, \
             patch("scripts.human_approval_dashboard.threading.Thread") as thread:
            result = self.dashboard.regenerate()
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--pages"):command.index("--pages") + 2], ["--pages", "2"])
        self.assertIn("--vision-model", command)
        self.assertIn("qwen3-vl:30b", command)
        thread.return_value.start.assert_called_once()
        saved = self.dashboard.human()
        self.assertEqual(saved["pages"]["2"]["status"], "regenerating")
        self.assertNotIn("1", saved["pages"])
        self.assertEqual(result["pages"], [2])

    def test_rebuild_package_creates_new_qa_validated_output_without_touching_factory_package(self):
        self.dashboard.decide(1, "keep")
        self.dashboard.decide(2, "keep")
        old_qa = (self.final / "package_qa.json").read_bytes()
        result = self.dashboard.rebuild_package()
        output = Path(result["package_directory"])
        self.assertTrue(result["qa_passed"])
        self.assertEqual(json.loads((output / "package_qa.json").read_text()) ["prepared_pages"], 2)
        self.assertTrue((output / "etsy_uploads" / "Test-Book-Letter.pdf").is_file())
        self.assertEqual((self.final / "package_qa.json").read_bytes(), old_qa)
        self.assertTrue(self.dashboard.data()["qa_passed"])
        self.assertIsNone(self.dashboard.human()["product_approval"])
        approval = self.dashboard.approve()
        self.assertTrue(approval["ok"])
        self.assertFalse(approval["published"])
        self.assertTrue(Path(self.dashboard.human()["product_approval"]["package_qa"]).is_file())


if __name__ == "__main__":
    unittest.main()
