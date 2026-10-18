#!/usr/bin/env python3
"""Persistent, allow-listed local worker for AI Product Pipeline.

Jobs are opened as GitHub issues by the repository owner. Only two job types are
accepted; issue contents are data, never shell commands. Generated review
artifacts are committed to test_outputs so they can be inspected in ChatGPT.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO = "serag21/ai-product-pipeline"
OWNER = "serag21"
API_ISSUES = f"https://api.github.com/repos/{REPO}/issues?state=open&per_page=100"
DEFAULT_SERVER = "http://127.0.0.1:8188"
JOB_PREFIX = "[AI-PIPELINE JOB]"
MAX_PAGES = 60
DEFAULT_TIMEOUT_SECONDS = 12 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_command(args: list[str], *, cwd: Path, timeout: int = 600,
                check: bool = True) -> subprocess.CompletedProcess[str]:
    logging.info("Running: %s", " ".join(args))
    result = subprocess.run(
        args, cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
        check=False,
    )
    if result.stdout:
        logging.info("%s", result.stdout[-12000:])
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command returned {result.returncode}: {' '.join(args)}\n"
            f"{result.stdout[-5000:]}"
        )
    return result


def http_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-product-pipeline-local-worker/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def comfy_is_healthy(server: str) -> bool:
    try:
        request = urllib.request.Request(
            server.rstrip("/") + "/system_stats",
            headers={"User-Agent": "ai-product-pipeline-local-worker/1.0"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def load_state(state_path: Path) -> dict[str, Any]:
    if state_path.exists():
        try:
            obj = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and isinstance(obj.get("jobs"), dict):
                return obj
        except (OSError, json.JSONDecodeError):
            logging.exception("Worker state file is unreadable; preserving a backup.")
            backup = state_path.with_suffix(".corrupt-" + str(int(time.time())) + ".json")
            shutil.copy2(state_path, backup)
    return {"version": 1, "jobs": {}}


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(state_path)


def extract_job(body: str) -> dict[str, Any]:
    begin, end = "<job-json>", "</job-json>"
    start = body.find(begin)
    finish = body.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or finish < 0:
        raise ValueError(
            "Issue body must include a JSON object between <job-json> and </job-json>."
        )
    payload = json.loads(body[start + len(begin):finish].strip())
    if not isinstance(payload, dict):
        raise ValueError("Job payload must be a JSON object.")
    return payload


def safe_product_path(value: Any) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.startswith("products/"):
        raise ValueError("product must be a path inside products/.")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("product path traversal is not allowed.")
    resolved = (ROOT / candidate).resolve()
    if not resolved.is_relative_to((ROOT / "products").resolve()):
        raise ValueError("product must remain inside products/.")
    manifest_path = resolved / "prompts.json"
    if not manifest_path.is_file():
        raise ValueError(f"Product manifest not found: {value}/prompts.json")
    return candidate.as_posix(), resolved


def validate_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_type = payload.get("type")
    if job_type not in {"generate_review_set", "package_approved_product"}:
        raise ValueError(
            "Unsupported job type. Allowed: generate_review_set, package_approved_product."
        )
    product_rel, product_path = safe_product_path(payload.get("product"))
    job = dict(payload)
    job["_product_rel"] = product_rel
    job["_product_path"] = str(product_path)

    if job_type == "generate_review_set":
        manifest = json.loads((product_path / "prompts.json").read_text(encoding="utf-8"))
        pages = manifest.get("pages", [])
        if not pages:
            raise ValueError("Product manifest has no pages.")
        start = payload.get("start", 1)
        count = payload.get("count", len(pages))
        if type(start) is not int or type(count) is not int:
            raise ValueError("start and count must be integers.")
        if start < 1 or count < 1 or count > MAX_PAGES:
            raise ValueError(f"start must be >=1 and count must be 1..{MAX_PAGES}.")
        selected = pages[start - 1:start - 1 + count]
        if not selected:
            raise ValueError("The requested start/count selects no pages.")
        job["_selected_pages"] = selected
        job["_source_manifest"] = manifest
        mode = payload.get("sheet_mode", "auto")
        if mode not in {"auto", "contact", "variants"}:
            raise ValueError("sheet_mode must be auto, contact, or variants.")
        job["sheet_mode"] = mode
    else:
        if payload.get("owner_approved") is not True:
            raise ValueError(
                "Packaging requires owner_approved=true after the visual review checkpoint."
            )
        if not isinstance(payload.get("title"), str) or not payload["title"].strip():
            raise ValueError("package_approved_product requires a final title.")
        if not isinstance(payload.get("brand", "Little Haunts Club"), str):
            raise ValueError("brand must be a string.")
        manifest = json.loads((product_path / "prompts.json").read_text(encoding="utf-8"))
        pages = manifest.get("pages", [])
        if not pages or len(pages) > MAX_PAGES:
            raise ValueError(f"Product must have 1..{MAX_PAGES} pages.")
        missing = [
            item.get("page")
            for item in pages
            if not (product_path / "pages" / f"page_{int(item['page']):03d}.png").is_file()
        ]
        if missing:
            raise ValueError(f"Product has missing generated pages: {missing[:10]}")
        generation_manifest = product_path / "qa" / "generation_manifest.json"
        if generation_manifest.exists():
            gm = json.loads(generation_manifest.read_text(encoding="utf-8"))
            summary = gm.get("summary", {})
            if summary.get("passed") != len(pages) or summary.get("failed") != 0:
                raise ValueError("Generation QA is not fully green; refusing to package.")
    return job


def read_issues() -> list[dict[str, Any]]:
    raw = http_json(API_ISSUES)
    if not isinstance(raw, list):
        raise RuntimeError("Unexpected GitHub issues API response.")
    return [
        item for item in raw
        if "pull_request" not in item
        and item.get("user", {}).get("login", "").casefold() == OWNER.casefold()
        and str(item.get("title", "")).startswith(JOB_PREFIX)
    ]


def run_python(script: str, arguments: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS,
               check: bool = True) -> subprocess.CompletedProcess[str]:
    python = Path(sys.executable)
    return run_command(
        [str(python), str(ROOT / "scripts" / script), *arguments],
        cwd=ROOT, timeout=timeout, check=check,
    )


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def make_review_sheets(job: dict[str, Any], temp_rel: str, out_dir: Path,
                       generated_pages: list[Path]) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = job["_source_manifest"]
    chosen = job["_selected_pages"]
    has_candidate_slots = any("candidate_scene_slot" in item for item in chosen)
    requested_mode = job.get("sheet_mode", "auto")
    mode = "variants" if requested_mode == "auto" and has_candidate_slots else requested_mode
    if mode == "auto":
        mode = "contact"

    files: list[str] = []
    # Save the exact selected prompt/provenance data with the visual review.
    write_json(out_dir / "selected_manifest.json", {
        "source_product": job["_product_rel"],
        "source_title": manifest.get("title"),
        "worker_product": temp_rel,
        "pages": chosen,
        "sheet_mode": mode,
        "generated_at": utc_now(),
    })

    page_records = []
    for item in chosen:
        page_path = ROOT / temp_rel / "pages" / f"page_{int(item['page']):03d}.png"
        if page_path.is_file():
            page_records.append((item, page_path))

    if not page_records:
        return files

    if mode == "variants" and len(page_records) == len(chosen):
        run_python(
            "variant_review_sheets.py",
            ["--product", temp_rel, "--output", str(out_dir.relative_to(ROOT))],
        )
        files.extend(sorted(p.name for p in out_dir.glob("*.jpg")))
    else:
        run_python("contact_sheet.py", ["--product", temp_rel])
        source_sheet = ROOT / temp_rel / "qa" / "contact_sheet.jpg"
        if source_sheet.is_file():
            dest = out_dir / "contact_sheet.jpg"
            shutil.copy2(source_sheet, dest)
            files.append(dest.name)
        if len(page_records) <= 15:
            details = out_dir / "details"
            run_python(
                "detail_qa.py",
                ["--product", temp_rel, "--output", str(details.relative_to(ROOT))],
            )
            files.extend(str(p.relative_to(out_dir)) for p in sorted(details.glob("*.jpg")))

    return files


def commit_review_artifacts(issue_number: int, out_dir: Path) -> None:
    relative = out_dir.relative_to(ROOT).as_posix()
    # The worker clone is isolated on purpose; it never resets the user's working copy.
    run_command(["git", "fetch", "origin", "main"], cwd=ROOT, timeout=120)
    run_command(["git", "checkout", "-B", "main", "origin/main"], cwd=ROOT)
    run_command(
        ["git", "config", "user.name", "AI Product Pipeline Worker"], cwd=ROOT
    )
    run_command(
        ["git", "config", "user.email", "worker@users.noreply.github.com"], cwd=ROOT
    )
    run_command(["git", "add", "-A", "--", relative], cwd=ROOT)
    diff = run_command(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False)
    if diff.returncode == 0:
        logging.info("No new artifact changes to commit for issue #%s.", issue_number)
        return
    run_command(
        ["git", "commit", "-m", f"Add automation artifacts for job #{issue_number}"],
        cwd=ROOT,
    )
    last_error = None
    for attempt in range(1, 4):
        try:
            run_command(["git", "push", "origin", "main"], cwd=ROOT, timeout=180)
            return
        except Exception as exc:
            last_error = exc
            logging.warning("Push attempt %s/3 failed: %s", attempt, exc)
            try:
                run_command(["git", "fetch", "origin", "main"], cwd=ROOT, timeout=120)
                run_command(["git", "rebase", "origin/main"], cwd=ROOT, timeout=120)
            except Exception:
                logging.exception("Could not rebase worker result after a push conflict.")
                break
    raise RuntimeError(f"Could not push review artifacts after three attempts: {last_error}")


def run_review_job(issue: dict[str, Any], job: dict[str, Any],
                   state_path: Path, state: dict[str, Any]) -> None:
    number = int(issue["number"])
    temp_id = f"worker-job-{number}"
    temp_rel = f"products/worker-runs/{temp_id}"
    temp_root = ROOT / temp_rel
    out_dir = ROOT / "test_outputs" / "automation_jobs" / f"job-{number:06d}"
    if temp_root.exists():
        shutil.rmtree(temp_root)
    if out_dir.exists():
        shutil.rmtree(out_dir)

    temp_manifest = copy.deepcopy(job["_source_manifest"])
    temp_manifest["product_id"] = temp_id
    temp_manifest["title"] = f"{temp_manifest.get('title', temp_id)} — Review Job #{number}"
    temp_manifest["pages"] = copy.deepcopy(job["_selected_pages"])
    temp_manifest["automation_source_product"] = job["_product_rel"]
    temp_manifest["automation_issue_number"] = number
    temp_root.mkdir(parents=True, exist_ok=True)
    write_json(temp_root / "prompts.json", temp_manifest)

    state["jobs"][str(number)] = {
        "status": "running", "started_at": utc_now(),
        "type": "generate_review_set", "product": job["_product_rel"],
    }
    save_state(state_path, state)
    summary: dict[str, Any] = {
        "issue_number": number,
        "type": "generate_review_set",
        "source_product": job["_product_rel"],
        "source_title": job["_source_manifest"].get("title"),
        "worker_product": temp_rel,
        "requested_pages": len(job["_selected_pages"]),
        "started_at": utc_now(),
    }
    attempt_logs = []
    generation_ok = False
    # Bounded retries are for transient ComfyUI/generation errors only.
    for attempt in range(1, 4):
        logging.info("Job #%s generation attempt %s/3.", number, attempt)
        result = run_python(
            "generate_product.py",
            ["--product", temp_rel, "--count", str(len(job["_selected_pages"])),
             "--start", "1", "--server", job.get("server", DEFAULT_SERVER)],
            timeout=DEFAULT_TIMEOUT_SECONDS, check=False,
        )
        attempt_logs.append({
            "attempt": attempt, "return_code": result.returncode,
            "tail": result.stdout[-4000:],
        })
        qa_path = temp_root / "qa" / "generation_manifest.json"
        if qa_path.exists():
            try:
                qa = json.loads(qa_path.read_text(encoding="utf-8"))
                if qa.get("summary", {}).get("failed") == 0 and qa.get("summary", {}).get("passed") == len(job["_selected_pages"]):
                    generation_ok = True
                    break
            except json.JSONDecodeError:
                logging.exception("Generation QA manifest could not be parsed.")
        if not comfy_is_healthy(job.get("server", DEFAULT_SERVER)):
            logging.warning("ComfyUI became unavailable; pausing further retries.")
            break

    qa_path = temp_root / "qa" / "generation_manifest.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8")) if qa_path.exists() else {}
    generated_pages = sorted((temp_root / "pages").glob("page_*.png"))
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet_files: list[str] = []
    if generated_pages:
        try:
            sheet_files = make_review_sheets(job, temp_rel, out_dir, generated_pages)
        except Exception as exc:
            logging.exception("Review sheet generation failed.")
            summary["sheet_error"] = str(exc)
            # Keep at least a readable review sheet when variants are incomplete.
            try:
                run_python("contact_sheet.py", ["--product", temp_rel])
                source_sheet = temp_root / "qa" / "contact_sheet.jpg"
                if source_sheet.exists():
                    shutil.copy2(source_sheet, out_dir / "contact_sheet.jpg")
                    sheet_files.append("contact_sheet.jpg")
            except Exception:
                logging.exception("Fallback contact sheet also failed.")
    else:
        write_json(out_dir / "selected_manifest.json", {
            "source_product": job["_product_rel"],
            "pages": job["_selected_pages"],
            "worker_product": temp_rel,
        })

    summary.update({
        "status": "ready_for_visual_review" if generation_ok else "needs_attention",
        "generation_attempts": attempt_logs,
        "generation_summary": qa.get("summary", {}),
        "generated_page_files": [p.name for p in generated_pages],
        "review_files": sheet_files,
        "completed_at": utc_now(),
        "artifacts_directory": f"test_outputs/automation_jobs/job-{number:06d}",
        "artifact_url": f"https://github.com/{REPO}/tree/main/test_outputs/automation_jobs/job-{number:06d}",
        "local_pages_directory": temp_rel + "/pages",
        "note": "Generation PASS is not visual approval. Do not package or publish until the owner selects the pages.",
    })
    write_json(out_dir / "result.json", summary)
    state["jobs"][str(number)] = {
        "status": summary["status"], "completed_at": summary["completed_at"],
        "result": summary["artifact_url"],
    }
    save_state(state_path, state)
    commit_review_artifacts(number, out_dir)
    logging.info(
        "Job #%s finished with status %s. Review: %s",
        number, summary["status"], summary["artifact_url"],
    )


def run_package_job(issue: dict[str, Any], job: dict[str, Any],
                    state_path: Path, state: dict[str, Any]) -> None:
    number = int(issue["number"])
    product_rel = job["_product_rel"]
    product_path = Path(job["_product_path"])
    out_dir = ROOT / "test_outputs" / "automation_jobs" / f"job-{number:06d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    state["jobs"][str(number)] = {
        "status": "running", "started_at": utc_now(),
        "type": "package_approved_product", "product": product_rel,
    }
    save_state(state_path, state)

    run_python("package_printable_product.py", ["--product", product_rel], timeout=3600)
    final_root = product_path / "final"
    qa_path = final_root / "package_qa.json"
    if not qa_path.is_file():
        raise RuntimeError("Packaging script completed without package_qa.json.")
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    if qa.get("status") != "PACKAGE_BUILT_REVIEW_REQUIRED":
        raise RuntimeError(f"Unexpected package QA status: {qa.get('status')}")
    # The package stays on the local PC; only safe review metadata is committed.
    shutil.copy2(qa_path, out_dir / "package_qa.json")
    shutil.copy2(final_root / "etsy_listing_draft.md", out_dir / "etsy_listing_draft.md")
    summary = {
        "issue_number": number, "type": "package_approved_product",
        "product": product_rel, "title": job["title"], "brand": job.get("brand", "Little Haunts Club"),
        "status": "package_ready_locally", "completed_at": utc_now(),
        "local_package_directory": str(final_root),
        "review_files": ["package_qa.json", "etsy_listing_draft.md"],
        "note": "Files are packaged locally. This does not create, upload, or publish an Etsy listing.",
    }
    write_json(out_dir / "result.json", summary)
    state["jobs"][str(number)] = {
        "status": "package_ready_locally", "completed_at": summary["completed_at"],
        "result": f"https://github.com/{REPO}/tree/main/test_outputs/automation_jobs/job-{number:06d}",
    }
    save_state(state_path, state)
    commit_review_artifacts(number, out_dir)


def process_issue(issue: dict[str, Any], state_path: Path,
                  state: dict[str, Any], server: str) -> None:
    number = int(issue["number"])
    key = str(number)
    existing = state["jobs"].get(key, {})
    if existing.get("status") in {
        "ready_for_visual_review", "needs_attention", "package_ready_locally", "rejected"
    }:
        return

    # A result in the public repository is the durable idempotency check if the
    # local state file was lost or the worker was reinstalled.
    remote_result = ROOT / "test_outputs" / "automation_jobs" / f"job-{number:06d}" / "result.json"
    if remote_result.exists():
        try:
            stored = json.loads(remote_result.read_text(encoding="utf-8"))
            status = stored.get("status")
            if status in {"ready_for_visual_review", "needs_attention", "package_ready_locally"}:
                state["jobs"][key] = {"status": status, "result": stored.get("artifact_url"), "recovered": True}
                save_state(state_path, state)
                return
        except (OSError, json.JSONDecodeError):
            pass

    try:
        payload = extract_job(str(issue.get("body", "")))
        # The ComfyUI endpoint is an operator-controlled setting, not shell input.
        payload.setdefault("server", server)
        job = validate_job(payload)
    except Exception as exc:
        logging.error("Rejecting issue #%s: %s", number, exc)
        state["jobs"][key] = {"status": "rejected", "error": str(exc), "completed_at": utc_now()}
        save_state(state_path, state)
        return

    if job["type"] == "generate_review_set":
        if not comfy_is_healthy(job.get("server", server)):
            logging.info("ComfyUI unavailable; leaving job #%s queued.", number)
            return
    try:
        if job["type"] == "generate_review_set":
            run_review_job(issue, job, state_path, state)
        else:
            run_package_job(issue, job, state_path, state)
    except Exception as exc:
        logging.exception("Job #%s failed.", number)
        state["jobs"][key] = {
            "status": "failed", "error": str(exc), "completed_at": utc_now(),
        }
        save_state(state_path, state)


def configure_logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "worker.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--poll-seconds", type=int, default=90)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--once", action="store_true", help="Check the queue once and exit.")
    parser.add_argument("--check", action="store_true", help="Check network/repo configuration, then exit.")
    args = parser.parse_args()
    if args.poll_seconds < 30:
        parser.error("--poll-seconds must be at least 30 seconds.")
    state_dir = Path(args.state_dir) if args.state_dir else Path(os.environ.get(
        "LOCALAPPDATA", str(Path.home())
    )) / "AIProductPipelineWorker"
    state_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(state_dir)
    state_path = state_dir / "state.json"

    remote = run_command(["git", "remote", "get-url", "origin"], cwd=ROOT).stdout
    if "serag21/ai-product-pipeline" not in remote:
        logging.error("This checkout is not the expected repository: %s", remote.strip())
        return 2
    try:
        who = http_json("https://api.github.com/repos/serag21/ai-product-pipeline", timeout=15)
        if who.get("full_name", "").casefold() != REPO.casefold():
            raise RuntimeError("Unexpected GitHub repository metadata.")
    except Exception as exc:
        logging.error("GitHub API check failed: %s", exc)
        if args.check:
            return 2

    if args.check:
        logging.info("Worker configuration is valid. ComfyUI healthy: %s", comfy_is_healthy(args.server))
        logging.info("ComfyUI may be started later; queued generation jobs will wait for it.")
        return 0

    logging.info("Worker online. Repo=%s Server=%s Poll=%ss", REPO, args.server, args.poll_seconds)
    while True:
        state = load_state(state_path)
        try:
            issues = read_issues()
            for issue in sorted(issues, key=lambda x: int(x["number"])):
                process_issue(issue, state_path, state, args.server)
                state = load_state(state_path)
        except urllib.error.HTTPError as exc:
            logging.error("GitHub HTTP error: %s", exc)
        except Exception:
            logging.exception("Queue poll failed; will retry next cycle.")
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
