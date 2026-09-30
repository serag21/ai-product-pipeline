#!/usr/bin/env python
"""Smoke test for the real Flux.2 ComfyUI workflow."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comfy.client import ComfyUIClient, ComfyUIError


EXPECTED = {
    "6": ("CLIPTextEncode", "text"),
    "25": ("RandomNoise", "noise_seed"),
    "50": ("PrimitiveNode", "value"),
    "51": ("PrimitiveNode", "value"),
    "48": ("Flux2Scheduler", "steps"),
    "26": ("FluxGuidance", "guidance"),
    "16": ("KSamplerSelect", "sampler_name"),
    "9": ("SaveImage", "filename_prefix"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workflow",
        default="workflows/flux2_coloring_api.json",
        help="ComfyUI API-format workflow JSON",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "A friendly cartoon turtle sitting in a small garden, "
            "clean black and white children's coloring book line art, "
            "bold smooth outlines, white background, no color, "
            "no grayscale, no shading, no text"
        ),
    )
    parser.add_argument("--width", type=int, default=1536)
    parser.add_argument("--height", type=int, default=1984)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--guidance", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument(
        "--output",
        default="output/comfy_smoke_test.png",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workflow_path = PROJECT_ROOT / args.workflow

    if not workflow_path.exists():
        print(f"ERROR: workflow not found: {workflow_path}")
        print(
            "Export the workflow from ComfyUI with "
            "File -> Export Workflow (API), then save it at "
            "workflows/flux2_coloring_api.json."
        )
        return 1

    client = ComfyUIClient(args.server)

    try:
        stats = client.health()
        print("ComfyUI is reachable.")
        print(f"Server: {args.server}")
        print(f"Devices: {stats.get('devices', [])}")

        workflow = client.load_workflow(workflow_path)

        if "nodes" in workflow and "links" in workflow:
            print(
                "ERROR: this is ComfyUI UI/save format, not API format. "
                "Export with File -> Export Workflow (API)."
            )
            return 1

        missing = [node_id for node_id in EXPECTED if node_id not in workflow]
        if missing:
            raise ComfyUIError(
                f"Expected node IDs are missing from the API workflow: {missing}"
            )

        for node_id, (class_type, input_name) in EXPECTED.items():
            actual_class = workflow[node_id].get("class_type")
            if actual_class != class_type:
                raise ComfyUIError(
                    f"Node {node_id}: expected {class_type}, got {actual_class}"
                )
            if input_name not in workflow[node_id].get("inputs", {}):
                raise ComfyUIError(
                    f"Node {node_id}: expected input {input_name!r} was not found."
                )

        seed = args.seed if args.seed is not None else client.random_seed()

        workflow = client.patch_workflow(
            workflow,
            {
                "6": {"text": args.prompt},
                "25": {"noise_seed": seed},
                "50": {"value": args.width},
                "51": {"value": args.height},
                "48": {
                    "steps": args.steps,
                    "width": args.width,
                    "height": args.height,
                },
                "26": {"guidance": args.guidance},
                "9": {"filename_prefix": "coloring_smoke"},
            },
        )

        print(
            f"Generating {args.width}x{args.height}, "
            f"{args.steps} steps, seed {seed}..."
        )

        def progress(data: dict) -> None:
            value = data.get("value")
            maximum = data.get("max")
            if value is not None and maximum:
                print(f"  progress: {value}/{maximum}")

        paths = client.generate(
            workflow,
            output_node="9",
            destination=PROJECT_ROOT / args.output,
            on_progress=progress,
        )

        print("SUCCESS")
        for path in paths:
            print(f"Output: {path}")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}")
        if isinstance(exc, ComfyUIError) and exc.prompt_id:
            print(f"Prompt ID for recovery: {exc.prompt_id}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
