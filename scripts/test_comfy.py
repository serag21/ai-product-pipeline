#!/usr/bin/env python
"""Smoke test for the local Flux.2 ComfyUI image-generation workflows."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from comfy.client import ComfyUIClient, ComfyUIError


PROMPT = (
    "A friendly cartoon turtle sitting in a small garden, children's coloring book page, "
    "clean black contour line art, bold smooth outlines, large open areas for coloring, "
    "simple rounded shapes, white page background, uncluttered composition, clear separation "
    "between the turtle and garden elements, playful expressive face"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workflow",
        default="workflows/flux2_klein_4b_coloring_api.json",
        help="ComfyUI API-format workflow JSON",
    )
    parser.add_argument(
        "--prompt",
        default=PROMPT,
        help="Positive prompt sent to the workflow",
    )
    parser.add_argument("--width", type=int, default=1536)
    parser.add_argument("--height", type=int, default=1984)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument(
        "--guidance",
        type=float,
        default=5.0,
        help="CFG/guidance value; ignored when the workflow has neither control node",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument(
        "--output",
        default="output/comfy_smoke_test.png",
    )
    return parser.parse_args()


def detect_profile(workflow: dict) -> dict:
    # Current production target: FLUX.2 Klein 4B Base, adapted from the
    # existing API-format Klein workflow used by local-image-model-lab.
    if workflow.get("2", {}).get("class_type") == "UNETLoader" and        workflow.get("14", {}).get("class_type") == "CFGGuider":
        return {
            "name": "FLUX.2 Klein 4B Base",
            "prompt_node": "9",
            "seed_node": "15",
            "latent_node": "7",
            "scheduler_node": "8",
            "guidance_node": "14",
            "guidance_input": "cfg",
            "output_node": "19",
        }

    # Backward compatibility with the original FLUX.2 Dev workflow.
    if workflow.get("6", {}).get("class_type") == "CLIPTextEncode" and        workflow.get("26", {}).get("class_type") == "FluxGuidance":
        return {
            "name": "FLUX.2 Dev (legacy smoke-test workflow)",
            "prompt_node": "6",
            "seed_node": "25",
            "latent_node": "47",
            "scheduler_node": "48",
            "guidance_node": "26",
            "guidance_input": "guidance",
            "output_node": "9",
        }

    raise ComfyUIError(
        "Unrecognized workflow schema. Expected the Klein 4B workflow "
        "or the legacy FLUX.2 Dev workflow."
    )


def validate_profile(workflow: dict, profile: dict) -> None:
    expected = {
        profile["prompt_node"]: ("CLIPTextEncode", "text"),
        profile["seed_node"]: ("RandomNoise", "noise_seed"),
        profile["latent_node"]: ("EmptyFlux2LatentImage", "width"),
        profile["scheduler_node"]: ("Flux2Scheduler", "steps"),
        profile["guidance_node"]: ("CFGGuider" if profile["guidance_node"] == "14" else "FluxGuidance",
                                   profile["guidance_input"]),
        profile["output_node"]: ("SaveImage", "filename_prefix"),
    }

    missing = [node_id for node_id in expected if node_id not in workflow]
    if missing:
        raise ComfyUIError(
            f"Expected execution nodes are missing from the API workflow: {missing}"
        )

    for node_id, (class_type, input_name) in expected.items():
        actual_class = workflow[node_id].get("class_type")
        if actual_class != class_type:
            raise ComfyUIError(
                f"Node {node_id}: expected {class_type}, got {actual_class}"
            )
        if input_name not in workflow[node_id].get("inputs", {}):
            raise ComfyUIError(
                f"Node {node_id}: expected input {input_name!r} was not found."
            )


def main() -> int:
    args = parse_args()
    workflow_path = PROJECT_ROOT / args.workflow

    if not workflow_path.exists():
        print(f"ERROR: workflow not found: {workflow_path}")
        print(
            "Expected workflows/flux2_klein_4b_coloring_api.json. "
            "The official ComfyUI template is also available from the "
            "Flux.2 Klein 4B workflow templates."
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

        profile = detect_profile(workflow)
        validate_profile(workflow, profile)
        print(f"Workflow: {profile['name']}")

        seed = args.seed if args.seed is not None else client.random_seed()

        workflow = client.patch_workflow(
            workflow,
            {
                profile["prompt_node"]: {"text": args.prompt},
                profile["seed_node"]: {"noise_seed": seed},
                profile["latent_node"]: {
                    "width": args.width,
                    "height": args.height,
                },
                profile["scheduler_node"]: {
                    "steps": args.steps,
                    "width": args.width,
                    "height": args.height,
                },
                profile["guidance_node"]: {
                    profile["guidance_input"]: args.guidance,
                },
                profile["output_node"]: {"filename_prefix": "coloring_smoke"},
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
            output_node=profile["output_node"],
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
