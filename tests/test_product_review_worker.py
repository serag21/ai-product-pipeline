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
        result = dict(next(self.reviews))
        result.setdefault("checks", {"no_large_solid_black_regions": True, "coloring_areas_open": True,
            "line_art_clean": True, "scene_readable_and_consistent": True})
        return result


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
        self.assertEqual(reviewer.prompts[1], "")
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
            {"decision": "accept", "score": 65, "summary": "Present rests on cushion.",
             "defects": ["Present should stand directly on the floor."]},
            {"decision": "accept", "score": 90, "summary": "Resolved."},
        ])
        local_worker.run(self.args("--max-attempts", "2", "--accept-score", "80"), client=FakeClient(), reviewer=reviewer)
        state = json.loads((self.product / "review_runs/test-run/state.json").read_text())
        self.assertEqual(state["candidates"]["1"]["status"], "accepted_by_vision")
        self.assertEqual(state["candidates"]["1"]["attempts"][0]["review"]["decision"], "retry")
        response = type("Response", (), {"raise_for_status": lambda self: None,
            "json": lambda self: {"choices": [{"message": {"content": "not json"}}]}})()
        with patch.object(local_worker.requests, "post", return_value=response):
            with patch.object(local_worker.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "no valid JSON after 3 attempts"):
                    local_worker.OpenAICompatibleReviewer("http://localhost:1234/v1", "vision").review(
                        Path(state["candidates"]["1"]["attempts"][0]["file"]), "criteria")

    def test_low_numeric_score_without_named_defect_does_not_reject_clear_checks(self):
        (self.product / "pages").mkdir(parents=True, exist_ok=True)
        sample_page = Image.new("RGB", (1536, 1984), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(sample_page).rectangle((300, 300, 1200, 1600), outline="black", width=8)
        sample_page.save(self.product / "pages/page_001.png")
        reviewer = FakeReviewer([{"decision": "accept", "score": 5,
            "summary": "All specific checks pass; no defects found.", "defects": [], "retry_prompt": ""}])
        local_worker.run(self.args("--review-existing", "--max-attempts", "1", "--accept-score", "80"),
                         client=FakeClient(), reviewer=reviewer)
        state = json.loads((self.product / "review_runs/test-run/state.json").read_text())
        candidate = state["candidates"]["1"]["attempts"][0]
        self.assertEqual(state["candidates"]["1"]["status"], "accepted_by_vision")
        self.assertEqual(candidate["review"]["decision"], "accept")

    def test_vision_retries_transient_empty_reply(self):
        image_path = self.product / "sample.png"
        Image.new("RGB", (32, 32), "white").save(image_path)
        empty = type("Response", (), {"raise_for_status": lambda self: None,
            "json": lambda self: {"message": {"content": ""}}})()
        valid = type("Response", (), {"raise_for_status": lambda self: None,
            "json": lambda self: {"message": {"content": json.dumps({
                "decision": "accept", "score": 92, "summary": "clear line art", "defects": [],
                "retry_prompt": "", "checks": {"no_large_solid_black_regions": True,
                "coloring_areas_open": True, "line_art_clean": True, "scene_readable_and_consistent": True},
            })}}})()
        unload = type("Response", (), {"raise_for_status": lambda self: None})()
        with patch.object(local_worker.requests, "post", side_effect=[empty, valid, unload]) as post, \
             patch.object(local_worker.time, "sleep"):
            reviewer = local_worker.OpenAICompatibleReviewer(
                "http://127.0.0.1:11434/v1", "qwen3-vl:30b",
                unload_endpoint="http://127.0.0.1:11434/api/generate")
            result = reviewer.review(image_path, "test criteria")
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args_list[0].args[0], "http://127.0.0.1:11434/api/chat")
        self.assertFalse(post.call_args_list[0].kwargs["json"]["think"])

    def test_review_validator_preserves_a_single_defect_string(self):
        review = local_worker.validate_review({"decision": "retry", "score": 60, "defects": "prop is unsupported"})
        self.assertEqual(review["defects"], ["prop is unsupported"])

    def test_failed_specific_check_overrides_high_numerical_score(self):
        review = local_worker.validate_review({"decision": "accept", "score": 99, "summary": "Looks good.",
            "defects": [], "retry_prompt": "", "checks": {"no_large_solid_black_regions": False,
            "coloring_areas_open": True, "line_art_clean": True, "scene_readable_and_consistent": True}})
        self.assertEqual(review["decision"], "retry")
        self.assertTrue(any("large solid black" in defect for defect in review["defects"]))
        self.assertIn("Specific visual checks failed", review["summary"])

    def test_policy_rereview_preserves_acceptable_winner_without_generation(self):
        source = self.product / "pages/page_001.png"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (1536, 1984), "white").save(source)
        from PIL import ImageDraw
        with Image.open(source) as image:
            ImageDraw.Draw(image).rectangle((100, 100, 900, 1700), outline="black", width=30)
            image.save(source)
        client, reviewer = FakeClient(), FakeReviewer([{"decision": "accept", "score": 93,
            "summary": "Clear and colorable.", "checks": {"no_large_solid_black_regions": True,
            "coloring_areas_open": True, "line_art_clean": True, "scene_readable_and_consistent": True}}])
        local_worker.run(self.args("--review-existing", "--re-review-accepted"), client=client, reviewer=reviewer)
        state_path = self.product / "review_runs/test-run/state.json"
        state = json.loads(state_path.read_text())
        # Simulate a previously accepted winner, then re-review it under policy 2.
        record = state["candidates"]["1"]
        record["attempts"][0]["review_policy_version"] = 1
        local_worker.atomic_json(state_path, state)
        reviewer = FakeReviewer([{"decision": "accept", "score": 97, "summary": "Passes strict checks.",
            "checks": {"no_large_solid_black_regions": True, "coloring_areas_open": True,
            "line_art_clean": True, "scene_readable_and_consistent": True}}])
        self.assertEqual(local_worker.run(self.args("--re-review-accepted"), client=client, reviewer=reviewer), 0)
        state = json.loads(state_path.read_text())
        record = state["candidates"]["1"]
        self.assertEqual(client.calls, 0)
        self.assertEqual(record["status"], "accepted_by_vision")
        self.assertEqual(record["attempts"][0]["review_policy_version"], local_worker.REVIEW_POLICY_VERSION)
        self.assertEqual(record["attempts"][0]["review_history"][0]["decision"], "accept")
        review_calls = len(reviewer.prompts)
        self.assertEqual(local_worker.run(self.args("--re-review-accepted"), client=client, reviewer=reviewer), 0)
        self.assertEqual(len(reviewer.prompts), review_calls)
        self.assertEqual(client.calls, 0)

    def test_pixel_gate_finds_a_large_solid_black_fill(self):
        image_path = self.product / "black-doorway.png"
        image = Image.new("RGB", (1536, 1984), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle((500, 500, 1000, 1100), fill="black")
        image.save(image_path)
        region = local_worker.find_large_solid_black_region(image_path)
        self.assertIsNotNone(region)
        checked = local_worker.enforce_image_checks(local_worker.validate_review({
            "decision": "accept", "score": 100, "summary": "Perfect.", "defects": [], "retry_prompt": "",
            "checks": {"no_large_solid_black_regions": True, "coloring_areas_open": True,
                "line_art_clean": True, "scene_readable_and_consistent": True},
        }), image_path)
        self.assertEqual(checked["decision"], "retry")
        self.assertFalse(checked["checks"]["no_large_solid_black_regions"])

    def test_retry_limit_is_bounded_at_fourteen(self):
        self.assertEqual(self.args("--max-attempts", "14").max_attempts, 14)
        with self.assertRaises(SystemExit):
            self.args("--max-attempts", "15")

    def test_current_scene_plan_overrides_conflicting_retry_feedback(self):
        result = local_worker.add_review_feedback("Draw a white plaque, not a hole.", "The hole was filled black.")
        self.assertIn("hole was filled black", result)
        self.assertIn("current scene prompt and QA focus are authoritative", result)

    def test_ollama_model_is_unloaded_after_real_vision_review(self):
        image_path = self.product / "sample.png"
        Image.new("RGB", (32, 32), "white").save(image_path)
        response = type("Response", (), {"raise_for_status": lambda self: None,
            "json": lambda self: {"message": {"content": json.dumps({
                "decision": "accept", "score": 91, "summary": "clear line art", "defects": [], "retry_prompt": "",
                "checks": {"no_large_solid_black_regions": True, "coloring_areas_open": True,
                    "line_art_clean": True, "scene_readable_and_consistent": True},
            })}}})()
        unload = type("Response", (), {"raise_for_status": lambda self: None})()
        with patch.object(local_worker.requests, "post", side_effect=[response, unload]) as post:
            reviewer = local_worker.OpenAICompatibleReviewer(
                "http://127.0.0.1:11434/v1", "qwen3-vl:30b",
                unload_endpoint="http://127.0.0.1:11434/api/generate",
                max_tokens=512, reasoning_effort="none")
            result = reviewer.review(image_path, "test criteria")
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(post.call_args_list[0].kwargs["json"]["options"]["num_predict"], 512)
        self.assertFalse(post.call_args_list[0].kwargs["json"]["think"])
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/api/generate")
        self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], 0)

    def test_retry_bound_and_bad_configuration(self):
        with self.assertRaises(SystemExit):
            local_worker.parse_args(["--max-attempts", "15"])
        with self.assertRaises(SystemExit):
            local_worker.parse_args(["--vision-endpoint", "http://localhost:1234/v1"])


if __name__ == "__main__":
    unittest.main()
