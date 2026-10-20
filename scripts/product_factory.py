#!/usr/bin/env python
"""Persistent Windows-local reviewer, retry, and package supervisor."""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from comfy.client import ComfyUIClient
from product_review_worker import OpenAICompatibleReviewer, atomic_json, load_product, read_state, resolve_product_root


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def user_environment(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value) if value else None
        except (OSError, ImportError):
            return None
    return None


def read_config(product_root: Path) -> dict[str, Any]:
    config_path = product_root / "factory_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    vision = config.get("vision", {})
    endpoint = vision.get("endpoint") or os.getenv("VISION_API_BASE_URL")
    model = vision.get("model") or os.getenv("VISION_MODEL")
    key_name = vision.get("api_key_env", "VISION_API_KEY")
    api_key = user_environment(key_name)
    if not endpoint or not model:
        return {"ready": False, "reason": "vision endpoint and model are not configured", "config_path": str(config_path)}
    return {"ready": True, "endpoint": str(endpoint).rstrip("/"), "model": str(model), "api_key": api_key,
            "key_env": key_name, "max_attempts": int(config.get("generation", {}).get("max_attempts", 3)),
            "accept_score": int(config.get("generation", {}).get("accept_score", 80)),
            "unload_endpoint": config.get("vision", {}).get("unload_endpoint"),
            "max_tokens": max(1, int(vision.get("max_tokens", 1024))),
            "reasoning_effort": vision.get("reasoning_effort"),
            "poll_seconds": max(5, int(config.get("supervisor", {}).get("poll_seconds", 20))),
            "service_retry_limit": max(1, int(config.get("supervisor", {}).get("service_retry_limit", 3)))}


def write_status(path: Path, status: str, run_id: str, **extra: Any) -> None:
    atomic_json(path, {"run_id": run_id, "status": status, "updated_at": now(), **extra})


def build_completion_report(product: dict[str, Any], run_id: str, model: str, state: dict[str, Any],
                           package_dir: Path, qa: dict[str, Any]) -> dict[str, Any]:
    return {"product_id": product["product_id"], "run_id": run_id, "completed_at": now(),
        "status": "READY_FOR_FINAL_HUMAN_REVIEW", "vision_provider": model,
        "reviewed_pages": len(product["pages"]),
        "attempts": sum(len(record.get("attempts", [])) for record in state["candidates"].values()),
        "deliverables_directory": str(package_dir), "upload_files": [f["name"] for f in qa["etsy_upload_files"]],
        "pdf_render_checks": qa["pdf_render_checks"], "human_approval_required": True, "published": False}


def make_assembly(product_root: Path, run_dir: Path, source_product: dict[str, Any], state: dict[str, Any]) -> Path:
    assembly = run_dir / "assembly_source"
    (assembly / "pages").mkdir(parents=True, exist_ok=True)
    (assembly / "qa").mkdir(parents=True, exist_ok=True)
    shutil.copy2(product_root / "prompts.json", assembly / "prompts.json")
    generated_records = []
    for item in source_product["pages"]:
        page = int(item["page"])
        record = state.get("candidates", {}).get(str(page))
        if not record or record.get("status") != "accepted_by_vision":
            raise RuntimeError(f"Page {page} is not accepted by the configured vision reviewer.")
        candidate_path = Path(record["best_candidate"])
        if not candidate_path.is_file():
            raise RuntimeError(f"Accepted page {page} candidate is missing: {candidate_path}")
        destination = assembly / "pages" / f"page_{page:03d}.png"
        if not destination.exists():
            shutil.copy2(candidate_path, destination)
        best = next((c for c in record.get("attempts", []) if c.get("file") == record["best_candidate"]), {})
        generated_records.append({"page": page, "variant_id": item.get("variant_id"), "label": item.get("label"),
                                  "seed": best.get("seed"), "file": str(destination), "qa": best.get("qa", {})})
    atomic_json(assembly / "qa" / "generation_manifest.json", {
        "product_id": source_product["product_id"], "workflow": "workflows/flux2_coloring_api.json",
        "canvas": source_product.get("generation", {}), "pages": generated_records,
        "summary": {"requested": len(generated_records), "passed": len(generated_records), "failed": 0},
    })
    return assembly


def monitor_worker(command: list[str], state_path: Path, status_path: Path, run_id: str,
                   log_path: Path, poll_seconds: int, total_pages: int, child_env: dict[str, str]) -> int:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now()}] Starting worker: {' '.join(command)}\n")
        log.flush()
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=flags, env=child_env)
        last_done, initial_done, started = None, None, time.monotonic()
        while process.poll() is None:
            cancel_path = status_path.with_name("cancel.request")
            if cancel_path.exists():
                process.terminate()
                process.wait(timeout=30)
                cancel_path.unlink(missing_ok=True)
                write_status(status_path, "cancelled", run_id, message="Saved run state is resumable.")
                return 130
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                records = state.get("candidates", {}).values()
                done = sum(r.get("status") == "accepted_by_vision" for r in records)
                total = total_pages
                if last_done is None:
                    last_done = initial_done = done
                    write_status(status_path, "reviewing", run_id, completed_pages=done, total_pages=total,
                                 eta_seconds=None, last_progress_at=now())
                elif done != last_done:
                    elapsed = max(1, time.monotonic() - started)
                    newly_completed = done - initial_done
                    rate = newly_completed / elapsed
                    eta = int((total - done) / rate) if rate else None
                    write_status(status_path, "reviewing", run_id, completed_pages=done, total_pages=total,
                                 eta_seconds=eta, last_progress_at=now())
                    last_done = done
            except (OSError, json.JSONDecodeError):
                pass
            time.sleep(poll_seconds)
        log.write(f"[{now()}] Worker exited {process.returncode}.\n")
        return int(process.returncode or 0)


def run_supervisor(args: argparse.Namespace) -> int:
    product_root = resolve_product_root(args.product)
    product = load_product(product_root)
    run_dir = product_root / "review_runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = product_root / "factory_status.json"
    log_path = product_root / "factory.log"
    failure_path = run_dir / "failure.flag"
    service_failures = 0
    retry_exhausted_requested = False
    print(f"Product factory: {product['product_id']} | run={args.run_id}", flush=True)
    while True:
        cancel_path = status_path.with_name("cancel.request")
        if cancel_path.exists():
            cancel_path.unlink(missing_ok=True)
            write_status(status_path, "cancelled", args.run_id)
            return 130
        config = read_config(product_root)
        if not config["ready"]:
            message = (f"Vision review is required. Configure {product_root / 'factory_config.json'} "
                       "with endpoint and model. This service will resume automatically when that file is saved.")
            write_status(status_path, "waiting_for_configuration", args.run_id, message=message,
                         config_file=config["config_path"])
            print(message, flush=True)
            time.sleep(args.poll_seconds or 20)
            continue
        if not 1 <= config["max_attempts"] <= 8 or not 1 <= config["accept_score"] <= 100:
            write_status(status_path, "configuration_error", args.run_id,
                         message="max_attempts must be 1..8 and accept_score 1..100")
            return 2
        reviewer = OpenAICompatibleReviewer(config["endpoint"], config["model"], config["api_key"],
            unload_endpoint=config["unload_endpoint"], max_tokens=config["max_tokens"],
            reasoning_effort=config["reasoning_effort"])
        try:
            write_status(status_path, "freeing_comfy_vram", args.run_id,
                         message="Unloading FLUX models before loading the vision reviewer.")
            ComfyUIClient().free_memory()
        except Exception as exc:
            write_status(status_path, "waiting_for_comfyui", args.run_id, error=str(exc),
                         message="ComfyUI must be reachable and release VRAM before vision inference.")
            time.sleep(config["poll_seconds"])
            continue
        try:
            reviewer.check()
        except Exception as exc:
            service_failures += 1
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{now()}] Vision provider check failed: {exc}\n")
            write_status(status_path, "waiting_for_provider", args.run_id, error=str(exc), retry=service_failures)
            if service_failures >= config["service_retry_limit"]:
                failure_path.write_text(str(exc), encoding="utf-8")
                write_status(status_path, "provider_error", args.run_id, error=str(exc),
                             message="Fix the provider configuration; supervisor continues polling.")
                service_failures = 0
            time.sleep(config["poll_seconds"])
            continue
        service_failures = 0
        state_path = run_dir / "state.json"
        state = read_state(state_path, product["product_id"])
        if state.get("candidates") and all(state["candidates"].get(str(int(p["page"])), {}).get("status") == "accepted_by_vision"
                                            for p in product["pages"]):
            break
        command = [sys.executable, "scripts/product_review_worker.py", "--product", str(product_root), "--run-id", args.run_id,
                   "--review-existing", "--max-attempts", str(config["max_attempts"]), "--accept-score", str(config["accept_score"]),
                   "--vision-endpoint", config["endpoint"], "--vision-model", config["model"],
                   "--vision-max-tokens", str(config["max_tokens"])]
        if config["reasoning_effort"]:
            command.extend(["--vision-reasoning-effort", str(config["reasoning_effort"])])
        if config["unload_endpoint"]:
            command.extend(["--vision-unload-endpoint", str(config["unload_endpoint"])])
        if retry_exhausted_requested:
            command.append("--retry-exhausted")
            retry_exhausted_requested = False
        write_status(status_path, "starting_review", args.run_id, total_pages=len(product["pages"]))
        child_env = os.environ.copy()
        if config["api_key"]:
            child_env[config["key_env"]] = config["api_key"]
        result = monitor_worker(command, state_path, status_path, args.run_id, log_path,
                                args.poll_seconds or config["poll_seconds"], len(product["pages"]), child_env)
        if result == 130:
            return 130
        state = read_state(state_path, product["product_id"])
        unresolved = [int(p["page"]) for p in product["pages"]
                      if state.get("candidates", {}).get(str(int(p["page"])), {}).get("status") != "accepted_by_vision"]
        if unresolved:
            service_failures += 1
            write_status(status_path, "retrying_service", args.run_id, unresolved_pages=unresolved, retry=service_failures)
            if service_failures >= config["service_retry_limit"]:
                failure_path.write_text(f"Unresolved pages after {service_failures} worker restarts: {unresolved}", encoding="utf-8")
                write_status(status_path, "needs_attention", args.run_id, unresolved_pages=unresolved,
                             message="Inspect factory.log and state, fix the provider/server, then create retry.request.")
                while not (run_dir / "retry.request").exists():
                    if cancel_path.exists():
                        break
                    time.sleep(config["poll_seconds"])
                (run_dir / "retry.request").unlink(missing_ok=True)
                retry_exhausted_requested = True
                service_failures = 0
            else:
                time.sleep(config["poll_seconds"])
            continue
        break

    state = read_state(run_dir / "state.json", product["product_id"])
    assembly_root = make_assembly(product_root, run_dir, product, state)
    package_dir = run_dir / "final_deliverables"
    if not (package_dir / "package_qa.json").is_file():
        write_status(status_path, "assembling", args.run_id, total_pages=len(product["pages"]))
        cmd = [sys.executable, "scripts/package_printable_product.py", "--product", str(assembly_root), "--output", str(package_dir)]
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[{now()}] Packaging stdout:\n{result.stdout}\nPackaging stderr:\n{result.stderr}\n")
        if result.returncode:
            write_status(status_path, "package_error", args.run_id, error=result.stderr[-3000:])
            return result.returncode
    qa = json.loads((package_dir / "package_qa.json").read_text(encoding="utf-8"))
    if qa.get("prepared_pages") != len(product["pages"]) or len(qa.get("pdf_render_checks", [])) != 2:
        write_status(status_path, "package_error", args.run_id, error="PDF/page package QA incomplete")
        return 3
    completion = build_completion_report(product, args.run_id, config["model"], state, package_dir, qa)
    atomic_json(package_dir / "COMPLETION_REPORT.json", completion)
    (package_dir / "COMPLETION_REPORT.md").write_text(
        f"# Product package ready for final review\n\n{product.get('title', product['product_id'])}\n\n"
        f"Vision reviewed: {completion['reviewed_pages']} pages with `{config['model']}`.\n\n"
        "Review every page and listing image before uploading. Nothing has been uploaded or published.\n\n"
        "## Delivery files\n\n" + "\n".join(f"- `{name}`" for name in completion["upload_files"]) + "\n", encoding="utf-8")
    write_status(status_path, "ready_for_final_human_review", args.run_id,
                 completion_report=str(package_dir / "COMPLETION_REPORT.md"), deliverables_directory=str(package_dir),
                 human_approval_required=True, published=False)
    print(f"PACKAGE READY for final human review: {package_dir}", flush=True)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Product package ready for final review:\n{package_dir}",
                                            "AI Product Pipeline", 0x40)
        except Exception:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", default="products/little-haunts-club-spooky-neighbors-15")
    parser.add_argument("--run-id", default="factory-main")
    parser.add_argument("--poll-seconds", type=int, default=20)
    args = parser.parse_args()
    signal.signal(signal.SIGINT, lambda *_: (resolve_product_root(args.product) / "cancel.request").write_text("signal", encoding="utf-8"))
    product_root = resolve_product_root(args.product)
    product_root.mkdir(parents=True, exist_ok=True)
    mutex_handle = None
    lock_handle = None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            mutex_name = "Local\\AIProductFactory-" + hashlib.sha256(
                str(product_root.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
            mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
            if not mutex_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            wait_result = kernel32.WaitForSingleObject(mutex_handle, 0)
            if wait_result not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED
                print("A supervisor for this product is already running.", flush=True)
                return 0
        else:
            lock_handle = (product_root / "factory.lock").open("a+b")
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("A supervisor for this product is already running.", flush=True)
        return 0
    pid_path = product_root / "factory.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: pid_path.unlink(missing_ok=True))
    try:
        return run_supervisor(args)
    except Exception as exc:
        product_root = resolve_product_root(args.product)
        with (product_root / "factory.log").open("a", encoding="utf-8") as log:
            log.write(f"\n[{now()}] FATAL: {exc}\n")
        write_status(product_root / "factory_status.json", "fatal_error", args.run_id, error=str(exc))
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if mutex_handle:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(mutex_handle)
            ctypes.windll.kernel32.CloseHandle(mutex_handle)
        if lock_handle:
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
