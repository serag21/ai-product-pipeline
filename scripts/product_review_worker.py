#!/usr/bin/env python
"""Resumable local art generation, vision review, and bounded retry controller."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from comfy.client import ComfyUIClient, ComfyUIError
from generate_product import (  # noqa: E402
    GUIDANCE_NODE, LATENT_NODE, OUTPUT_NODE, PROMPT_NODE, REFERENCE_WORKFLOW_PATH,
    SCHEDULER_NODE, SEED_NODE, WORKFLOW_PATH, build_prompt, generation_settings,
    load_product, quality_check, resolve_product_root, validate_workflow,
)


class VisionReviewer(Protocol):
    def review(self, image: Path, criteria: str, prior_feedback: str = "") -> dict[str, Any]: ...


class OpenAICompatibleReviewer:
    """Adapter for local or remote OpenAI-compatible multimodal chat endpoints."""
    def __init__(self, endpoint: str, model: str, api_key: str | None = None, timeout: int = 180,
                 unload_endpoint: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        if self.endpoint.endswith("/chat/completions"):
            self.url = self.endpoint
        else:
            self.url = self.endpoint + "/chat/completions"
        self.model, self.api_key, self.timeout = model, api_key, timeout
        self.unload_endpoint = unload_endpoint.rstrip("/") if unload_endpoint else None

    def unload(self) -> None:
        """Free a local inference model before ComfyUI is allowed to generate."""
        if not self.unload_endpoint:
            return
        response = requests.post(self.unload_endpoint, timeout=self.timeout, json={
            "model": self.model, "prompt": "", "stream": False, "keep_alive": 0,
        })
        response.raise_for_status()

    def check(self) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = requests.get(self.endpoint + "/models", headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        model_ids = [str(row.get("id", "")) for row in payload.get("data", []) if isinstance(row, dict)]
        if model_ids and self.model not in model_ids:
            raise RuntimeError(f"Vision model {self.model!r} was not returned by {self.endpoint}/models. Available: {', '.join(model_ids)}")
        # Confirm the configured model accepts images before starting a long GPU run.
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (96, 96), "white")
        ImageDraw.Draw(image).rectangle((28, 28, 68, 68), fill="black")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        probe = requests.post(self.url, headers={**headers, "Content-Type": "application/json"}, timeout=self.timeout, json={
            "model": self.model, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Inspect the image. Return JSON only: {\"vision_ok\": true} if you can see a black square centered on white."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]}], "response_format": {"type": "json_object"},
        })
        try:
            probe.raise_for_status()
            probe_content = probe.json()["choices"][0]["message"]["content"]
            if isinstance(probe_content, list):
                probe_content = "".join(part.get("text", "") for part in probe_content)
            if not json.loads(probe_content).get("vision_ok"):
                raise RuntimeError(f"Configured model {self.model!r} did not confirm the image input probe.")
        finally:
            self.unload()

    def review(self, image: Path, criteria: str, prior_feedback: str = "") -> dict[str, Any]:
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        instruction = (
            "Review this coloring-page candidate against the art direction. Return only JSON with keys "
            "decision (accept|retry|manual), score (0-100), summary, defects (array of strings), "
            "retry_prompt (string). Accept only when it meets the criteria. If uncertain choose manual.\n"
            f"Criteria: {criteria}\nPrevious review feedback to address: {prior_feedback or 'none'}"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.post(self.url, headers=headers, timeout=self.timeout, json={
            "model": self.model, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]}],
            "response_format": {"type": "json_object"},
        })
        try:
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            result = json.loads(content)
        finally:
            self.unload()
        return validate_review(result)


def validate_review(review: dict[str, Any]) -> dict[str, Any]:
    decision = str(review.get("decision", "manual")).lower()
    if decision not in {"accept", "retry", "manual"}:
        decision = "manual"
    try:
        score = max(0, min(100, int(review.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    defects = review.get("defects", [])
    return {
        "decision": decision, "score": score,
        "summary": str(review.get("summary", "")),
        "defects": [str(v) for v in defects] if isinstance(defects, list) else [],
        "retry_prompt": str(review.get("retry_prompt", "")),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_state(path: Path, product_id: str) -> dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("product_id") != product_id:
            raise RuntimeError("Run state belongs to a different product.")
        return state
    return {"schema_version": 1, "product_id": product_id, "created_at": utc_now(),
            "updated_at": utc_now(), "candidates": {}, "human_approval_required": True}


def criteria_for(product: dict[str, Any], item: dict[str, Any]) -> str:
    return "\n".join([
        f"Product: {product.get('title', product['product_id'])}",
        f"Scene: {item.get('candidate_title', item.get('label', item.get('variant_id', 'candidate')))}",
        f"Art direction: {product.get('visual_bible', '')}",
        f"Scene rationale: {item.get('design_rationale', '')}",
        f"Specific QA focus: {item.get('qa_focus', '')}",
        "Check concept readability, anatomy, physical logic, open coloring areas, clean linework, clutter, and unintended color or filled areas.",
    ])


def make_workflow(item: dict[str, Any], product: dict[str, Any], workflow: dict[str, Any],
                  width: int, height: int, steps: int, guidance: float, prompt: str,
                  seed: int, page_no: int, attempt: int, run_id: str) -> dict[str, Any]:
    wf = copy.deepcopy(workflow)
    wf[PROMPT_NODE]["inputs"]["text"] = prompt
    wf[SEED_NODE]["inputs"]["noise_seed"] = seed
    wf[LATENT_NODE]["inputs"].update(width=width, height=height)
    wf[SCHEDULER_NODE]["inputs"].update(width=width, height=height, steps=steps)
    wf[GUIDANCE_NODE]["inputs"]["guidance"] = guidance
    wf[OUTPUT_NODE]["inputs"]["filename_prefix"] = f"review_{run_id}_page_{page_no:03d}_try_{attempt:02d}"
    return wf


def generate_one(client: ComfyUIClient, item: dict[str, Any], product: dict[str, Any],
                 workflow: dict[str, Any], destination: Path, page_no: int, attempt: int,
                 run_id: str, prompt: str, seed: int, timeout: int) -> dict[str, Any]:
    width, height, steps, guidance = generation_settings(product)
    wf = make_workflow(item, product, workflow, width, height, steps, guidance,
                       prompt, seed, page_no, attempt, run_id)
    prompt_id = client.submit(wf)
    history = client.wait(prompt_id, timeout=timeout)
    images = history.get("outputs", {}).get(OUTPUT_NODE, {}).get("images", [])
    if not images:
        raise ComfyUIError(f"No images returned from output node {OUTPUT_NODE}.", prompt_id)
    client.download_image(images[-1], destination)
    return {"prompt_id": prompt_id, "qa": quality_check(destination)}


def create_manual_bundle(run_dir: Path, state: dict[str, Any]) -> None:
    rows = ["# Art review bundle", "", "Automated visual review was not configured or a review needs human attention.",
            "No candidate is marked as visually approved. Human approval remains required.", ""]
    for key, record in sorted(state["candidates"].items()):
        rows.extend([f"## {key} — {record.get('status', 'pending')}", ""])
        if record.get("best_candidate"):
            rows.append(f"Best-known candidate: `{record['best_candidate']}`")
        for candidate in record.get("attempts", []):
            rows.append(f"- Attempt {candidate['attempt']}: `{candidate['file']}` — {candidate.get('review', {}).get('summary', 'pending manual review')}")
        rows.append("")
    (run_dir / "REVIEW.md").write_text("\n".join(rows), encoding="utf-8")
    atomic_json(run_dir / "reviews.json", {
        "schema_version": 1, "generated_at": utc_now(), "automated_review_configured": state.get("vision_configured", False),
        "human_approval_required": True, "candidates": state["candidates"],
    })


def run(args: argparse.Namespace, client: ComfyUIClient | None = None,
        reviewer: VisionReviewer | None = None) -> int:
    product_root = resolve_product_root(args.product)
    product = load_product(product_root)
    pages = product.get("pages", [])
    requested = set(args.pages or [])
    selected = [p for p in pages if not requested or int(p["page"]) in requested]
    if not selected:
        raise RuntimeError("No pages selected; check --pages.")
    run_dir = (product_root / "review_runs" / args.run_id).resolve()
    if product_root.resolve() not in run_dir.parents:
        raise RuntimeError("Run path escapes the product directory.")
    state_path = run_dir / "state.json"
    state = read_state(state_path, product["product_id"])
    state["vision_configured"] = reviewer is not None
    state["max_attempts"] = args.max_attempts
    state["updated_at"] = utc_now()
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(state_path, state)

    if client is None:
        client = ComfyUIClient(args.server)
    health = client.health()
    print(f"ComfyUI: {args.server} | devices: {health.get('devices', [])}")
    reference = Path(args.reference).resolve() if args.reference else None
    workflow_path = REFERENCE_WORKFLOW_PATH if reference else WORKFLOW_PATH
    workflow = ComfyUIClient.load_workflow(workflow_path)
    validate_workflow(workflow)
    if reference:
        if not reference.is_file():
            raise RuntimeError(f"Reference image not found: {reference}")
        uploaded = client.upload_image(reference)
        name = uploaded.get("name", "")
        if uploaded.get("subfolder"):
            name = f"{uploaded['subfolder']}/{name}"
        workflow["70"]["inputs"]["image"] = name

    for item in selected:
        page_no = int(item["page"])
        key = str(page_no)
        record = state["candidates"].setdefault(key, {"page": page_no, "variant_id": item.get("variant_id"),
            "status": "pending", "attempts": [], "best_candidate": None})
        if record["status"] in {"accepted_by_vision", "accepted_by_human", "manual_review"}:
            continue
        if record["status"] == "exhausted":
            if not args.retry_exhausted:
                continue
            record["status"] = "pending"
        if record["status"] == "paused":
            record["status"] = "pending"
        if args.review_existing and not record["attempts"]:
            source = product_root / "pages" / f"page_{page_no:03d}.png"
            if not source.is_file():
                raise RuntimeError(f"Existing page requested for review but not found: {source}")
            original_manifest = product_root / "qa" / "generation_manifest.json"
            original_record = {}
            if original_manifest.is_file():
                original_pages = json.loads(original_manifest.read_text(encoding="utf-8")).get("pages", [])
                original_record = next((p for p in original_pages if int(p.get("page", -1)) == page_no), {})
            destination = run_dir / "candidates" / f"page_{page_no:03d}_existing.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            candidate = {"attempt": 1, "file": str(destination), "seed": original_record.get("seed", item.get("seed")),
                         "prompt_id": original_record.get("prompt_id", "existing-candidate"),
                         "source_file": str(source), "qa": quality_check(destination), "created_at": utc_now()}
            record["attempts"].append(candidate)
            record["best_candidate"] = candidate["file"]
            atomic_json(state_path, state)
        prior_feedback = ""
        if record["attempts"] and record["attempts"][-1].get("review"):
            prior_feedback = record["attempts"][-1].get("review", {}).get("summary", "") + "; " + "; ".join(record["attempts"][-1].get("review", {}).get("defects", []))
        while True:
            pending_candidate = record["attempts"][-1] if record["attempts"] and not record["attempts"][-1].get("review") else None
            if pending_candidate:
                attempt = int(pending_candidate["attempt"])
                destination = Path(pending_candidate["file"])
                generated = {"prompt_id": pending_candidate.get("prompt_id"), "qa": pending_candidate.get("qa", quality_check(destination))}
                candidate = pending_candidate
            else:
                if len(record["attempts"]) >= args.max_attempts:
                    record["status"] = "exhausted"
                    break
                attempt = len(record["attempts"]) + 1
                destination = run_dir / "candidates" / f"page_{page_no:03d}_attempt_{attempt:02d}.png"
                if destination.exists():
                    attempt += 1
                    destination = run_dir / "candidates" / f"page_{page_no:03d}_attempt_{attempt:02d}_resume_{int(time.time())}.png"
                prompt = build_prompt(product, item)
                if prior_feedback:
                    prompt += f"\n\nPrevious review feedback to fix: {prior_feedback}. Make a new composition that directly corrects these issues."
                base_seed = int(item["seed"]) if item.get("seed") is not None else 120000 + page_no * 7919
                seed = (base_seed + attempt - 1) % (2**32)
                generated = generate_one(client, item, product, workflow, destination, page_no, attempt,
                                         args.run_id, prompt, seed, args.timeout)
                candidate = {"attempt": attempt, "file": str(destination), "seed": seed,
                             "prompt_id": generated["prompt_id"], "qa": generated["qa"], "created_at": utc_now()}
                record["attempts"].append(candidate)
                if record["best_candidate"] is None or generated["qa"].get("pass"):
                    record["best_candidate"] = candidate["file"]
                # Persist the immutable image record before calling a remote review service.
                atomic_json(state_path, state)
            try:
                if not generated["qa"].get("pass"):
                    candidate["review"] = {"decision": "manual", "score": 0,
                        "summary": "Basic image integrity check failed; requires human review.", "defects": [], "retry_prompt": ""}
                    record["status"] = "manual_review"
                    break
                if reviewer is None:
                    candidate["review"] = {"decision": "manual", "score": 0,
                        "summary": "Vision provider not configured; no visual review was performed.", "defects": [], "retry_prompt": ""}
                    record["status"] = "manual_review"
                    break
                if hasattr(client, "free_memory"):
                    # Do not run Qwen-VL alongside the FLUX checkpoint. Ollama
                    # is unloaded again by the reviewer before any retry.
                    client.free_memory()
                review = validate_review(reviewer.review(destination, criteria_for(product, item), prior_feedback))
                if review["decision"] == "accept" and review["score"] < args.accept_score:
                    review["decision"] = "retry"
                    review["summary"] = f"Score {review['score']} is below acceptance threshold {args.accept_score}. " + review["summary"]
                candidate["review"] = review
                if review["decision"] == "accept":
                    record["status"] = "accepted_by_vision"
                    record["best_candidate"] = candidate["file"]
                    break
                if review["decision"] == "manual":
                    record["status"] = "manual_review"
                    break
                prior_feedback = "; ".join(filter(None, [review["summary"], "; ".join(review["defects"]), review["retry_prompt"]]))
                current_best = next((c for c in record["attempts"] if c["file"] == record["best_candidate"]), None)
                if current_best is None or review["score"] > current_best.get("review", {}).get("score", -1):
                    record["best_candidate"] = candidate["file"]
            except KeyboardInterrupt:
                record["status"] = "paused"
                state["updated_at"] = utc_now()
                atomic_json(state_path, state)
                create_manual_bundle(run_dir, state)
                print(f"Paused. Resume with --run-id {args.run_id}.")
                return 130
            except Exception as exc:
                record["status"] = "paused"
                record["last_error"] = str(exc)
                state["updated_at"] = utc_now()
                atomic_json(state_path, state)
                create_manual_bundle(run_dir, state)
                raise
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
        if record["status"] == "pending":
            record["status"] = "exhausted"
        atomic_json(state_path, state)
    state["updated_at"] = utc_now()
    atomic_json(state_path, state)
    create_manual_bundle(run_dir, state)
    print(f"Run bundle: {run_dir}")
    print("Human approval is required before any candidate can be used in a final product.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", default="products/little-haunts-club-targeted-iteration-lab-6")
    parser.add_argument("--pages", type=int, nargs="*", help="Manifest page numbers; defaults to all pages")
    parser.add_argument("--review-existing", action="store_true", help="Review and preserve existing pages before generating replacements")
    parser.add_argument("--retry-exhausted", action="store_true", help="Continue exhausted scenes after raising --max-attempts")
    parser.add_argument("--approve", type=int, nargs="+", help="Record human approval for generated candidate pages and exit")
    parser.add_argument("--run-id", default="targeted-review")
    parser.add_argument("--server", default=os.getenv("COMFYUI_URL", "http://127.0.0.1:8188"))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--accept-score", type=int, default=80)
    parser.add_argument("--reference")
    parser.add_argument("--vision-endpoint", default=os.getenv("VISION_API_BASE_URL"))
    parser.add_argument("--vision-model", default=os.getenv("VISION_MODEL"))
    parser.add_argument("--vision-api-key", default=os.getenv("VISION_API_KEY"))
    parser.add_argument("--vision-unload-endpoint", default=os.getenv("VISION_UNLOAD_ENDPOINT"))
    args = parser.parse_args(argv)
    if args.max_attempts < 1 or args.max_attempts > 5:
        parser.error("--max-attempts must be between 1 and 5")
    if not 1 <= args.accept_score <= 100:
        parser.error("--accept-score must be between 1 and 100")
    if bool(args.vision_endpoint) != bool(args.vision_model):
        parser.error("Configure both --vision-endpoint and --vision-model, or neither")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.approve:
        product_root = resolve_product_root(args.product)
        product = load_product(product_root)
        state_path = product_root / "review_runs" / args.run_id / "state.json"
        state = read_state(state_path, product["product_id"])
        for page in args.approve:
            record = state.get("candidates", {}).get(str(page))
            if not record or not record.get("best_candidate"):
                print(f"Cannot approve page {page}: no generated candidate is recorded.", file=sys.stderr)
                return 2
            record["status"] = "accepted_by_human"
            record["human_approved_at"] = utc_now()
        state["updated_at"] = utc_now()
        atomic_json(state_path, state)
        create_manual_bundle(state_path.parent, state)
        print(f"Human approval recorded for pages: {', '.join(map(str, args.approve))}")
        return 0
    reviewer = OpenAICompatibleReviewer(args.vision_endpoint, args.vision_model, args.vision_api_key,
                                        unload_endpoint=args.vision_unload_endpoint) if args.vision_endpoint else None
    try:
        return run(args, reviewer=reviewer)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
