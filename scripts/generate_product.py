#!/usr/bin/env python3
"""Deterministic batch generator for printable coloring products via local ComfyUI."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from PIL import Image, ImageStat

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comfy.client import ComfyUIClient, ComfyUIError


WORKFLOW_PATH = PROJECT_ROOT / "workflows" / "flux2_coloring_api.json"
REFERENCE_WORKFLOW_PATH = PROJECT_ROOT / "workflows" / "flux2_coloring_reference_api.json"

PROMPT_NODE = "6"
SEED_NODE = "25"
LATENT_NODE = "47"
SCHEDULER_NODE = "48"
GUIDANCE_NODE = "26"
OUTPUT_NODE = "9"


def resolve_product_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def load_product(product_root: Path) -> dict:
    path = product_root / "prompts.json"
    if not path.is_file():
        raise RuntimeError(f"Product manifest not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def generation_settings(product: dict) -> tuple[int, int, int, float]:
    settings = product.get("generation", {})
    return (
        int(settings.get("width", 1536)),
        int(settings.get("height", 1984)),
        int(settings.get("steps", 20)),
        float(settings.get("guidance", 4.0)),
    )


def build_prompt(product: dict, item: dict) -> str:
    if "prompt" in item:
        prompt = item["prompt"]
        if isinstance(prompt, str):
            return prompt
        return json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))

    if product.get("prompt_format") == "json":
        bible = product.get("visual_bible", {})
        payload = {
            "scene": item["scene"],
            "subjects": item["subjects"],
            "style": bible["style"],
            "color_palette": bible["color_palette"],
            "lighting": item.get("lighting", bible["lighting"]),
            "mood": item.get("mood", bible["mood"]),
            "background": item["background"],
            "composition": item["composition"],
            "rendering": bible["rendering"],
            "page_design": bible["page_design"],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return f'{item["subject"]}. {product["global_prompt"]}'


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

    if "70" in workflow:
        reference_nodes = {
            "70": ("LoadImage", "image"),
            "71": ("VAEEncode", "pixels"),
            "72": ("ReferenceLatent", "conditioning"),
        }
        for node_id, (class_type, input_name) in reference_nodes.items():
            node = workflow.get(node_id)
            if not isinstance(node, dict) or node.get("class_type") != class_type:
                raise RuntimeError(f"Invalid reference workflow node {node_id}: expected {class_type}.")
            if input_name not in (node.get("inputs") or {}):
                raise RuntimeError(f"Reference workflow node {node_id} lacks input {input_name!r}.")
        if workflow.get("22", {}).get("inputs", {}).get("conditioning") != ["72", 0]:
            raise RuntimeError("Reference workflow must route reference-conditioned output into BasicGuider.")


def quality_check(path: Path) -> dict:
    with Image.open(path) as source:
        image = source.convert("RGB")
    width, height = image.size
    gray = image.convert("L")
    stat = ImageStat.Stat(gray)
    return {
        "pass": width >= 1200 and height >= 1600 and stat.stddev[0] >= 8.0,
        "width": width,
        "height": height,
        "grayscale_stddev": round(stat.stddev[0], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", default="products/construction-vehicles-toddler-30")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--reference", help="Optional image path used for FLUX.2 reference conditioning.")
    args = parser.parse_args()

    product_root = resolve_product_root(args.product)
    product = load_product(product_root)
    pages = product["pages"]
    selected = pages[args.start - 1 : args.start - 1 + args.count]
    if not selected:
        raise RuntimeError("No product pages selected.")

    width, height, steps, guidance = generation_settings(product)
    pages_root = product_root / "pages"
    qa_root = product_root / "qa"

    reference_path = None
    if args.reference:
        reference_path = Path(args.reference)
        if not reference_path.is_absolute():
            reference_path = PROJECT_ROOT / reference_path
        reference_path = reference_path.resolve()
        if not reference_path.is_file():
            raise RuntimeError(f"Reference image not found: {reference_path}")
        workflow_path = REFERENCE_WORKFLOW_PATH
    else:
        workflow_path = WORKFLOW_PATH

    workflow = ComfyUIClient.load_workflow(workflow_path)
    validate_workflow(workflow)

    pages_root.mkdir(parents=True, exist_ok=True)
    qa_root.mkdir(parents=True, exist_ok=True)

    client = ComfyUIClient(args.server)
    health = client.health()
    uploaded_reference = None
    if reference_path:
        uploaded_reference = client.upload_image(reference_path)
        uploaded_name = uploaded_reference.get("name", "")
        uploaded_subfolder = uploaded_reference.get("subfolder", "")
        if uploaded_subfolder:
            uploaded_name = f"{uploaded_subfolder}/{uploaded_name}"
        workflow["70"]["inputs"]["image"] = uploaded_name
        print(f"Reference image uploaded to ComfyUI: {uploaded_name}")
    print(f"Product: {product['product_id']}")
    print(f"ComfyUI: {args.server}")
    print(f"GPU devices reported: {health.get('devices', [])}")
    print(f"Workflow: {workflow_path}")
    print(
        f"Canvas: {width}x{height} | steps={steps} | "
        f"guidance={guidance} | items={len(selected)}"
    )

    manifest = {
        "product_id": product["product_id"],
        "workflow": str(workflow_path.relative_to(PROJECT_ROOT)),
        "reference_image": str(reference_path.relative_to(PROJECT_ROOT)) if reference_path else None,
        "canvas": {"width": width, "height": height},
        "steps": steps,
        "guidance": guidance,
        "pages": [],
    }

    for item in selected:
        page_no = int(item["page"])
        prompt = build_prompt(product, item)
        seed = 120000 + page_no * 7919
        wf = copy.deepcopy(workflow)
        wf[PROMPT_NODE]["inputs"]["text"] = prompt
        wf[SEED_NODE]["inputs"]["noise_seed"] = seed
        wf[LATENT_NODE]["inputs"]["width"] = width
        wf[LATENT_NODE]["inputs"]["height"] = height
        wf[SCHEDULER_NODE]["inputs"]["width"] = width
        wf[SCHEDULER_NODE]["inputs"]["height"] = height
        wf[SCHEDULER_NODE]["inputs"]["steps"] = steps
        wf[GUIDANCE_NODE]["inputs"]["guidance"] = guidance
        wf[OUTPUT_NODE]["inputs"]["filename_prefix"] = (
            f"coloring_{product['product_id']}_page_{page_no:03d}"
        )

        print(
            f"Generating page {page_no:02d} | "
            f"variant={item.get('variant_id', page_no)} | seed={seed}"
        )
        try:
            prompt_id = client.submit(wf)
            history = client.wait(prompt_id, timeout=args.timeout)
            images = history.get("outputs", {}).get(OUTPUT_NODE, {}).get("images", [])
            if not images:
                raise ComfyUIError(
                    f"No images returned from output node {OUTPUT_NODE}.",
                    prompt_id,
                )
            destination = pages_root / f"page_{page_no:03d}.png"
            client.download_image(images[-1], destination)
            qa = quality_check(destination)
            record = {
                "page": page_no,
                "variant_id": item.get("variant_id"),
                "family": item.get("family"),
                "label": item.get("label"),
                "prompt_format": item.get("prompt_format"),
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
            manifest["pages"].append({
                "page": page_no,
                "variant_id": item.get("variant_id"),
                "family": item.get("family"),
                "label": item.get("label"),
                "prompt_format": item.get("prompt_format"),
                "seed": seed,
                "status": "failed",
                "error": str(exc),
            })
            print(f"FAIL page {page_no:02d}: {exc}", file=sys.stderr)

        (qa_root / "generation_manifest.partial.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

    passed = sum(1 for p in manifest["pages"] if p.get("qa", {}).get("pass"))
    failed = len(manifest["pages"]) - passed
    manifest["summary"] = {
        "requested": len(selected),
        "passed": passed,
        "failed": failed,
    }
    (qa_root / "generation_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest["summary"], indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
