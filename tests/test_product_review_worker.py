import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts import product_review_worker as local_worker


class FakeClient:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self.events = []

    def health(self):
        return {"devices": ["mock GPU"]}

    @staticmethod
    def load_workflow(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def submit(self, workflow):
        self.calls += 1
        self.events.append("generate")
        return f"prompt-{self.calls}"

    def free_memory(self):
        self.events.append("free_comfy_vram")

    def wait(self, prompt_id, timeout=900):
        return {"outputs": {"9": {"images": [{"filename": "mock.png"}]}}}

    def download_image(self, info, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1536, 1984), "white").save(destination)
        # Make the image pass basic contrast QA.
        image = Image.open(destination)
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle((100, 100, 900, 1700), outline="black", width=30)
        image.save(destination)
        return destination


class FakeReviewer:
    def __init__(self, reviews, events=None):
        self.reviews = iter(reviews)
        self.prompts = []
        self.events = events

    def review(self, image, criteria, prior_feedback=""):
        if self.events is not None:
            self.events.append("vision_review")
        self.prompts.append(prior_feedback)
        return next(self.reviews)


class LocalWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.product = Path(self.temp.name) / "product"
        self.product.mkdir()
        original = json.loads((local_worker.ROOT / "products/little-haunts-club-targeted-iteration-lab-6/prompts.json").read_text(encoding="utf-8"))
        original["pages"] = original["pages"][:1]
        (self.product / "prompts.json").write_text(json.dumps(original), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def args(self, *extra):
        return local_worker.parse_args(["--product", str(self.product), "--run-id", "test-run", *extra])

    def test_no_provider_creates_manual_bundle_without_claiming_review(self):
        self.assertEqual(local_worker.run(self.args(), client=FakeClient()), 0)
        run = self.product / "review_runs/test-run"
        state = json.loads((run / "state.json").read_text())
        self.assertEqual(state["candidates"]["1"]["status"], "manual_review")
        self.assertEqual(state["candidates"]["1"]["attempts"][0]["review"]["decision"], "manual")
        self.assertTrue((run / "REVIEW.md").is_file())
        self.assertTrue((run / "reviews.json").is_file())
        self.assertTrue((run / "candidates/page_001_attempt_01.png").is_file())

    def test_retry_keeps_both_candidates_and_records_feedback(self):
        client = FakeClient()
        reviewer = FakeReviewer([
            {"decision": "retry", "score": 35, "summary": "Birdhouse is black-filled.", "defects": ["opening is dark"], "retry_prompt": "Keep opening white."},
            {"decision": "accept", "score": 90, "summary": "Clear line art.", "defects": [], "retry_prompt": ""},
        ], client.events)
        self.assertEqual(local_worker.run(self.args("--max-attempts", "2"), client=client, reviewer=reviewer), 0)
        self.assertEqual(client.events, ["generate", "free_comfy_vram", "vision_review",
                                        "generate", "free_comfy_vram", "vision_review"])
        record = json.loads((self.product / "review_runs/test-run/state.json").read_text())["candidates"]["1"]
        self.assertEqual(record["status"], "accepted_by_vision")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertNotEqual(record["attempts"][0]["file"], record["attempts"][1]["file"])
        self.assertIn("black-filled", reviewer.prompts[1])
        self.assertTrue(all(Path(c["file"]).is_file() for c in record["attempts"]))

    def test_state_resume_skips_completed_candidate(self):
        reviewer = FakeReviewer([{"decision": "accept", "score": 90}])
        client = FakeClient()
        local_worker.run(self.args(), client=client, reviewer=reviewer)
        local_worker.run(self.args(), client=client, reviewer=reviewer)
        self.assertEqual(client.calls, 1)

    def test_existing_candidate_review_does_not_touch_source_or_generate(self):
        source = self.product / "pages/page_001.png"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (1536, 1984), "white").save(source)
        from PIL import ImageDraw
        with Image.open(source) as image:
            ImageDraw.Draw(image).rectangle((100, 100, 900, 1700), outline="black", width=30)
            image.save(source)
        before = source.read_bytes()
        reviewer = FakeReviewer([{"decision": "accept", "score": 94, "summary": "Good page."}])
        client = FakeClient()
        self.assertEqual(local_worker.run(self.args("--review-existing"), client=client, reviewer=reviewer), 0)
        self.assertEqual(client.calls, 0)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(reviewer.prompts, [""])

    def test_retry_exhaustion_retains_every_candidate(self):
        reviewer = FakeReviewer([
            {"decision": "retry", "score": 40, "summary": "First defect", "defects": ["clutter"]},
            {"decision": "retry", "score": 50, "summary": "Still weak", "defects": ["dark opening"]},
        ])
        client = FakeClient()
        self.assertEqual(local_worker.run(self.args("--max-attempts", "2"), client=client, reviewer=reviewer), 0)
        record = json.loads((self.product / "review_runs/test-run/state.json").read_text())["candidates"]["1"]
        self.assertEqual(record["status"], "exhausted")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertTrue(all(Path(candidate["file"]).is_file() for candidate in record["attempts"]))
        self.assertEqual(client.calls, 2)

    def test_increasing_attempt_bound_resumes_exhausted_page(self):
        run = self.product / "review_runs/test-run"
        run.mkdir(parents=True)
        product_id = json.loads((self.product / "prompts.json").read_text(encoding="utf-8"))["product_id"]
        local_worker.atomic_json(run / "state.json", {
            "schema_version": 1,
            "product_id": product_id,
            "candidates": {"1": {
                "page": 1,
                "status": "exhausted",
                "best_candidate": "previous-best.png",
                "attempts": [
                    {"attempt": 1, "file": "attempt-1.png", "review": {"decision": "retry", "summary": "first"}},
                    {"attempt": 2, "file": "attempt-2.png", "review": {"decision": "retry", "summary": "second"}},
                ],
            }},
        })
        client = FakeClient()
        reviewer = FakeReviewer([{"decision": "accept", "score": 92, "summary": "Resolved."}])

        self.assertEqual(local_worker.run(self.args("--max-attempts", "3"), client=client, reviewer=reviewer), 0)

        record = json.loads((run / "state.json").read_text(encoding="utf-8"))["candidates"]["1"]
        self.assertEqual(record["status"], "accepted_by_vision")
        self.assertEqual([candidate["attempt"] for candidate in record["attempts"]], [1, 2, 3])
        self.assertEqual(client.calls, 1)

    def test_low_accept_score_is_retried_and_malformed_provider_fails_closed(self):
        reviewer = FakeReviewer([
            {"decision": "accept", "score": 65, "summary": "Several scene issues."},
            {"decision": "accept", "score": 90, "summary": "Resolved."},
        ])
        local_worker.run(self.args("--max-attempts", "2", "--accept-score", "80"), client=FakeClient(), reviewer=reviewer)
        state = json.loads((self.product / "review_runs/test-run/state.json").read_text())
        self.assertEqual(state["candidates"]["1"]["status"], "accepted_by_vision")
        self.assertEqual(state["candidates"]["1"]["attempts"][0]["review"]["decision"], "retry")
        response = type("Response", (), {"raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "not json"}}]}})()
        with patch.object(local_worker.requests, "post", return_value=response):
            with self.assertRaises(json.JSONDecodeError):
                local_worker.OpenAICompatibleReviewer("http://localhost:1234/v1", "vision").review(
                    Path(state["candidates"]["1"]["attempts"][0]["file"]), "criteria")

    def test_ollama_model_is_unloaded_after_real_vision_review(self):
        image_path = self.product / "sample.png"
        Image.new("RGB", (32, 32), "white").save(image_path)
        response = type("Response", (), {"raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": json.dumps({
                "decision": "accept", "score": 91, "summary": "clear line art", "defects": [],
            })}}]}})()
        unload = type("Response", (), {"raise_for_status": lambda self: None})()
        with patch.object(local_worker.requests, "post", side_effect=[response, unload]) as post:
            reviewer = local_worker.OpenAICompatibleReviewer(
                "http://127.0.0.1:11434/v1", "qwen3-vl:30b",
                unload_endpoint="http://127.0.0.1:11434/api/generate")
            result = reviewer.review(image_path, "test criteria")
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/api/generate")
        self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], 0)

    def test_retry_bound_and_bad_configuration(self):
        with self.assertRaises(SystemExit):
            local_worker.parse_args(["--max-attempts", "6"])
        with self.assertRaises(SystemExit):
            local_worker.parse_args(["--vision-endpoint", "http://localhost:1234/v1"])


if __name__ == "__main__":
    unittest.main()
