from __future__ import annotations

import unittest

from scripts.business_worker import validate_request


class BusinessWorkerRequestTests(unittest.TestCase):
    def request(self) -> dict:
        return {
            "schema_version": 1,
            "job_id": "2026-10-17-little-haunts-001",
            "action": "generate_for_review",
            "product_id": "little-haunts-club-spooky-neighbors-15",
            "start": 1,
            "count": 15,
            "branch": "main",
            "server": "http://127.0.0.1:8188",
        }

    def test_accepts_valid_local_generation_request(self) -> None:
        result = validate_request(
            self.request(),
            "2026-10-17-little-haunts-001.json",
            "main",
        )
        self.assertEqual(result["action"], "generate_for_review")
        self.assertEqual(result["count"], 15)
        self.assertEqual(result["server"], "http://127.0.0.1:8188")

    def test_rejects_arbitrary_action(self) -> None:
        data = self.request()
        data["action"] = "run_shell_command"
        with self.assertRaisesRegex(RuntimeError, "Unsupported action"):
            validate_request(data, "2026-10-17-little-haunts-001.json", "main")

    def test_rejects_filename_job_id_mismatch(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must match job_id"):
            validate_request(self.request(), "another-job.json", "main")

    def test_rejects_path_traversal_product_id(self) -> None:
        data = self.request()
        data["product_id"] = "../secrets"
        with self.assertRaisesRegex(RuntimeError, "product_id"):
            validate_request(data, "2026-10-17-little-haunts-001.json", "main")

    def test_rejects_nonlocal_comfyui_endpoint(self) -> None:
        data = self.request()
        data["server"] = "http://attacker.example:8188"
        with self.assertRaisesRegex(RuntimeError, "local ComfyUI"):
            validate_request(data, "2026-10-17-little-haunts-001.json", "main")

    def test_rejects_branch_mismatch(self) -> None:
        data = self.request()
        data["branch"] = "feature"
        with self.assertRaisesRegex(RuntimeError, "branch"):
            validate_request(data, "2026-10-17-little-haunts-001.json", "main")

    def test_rejects_bad_count(self) -> None:
        data = self.request()
        data["count"] = 1000
        with self.assertRaisesRegex(RuntimeError, "count"):
            validate_request(data, "2026-10-17-little-haunts-001.json", "main")


if __name__ == "__main__":
    unittest.main()
