#!/usr/bin/env python3
"""Long-running local bridge for GitHub-queued coloring-book generation jobs.

This worker executes a narrow allowlist of deterministic actions; GitHub requests
cannot supply shell commands, paths outside products/, or arbitrary executable
arguments. It keeps restart-safe state under LOCALAPPDATA, waits for local
ComfyUI, creates a visual review sheet, and pushes only that job's review files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parents[1]
REPO_DEFAULT = "serag21/ai-product-pipeline"
BRANCH_DEFAULT = "main"
POLL_SECONDS_DEFAULT = 45
MAX_ATTEMPTS = 3
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
PRODUCT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
USER_AGENT = "little-haunts-local-worker/1.0"
DEFAULT_STATE_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))
) / "LittleHauntsBusiness"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}.")
    return value


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout[-5000:]}")
    return result


def ensure_correct_repo(branch: str) -> None:
    top = run_git(["rev-parse", "--show-toplevel"]).stdout.strip()
    if Path(top).resolve() != ROOT.resolve():
        raise RuntimeError(f"Script is not running from its expected repository root: {top}")
    current = run_git(["branch", "--show-current"]).stdout.strip()
    if current != branch:
        raise RuntimeError(
            f"Worker expects branch {branch!r}, but this checkout is on {current!r}. "
            "Switch the canonical checkout to the configured branch before starting it."
        )


def ensure_clean_checkout() -> None:
    status = run_git(["status", "--porcelain"]).stdout.strip()
    if status:
        raise RuntimeError(
            "Refusing to sync because the canonical repository has uncommitted changes. "
            "Commit or stash them first; the worker will not overwrite them.\n" + status[:3000]
        )


def sync_latest(branch: str) -> None:
    ensure_correct_repo(branch)
    ensure_clean_checkout()
    run_git(["fetch", "origin", branch])
    ahead = int(run_git(["rev-list", "--count", f"origin/{branch}..HEAD"]).stdout.strip() or "0")
    if ahead:
        raise RuntimeError(
            f"Local branch has {ahead} commit(s) not on origin/{branch}; refusing to push or rewrite them."
        )
    behind = int(run_git(["rev-list", "--count", f"HEAD..origin/{branch}"]).stdout.strip() or "0")
    if behind:
        run_git(["pull", "--ff-only", "origin", branch])
    ensure_clean_checkout()


def safe_product_root(product_id: str) -> Path:
    if not PRODUCT_ID_RE.fullmatch(product_id):
        raise RuntimeError("product_id contains unsupported characters.")
    products_root = (ROOT / "products").resolve()
    product_root = (products_root / product_id).resolve()
    if not product_root.is_relative_to(products_root):
        raise RuntimeError("Resolved product path escapes products/.")
    manifest_path = product_root / "prompts.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Product manifest not found: {manifest_path}")
    product = load_json(manifest_path)
    if product.get("product_id") != product_id:
        raise RuntimeError(
            f"Manifest product_id {product.get('product_id')!r} does not match request {product_id!r}."
        )
    if not isinstance(product.get("pages"), list) or not product["pages"]:
        raise RuntimeError("Product manifest must contain a non-empty pages array.")
    return product_root


def validate_request(raw: dict[str, Any], filename: str, branch: str) -> dict[str, Any]:
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise RuntimeError("job_id is missing or invalid.")
    if Path(filename).stem != job_id:
        raise RuntimeError(f"Request filename {filename!r} must match job_id {job_id!r}.")
    if raw.get("schema_version") != 1:
        raise RuntimeError("Unsupported request schema_version; expected 1.")
    if raw.get("action") != "generate_for_review":
        raise RuntimeError("Unsupported action; only generate_for_review is allowed.")
    product_id = raw.get("product_id")
    if not isinstance(product_id, str) or not PRODUCT_ID_RE.fullmatch(product_id):
        raise RuntimeError("product_id is missing or invalid.")
    if raw.get("branch", branch) != branch:
        raise RuntimeError(f"Request branch must match configured branch {branch!r}.")
    count = raw.get("count")
    start = raw.get("start", 1)
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100):
        raise RuntimeError("count must be an integer from 1 to 100.")
    if not isinstance(start, int) or isinstance(start, bool) or not 1 <= start <= 100:
        raise RuntimeError("start must be an integer from 1 to 100.")
    server = raw.get("server", "http://127.0.0.1:8188")
    if server not in ("http://127.0.0.1:8188", "http://localhost:8188"):
        raise RuntimeError("Only the local ComfyUI endpoint is allowed.")
    return {
        "schema_version": 1,
        "job_id": job_id,
        "action": "generate_for_review",
        "product_id": product_id,
        "count": count,
        "start": start,
        "server": server,
        "branch": branch,
        "requested_at": str(raw.get("requested_at", "")),
    }


def load_worker_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "jobs": {}}
    value = load_json(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("jobs"), dict):
        raise RuntimeError(f"Invalid local worker state file: {path}")
    return value


def load_font(size: int, bold: bool = False):
    fonts = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"]
        if bold else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"]
    )
    for name in fonts:
        if Path(name).is_file():
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
    return ImageFont.load_default()


def create_review_sheet(product_root: Path, destination: Path, title: str) -> list[dict[str, Any]]:
    manifest = load_json(product_root / "prompts.json")
    pages = manifest["pages"]
    labels = {
        int(item["page"]): str(item.get("label", f"Page {int(item['page']):02d}"))
        for item in pages
    }
    page_paths = [
        product_root / "pages" / f"page_{int(item['page']):03d}.png"
        for item in pages
    ]
    missing = [str(path) for path in page_paths if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"Cannot build review sheet: {len(missing)} generated page(s) are missing. "
            + ", ".join(missing[:8])
        )

    columns, tile_w = 5, 300
    margin, gap, heading_h, label_h = 32, 18, 96, 46
    tile_h = round(tile_w * 1984 / 1536)
    rows = (len(page_paths) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (
            2 * margin + columns * tile_w + (columns - 1) * gap,
            2 * margin + heading_h + rows * (tile_h + label_h) + (rows - 1) * gap,
        ),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    draw.text((margin, margin), title[:100], font=load_font(30, True), fill="#222222")
    draw.text(
        (margin, margin + 43),
        f"{len(page_paths)} generated page(s) | inspect details before approving | {utc_now()}",
        font=load_font(17),
        fill="#555555",
    )

    page_metrics: list[dict[str, Any]] = []
    for index, (page_spec, path) in enumerate(zip(pages, page_paths)):
        page_no = int(page_spec["page"])
        with Image.open(path) as loaded:
            image = loaded.convert("RGB")
            gray = image.convert("L")
            width, height = image.size
            stddev = round(ImageStat.Stat(gray).stddev[0], 3)
            thumbnail = ImageOps.contain(image, (tile_w - 10, tile_h - 10))
        col, row = index % columns, index // columns
        x = margin + col * (tile_w + gap)
        y = margin + heading_h + row * (tile_h + label_h + gap)
        sheet.paste(thumbnail, (x + (tile_w - thumbnail.width) // 2, y + (tile_h - thumbnail.height) // 2))
        label = f"Page {page_no:02d}: {labels.get(page_no, path.stem)}"
        draw.text((x, y + tile_h + 6), label[:42], font=load_font(14, True), fill="#111111")
        page_metrics.append({
            "page": page_no,
            "label": labels.get(page_no, path.stem),
            "width": width,
            "height": height,
            "grayscale_stddev": stddev,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, "JPEG", quality=91, optimize=True, subsampling=0)
    return page_metrics


def ensure_comfyui(server: str, auto_start: bool, timeout: int = 180) -> None:
    health_url = server.rstrip("/") + "/system_stats"

    def healthy() -> bool:
        try:
            response = requests.get(health_url, timeout=5)
            return response.ok
        except requests.RequestException:
            return False

    if healthy():
        return

    if auto_start and os.name == "nt":
        launch = Path(os.environ.get(
            "COMFYUI_LAUNCH_BAT",
            r"C:\ComfyUI_NEW\ComfyUI\run_nvidia_gpu.bat",
        ))
        if launch.is_file():
            logging.info("ComfyUI is unavailable; attempting to start %s", launch)
            command = f'start "" "{launch}"'
            subprocess.Popen(
                ["cmd.exe", "/c", command],
                cwd=str(launch.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            logging.warning(
                "Auto-start skipped: ComfyUI launcher not found at %s. "
                "Set COMFYUI_LAUNCH_BAT or start ComfyUI manually.",
                launch,
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if healthy():
            logging.info("ComfyUI health check passed.")
            return
        time.sleep(3)
    raise RuntimeError(
        f"ComfyUI did not become healthy at {server} within {timeout}s. "
        "The request is retained and will be retried within the configured retry budget."
    )


class GitHubQueue:
    def __init__(self, repo: str, branch: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise RuntimeError("Invalid GitHub repo setting.")
        self.repo = repo
        self.branch = branch
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        token = os.environ.get("AI_PIPELINE_GITHUB_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.etag: str | None = None

    def list_requests(self) -> list[tuple[str, str]]:
        url = (
            f"https://api.github.com/repos/{self.repo}/contents/control/requests"
            f"?ref={quote(self.branch, safe='')}"
        )
        headers = {"If-None-Match": self.etag} if self.etag else {}
        response = self.session.get(url, headers=headers, timeout=20)
        if response.status_code == 304:
            return []
        response.raise_for_status()
        self.etag = response.headers.get("ETag")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("message") == "Not Found":
            return []
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected GitHub directory response: {str(payload)[:1000]}")
        return [
            (item["name"], item["download_url"])
            for item in payload
            if item.get("type") == "file"
            and item.get("name", "").endswith(".json")
            and isinstance(item.get("download_url"), str)
        ]

    def load_request(self, filename: str, download_url: str) -> dict[str, Any]:
        response = self.session.get(download_url, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Request {filename} must contain a JSON object.")
        return data


def list_ready_jobs(queue: GitHubQueue, state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    jobs: list[tuple[str, dict[str, Any]]] = []
    for filename, url in queue.list_requests():
        try:
            raw = queue.load_request(filename, url)
            request = validate_request(raw, filename, queue.branch)
        except Exception as exc:
            logging.error("Ignoring invalid request %s: %s", filename, exc)
            continue
        existing = state["jobs"].get(request["job_id"], {})
        if existing.get("status") == "completed":
            continue
        if int(existing.get("attempts", 0)) >= MAX_ATTEMPTS and existing.get("status") == "failed":
            continue
        jobs.append((filename, request))
    jobs.sort(key=lambda pair: (pair[1].get("requested_at", ""), pair[1]["job_id"]))
    return jobs


def push_review_outputs(branch: str, review_dir: Path) -> None:
    relative = review_dir.relative_to(ROOT).as_posix()
    if not review_dir.is_dir() or not (review_dir / "contact_sheet.jpg").is_file():
        raise RuntimeError(f"Expected review files are missing under {review_dir}.")

    # Sync first. The only files staged below are the review artifacts for this job.
    ensure_correct_repo(branch)
    status_lines = run_git(["status", "--porcelain"]).stdout.splitlines()
    allowed_prefix = f"?? {relative}/"
    allowed_modified_prefix = f" M {relative}/"
    unexpected = [
        line for line in status_lines
        if not (line.startswith(allowed_prefix) or line.startswith(allowed_modified_prefix))
    ]
    if unexpected:
        raise RuntimeError(
            "Refusing to publish review artifacts because unrelated working-tree changes exist:\n"
            + "\n".join(unexpected[:20])
        )

    run_git(["fetch", "origin", branch])
    ahead = int(run_git(["rev-list", "--count", f"origin/{branch}..HEAD"]).stdout.strip() or "0")
    if ahead:
        # The only accepted ahead commit is this job's review-artifact commit.
        subject = run_git(["log", "-1", "--pretty=%s"]).stdout.strip()
        if not subject.startswith("automation: publish review sheet "):
            raise RuntimeError(
                f"Local branch has an unexpected unpushed commit: {subject!r}. Refusing to push it."
            )
        run_git(["rebase", f"origin/{branch}"])
    else:
        behind = int(run_git(["rev-list", "--count", f"HEAD..origin/{branch}"]).stdout.strip() or "0")
        if behind:
            # Untracked review files use a unique job ID and cannot collide with code updates.
            run_git(["pull", "--rebase", "origin", branch])

    run_git(["add", "--", relative])
    staged = run_git(["diff", "--cached", "--name-only"]).stdout.splitlines()
    if staged:
        run_git(["-c", "core.safecrlf=false", "commit", "-m", f"automation: publish review sheet {review_dir.name}"])
    run_git(["push", "origin", f"HEAD:{branch}"])
    logging.info("Review sheet pushed to %s/%s/%s", "https://github.com", "serag21/ai-product-pipeline", relative)


def process_job(
    filename: str,
    request: dict[str, Any],
    queue: GitHubQueue,
    state: dict[str, Any],
    state_path: Path,
    branch: str,
    auto_start_comfy: bool,
) -> None:
    job_id = request["job_id"]
    record = state["jobs"].setdefault(job_id, {
        "status": "queued",
        "attempts": 0,
        "created_at": utc_now(),
    })
    if record.get("status") == "push_pending":
        review_dir = ROOT / str(record.get("review_dir", ""))
        push_review_outputs(branch, review_dir)
        record.update({"status": "completed", "completed_at": utc_now(), "error": None})
        atomic_json(state_path, state)
        return

    record["attempts"] = int(record.get("attempts", 0)) + 1
    record.update({"status": "running", "started_at": utc_now(), "last_error": None})
    atomic_json(state_path, state)
    logging.info("Starting job %s for product %s", job_id, request["product_id"])

    try:
        sync_latest(branch)
        product_root = safe_product_root(request["product_id"])
        product = load_json(product_root / "prompts.json")
        total = len(product["pages"])
        start = request["start"]
        count = request["count"] if request["count"] is not None else total - start + 1
        if start + count - 1 > total:
            raise RuntimeError(
                f"Requested pages {start}..{start + count - 1}, but manifest has {total} entries."
            )

        ensure_comfyui(request["server"], auto_start_comfy)
        command = [
            sys.executable,
            str(ROOT / "scripts" / "generate_product.py"),
            "--product", str(product_root),
            "--count", str(count),
            "--start", str(start),
            "--server", request["server"],
            "--timeout", "1800",
        ]
        logging.info("Running generation for %d manifest page(s).", count)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(3600, count * 1800),
        )
        (DEFAULT_STATE_DIR / "logs").mkdir(parents=True, exist_ok=True)
        (DEFAULT_STATE_DIR / "logs" / f"{job_id}-generation.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        if completed.returncode:
            raise RuntimeError(
                f"Generator returned exit code {completed.returncode}. "
                f"Log: {DEFAULT_STATE_DIR / 'logs' / f'{job_id}-generation.log'}\n"
                + completed.stdout[-4000:]
            )

        review_dir = ROOT / "test_outputs" / "automated_reviews" / job_id
        review_dir.mkdir(parents=True, exist_ok=True)
        title = str(product.get("title") or product.get("product_id") or request["product_id"])
        metrics = create_review_sheet(
            product_root,
            review_dir / "contact_sheet.jpg",
            title,
        )
        expected = [int(item["page"]) for item in product["pages"][start - 1:start - 1 + count]]
        if [item["page"] for item in metrics] != [int(item["page"]) for item in product["pages"]]:
            # The book-level sheet intentionally represents the entire current manifest.
            # Generation must still have completed for every required page.
            missing = [
                page for page in expected
                if not (product_root / "pages" / f"page_{page:03d}.png").is_file()
            ]
            if missing:
                raise RuntimeError(f"Required generated pages are missing: {missing}")

        report = {
            "schema_version": 1,
            "job_id": job_id,
            "request_file": f"control/requests/{filename}",
            "action": request["action"],
            "product_id": request["product_id"],
            "title": title,
            "status": "READY_FOR_VISUAL_REVIEW",
            "started_at": record.get("started_at"),
            "review_prepared_at": utc_now(),
            "expected_generated_pages": expected,
            "manifest_page_count": len(product["pages"]),
            "contact_sheet": f"test_outputs/automated_reviews/{job_id}/contact_sheet.jpg",
            "contact_sheet_url": (
                f"https://github.com/serag21/ai-product-pipeline/blob/{branch}/"
                f"test_outputs/automated_reviews/{job_id}/contact_sheet.jpg"
            ),
            "generation_log_local_path": str(DEFAULT_STATE_DIR / "logs" / f"{job_id}-generation.log"),
            "page_metrics": metrics,
            "owner_approval_required": True,
            "publishing_allowed": False,
            "note": (
                "This is a candidate review sheet, not an approved sale-ready book. "
                "Inspect every page for scene quality, anatomy, connected props, linework, "
                "motif repetition, and printing behavior before approving."
            ),
        }
        atomic_json(review_dir / "run_report.json", report)
        record.update({
            "status": "push_pending",
            "review_dir": review_dir.relative_to(ROOT).as_posix(),
            "review_url": report["contact_sheet_url"],
            "prepared_at": utc_now(),
            "last_error": None,
        })
        atomic_json(state_path, state)
        push_review_outputs(branch, review_dir)
        record.update({
            "status": "completed",
            "completed_at": utc_now(),
            "error": None,
            "review_url": report["contact_sheet_url"],
        })
        atomic_json(state_path, state)
        logging.info("Job %s ready for visual review: %s", job_id, report["contact_sheet_url"])
    except Exception as exc:
        record.update({"status": "failed", "failed_at": utc_now(), "last_error": str(exc)})
        atomic_json(state_path, state)
        logging.exception("Job %s failed (attempt %d/%d).", job_id, record["attempts"], MAX_ATTEMPTS)


def setup_logging(state_dir: Path) -> None:
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "worker.log", encoding="utf-8"),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "once", "status"))
    parser.add_argument("--repo", default=os.environ.get("AI_PIPELINE_GITHUB_REPO", REPO_DEFAULT))
    parser.add_argument("--branch", default=os.environ.get("AI_PIPELINE_GITHUB_BRANCH", BRANCH_DEFAULT))
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS_DEFAULT)
    parser.add_argument("--no-auto-start-comfy", action="store_true")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    args = parser.parse_args()

    if args.poll_seconds < 15:
        parser.error("--poll-seconds must be at least 15 seconds.")

    state_dir = Path(args.state_dir).expanduser().resolve()
    state_path = state_dir / "worker-state.json"
    setup_logging(state_dir)
    state = load_worker_state(state_path)

    if args.command == "status":
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    queue = GitHubQueue(args.repo, args.branch)
    auto_start_comfy = not args.no_auto_start_comfy

    while True:
        try:
            state = load_worker_state(state_path)
            jobs = list_ready_jobs(queue, state)
            if not jobs:
                if args.command == "once":
                    logging.info("No new eligible requests found.")
                    return 0
                time.sleep(args.poll_seconds)
                continue
            filename, request = jobs[0]
            process_job(
                filename, request, queue, state, state_path,
                args.branch, auto_start_comfy,
            )
            if args.command == "once":
                return 0
        except KeyboardInterrupt:
            logging.info("Worker stopped by user.")
            return 130
        except Exception:
            logging.exception("Worker loop encountered an error; it will retry.")
            if args.command == "once":
                return 1
            time.sleep(min(args.poll_seconds, 60))


if __name__ == "__main__":
    raise SystemExit(main())
