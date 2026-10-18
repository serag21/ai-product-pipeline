from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import local_worker


class LocalWorkerTests(unittest.TestCase):
    def test_extract_job_markers(self):
        body = """Queue this job:
<job-json>
{"type": "generate_review_set", "product": "products/example"}
</job-json>
"""
        self.assertEqual(
            local_worker.extract_job(body)["type"],
            "generate_review_set",
        )

    def test_extract_job_rejects_missing_markers(self):
        with self.assertRaises(ValueError):
            local_worker.extract_job('{"type": "generate_review_set"}')

    def test_product_path_rejects_traversal(self):
        with self.assertRaises(ValueError):
            local_worker.safe_product_path("products/../.git")

    def test_validate_candidate_review_job(self):
        job = local_worker.validate_job({
            "type": "generate_review_set",
            "product": "products/little-haunts-club-monster-workshop-lab-3",
            "start": 1,
            "count": 3,
            "sheet_mode": "auto",
            "server": "http://127.0.0.1:8188",
        })
        self.assertEqual(len(job["_selected_pages"]), 3)
        self.assertEqual(job["type"], "generate_review_set")

    def test_worker_rejects_remote_comfy_server(self):
        with self.assertRaisesRegex(ValueError, "local ComfyUI"):
            local_worker.validate_job({
                "type": "generate_review_set",
                "product": "products/little-haunts-club-monster-workshop-lab-3",
                "count": 1,
                "server": "http://example.com:8188",
            })

    def test_packaging_requires_both_approval_flags(self):
        with self.assertRaisesRegex(ValueError, "owner_approved=true"):
            local_worker.validate_job({
                "type": "package_approved_product",
                "product": "products/little-haunts-club-monster-workshop-lab-3",
                "title": "Final title",
                "owner_approved": True,
            })

    def test_packaging_refuses_variant_labs(self):
        with self.assertRaisesRegex(ValueError, "candidate/variant lab"):
            local_worker.validate_job({
                "type": "package_approved_product",
                "product": "products/little-haunts-club-monster-workshop-lab-3",
                "title": "Final title",
                "owner_approved": True,
                "selection_confirmed": True,
            })


if __name__ == "__main__":
    unittest.main()
