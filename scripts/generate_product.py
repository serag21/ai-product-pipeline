#!/usr/bin/env python3
"""Deterministic batch generator for printable coloring products via local ComfyUI."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageStat

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comfy.client import ComfyUIClient, ComfyUIError


WORKFLOW_PATH = PROJECT_ROOT / "workflows" / "flux2_coloring_api.json"
OUTPUT_ROOT = PROJECT_ROOT / "products" / "construction-vehicles-toddler-30"
PAGES_ROOT = OUTPUT_ROOT / "pages"
QA_ROOT = OUTPUT_ROOT / "qa"

PROMPT_NODE = "6"
SEED_NODE = "25"
LATENT_NODE = "47"
SCHEDULER_NODE = "48"
GUIDANCE_NODE = "26"
OUTPUT_NODE = "9"

WIDTH = 1536
HEIGHT = 1984
STEPS = 20
GUIDANCE = 4.0


def load_product() -> dict:
    path = OUTPUT_ROOT / "prompts.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_workflow(workflow: dict) -> None:
    expected = {
        PROMPT_NODE: ("CLIPTextEncode", "text"),
        SEED_NODE: ("RandomNoise", "noise_seed"),
        LATENT_NODE: ("EmptyFlux2LatentImage", "width"),
        SCHEDULER_NODE: ("Flux2Scheduler", "steps"),
        GUIDANCE_NODE: ("FluxGuidance", "guidance"),
        OUTPUT_NODE: ("SaveImage", "filename_prefix"),
    }
    for node_id, (class_type, input_name) in expected.items():
        node = workflow.get(node_id)
        if not isinstance(node, dict):
            raise RuntimeError(f"Workflow is missing required node {node_id}.")
        if node.get("class_type") != class_type:
            raise RuntimeError(
                f"Workflow node {node_id}: expected {class_type}, got {node.get('class_type')!r}."
            )
        if input_name not in (node.get("inputs") or {}):
            raise RuntimeError(
                f"Workflow node {node_id}: missing required input {input_name!r}."
            )


def quality_check(path: Path) -> dict:
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    gray = image.convert("L")
    stat = ImageStat.Stat(gray)
    return {
        "pass": (
            width >= 1200
            and height >= 1600
            and stat.stddev[0] >= 8.0
        ),
        "width": width,
        "height": height,
        "grayscale_stddev": round(stat.stddev[0], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    product = load_product()
    pages = product["pages"]
    selected = pages[args.start - 1 : args.start - 1 + args.count]
    if not selected:
        raise RuntimeError("No product pages selected.")

    workflow = ComfyUIClient.load_workflow(WORKFLOW_PATH)
    validate_workflow(workflow)

    PAGES_ROOT.mkdir(parents=True, exist_ok=True)
    QA_ROOT.mkdir(parents=True, exist_ok=True)

    client = ComfyUIClient(args.server)
    health = client.health()
    print(f"ComfyUI: {args.server}")
    print(f"GPU devices reported: {health.get('devices', [])}")
    print(f"Workflow: {WORKFLOW_PATH}")
    print(f"Canvas: {WIDTH}x{HEIGHT} | steps={STEPS} | guidance={GUIDANCE}")

    manifest = {
        "product_id": product["product_id"],
        "workflow": str(WORKFLOW_PATH.relative_to(PROJECT_ROOT)),
        "canvas": {"width": WIDTH, "height": HEIGHT},
        "steps": STEPS,
        "guidance": GUIDANCE,
        "pages": [],
    }

    for item in selected:
        page_no = item["page"]
        prompt = f'{item["subject"]}. {product["global_prompt"]}'
        seed = 120000 + page_no * 7919
        wf = copy.deepcopy(workflow)
        wf[PROMPT_NODE]["inputs"]["text"] = prompt
        wf[SEED_NODE]["inputs"]["noise_seed"] = seed
        wf[LATENT_NODE]["inputs"]["width"] = WIDTH
        wf[LATENT_NODE]["inputs"]["height"] = HEIGHT
        wf[SCHEDULER_NODE]["inputs"]["width"] = WIDTH
        wf[SCHEDULER_NODE]["inputs"]["height"] = HEIGHT
        wf[SCHEDULER_NODE]["inputs"]["steps"] = STEPS
        wf[GUIDANCE_NODE]["inputs"]["guidance"] = GUIDANCE
        wf[OUTPUT_NODE]["inputs"]["filename_prefix"] = f"coloring_{product['product_id']}_page_{page_no:03d}"

        print(f"Generating page {page_no:02d} | seed={seed}")
        try:
            prompt_id = client.submit(wf)
            history = client.wait(prompt_id, timeout=args.timeout)
            images = history.get("outputs", {}).get(OUTPUT_NODE, {}).get("images", [])
            if not images:
                raise ComfyUIError(
                    f"No images returned from output node {OUTPUT_NODE}.",
                    prompt_id,
                )
            destination = PAGES_ROOT / f"page_{page_no:03d}.png"
            client.download_image(images[-1], destination)
            qa = quality_check(destination)
            record = {
                "page": page_no,
                "seed": seed,
                "prompt_id": prompt_id,
                "file": str(destination.relative_to(PROJECT_ROOT)),
                "qa": qa,
            }
            manifest["pages"].append(record)
            print(
                f"PASS page {page_no:02d} | "
                f"{qa['width']}x{qa['height']} | seed {seed}"
            )
            if not qa["pass"]:
                raise RuntimeError(f"Basic QA failed for page {page_no}: {qa}")
        except Exception as exc:
            manifest["pages"].append(
                {"page": page_no, "seed": seed, "status": "failed", "error": str(exc)}
            )
            print(f"FAIL page {page_no:02d}: {exc}", file=sys.stderr)

        (QA_ROOT / "generation_manifest.partial.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

    passed = sum(1 for p in manifest["pages"] if p.get("qa", {}).get("pass"))
    failed = len(manifest["pages"]) - passed
    manifest["summary"] = {"requested": len(selected), "passed": passed, "failed": failed}
    (QA_ROOT / "generation_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest["summary"], indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
