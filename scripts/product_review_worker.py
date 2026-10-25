#!/usr/bin/env python
"""Resumable local art generation, vision review, and bounded retry controller."""
from __future__ import annotations

import argparse
import hashlib
import base64
import copy
import json
import os
import shutil
import sys
import time
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
REVIEW_POLICY_VERSION = 8
REVIEW_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "retry", "manual"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "defects": {"type": "array", "items": {"type": "string"}},
        "retry_prompt": {"type": "string"},
        "checks": {"type": "object", "properties": {
            "no_large_solid_black_regions": {"type": "boolean"},
            "coloring_areas_open": {"type": "boolean"},
            "line_art_clean": {"type": "boolean"},
            "scene_readable_and_consistent": {"type": "boolean"},
        }, "required": ["no_large_solid_black_regions", "coloring_areas_open", "line_art_clean", "scene_readable_and_consistent"], "additionalProperties": False},
    },
    "required": ["decision", "score", "summary", "defects", "retry_prompt", "checks"],
    "additionalProperties": False,
}
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
                 unload_endpoint: str | None = None, max_tokens: int = 1024,
                 reasoning_effort: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        if self.endpoint.endswith("/chat/completions"):
            self.url = self.endpoint
        else:
            self.url = self.endpoint + "/chat/completions"
        self.model, self.api_key, self.timeout = model, api_key, timeout
        self.unload_endpoint = unload_endpoint.rstrip("/") if unload_endpoint else None
        endpoint_parts = urlsplit(self.endpoint)
        unload_path = urlsplit(self.unload_endpoint).path.rstrip("/") if self.unload_endpoint else ""
        is_loopback = endpoint_parts.hostname in {"127.0.0.1", "localhost", "::1"}
        self.ollama_chat_url = (f"{endpoint_parts.scheme}://{endpoint_parts.netloc}/api/chat"
                                if is_loopback and unload_path == "/api/generate" else None)
        self.max_tokens = max(1, int(max_tokens))
        self.reasoning_effort = reasoning_effort

    def completion_options(self, max_tokens: int | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {"max_tokens": max_tokens or self.max_tokens}
        if self.reasoning_effort:
            options["reasoning_effort"] = self.reasoning_effort
        return options

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
        if self.ollama_chat_url:
            probe = requests.post(self.ollama_chat_url, timeout=self.timeout, json={
                "model": self.model, "think": False, "stream": False,
                "messages": [{"role": "user", "content": "If this image contains a black square centered on white, set vision_ok true. /no_think",
                              "images": [encoded]}],
                "format": {"type": "object", "properties": {"vision_ok": {"type": "boolean"}},
                           "required": ["vision_ok"], "additionalProperties": False},
                "options": {"num_predict": min(256, self.max_tokens), "temperature": 0},
            })
        else:
            probe = requests.post(self.url, headers={**headers, "Content-Type": "application/json"}, timeout=self.timeout, json={
                "model": self.model, "temperature": 0,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Look at this image. If you see a black square centered on a white background, return exactly {\"vision_ok\": true}; otherwise return {\"vision_ok\": false}. JSON only. /no_think"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ]}], "response_format": {"type": "json_object"},
                **self.completion_options(max_tokens=min(256, self.max_tokens)),
            })
        try:
            probe.raise_for_status()
            probe_payload = probe.json()
            probe_content = (probe_payload["message"]["content"] if self.ollama_chat_url
                             else probe_payload["choices"][0]["message"]["content"])
            if isinstance(probe_content, list):
                probe_content = "".join(part.get("text", "") for part in probe_content)
            if not json.loads(probe_content).get("vision_ok"):
                raise RuntimeError(f"Configured model {self.model!r} did not confirm the image input probe.")
        finally:
            self.unload()

    def review(self, image: Path, criteria: str, prior_feedback: str = "") -> dict[str, Any]:
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        instruction = (
            f"Visually inspect this printable coloring page independently. Criteria: {criteria}\n"
            "Judge only what is visible in this image; do not assume any defect from a prior attempt or prompt. "
            "Return only the required JSON fields. Hard gate: broad solid-black regions that remove a colorable "
            "surface (doorway, window, tray, clothing, wing, hair, animal body, or prop) require retry, regardless "
            "of score. Localized eyes, pupils, noses, and mouth details may be black. Broad black-filled costume, "
            "wing, hair, fur, bird body, or major prop surfaces are uncolorable and must fail coloring_areas_open, "
            "including when fragmented into smaller patches. Large surfaces should be white inside clean outlines. "
            "Set each check honestly. Give named "
            "defects and a concrete retry_prompt. Check scene QA, coloring usability, line cleanliness, and consistency. "
            "/no_think\nPrevious defects to correct: " + (prior_feedback or "none")
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        result = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                if self.ollama_chat_url:
                    response = requests.post(self.ollama_chat_url, timeout=self.timeout, json={
                        "model": self.model, "think": False, "stream": False,
                        "messages": [{"role": "user", "content": instruction, "images": [encoded]}],
                        "format": REVIEW_JSON_SCHEMA,
                        "options": {"num_predict": self.max_tokens, "temperature": 0},
                    })
                else:
                    response = requests.post(self.url, headers=headers, timeout=self.timeout, json={
                        "model": self.model, "temperature": 0,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": instruction},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                        ]}],
                        "response_format": {"type": "json_object"},
                        **self.completion_options(),
                    })
                response.raise_for_status()
                payload = response.json()
                content = (payload["message"]["content"] if self.ollama_chat_url
                           else payload["choices"][0]["message"]["content"])
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content)
                if not content or not str(content).strip():
                    raise ValueError(f"Vision endpoint returned empty content (HTTP {getattr(response, 'status_code', 'unknown')}).")
                result = json.loads(content)
                break
            except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1 + attempt)
            finally:
                # Keep the reviewer loaded across transient retries. Unloading
                # between retries repeatedly cold-starts a large local model.
                if result is not None or attempt == 2:
                    self.unload()
        if result is None:
            raise RuntimeError(f"Vision review returned no valid JSON after 3 attempts: {last_error}") from last_error
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
    if isinstance(defects, str):
        defects = [defects] if defects else []
    check_names = ("no_large_solid_black_regions", "coloring_areas_open", "line_art_clean", "scene_readable_and_consistent")
    raw_checks = review.get("checks", {})
    checks = {name: raw_checks.get(name) for name in check_names} if isinstance(raw_checks, dict) else {name: None for name in check_names}
    failed = [name for name, value in checks.items() if value is not True]
    if decision == "accept" and failed:
        decision = "retry"
        reason = "Specific visual checks failed or were missing: " + ", ".join(failed)
        defects = list(defects) if isinstance(defects, list) else []
        defects.extend(f"Required printable-art check failed: {name.replace('_', ' ')}." for name in failed)
        summary = str(review.get("summary", ""))
        review = {**review, "summary": reason + (" " + summary if summary else "")}
    return {
        "decision": decision, "score": score,
        "summary": str(review.get("summary", "")),
        "defects": [str(v) for v in defects] if isinstance(defects, list) else [],
        "retry_prompt": str(review.get("retry_prompt", "")),
        "checks": checks,
    }


def find_large_solid_black_region(image_path: Path) -> dict[str, Any] | None:
    """Find broad near-black connected fills while ignoring thin line art."""
    from PIL import Image
    with Image.open(image_path) as source:
        gray = source.convert("L")
        gray.thumbnail((384, 512), Image.Resampling.BILINEAR)
        width, height = gray.size
        pixels = gray.load()
        visited = bytearray(width * height)
        best: tuple[int, int, int, int, int, int] | None = None
        for start_y in range(height):
            for start_x in range(width):
                start = start_y * width + start_x
                if visited[start] or pixels[start_x, start_y] >= 48:
                    continue
                visited[start] = 1
                pending = deque([start])
                area = 0
                min_x = max_x = start_x
                min_y = max_y = start_y
                while pending:
                    index = pending.pop()
                    x, y = index % width, index // width
                    area += 1
                    min_x, max_x = min(min_x, x), max(max_x, x)
                    min_y, max_y = min(min_y, y), max(max_y, y)
                    for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                        if 0 <= nx < width and 0 <= ny < height:
                            neighbor = ny * width + nx
                            if not visited[neighbor] and pixels[nx, ny] < 48:
                                visited[neighbor] = 1
                                pending.append(neighbor)
                box_area = (max_x - min_x + 1) * (max_y - min_y + 1)
                if best is None or area > best[0]:
                    best = (area, box_area, min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
        if best is None:
            return None
        area, box_area, x, y, box_width, box_height = best
        fill_ratio = area / max(1, box_area)
        area_ratio = area / (width * height)
        if area_ratio >= 0.004 and fill_ratio >= 0.34 and box_width >= 24:
            return {"area_ratio": round(area_ratio, 5), "fill_ratio": round(fill_ratio, 3),
                    "bbox": [x, y, box_width, box_height]}
        # Closed outlines can connect a filled object to the rest of the line art,
        # making its whole-page component sparse. Also look for broad dense patches
        # and horizontal/vertical bands; smaller 24px windows confuse large pupils
        # with uncolorable fills.
        windows = ((48, 80, 0.42), (80, 48, 0.42), (64, 64, 0.40),
                   (32, 32, 0.76), (48, 16, 0.78), (16, 48, 0.78))
        stride = 4
        integral = [0] * ((width + 1) * (height + 1))
        for row in range(height):
            row_sum = 0
            for column in range(width):
                row_sum += int(pixels[column, row] < 48)
                integral[(row + 1) * (width + 1) + column + 1] = integral[row * (width + 1) + column + 1] + row_sum
        for block_width, block_height, density in windows:
            minimum = block_width * block_height * density
            for top in range(0, height - block_height + 1, stride):
                for left in range(0, width - block_width + 1, stride):
                    right, bottom = left + block_width, top + block_height
                    black = (integral[bottom * (width + 1) + right] - integral[top * (width + 1) + right]
                             - integral[bottom * (width + 1) + left] + integral[top * (width + 1) + left])
                    if black >= minimum:
                        return {"area_ratio": round(black / (width * height), 5), "fill_ratio": round(black / (block_width * block_height), 3),
                                "bbox": [left, top, block_width, block_height], "check": "dense_local_fill"}
    return None


def enforce_image_checks(review: dict[str, Any], image_path: Path) -> dict[str, Any]:
    region = find_large_solid_black_region(image_path)
    from PIL import Image
    with Image.open(image_path) as image:
        luminance = image.convert("L")
        histogram = luminance.histogram()
        total = max(1, luminance.width * luminance.height)
        # Line art can have antialiased edges, but broad gray rendering and cast
        # shadows fill areas that should stay white for a printable coloring page.
        gray_fill_ratio = sum(histogram[56:221]) / total
    gray_fill = gray_fill_ratio >= 0.075
    if not region and not gray_fill:
        return review
    result = dict(review)
    checks = dict(result.get("checks", {}))
    if region:
        checks["no_large_solid_black_regions"] = False
    if gray_fill:
        checks["coloring_areas_open"] = False
    result["checks"] = checks
    result["decision"] = "retry"
    defects = []
    if region:
        defects.append("Automated pixel check found a broad near-solid-black region that removes coloring space "
                       f"(area {region['area_ratio']:.1%}, fill {region['fill_ratio']:.0%}); redraw it as white paper inside clean outlines.")
    if gray_fill:
        defects.append(f"Automated pixel check found widespread gray fill ({gray_fill_ratio:.1%} of the page); remove gray fur/clothing/prop fills and cast shadows, leaving those surfaces white inside clean black outlines.")
    defect = " ".join(defects)
    result["defects"] = list(result.get("defects", [])) + [defect]
    result["summary"] = defect + " " + result.get("summary", "")
    result["retry_prompt"] = (result.get("retry_prompt", "") + " Keep every broad surface white and colorable inside crisp black outlines. Do not use gray fills, grayscale fur or clothing, cast shadows, gradients, crosshatching, or solid black fill.").strip()
    if region:
        result["pixel_check"] = region
    if gray_fill:
        result["gray_fill_ratio"] = round(gray_fill_ratio, 5)
    return result


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
    scene = item.get("candidate_title", item.get("scene_title", item.get("label", item.get("variant_id", "candidate"))))
    focus = str(item.get("qa_focus", ""))
    special = []
    if "doghouse" in focus.lower() or "doorway" in focus.lower():
        special.append("Doghouse doorway must stay white inside a thin outline; no black cavity.")
    if "feeder" in str(scene).lower():
        special.append("Bird feeder is an open tray with a visible white interior; no dark aperture or seed fill.")
    return " ".join(filter(None, [
        f"Product: {product.get('title', product['product_id'])}.", f"Scene: {scene}.",
        "Style: crisp monochrome line art, clean contours, consistent weight, broad white colorable surfaces, framed portrait scene.",
        f"Scene checks: {focus}", "Review anatomy, physical logic, scene readability/variety, clutter, line cleanliness, and coloring usability.",
        "Large solid-black uncolorable regions are a hard rejection, regardless of score. Localized eyes and facial details may be black; reject black-filled clothing, wings, fur, hair, whole animals, or major props even if fragmented across patches.", *special,
    ]))


def add_review_feedback(prompt: str, feedback: str) -> str:
    if not feedback:
        return prompt
    return (f"{prompt}\n\nPrevious review feedback to address: {feedback}. "
            "The current scene prompt and QA focus are authoritative and supersede conflicting earlier notes. "
            "Follow the current scene direction exactly while correcting compatible defects.")


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
    cycle_limits = {}
    for page in selected:
        key = str(int(page["page"]))
        historical = len(state.get("candidates", {}).get(key, {}).get("attempts", []))
        cycle_limits[key] = (min(args.cumulative_safety_ceiling, historical + args.cycle_attempts)
                             if args.cycle_attempts else args.max_attempts)
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
        if args.re_review_accepted and (record["status"] == "accepted_by_vision" or record.get("re_review_pending")):
            best = next((c for c in record.get("attempts", []) if c.get("file") == record.get("best_candidate")), None)
            if best and best.get("review_policy_version", 0) >= REVIEW_POLICY_VERSION and not record.get("re_review_pending"):
                continue
            if best and best.get("review_policy_version", 0) < REVIEW_POLICY_VERSION:
                destination = Path(best["file"])
                if not destination.is_file():
                    raise RuntimeError(f"Accepted candidate is missing: {destination}")
                if reviewer is None:
                    raise RuntimeError("Re-review requires the configured vision provider.")
                if hasattr(client, "free_memory"):
                    client.free_memory()
                old_review = best.get("review")
                if old_review:
                    best.setdefault("review_history", []).append(old_review)
                try:
                    review = enforce_image_checks(validate_review(reviewer.review(destination, criteria_for(product, item), "")), destination)
                except Exception as exc:
                    record["status"] = "paused"
                    record["re_review_pending"] = True
                    record["last_error"] = str(exc)
                    state["updated_at"] = utc_now()
                    atomic_json(state_path, state)
                    create_manual_bundle(run_dir, state)
                    raise
                if review["decision"] == "accept" and review["score"] < args.accept_score and review["defects"]:
                    review["decision"] = "retry"
                    review["summary"] = f"Score {review['score']} is below acceptance threshold {args.accept_score}. " + review["summary"]
                best["review"] = review
                best["review_policy_version"] = REVIEW_POLICY_VERSION
                record.pop("re_review_pending", None)
                record["last_quality_review"] = {"policy_version": REVIEW_POLICY_VERSION, "reviewed_at": utc_now(), "candidate": best["file"]}
                if review["decision"] == "accept":
                    record["status"] = "accepted_by_vision"
                    atomic_json(state_path, state)
                    continue
                if review["decision"] == "manual":
                    record["status"] = "manual_review"
                    atomic_json(state_path, state)
                    continue
                prior_feedback = "; ".join(filter(None, [review["summary"], "; ".join(review["defects"]), review["retry_prompt"]]))
                record["status"] = "pending"
                atomic_json(state_path, state)
        elif record["status"] in {"accepted_by_vision", "accepted_by_human", "manual_review"}:
            continue
        if record["status"] == "exhausted":
            # A larger configured attempt bound can resume an exhausted page
            # automatically without discarding its prior candidates. The flag
            # still permits an explicit same-bound retry when requested.
            if not args.retry_exhausted and len(record["attempts"]) >= cycle_limits[key]:
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
                if len(record["attempts"]) >= cycle_limits[key]:
                    record["status"] = "exhausted"
                    break
                attempt = len(record["attempts"]) + 1
                destination = run_dir / "candidates" / f"page_{page_no:03d}_attempt_{attempt:02d}.png"
                if destination.exists():
                    attempt += 1
                    destination = run_dir / "candidates" / f"page_{page_no:03d}_attempt_{attempt:02d}_resume_{int(time.time())}.png"
                prompt = build_prompt(product, item)
                dashboard_state_path = run_dir / "dashboard_state.json"
                if dashboard_state_path.is_file():
                    human_state = json.loads(dashboard_state_path.read_text(encoding="utf-8"))
                    human_note = str(human_state.get("pages", {}).get(key, {}).get("note", "")).strip()
                    # The book dashboard owns a separate selection record so legacy approvals
                    # remain immutable; its current art-direction note takes precedence here.
                    book_selection_path = run_dir / "book_selection.json"
                    if book_selection_path.is_file():
                        book_selection = json.loads(book_selection_path.read_text(encoding="utf-8"))
                        book_page = next((p for p in book_selection.get("pages", []) if str(p.get("page")) == key), {})
                        human_note = str(book_page.get("note", human_note)).strip()
                    if human_note:
                        prompt += (f"\n\nHuman art-direction instruction for this rejected page: {human_note}. "
                                   "Treat this as a hard constraint; fix the named defect and change the layout meaningfully.")
                prompt = add_review_feedback(prompt, prior_feedback)
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
                # Keep generation feedback out of the visual judgment to avoid anchoring the reviewer on an old defect.
                review = enforce_image_checks(validate_review(reviewer.review(destination, criteria_for(product, item), "")), destination)
                if review["decision"] == "accept" and review["score"] < args.accept_score and review["defects"]:
                    review["decision"] = "retry"
                    review["summary"] = f"Score {review['score']} is below acceptance threshold {args.accept_score}. " + review["summary"]
                candidate["review"] = review
                candidate["review_policy_version"] = REVIEW_POLICY_VERSION if args.re_review_accepted else candidate.get("review_policy_version", 1)
                if review["decision"] == "accept":
                    record["status"] = "accepted_by_vision"
                    record["best_candidate"] = candidate["file"]
                    break
                if review["decision"] == "manual":
                    record["status"] = "manual_review"
                    break
                record["status"] = "pending"
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
    parser.add_argument("--re-review-accepted", action="store_true", help="Re-review accepted winners under strict printable-art checks; retries only failed pages")
    parser.add_argument("--retry-exhausted", action="store_true", help="Continue exhausted scenes after raising --max-attempts")
    parser.add_argument("--cycle-attempts", type=int, help="Per-page new-attempt budget for this human regeneration cycle")
    parser.add_argument("--cumulative-safety-ceiling", type=int, default=60, help="Lifetime attempt ceiling per page, including prior cycles")
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
    parser.add_argument("--vision-max-tokens", type=int, default=int(os.getenv("VISION_MAX_TOKENS", "1024")))
    parser.add_argument("--vision-reasoning-effort", default=os.getenv("VISION_REASONING_EFFORT"))
    args = parser.parse_args(argv)
    if args.max_attempts < 1 or args.max_attempts > 14:
        parser.error("--max-attempts must be between 1 and 14")
    if args.cycle_attempts is not None and not 1 <= args.cycle_attempts <= 14:
        parser.error("--cycle-attempts must be between 1 and 14")
    if args.cumulative_safety_ceiling < 1 or args.cumulative_safety_ceiling > 120:
        parser.error("--cumulative-safety-ceiling must be between 1 and 120")
    if not 1 <= args.accept_score <= 100:
        parser.error("--accept-score must be between 1 and 100")
    if bool(args.vision_endpoint) != bool(args.vision_model):
        parser.error("Configure both --vision-endpoint and --vision-model, or neither")
    if args.vision_max_tokens < 1:
        parser.error("--vision-max-tokens must be positive")
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
        unload_endpoint=args.vision_unload_endpoint, max_tokens=args.vision_max_tokens,
        reasoning_effort=args.vision_reasoning_effort) if args.vision_endpoint else None
    mutex_handle = None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            product_root = resolve_product_root(args.product)
            lock_key = f"{product_root.resolve()}|{args.run_id}".casefold().encode("utf-8")
            name = "Local\\AIProductReview-" + hashlib.sha256(lock_key).hexdigest()[:24]
            mutex_handle = kernel32.CreateMutexW(None, False, name)
            if not mutex_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            result = kernel32.WaitForSingleObject(mutex_handle, 0)
            if result not in (0, 0x80):
                print("A review worker for this product run is already active.", flush=True)
                return 0
        return run(args, reviewer=reviewer)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if mutex_handle:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(mutex_handle)
            ctypes.windll.kernel32.CloseHandle(mutex_handle)


if __name__ == "__main__":
    raise SystemExit(main())
