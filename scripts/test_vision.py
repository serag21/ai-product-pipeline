#!/usr/bin/env python3
"""Exercise a configured vision endpoint against a real product page."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from comfy.client import ComfyUIClient  # noqa: E402
from product_review_worker import OpenAICompatibleReviewer, criteria_for, load_product, resolve_product_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", default="products/little-haunts-club-spooky-neighbors-15")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="qwen3-vl:30b")
    parser.add_argument("--unload-endpoint", default="http://127.0.0.1:11434/api/generate")
    args = parser.parse_args()

    product_root = resolve_product_root(args.product)
    product = load_product(product_root)
    item = next((page for page in product["pages"] if int(page["page"]) == args.page), None)
    if item is None:
        parser.error(f"Page {args.page} is not in the product manifest")
    image = product_root / "pages" / f"page_{args.page:03d}.png"
    if not image.is_file():
        parser.error(f"Page image is missing: {image}")

    reviewer = OpenAICompatibleReviewer(args.endpoint, args.model, timeout=600,
                                        unload_endpoint=args.unload_endpoint)
    ComfyUIClient().free_memory()
    reviewer.check()
    result = reviewer.review(image, criteria_for(product, item))
    print(json.dumps({"image": str(image), "model": args.model, "review": result}, indent=2))

    parsed = urlsplit(args.unload_endpoint)
    api_root = f"{parsed.scheme}://{parsed.netloc}"
    process_list = requests.get(api_root + "/api/ps", timeout=20)
    process_list.raise_for_status()
    resident = [entry.get("name") for entry in process_list.json().get("models", [])
                if entry.get("name") == args.model]
    if resident:
        raise RuntimeError(f"Vision model remained resident after unload: {resident}")
    print("PASS: actual page was reviewed and the vision model was unloaded for ComfyUI GPU work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
