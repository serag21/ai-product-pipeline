#!/usr/bin/env python3
"""Local-only human approval dashboard for an existing product factory run."""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from product_review_worker import REVIEW_POLICY_VERSION
from etsy_publisher import EtsyPublisher
from local_services import DEFAULT_COMFY, ensure_comfy, ensure_ollama


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def contained(path: Path, roots: list[Path]) -> bool:
    path = path.resolve()
    return any(path == root.resolve() or root.resolve() in path.parents for root in roots)


def package_quality_passes(qa: dict[str, Any], total_pages: int) -> bool:
    pdfs = qa.get("pdf_render_checks", [])
    uploads = qa.get("etsy_upload_files", [])
    listings = qa.get("listing_images", [])
    return (qa.get("prepared_pages") == total_pages and qa.get("etsy_file_count", 0) >= 3
        and len(uploads) == qa.get("etsy_file_count") and all(x.get("under_configured_limit") and x.get("under_safe_19mb") for x in uploads)
        and len(listings) >= 4 and all(x.get("dimensions") == [2400, 2400] for x in listings)
        and len(pdfs) == 2 and all(x.get("page_count") == total_pages and x.get("ordered_pages_verified") == total_pages
            and x.get("embedded_page_images_verified") == total_pages and x.get("render_samples_pass") for x in pdfs)
        and not qa.get("duplicate_check", {}).get("exact_duplicates", ["missing duplicate QA"]))


class Dashboard:
    def __init__(self, product_root: Path, run_id: str):
        self.product_root = product_root.resolve()
        self.run_id = run_id
        self.run_dir = (self.product_root / "review_runs" / run_id).resolve()
        if self.product_root not in self.run_dir.parents:
            raise ValueError("Run directory escapes the product directory")
        self.state_path = self.run_dir / "state.json"
        self.manifest_path = self.product_root / "prompts.json"
        self.final_dir = self.run_dir / "final_deliverables"
        self.human_path = self.run_dir / "dashboard_state.json"
        self.status_path = self.run_dir / "dashboard_status.json"
        self.library_path = self.run_dir / "candidate_library.json"
        self.book_path = self.run_dir / "book_selection.json"
        self.etsy_publisher = EtsyPublisher(self.run_dir)
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.human_path.is_file():
            state = self.load_json(self.state_path, {"candidates": {}})
            baseline = {str(page): rec.get("best_candidate") for page, rec in state.get("candidates", {}).items() if rec.get("best_candidate")}
            self.save_human({"schema_version": 1, "pages": {}, "product_approval": None, "package_selection": baseline})

    def load_json(self, path: Path, default: Any = None) -> Any:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def save_human(self, data: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temp = self.human_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, self.human_path)

    def human(self) -> dict[str, Any]:
        return self.load_json(self.human_path, {"schema_version": 1, "pages": {}, "product_approval": None, "package_selection": {}})

    def set_status(self, status: str, **values: Any) -> None:
        self.status_path.write_text(json.dumps({"status": status, "updated_at": utc_now(), **values}, indent=2), encoding="utf-8")

    def asset_url(self, path: str | Path | None) -> str | None:
        if not path:
            return None
        candidate = Path(path).resolve()
        roots = [self.run_dir / "candidates", self.run_dir / "candidate_library", self.product_root / "pages", self.final_dir,
                 self.product_root / "qa", *self.run_dir.glob("final_deliverables_*")]
        if not contained(candidate, roots) or not candidate.is_file():
            return None
        token = base64.urlsafe_b64encode(str(candidate).encode()).decode().rstrip("=")
        return "/asset/" + token

    def data(self) -> dict[str, Any]:
        if self.book_path.is_file():
            return self.book_data()
        manifest = self.load_json(self.manifest_path, {})
        state = self.load_json(self.state_path, {"candidates": {}})
        human = self.human()
        current_package = Path(human.get("current_package", self.final_dir)).resolve()
        if not contained(current_package, [self.run_dir]):
            current_package = self.final_dir
        qa = self.load_json(current_package / "package_qa.json", {})
        completion = self.load_json(self.final_dir / "COMPLETION_REPORT.json", {})
        pages = []
        for spec in manifest.get("pages", []):
            number = int(spec["page"])
            record = state.get("candidates", {}).get(str(number), {})
            attempts = record.get("attempts", [])
            selected_file = human.get("pages", {}).get(str(number), {}).get("candidate_file") or record.get("best_candidate")
            selected = next((a for a in attempts if a.get("file") == selected_file), None)
            rejected = [a for a in attempts if a is not selected and a.get("review", {}).get("decision") != "accept"]
            rejected.sort(key=lambda a: (a.get("review", {}).get("score", 0), a.get("attempt", 0)), reverse=True)
            status = human.get("pages", {}).get(str(number), {})
            pages.append({
                "page": number, "label": spec.get("label", f"Page {number}"), "variant_id": spec.get("variant_id"),
                "human_status": status.get("status", "needs_review"), "human_note": status.get("note", ""),
                "selected_file": selected_file, "image": self.asset_url(selected_file),
                "ai_status": record.get("status", "missing"), "ai_review": selected.get("review", {}) if selected else {},
                "attempt_count": len(attempts),
                "alternatives": [{"attempt": a.get("attempt"), "score": a.get("review", {}).get("score"),
                    "decision": a.get("review", {}).get("decision"), "summary": a.get("review", {}).get("summary", ""),
                    "defects": a.get("review", {}).get("defects", []), "image": self.asset_url(a.get("file")),
                    "candidate_file": a.get("file"), "eligible": a.get("review", {}).get("decision") == "accept"
                        and a.get("review_policy_version", 0) >= REVIEW_POLICY_VERSION and a.get("qa", {}).get("pass", False)}
                    for a in rejected[:3]],
                "promotable": [{"attempt": a.get("attempt"), "score": a.get("review", {}).get("score"),
                    "summary": a.get("review", {}).get("summary", ""), "image": self.asset_url(a.get("file")),
                    "candidate_file": a.get("file"), "eligible": True}
                    for a in attempts if a is not selected and a.get("review", {}).get("decision") == "accept"
                    and a.get("review_policy_version", 0) >= REVIEW_POLICY_VERSION and a.get("qa", {}).get("pass", False)],
            })
        approved_pages = sum(p["human_status"] == "approved" for p in pages)
        qa_passed = package_quality_passes(qa, len(pages))
        if qa_passed:
            package_selection = human.get("package_selection", {})
            qa_passed = all(package_selection.get(str(p["page"])) == p["selected_file"] for p in pages)
        if qa_passed:
            qa_passed = all(p["selected_file"] and Path(p["selected_file"]).is_file() for p in pages)
        status = self.load_json(self.status_path, {"status": "idle"})
        return {"product": {"title": manifest.get("title", self.product_root.name), "product_id": manifest.get("product_id"),
                "run_id": self.run_id, "listing": manifest.get("listing", {})},
            "pages": pages, "approved_pages": approved_pages, "total_pages": len(pages), "qa_passed": qa_passed,
            "qa": qa, "completion": completion, "human": human, "status": status,
            "package_files": self.package_files(current_package), "published": False,
            "candidate_library": self.candidate_library(),
            "candidate_archive": [{"page": p["page"], "label": p["label"], "candidate_file": a.get("file"),
                "attempt": a.get("attempt"), "review": a.get("review", {}), "qa": a.get("qa", {}),
                "image": self.asset_url(a.get("file"))}
                for p in pages for a in state.get("candidates", {}).get(str(p["page"]), {}).get("attempts", [])
                if a.get("file") != p.get("selected_file")]}

    def book_data(self) -> dict[str, Any]:
        """Return the mutable book selection while retaining the factory decision history."""
        selection = self.load_json(self.book_path, {"pages": []})
        manifest = self.load_json(self.manifest_path, {})
        library = self.candidate_library()
        items = {item.get("file"): item for item in library.get("items", []) if item.get("file")}
        pages = []
        for record in selection.get("pages", []):
            path = record.get("file")
            candidate = items.get(path, {})
            pages.append({**record, "image": self.asset_url(path), "human_status": record.get("approval_status", "needs_review"),
                "human_approved": bool(record.get("human_approved")), "ai_review": record.get("review", candidate.get("review", {})),
                "candidate_id": record.get("candidate_id"), "candidate_category": candidate.get("category", "approved" if record.get("human_approved") else "candidate")})
        current_package = Path(selection.get("current_package", self.final_dir)).resolve()
        if not contained(current_package, [self.run_dir]):
            current_package = self.final_dir
        qa = self.load_json(current_package / "package_qa.json", {})
        qa_passed = package_quality_passes(qa, len(pages)) and selection.get("packaged_revision") == selection.get("revision")
        if qa_passed:
            qa_passed = all(p.get("file") and Path(p["file"]).is_file() for p in pages)
        bulk_checked = [int(x) for x in selection.get("bulk_checked_pages", []) if 1 <= int(x) <= len(pages)]
        return {"mode": "book", "product": {"title": manifest.get("title", self.product_root.name), "product_id": manifest.get("product_id"),
                "run_id": self.run_id, "listing": manifest.get("listing", {}), "brand": manifest.get("brand", "Little Haunts Club")},
            "pages": pages, "approved_pages": sum(p["human_approved"] for p in pages), "total_pages": len(pages),
            "revision": selection.get("revision", 0), "qa_passed": qa_passed, "qa": qa,
            "package_approved": bool(selection.get("product_approval")), "product_approval": selection.get("product_approval"),
            "status": self.load_json(self.status_path, {"status": "idle"}), "package_files": self.package_files(current_package),
            "package_directory": str(current_package), "published": False, "bulk_checked_pages": bulk_checked,
            "candidate_library": library, "candidate_archive": [], "etsy": self.etsy_status()}

    def book_selection(self) -> dict[str, Any]:
        return self.load_json(self.book_path, {"schema_version": 1, "revision": 0, "pages": [], "removed_pages": [], "history": []})

    def save_book(self, selection: dict[str, Any], event: str, *, bump_revision: bool = True, **detail: Any) -> None:
        selection["page_count"] = len(selection.get("pages", []))
        if bump_revision:
            selection["revision"] = int(selection.get("revision", 0)) + 1
        selection["updated_at"] = utc_now()
        selection.setdefault("history", []).append({"at": selection["updated_at"], "event": event, **detail})
        selection["product_approval"] = None
        self.save_json_atomic(self.book_path, selection)

    def book_candidate(self, candidate_file: str) -> dict[str, Any]:
        library = self.load_json(self.library_path, {"items": []})
        candidate = next((item for item in library.get("items", []) if item.get("file") == candidate_file), None)
        if candidate and candidate.get("category") == "approved":
            raise ValueError("Approved pages stay in the book as their own artwork; choose a replacement or new-scene option")
        if not candidate or candidate.get("review", {}).get("decision") != "accept" or not candidate.get("qa", {}).get("pass"):
            raise ValueError("Choose a candidate that passed image QA and visual review")
        source = Path(candidate_file).resolve()
        if not contained(source, [self.run_dir / "candidate_library", self.run_dir / "candidates", self.product_root / "pages"]) or not source.is_file():
            raise ValueError("Candidate image is missing or outside the local candidate library")
        return candidate

    def book_keep(self, page: int) -> dict[str, Any]:
        with self.lock:
            selection = self.book_selection()
            if not 1 <= page <= len(selection.get("pages", [])):
                raise ValueError("Unknown book page")
            record = selection["pages"][page - 1]
            record.update({"approval_status": "approved", "human_approved": True, "locked": True, "approved_at": utc_now()})
            self.save_book(selection, "human_approved_page", bump_revision=False, page=page, candidate_id=record.get("candidate_id"))
            self.update_package_metadata(selection)
            return {"ok": True, "page": page, "human_approved": True}

    def book_bulk_keep(self, pages: list[int]) -> dict[str, Any]:
        with self.lock:
            selection = self.book_selection()
            normalized = sorted(set(int(p) for p in pages))
            if not normalized or any(not 1 <= p <= len(selection.get("pages", [])) for p in normalized):
                raise ValueError("Select one or more current book pages to approve")
            for page in normalized:
                record = selection["pages"][page - 1]
                record.update({"approval_status": "approved", "human_approved": True, "locked": True, "approved_at": utc_now()})
            selection["bulk_checked_pages"] = []
            self.save_book(selection, "bulk_human_approved_pages", bump_revision=False, pages=normalized)
            self.update_package_metadata(selection)
            return {"ok": True, "pages": normalized}

    def book_replace(self, page: int, candidate_file: str) -> dict[str, Any]:
        with self.lock:
            selection = self.book_selection()
            if not 1 <= page <= len(selection.get("pages", [])):
                raise ValueError("Unknown book page")
            candidate = self.book_candidate(candidate_file)
            if any(int(item.get("page", 0)) != page and item.get("file") == candidate_file for item in selection.get("pages", [])):
                raise ValueError("That image is already used on another selected page")
            record = selection["pages"][page - 1]
            previous = {k: record.get(k) for k in ("file", "candidate_id", "approval_status", "human_approved", "locked", "note")}
            record.setdefault("selection_history", []).append({"at": utc_now(), "event": "replaced", "previous": previous})
            record.update({"file": candidate_file, "candidate_id": candidate.get("id"), "source": "candidate_library",
                "review": candidate.get("review", {}), "qa": candidate.get("qa", {}), "approval_status": "needs_review",
                "human_approved": False, "locked": False, "replaces_candidate_id": previous.get("candidate_id")})
            self.save_book(selection, "candidate_selected_for_review", page=page, candidate_id=candidate.get("id"))
            result = self.rebuild_book_package()
            return {"ok": True, "page": page, "status": "needs_review", **result}

    def book_add(self, candidate_file: str) -> dict[str, Any]:
        with self.lock:
            selection = self.book_selection()
            candidate = self.book_candidate(candidate_file)
            if any(item.get("file") == candidate_file for item in selection.get("pages", [])):
                raise ValueError("That image is already selected in the book")
            page = max((int(x.get("page", 0)) for x in selection.get("pages", [])), default=0) + 1
            selection.setdefault("pages", []).append({"page": page, "label": candidate.get("title", f"Page {page:02d}"),
                "variant_id": candidate.get("id"), "file": candidate_file, "candidate_id": candidate.get("id"),
                "source": "candidate_library", "approval_status": "needs_review", "human_approved": False,
                "locked": False, "note": "", "review": candidate.get("review", {}), "qa": candidate.get("qa", {}), "original_slot": None})
            self.save_book(selection, "candidate_added_to_book", page=page, candidate_id=candidate.get("id"))
            result = self.rebuild_book_package()
            return {"ok": True, "page": page, "status": "needs_review", **result}

    def book_remove(self, page: int) -> dict[str, Any]:
        with self.lock:
            selection = self.book_selection()
            if not 1 <= page <= len(selection.get("pages", [])):
                raise ValueError("Unknown book page")
            removed = selection["pages"].pop(page - 1)
            removed["removed_at"] = utc_now()
            selection.setdefault("removed_pages", []).append(removed)
            selection["bulk_checked_pages"] = []
            for number, record in enumerate(selection["pages"], 1):
                record["page"] = number
            self.save_book(selection, "page_removed_from_book", page=page, candidate_id=removed.get("candidate_id"))
            result = self.rebuild_book_package()
            return {"ok": True, "removed_page": page, "page_count": len(selection["pages"]), **result}

    def book_note(self, page: int, note: str) -> dict[str, Any]:
        with self.lock:
            selection = self.book_selection()
            if not 1 <= page <= len(selection.get("pages", [])):
                raise ValueError("Unknown book page")
            record = selection["pages"][page - 1]
            record["note"] = note[:2000]
            record["note_updated_at"] = utc_now()
            self.save_json_atomic(self.book_path, selection)
            return {"ok": True, "page": page, "saved_at": record["note_updated_at"]}

    def book_bulk_selection(self, pages: list[int]) -> dict[str, Any]:
        selection = self.book_selection()
        checked = sorted(set(int(p) for p in pages if 1 <= int(p) <= len(selection.get("pages", []))))
        selection["bulk_checked_pages"] = checked
        self.save_json_atomic(self.book_path, selection)
        return {"ok": True, "pages": checked}

    def rebuild_book_package(self) -> dict[str, Any]:
        """Package the selected artwork without treating pending art as approved."""
        selection = self.book_selection()
        pages = sorted(selection.get("pages", []), key=lambda p: int(p["page"]))
        if not pages:
            raise ValueError("Add at least one page before building the product")
        if [int(p["page"]) for p in pages] != list(range(1, len(pages) + 1)):
            raise ValueError("Book page order must be contiguous, starting at page 1")
        self.set_status("packaging", message=f"Building a {len(pages)}-page review package and running file and PDF checks.")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        assembly = self.run_dir / f"book_assembly_rev_{selection.get('revision', 0)}_{stamp}"
        output = self.run_dir / f"final_deliverables_book_rev_{selection.get('revision', 0)}_{stamp}"
        (assembly / "pages").mkdir(parents=True, exist_ok=False)
        manifest = self.load_json(self.manifest_path, {})
        base_specs = {int(spec["page"]): spec for spec in manifest.get("pages", [])}
        candidate_specs = {item.get("file"): item for item in self.load_json(self.library_path, {"items": []}).get("items", [])}
        book_specs = []
        for record in pages:
            number = int(record["page"])
            source_number = int(record.get("original_slot") or number)
            spec = dict(base_specs.get(source_number, base_specs.get(number, {})))
            if not spec:
                candidate = candidate_specs.get(record.get("file"), {})
                title = str(candidate.get("title", record.get("label", f"Page {number}")))
                scene = re.sub(r"^(?:New\s+\d+\s*[·:-]\s*|Page\s+\d+\s*[·:-]\s*)", "", title)
                scene = re.sub(r"\s*[·:-]\s*(?:Layout|Option)\s+\d+(?:\s*[·:-].*)?$", "", scene)
                layout = str(candidate.get("layout", ""))
                spec = {"family": "candidate-library-scene", "prompt_format": "text", "scene_title": scene,
                    "character_archetype": scene.split(" ")[0] if scene else "Halloween neighbor",
                    "design_rationale": "An already generated and visually reviewed candidate selected by the book owner.",
                    "qa_focus": "Keep clean coherent anatomy, clear physical support, crisp black outlines and generous open areas to color.",
                    "prompt": f"Printable black-and-white Halloween coloring page, clean portrait line art with generous white margins. Show one memorable spooky neighbor actively doing this scene: {scene}. Specific composition and physical layout: {layout}. Cozy, cute-spooky storybook mood, clear readable activity, crisp consistent black outlines, large open colorable areas, no gray shading or broad black fills."}
            spec.update({"page": number, "label": record.get("label", f"Page {number:02d}"),
                "variant_id": record.get("variant_id", spec.get("variant_id"))})
            book_specs.append(spec)
        manifest["pages"] = book_specs
        if len(pages) != 17:
            manifest["product_id"] = f"little-haunts-club-spooky-neighbors-{len(pages)}"
            manifest["file_prefix"] = f"Little-Haunts-Club-{len(pages)}"
            manifest["title"] = f"Little Haunts Club: A Very Odd Halloween — {len(pages)} Printable Coloring Pages"
            listing = manifest.setdefault("listing", {})
            listing["title"] = f"Cute Spooky Halloween Coloring Pages — {len(pages)} Original Printable PDF + PNG"
            listing["description"] = str(listing.get("description", "")).replace("17", str(len(pages)))
        listing = manifest.setdefault("listing", {})
        hero = [n for n in listing.get("hero_page_numbers", []) if int(n) <= len(pages)]
        gallery = [n for n in listing.get("gallery_page_numbers", []) if int(n) <= len(pages)]
        listing["hero_page_numbers"] = list(dict.fromkeys([*hero, max(1, len(pages) - 1), len(pages)]))[-6:]
        listing["gallery_page_numbers"] = list(dict.fromkeys([*gallery, len(pages)]))[-9:]
        (assembly / "prompts.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        generation = []
        for record in pages:
            source = Path(record["file"]).resolve()
            if not contained(source, [self.run_dir / "candidates", self.run_dir / "candidate_library", self.product_root / "pages"]) or not source.is_file():
                raise ValueError(f"Selected page {record['page']} is missing or outside the product run")
            destination = assembly / "pages" / f"page_{int(record['page']):03d}.png"
            shutil.copy2(source, destination)
            generation.append({"page": int(record["page"]), "label": record.get("label", f"Page {record['page']}"),
                "variant_id": record.get("variant_id"), "file": str(destination), "qa": record.get("qa", {"pass": True})})
        (assembly / "qa").mkdir()
        (assembly / "qa" / "generation_manifest.json").write_text(json.dumps({"summary": {"requested": len(generation), "passed": len(generation), "failed": 0}, "pages": generation}, indent=2), encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "package_printable_product.py"), "--product", str(assembly), "--output", str(output)],
            cwd=ROOT, capture_output=True, text=True, timeout=900)
        if result.returncode:
            self.set_status("package_error", error=(result.stderr or result.stdout)[-4000:])
            raise RuntimeError((result.stderr or result.stdout)[-2000:])
        qa = json.loads((output / "package_qa.json").read_text(encoding="utf-8"))
        if not package_quality_passes(qa, len(pages)):
            self.set_status("package_error", error="Package QA did not pass")
            raise RuntimeError("Package QA did not pass; previous package remains intact")
        selection = self.book_selection()
        selection["current_package"] = str(output)
        selection["packaged_revision"] = selection.get("revision", 0)
        selection["package_qa"] = str(output / "package_qa.json")
        self.save_json_atomic(self.book_path, selection)
        self.update_package_metadata(selection)
        self.set_status("package_ready", package_directory=str(output), qa_passed=True, page_count=len(pages), published=False)
        return {"package_directory": str(output), "qa_passed": True, "page_count": len(pages)}

    def book_approve_product(self) -> dict[str, Any]:
        with self.lock:
            data = self.book_data()
            if data["approved_pages"] != data["total_pages"]:
                raise ValueError(f"Approve each selected page before approving the product ({data['approved_pages']}/{data['total_pages']})")
            if not data["qa_passed"]:
                raise ValueError("The current selection does not have passing package QA")
            selection = self.book_selection()
            selection["product_approval"] = {"approved_at": utc_now(), "revision": selection.get("revision"),
                "package_qa": selection.get("package_qa"), "published": False}
            self.save_json_atomic(self.book_path, selection)
            self.update_package_metadata(selection)
            return {"ok": True, "approved_at": selection["product_approval"]["approved_at"], "published": False}

    def update_package_metadata(self, selection: dict[str, Any] | None = None) -> None:
        selection = selection or self.book_selection()
        package = Path(selection.get("current_package", "")).resolve()
        if not package.is_dir() or not contained(package, [self.run_dir]):
            return
        path = package / "etsy_listing_metadata.json"
        metadata = self.load_json(path, {})
        pages = selection.get("pages", [])
        approved = sum(bool(p.get("human_approved")) for p in pages)
        metadata["page_approvals"] = [{"page": int(p["page"]), "candidate_id": p.get("candidate_id"),
            "human_approved": bool(p.get("human_approved")), "status": p.get("approval_status", "needs_review")} for p in pages]
        metadata["human_approved_pages"] = approved
        metadata["all_pages_human_approved"] = approved == len(pages)
        metadata["product_approval"] = selection.get("product_approval")
        metadata["human_approved"] = bool(selection.get("product_approval"))
        metadata["status"] = "HUMAN_APPROVED" if metadata["human_approved"] else "REVIEW_REQUIRED"
        self.save_json_atomic(path, metadata)

    def etsy_status(self) -> dict[str, Any]:
        try:
            return self.etsy_publisher.status()
        except Exception as exc:
            return {"configured": False, "authorized": False, "last_error": str(exc)}

    def etsy_connect(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.etsy_publisher.begin_auth(str(body.get("api_key", "")), str(body.get("shared_secret", "")), str(body.get("redirect_uri", "")))

    def etsy_finish_auth(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.etsy_publisher.finish_auth(str(body.get("code", "")), str(body.get("state", "")))

    def etsy_shops(self) -> list[dict[str, Any]]:
        return self.etsy_publisher.shops()

    def etsy_taxonomies(self) -> list[dict[str, Any]]:
        return self.etsy_publisher.taxonomies()

    def etsy_create_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self.book_data()
        if data["approved_pages"] != data["total_pages"] or not data["qa_passed"] or not data["package_approved"]:
            raise ValueError("Approve every page and the current package before uploading any Etsy draft files")
        if str(body.get("confirm", "")) != "CREATE DRAFT AND UPLOAD":
            raise ValueError("Type CREATE DRAFT AND UPLOAD to confirm transferring the draft files to Etsy")
        package_dir = Path(data["package_directory"])
        files = sorted([*package_dir.joinpath("etsy_uploads").glob("*.pdf"), *package_dir.joinpath("etsy_uploads").glob("*.zip")])
        images = sorted(package_dir.joinpath("listing_images").glob("*.jpg"))
        draft = self.etsy_publisher.create_draft_and_upload(shop_id=str(body.get("shop_id", "")),
            taxonomy_id=str(body.get("taxonomy_id", "")), price=str(body.get("price", "")),
            title=str(data["product"]["listing"].get("title", data["product"]["title"])),
            description=str(data["product"]["listing"].get("description", "")),
            tags=list(data["product"]["listing"].get("tags", [])), upload_files=files, images=images)
        return {"ok": True, "draft": draft, "status": self.etsy_status()}

    def etsy_publish(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self.book_data()
        if data["approved_pages"] != data["total_pages"] or not data["qa_passed"] or not data["package_approved"]:
            raise ValueError("Approve every page and the current product package before publishing")
        if str(body.get("confirm", "")) != "PUBLISH LISTING":
            raise ValueError("Type PUBLISH LISTING to confirm publication on Etsy")
        draft = self.etsy_publisher.status().get("draft") or {}
        if not draft:
            raise ValueError("Create and upload the Etsy draft before publishing")
        result = self.etsy_publisher.publish(str(draft["shop_id"]), str(draft["listing_id"]))
        return {"ok": True, "listing": result, "status": self.etsy_status()}

    def book_regenerate(self, page: int) -> dict[str, Any]:
        if self.proc and self.proc.poll() is None:
            raise ValueError("A regeneration is already running")
        selection = self.book_selection()
        if not 1 <= page <= len(selection.get("pages", [])):
            raise ValueError("Unknown book page")
        record = selection["pages"][page - 1]
        record["approval_status"] = "rejected"
        record["human_approved"] = False
        record["locked"] = False
        record["regeneration_requested_at"] = utc_now()
        self.save_json_atomic(self.book_path, selection)
        state = self.load_json(self.state_path, {"candidates": {}})
        history = state.setdefault("candidates", {}).setdefault(str(page), {"page": page, "variant_id": record.get("variant_id"),
            "status": "pending", "attempts": [], "best_candidate": None})
        if not any(attempt.get("file") == record.get("file") for attempt in history.get("attempts", [])):
            library = self.load_json(self.library_path, {"items": []})
            candidate = next((item for item in library.get("items", []) if item.get("file") == record.get("file")), {})
            attempt_no = max((int(x.get("attempt", 0)) for x in history.get("attempts", [])), default=0) + 1
            history.setdefault("attempts", []).append({"attempt": attempt_no, "file": record.get("file"), "qa": record.get("qa", candidate.get("qa", {"pass": True})),
                "review": record.get("review", candidate.get("review", {})), "review_policy_version": REVIEW_POLICY_VERSION,
                "source": "book_selection", "created_at": utc_now()})
            history["best_candidate"] = record.get("file")
        history["status"] = "pending"
        self.save_json_atomic(self.state_path, state)
        return self.regenerate()

    def candidate_library(self) -> dict[str, Any]:
        library = self.load_json(self.library_path, {"status": "not_started", "items": []})
        items = [dict(item) for item in library.get("items", []) if not str(item.get("id", "")).startswith("approved-p")]
        state = self.load_json(self.state_path, {"candidates": {}})
        for page, entry in self.human().get("pages", {}).items():
            if entry.get("status") != "approved":
                continue
            path = entry.get("candidate_file")
            candidate = next((x for x in state.get("candidates", {}).get(str(page), {}).get("attempts", []) if x.get("file") == path), {})
            items.append({"id": f"approved-p{page}", "category": "approved", "title": f"Approved page {int(page):02d}",
                "file": path, "status": "human approved · locked", "rank": 1000, "surface": True,
                "qa": candidate.get("qa", {}), "review": candidate.get("review", {})})
        items = [{**item, "image": self.asset_url(item.get("file"))} for item in items]
        return {**library, "items": items}

    def package_files(self, package_dir: Path | None = None) -> list[dict[str, Any]]:
        package_dir = package_dir or self.final_dir
        paths = [*sorted((package_dir / "etsy_uploads").glob("*.pdf")),
                 *sorted((package_dir / "etsy_uploads").glob("*.zip")),
                 *sorted((package_dir / "listing_images").glob("*.jpg")),
                 package_dir / "preview_original_pages.jpg", package_dir / "preview_print_ready_pages.jpg",
                 package_dir / "READ_ME_FIRST.txt", package_dir / "etsy_listing_draft.md", package_dir / "etsy_listing_metadata.json", package_dir / "package_qa.json"]
        return [{"name": p.name, "url": self.asset_url(p)} for p in paths if p.is_file()]

    def save_note(self, page: int, note: str) -> dict[str, Any]:
        with self.lock:
            human = self.human()
            entry = human.setdefault("pages", {}).setdefault(str(page), {"status": "needs_review"})
            entry["note"] = note[:2000]
            entry["note_updated_at"] = utc_now()
            self.save_human(human)
            return {"ok": True, "page": page, "saved_at": entry["note_updated_at"],
                    "status": entry.get("status", "needs_review")}

    def replace_from_library(self, page: int, candidate_file: str) -> dict[str, Any]:
        with self.lock:
            library = self.load_json(self.library_path, {"items": []})
            candidate = next((x for x in library.get("items", []) if x.get("file") == candidate_file), None)
            if not candidate or candidate.get("review", {}).get("decision") != "accept" or not candidate.get("qa", {}).get("pass"):
                raise ValueError("Choose a library candidate that passed image QA and visual review")
            source = Path(candidate_file).resolve()
            if not contained(source, [self.run_dir / "candidate_library"]) or not source.is_file():
                raise ValueError("Candidate image is missing or outside the candidate library")
            state = self.load_json(self.state_path, {"candidates": {}})
            record = state.setdefault("candidates", {}).setdefault(str(page), {"page": page, "attempts": []})
            if not any(a.get("file") == candidate_file for a in record.get("attempts", [])):
                attempt = max((int(a.get("attempt", 0)) for a in record.get("attempts", [])), default=0) + 1
                record.setdefault("attempts", []).append({"attempt": attempt, "file": candidate_file, "seed": candidate.get("seed"),
                    "qa": candidate["qa"], "review": candidate["review"], "review_policy_version": REVIEW_POLICY_VERSION,
                    "source": "candidate_library", "created_at": candidate.get("updated_at", utc_now())})
            record["best_candidate"] = candidate_file
            record["status"] = "accepted_by_vision"
            self.save_json_atomic(self.state_path, state)
            human = self.human(); entry = human.setdefault("pages", {}).setdefault(str(page), {})
            entry.update({"status": "needs_review", "candidate_file": candidate_file, "candidate_source": "candidate_library", "updated_at": utc_now()})
            human["product_approval"] = None; self.save_human(human)
            return {"ok": True, "page": page, "status": "needs_review", "candidate_file": candidate_file}

    def save_json_atomic(self, path: Path, data: Any) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def decide(self, page: int, action: str, note: str = "", candidate_file: str | None = None) -> dict[str, Any]:
        with self.lock:
            data = self.data()
            page_data = next((p for p in data["pages"] if p["page"] == page), None)
            if not page_data:
                raise ValueError("Unknown page")
            if action not in {"keep", "reject", "needs_changes", "replace"}:
                raise ValueError("Unsupported decision")
            if action == "replace":
                chosen = next((c for c in page_data["promotable"] if c["candidate_file"] == candidate_file), None)
                if not chosen:
                    raise ValueError("Only a current-policy, AI-accepted candidate can be promoted")
                status = "needs_review"
            else:
                status = {"keep": "approved", "reject": "rejected", "needs_changes": "needs_changes"}[action]
            human = self.human()
            human.setdefault("pages", {})[str(page)] = {"status": status, "candidate_file": candidate_file or page_data["selected_file"],
                "note": note[:2000], "updated_at": utc_now()}
            human["product_approval"] = None
            self.save_human(human)
            return {"ok": True, "page": page, "human_status": status, "product_approval": None}

    def approve(self) -> dict[str, Any]:
        if self.book_path.is_file():
            return self.book_approve_product()
        with self.lock:
            data = self.data()
            if data["approved_pages"] != data["total_pages"]:
                raise ValueError(f"Approve all selected pages individually first ({data['approved_pages']}/{data['total_pages']} approved)")
            if not data["qa_passed"]:
                raise ValueError("Current package QA has not passed; rebuild package and QA first")
            human = self.human()
            package_dir = Path(human.get("current_package", self.final_dir)).resolve()
            if not contained(package_dir, [self.run_dir]):
                package_dir = self.final_dir
            human["product_approval"] = {"approved_at": utc_now(), "package_qa": str(package_dir / "package_qa.json"), "published": False}
            self.save_human(human)
            return {"ok": True, "approved_at": human["product_approval"]["approved_at"], "published": False}

    def rebuild_package(self) -> dict[str, Any]:
        if self.book_path.is_file():
            return self.rebuild_book_package()
        with self.lock:
            data = self.data()
            if any(p["human_status"] in {"rejected", "needs_changes"} for p in data["pages"]):
                raise ValueError("Resolve rejected or needs-changes pages before packaging")
            if any(p["human_status"] != "approved" for p in data["pages"]):
                raise ValueError("All pages must be explicitly approved before rebuilding the final package")
            self.set_status("packaging", message="Rebuilding PDFs, ZIPs, listing assets, metadata, and QA.")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            assembly = self.run_dir / f"dashboard_assembly_{stamp}"
            output = self.run_dir / f"final_deliverables_human_approved_{stamp}"
            (assembly / "pages").mkdir(parents=True, exist_ok=False)
            shutil.copy2(self.manifest_path, assembly / "prompts.json")
            generation = []
            for page in data["pages"]:
                source = Path(page["selected_file"]).resolve()
                if not contained(source, [self.run_dir / "candidates", self.run_dir / "candidate_library", self.product_root / "pages"]) or not source.is_file():
                    raise ValueError("Selected page image is missing or outside the product run")
                destination = assembly / "pages" / f"page_{page['page']:03d}.png"
                shutil.copy2(source, destination)
                generation.append({"page": page["page"], "label": page["label"], "variant_id": page["variant_id"], "file": str(destination), "qa": {"pass": True}})
            (assembly / "qa").mkdir()
            (assembly / "qa" / "generation_manifest.json").write_text(json.dumps({"summary": {"requested": len(generation), "passed": len(generation), "failed": 0}, "pages": generation}, indent=2), encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "package_printable_product.py"), "--product", str(assembly), "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, timeout=600)
            if result.returncode:
                self.set_status("package_error", error=(result.stderr or result.stdout)[-4000:])
                raise RuntimeError((result.stderr or result.stdout)[-2000:])
            qa = json.loads((output / "package_qa.json").read_text(encoding="utf-8"))
            if not package_quality_passes(qa, data["total_pages"]):
                self.set_status("package_error", error="Package QA did not pass")
                raise RuntimeError("Package QA did not pass")
            # A rebuilt package is a fresh review artifact. Keep the factory's original outputs intact.
            self.set_status("package_ready", package_directory=str(output), qa_passed=True, published=False)
            human = self.human()
            human["product_approval"] = None
            human["current_package"] = str(output)
            human["package_selection"] = {str(p["page"]): p["selected_file"] for p in data["pages"]}
            self.save_human(human)
            return {"ok": True, "package_directory": str(output), "qa_passed": True, "published": False}

    def regenerate(self) -> dict[str, Any]:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                raise ValueError("A regeneration is already running")
            data = self.data()
            pages = [p["page"] for p in data["pages"] if p["human_status"] == "rejected"]
            if not pages:
                raise ValueError("Mark one or more pages Reject / Regenerate first")
            state = self.load_json(self.state_path, {})
            config = self.load_json(self.product_root / "factory_config.json", {})
            vision = config.get("vision", {})
            endpoint, model = vision.get("endpoint"), vision.get("model")
            if not endpoint or not model:
                raise ValueError("Configure the local vision endpoint and model in factory_config.json first")
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                ensure_comfy(config.get("comfy", {}).get("server", "http://127.0.0.1:8188"), config.get("comfy", {}).get("install", str(DEFAULT_COMFY)))
                ensure_ollama(endpoint, model)
            except Exception as exc:
                raise ValueError(f"Generation preflight failed; saved decisions are unchanged: {exc}") from exc
            max_cycle = int(config.get("generation", {}).get("dashboard_cycle_attempts", 3))
            cumulative_ceiling = int(config.get("generation", {}).get("cumulative_safety_ceiling", 60))
            limits = {}
            for page in pages:
                rec = state.get("candidates", {}).get(str(page))
                if not rec:
                    raise ValueError(f"No candidate history for page {page}")
                if len(rec.get("attempts", [])) >= cumulative_ceiling:
                    raise ValueError(f"Page {page} reached cumulative safety ceiling ({cumulative_ceiling})")
                limits[page] = min(cumulative_ceiling, len(rec.get("attempts", [])) + max_cycle)
            for page in pages:
                rec = state["candidates"][str(page)]
                rec["status"] = "pending"
            tmp = self.state_path.with_suffix(".dashboard.tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
            command = [sys.executable, str(ROOT / "scripts" / "product_review_worker.py"), "--product", str(self.product_root), "--run-id", self.run_id,
                "--pages", *map(str, pages), "--max-attempts", str(min(14, int(config.get("generation", {}).get("max_attempts", 14)))),
                "--cycle-attempts", str(max_cycle), "--cumulative-safety-ceiling", str(cumulative_ceiling),
                "--accept-score", str(config.get("generation", {}).get("accept_score", 80)), "--vision-endpoint", endpoint,
                "--vision-model", model, "--vision-max-tokens", str(vision.get("max_tokens", 2048))]
            if vision.get("unload_endpoint"):
                command += ["--vision-unload-endpoint", vision["unload_endpoint"]]
            if vision.get("reasoning_effort"):
                command += ["--vision-reasoning-effort", vision["reasoning_effort"]]
            child_env = os.environ.copy()
            key_env = vision.get("api_key_env", "VISION_API_KEY")
            if os.getenv(key_env):
                child_env["VISION_API_KEY"] = os.getenv(key_env, "")
            log = (self.run_dir / "dashboard_regeneration.log").open("a", encoding="utf-8")
            log.write(f"\n[{utc_now()}] Regenerating pages {pages}.\n")
            log.flush()
            self.proc = subprocess.Popen(command, cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT)
            log.close()
            human = self.human()
            if not self.book_path.is_file():
                for page in pages:
                    human["pages"][str(page)]["status"] = "regenerating"
                human["product_approval"] = None
                self.save_human(human)
            self.set_status("regenerating", pages=pages, message="ComfyUI generation and Ollama review run sequentially in the existing worker.")
            threading.Thread(target=self._watch_regeneration, args=(pages,), daemon=True).start()
            return {"ok": True, "pages": pages, "pid": self.proc.pid, "published": False}

    def _watch_regeneration(self, pages: list[int]) -> None:
        proc = self.proc
        if not proc:
            return
        while proc.poll() is None:
            with self.lock:
                state = self.load_json(self.state_path, {"candidates": {}})
                done = sum(state.get("candidates", {}).get(str(page), {}).get("status") == "accepted_by_vision" for page in pages)
                self.set_status("regenerating", pages=pages, completed_pages=done, total_pages=len(pages),
                                attempts={str(page): len(state.get("candidates", {}).get(str(page), {}).get("attempts", [])) for page in pages})
            time.sleep(2)
        code = proc.returncode
        with self.lock:
            if self.book_path.is_file():
                selection = self.book_selection()
                state = self.load_json(self.state_path, {"candidates": {}})
                library = self.load_json(self.library_path, {"items": []})
                for page in pages:
                    rec = state.get("candidates", {}).get(str(page), {})
                    target = next((p for p in selection.get("pages", []) if int(p.get("page", 0)) == page), None)
                    if not target:
                        continue
                    if rec.get("status") == "accepted_by_vision":
                        path = rec.get("best_candidate")
                        chosen = next((a for a in rec.get("attempts", []) if a.get("file") == path), {})
                        target.setdefault("selection_history", []).append({"at": utc_now(), "event": "regenerated_candidate", "previous_file": target.get("file")})
                        target.update({"file": path, "candidate_id": f"dashboard-p{page}-attempt-{chosen.get('attempt', 0)}",
                            "source": "dashboard_regeneration", "review": chosen.get("review", {}), "qa": chosen.get("qa", {}),
                            "approval_status": "needs_review", "human_approved": False, "locked": False})
                        item = {"id": target["candidate_id"], "category": "replacement" if target.get("original_slot") else "new_scene",
                            "title": f"Page {page:02d} · {target.get('label', 'Regenerated scene')}", "file": path, "status": "newly generated · human review required",
                            "rank": 120, "surface": True, "qa": chosen.get("qa", {}), "review": chosen.get("review", {}), "source": "dashboard_regeneration"}
                        library.setdefault("items", []).append(item)
                        target.pop("regeneration_requested_at", None)
                    else:
                        target["approval_status"] = "needs_review"
                        target["human_approved"] = False
                selection["product_approval"] = None
                self.save_book(selection, "dashboard_regeneration_finished", pages=pages, return_code=code)
                self.save_json_atomic(self.library_path, library)
                log_path = self.run_dir / "dashboard_regeneration.log"
                error = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if code else None
                if code == 0 and all(next((p for p in selection["pages"] if int(p["page"]) == n), {}).get("approval_status") == "needs_review" for n in pages):
                    try:
                        package = self.rebuild_book_package()
                        self.set_status("regeneration_finished", return_code=code, pages=pages, package=package)
                    except Exception as exc:
                        self.set_status("regeneration_package_error", return_code=code, pages=pages, error=str(exc), log=error)
                else:
                    self.set_status("regeneration_error" if code else "regeneration_finished", return_code=code, pages=pages, error=error)
                return
            human = self.human()
            for page in pages:
                rec = self.load_json(self.state_path, {}).get("candidates", {}).get(str(page), {})
                entry = human.get("pages", {}).setdefault(str(page), {})
                if rec.get("status") == "accepted_by_vision":
                    entry["status"] = "needs_review"
                    entry["candidate_file"] = rec.get("best_candidate")
                    if not entry.get("note"):
                        entry["note"] = "New candidate generated; human review required."
                else:
                    entry["status"] = "rejected"
            human["product_approval"] = None
            self.save_human(human)
            log_path = self.run_dir / "dashboard_regeneration.log"
            error = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if code else None
            self.set_status("regeneration_finished" if code == 0 else "regeneration_error", return_code=code,
                            pages=pages, error=error)


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Little Haunts — Human Approval</title><style>
:root{font:16px/1.45 Segoe UI,Arial,sans-serif;color:#26352f;background:#f5f2eb}*{box-sizing:border-box}body{margin:0}header{background:#233a32;color:white;padding:18px 28px;position:sticky;top:0;z-index:3}header h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:#dce8de}.wrap{max-width:1400px;margin:auto;padding:20px}nav{display:flex;gap:8px;margin-bottom:16px}.tab,button{border:0;border-radius:7px;padding:9px 13px;cursor:pointer;font:inherit}.tab{background:#e4e1d8}.tab.active{background:#385e4d;color:#fff}button{background:#395e4e;color:white}button.secondary{background:#e4e1d8;color:#26352f}button.danger{background:#a33e37}button:disabled{opacity:.45;cursor:not-allowed}.summary{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 18px}.pill{background:white;border:1px solid #ddd9ce;border-radius:999px;padding:6px 12px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px}.card,.panel{background:white;border:1px solid #e0ddd4;border-radius:12px;overflow:hidden}.card img{width:100%;height:420px;object-fit:contain;background:#fff;cursor:zoom-in}.card h2{font-size:17px;margin:8px 0}.content{padding:14px}.meta{font-size:13px;color:#655}.checks{font-size:14px;background:#f7f6f2;padding:10px;border-radius:7px;margin:10px 0}.actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.actions button{font-size:14px}.status{font-weight:600}.approved{color:#267246}.rejected,.needs_changes{color:#a33e37}.needs_review,.regenerating{color:#a36400}.section{display:none}.section.active{display:block}.alternatives{display:flex;gap:12px;overflow:auto}.alt{min-width:190px;max-width:240px;border:1px solid #ddd;padding:8px;border-radius:8px}.alt img{width:100%;height:190px;object-fit:contain;cursor:zoom-in}dialog{border:0;border-radius:12px;max-width:95vw;max-height:95vh}dialog img{max-width:88vw;max-height:84vh}textarea{width:100%;min-height:55px;border:1px solid #ccc;border-radius:6px;padding:7px;font:inherit}.filelinks a{margin-right:12px}.banner{padding:10px 12px;background:#eff4ee;border-radius:8px;margin:12px 0}.error{color:#9b3028}.foot{font-size:13px;color:#635f57;margin:20px 0}.panel{padding:18px;margin-bottom:14px}@media(max-width:600px){header{padding:14px}.wrap{padding:12px}.card img{height:340px}}
</style></head><body><header><h1>Little Haunts Club · Human approval</h1><p>Local review dashboard · no upload or publishing controls</p></header><main class="wrap"><div class="summary" id="summary"></div><div class="banner" id="status"></div><nav><button class="tab active" data-tab="pages">Final pages</button><button class="tab" data-tab="alternatives">Rejected alternatives</button><button class="tab" data-tab="package">Package & approval</button></nav><section id="pages" class="section active"><div class="actions" style="margin:0 0 14px"><button id="regenerate" class="danger">Run rejected page regenerations</button></div><div class="grid" id="grid"></div></section><section id="alternatives" class="section"><div id="alts"></div></section><section id="package" class="section"><div class="panel"><h2>Current package preview</h2><p id="qa"></p><div class="filelinks" id="files"></div><div class="actions"><button id="rebuild">Rebuild package & run QA</button><button id="approveAll" disabled>Approve whole product</button></div><p class="foot">Whole-product approval is a local review record only. It does not authorize or perform publishing.</p></div></section><p class="foot">Decisions persist in the factory run. AI verdicts and your human decisions are shown separately.</p></main><dialog id="zoom"><img></dialog><script>
let state;async function api(url,body){let r=await fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}function esc(s=''){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function showErr(e){document.getElementById('status').innerHTML='<span class="error">'+esc(e.message)+'</span>'}function reviewMarkup(p){let r=p.ai_review||{};let checks=r.checks||{};return `<div class="checks"><b>AI verdict: ${esc(r.decision||'none')} · score ${esc(r.score??'—')}</b><br>${esc(r.summary||'No assessment recorded')}<br><b>AI checks:</b> ${Object.entries(checks).map(([k,v])=>`${esc(k.replaceAll('_',' '))}: ${v?'pass':'fail'}`).join(' · ')||'not available'}${(r.defects||[]).length?'<br><b>Reasons:</b> '+esc(r.defects.join('; ')):''}</div>`}function card(p){return `<article class="card"><img src="${p.image||''}" data-zoom="${p.image||''}" alt="Page ${p.page}: ${esc(p.label)}"><div class="content"><h2>${String(p.page).padStart(2,'0')} · ${esc(String(p.label).replace(/^\d{1,2}\s*[·.:-]\s*/,''))}</h2><div class="meta">AI: ${esc(p.ai_status)} · Human: <span class="status ${esc(p.human_status)}">${esc(p.human_status.replaceAll('_',' '))}</span> · ${p.attempt_count} candidates</div>${reviewMarkup(p)}<textarea id="note${p.page}" placeholder="Optional note / change request">${esc(p.human_note)}</textarea><div class="actions"><button data-action="keep" data-page="${p.page}">Keep</button><button class="danger" data-action="reject" data-page="${p.page}">Reject / Regenerate</button><button class="secondary" data-action="needs_changes" data-page="${p.page}">Needs changes</button></div>${p.promotable.length?`<div class="actions"><select id="promote${p.page}">${p.promotable.map(c=>`<option value="${esc(c.candidate_file)}">Attempt ${c.attempt} · score ${c.score}</option>`).join('')}</select><button class="secondary" data-action="replace" data-page="${p.page}">Replace with candidate</button></div>`:''}</div></article>`}function render(){document.getElementById('summary').innerHTML=`<span class="pill">${esc(state.product.title)}</span><span class="pill">${state.approved_pages}/${state.total_pages} pages approved</span><span class="pill">Package QA: ${state.qa_passed?'passed':'not current'}</span><span class="pill">Published: no</span>`;let st=state.status||{};document.getElementById('status').textContent=`Factory status: ${st.status||'idle'}${st.message?' · '+st.message:''}${st.pages?' · pages '+st.pages.join(', '):''}${st.error?' · '+st.error:''}`;document.getElementById('grid').innerHTML=state.pages.map(card).join('');let groups=state.pages.map(p=>p.alternatives.length?`<div class="panel"><h2>Page ${p.page} · ${esc(p.label)}</h2><div class="alternatives">${p.alternatives.map(a=>`<div class="alt"><img src="${a.image||''}" data-zoom="${a.image||''}" alt="Candidate ${a.attempt}"><b>Attempt ${a.attempt} · score ${a.score??'—'} · ${esc(a.decision||'unreviewed')}</b><p>${esc(a.summary)}</p><p class="meta">${esc((a.defects||[]).join('; '))}</p></div>`).join('')}</div></div>`:'').join('');document.getElementById('alts').innerHTML=groups||'<div class="panel">No rejected alternatives recorded.</div>';document.getElementById('qa').textContent=state.qa_passed?'Latest package QA passed for '+state.total_pages+' pages.':'Package QA must be rebuilt after approving all selected pages.';document.getElementById('files').innerHTML=state.package_files.map(f=>`<a href="${f.url}" target="_blank">${esc(f.name)}</a>`).join('');document.getElementById('approveAll').disabled=state.approved_pages!==state.total_pages||!state.qa_passed;document.getElementById('regenerate').disabled=!state.pages.some(p=>p.human_status==='rejected')||['regenerating','packaging'].includes(st.status);document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{try{let page=+b.dataset.page,action=b.dataset.action,body={page,action,note:document.getElementById('note'+page)?.value||''};if(action==='replace')body.candidate_file=document.getElementById('promote'+page).value;await api('/api/decision',body);await refresh()}catch(e){showErr(e)}});document.querySelectorAll('[data-zoom]').forEach(i=>i.onclick=()=>{let d=document.getElementById('zoom');d.querySelector('img').src=i.dataset.zoom;d.showModal()})}async function refresh(){state=await api('/api/state');render()}document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.section').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')});document.getElementById('rebuild').onclick=async()=>{try{document.getElementById('rebuild').disabled=true;await api('/api/rebuild',{});await refresh()}catch(e){showErr(e)}finally{document.getElementById('rebuild').disabled=false}};document.getElementById('regenerate').onclick=async()=>{try{await api('/api/regenerate',{});await refresh()}catch(e){showErr(e)}};document.getElementById('approveAll').onclick=async()=>{if(confirm('Record human approval for this complete product? This will not upload or publish.'))try{await api('/api/approve',{});await refresh()}catch(e){showErr(e)}};refresh().catch(showErr);setInterval(()=>refresh().catch(()=>{}),5000);
</script></body></html>'''

HTML = HTML.replace("</style>", ".listingimages{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.listingimages img{width:160px;height:160px;object-fit:contain;background:#f7f6f2;border-radius:6px}.copyPreview{background:#f7f6f2;padding:12px;border-radius:8px;margin-top:14px;white-space:pre-wrap}\n</style>")
HTML = HTML.replace('<div class="filelinks" id="files"></div>', '<div class="filelinks" id="files"></div><div class="listingimages" id="listingImages"></div><div class="copyPreview" id="copyPreview"></div>')
HTML = HTML.replace("document.getElementById('files').innerHTML=state.package_files.map(f=>`<a href=\"${f.url}\" target=\"_blank\">${esc(f.name)}</a>`).join('');", "document.getElementById('files').innerHTML=state.package_files.map(f=>`<a href=\"${f.url}\" target=\"_blank\">${esc(f.name)}</a>`).join('');document.getElementById('listingImages').innerHTML=state.package_files.filter(f=>f.name.startsWith('listing_')).map(f=>`<a href=\"${f.url}\" target=\"_blank\"><img src=\"${f.url}\" alt=\"${esc(f.name)}\"></a>`).join('');let l=state.product.listing||{};document.getElementById('copyPreview').innerHTML=`<b>${esc(l.title||'Listing title')}</b>\\n${esc(l.tagline||'')}\\n\\n${esc(l.description||'')}\\n\\n<b>Tags:</b> ${esc((l.tags||[]).join(', '))}`;")
HTML = HTML.replace('<button class="tab" data-tab="alternatives">Rejected alternatives</button>', '<button class="tab" data-tab="alternatives">Complete page archive</button><button class="tab" data-tab="library">Candidate library</button>')
HTML = HTML.replace('<section id="package" class="section">', '<section id="library" class="section"><div class="panel"><h2>Production progress</h2><p id="libraryStatus"></p><div id="libraryGroups"></div></div></section><section id="package" class="section">')
HTML = HTML.replace("function render(){document.getElementById('summary')", "let noteDrafts={};document.addEventListener('input',e=>{if(e.target.matches('textarea[id^=note]'))noteDrafts[e.target.id]=e.target.value});function render(){let active=document.activeElement,focused=active&&active.matches('textarea[id^=note]')?active.id:null,caret=focused?active.selectionStart:null;document.querySelectorAll('textarea[id^=note]').forEach(t=>noteDrafts[t.id]=t.value);document.getElementById('summary')")
HTML = HTML.replace("document.getElementById('grid').innerHTML=state.pages.map(card).join('');", "let grid=document.getElementById('grid'),oldNotes=new Map([...grid.querySelectorAll('textarea[id^=note]')].map(t=>[t.id,t]));grid.innerHTML=state.pages.map(card).join('');grid.querySelectorAll('textarea[id^=note]').forEach(t=>{let old=oldNotes.get(t.id);if(old){if(!Object.prototype.hasOwnProperty.call(noteDrafts,t.id))old.value=t.value;t.replaceWith(old)}else if(Object.prototype.hasOwnProperty.call(noteDrafts,t.id))t.value=noteDrafts[t.id]});if(focused){let f=document.getElementById(focused);if(f){f.focus();if(caret!==null)f.setSelectionRange(caret,caret)}}")
HTML = HTML.replace("document.getElementById('qa').textContent=state.qa_passed?", "renderLibrary();document.getElementById('qa').textContent=state.qa_passed?")
LIBRARY_JS = r"""document.getElementById('grid').addEventListener('click',async e=>{let b=e.target.closest('[data-save-note]');if(!b)return;let page=+b.dataset.saveNote;try{await api('/api/note',{page,note:document.getElementById('note'+page).value});noteDrafts['note'+page]=document.getElementById('note'+page).value;b.textContent='Saved';setTimeout(()=>b.textContent='Save note',1600)}catch(err){showErr(err)}});function renderLibrary(){let lib=state.candidate_library||{items:[]},items=lib.items||[];document.getElementById('libraryStatus').textContent=`${lib.generated||0} generated · ${lib.completed||0}/${lib.planned||0} planned reviews complete · ${lib.status||'not started'}${lib.error?' · '+lib.error:''}`;let defs=[['Approved',x=>x.category==='approved'],['Replacement Options',x=>x.category==='replacement'],['New Scene Concepts',x=>x.category==='new_scene']];document.getElementById('libraryGroups').innerHTML=defs.map(([name,fn])=>{let rows=items.filter(x=>fn(x)&&x.surface!==false).sort((a,b)=>(b.rank||0)-(a.rank||0));return `<h3>${name} (${rows.length})</h3><div class="grid">${rows.map(x=>`<article class="card"><img src="${x.image||''}" data-zoom="${x.image||''}" alt="${esc(x.title)}"><div class="content"><h3>${esc(x.title||x.id)}</h3><div class="meta">${esc(x.status||'candidate')} · QA ${esc(x.qa?.pass?'pass':'check')} · Qwen ${esc(x.review?.score??'—')} · diversity ${esc(x.diversity_score??'—')}</div><p>${esc(x.review?.summary||x.note||'Queued')}</p><p class="meta">${esc((x.review?.defects||[]).join('; '))}</p></div></article>`).join('')}</div>`}).join('')+(items.some(x=>x.surface===false)?`<details><summary>Complete candidate archive (${items.filter(x=>x.surface===false).length})</summary><div class="grid">${items.filter(x=>x.surface===false).map(x=>`<article class="card"><img src="${x.image||''}" data-zoom="${x.image||''}" alt="${esc(x.title)}"><div class="content"><h3>${esc(x.title||x.id)}</h3><p>${esc(x.review?.summary||x.status||'')}</p></div></article>`).join('')}</div></details>`:'')} `};"""
LIBRARY_JS = LIBRARY_JS.replace('<p class="meta">${esc((x.review?.defects||[]).join(\'; \'))}</p></div></article>`', '<p class="meta">${esc((x.review?.defects||[]).join(\'; \'))}</p>${x.review?.decision===\'accept\'&&x.qa?.pass?`<div class="actions"><select data-library-target="${esc(x.file)}">${state.pages.map(p=>`<option value="${p.page}">Replace page ${p.page}</option>`).join(\'\')}</select><button data-library-replace="${esc(x.file)}">Use candidate</button></div>`:\'\'}</div></article>`')
LIBRARY_JS = LIBRARY_JS.replace("function renderLibrary()", "document.getElementById('libraryGroups').addEventListener('click',async e=>{let b=e.target.closest('[data-library-replace]');if(!b)return;let file=b.dataset.libraryReplace,sel=document.querySelector(`[data-library-target=\"${CSS.escape(file)}\"]`);try{await api('/api/library_replace',{page:+sel.value,candidate_file:file});await refresh()}catch(err){showErr(err)}});function renderLibrary()")
LIBRARY_JS = LIBRARY_JS.removesuffix(" `};") + ";"
HTML = HTML.replace("document.getElementById('rebuild').onclick=async()=>", LIBRARY_JS + "document.getElementById('rebuild').onclick=async()=>")
HTML = HTML.replace("refresh().catch(showErr);setInterval(()=>refresh().catch(()=>{}),5000);", "refresh().catch(showErr);setInterval(()=>refresh().catch(()=>{}),5000);")
HTML = HTML.replace('<textarea id="note${p.page}" placeholder="Optional note / change request">${esc(p.human_note)}</textarea><div class="actions"><button data-action="keep"', '<textarea id="note${p.page}" placeholder="Optional note / change request; saved critique informs the next generation">${esc(p.human_note)}</textarea><div class="actions"><button class="secondary" data-save-note="${p.page}">Save note</button><span class="meta">Keep = approve this version · Reject / Regenerate = queue a new design · Needs changes = pause until you add instructions</span></div><div class="actions"><button data-action="keep"')
HTML = HTML.replace("document.getElementById('alts').innerHTML=groups||'<div class=\"panel\">No rejected alternatives recorded.</div>';", "document.getElementById('alts').innerHTML=groups||'<div class=\"panel\">No rejected alternatives recorded.</div>';let archive=state.candidate_archive||[];document.getElementById('alts').innerHTML=archive.length?`<div class=\"grid\">${archive.map(a=>`<article class=\"card\"><img src=\"${a.image||''}\" data-zoom=\"${a.image||''}\" alt=\"Archived candidate\"><div class=\"content\"><h3>Page ${a.page} · ${esc(a.label)} · Attempt ${a.attempt}</h3><div class=\"meta\">${esc(a.review.decision||'unreviewed')} · ${esc(a.review.score??'—')} · ${esc(a.qa.pass?'QA pass':'QA check')}</div><p>${esc(a.review.summary||'')}</p><p>${esc((a.review.defects||[]).join('; '))}</p></div></article>`).join('')}</div>`:'<div class=\"panel\">No archived candidates.</div>'; ")

BOOK_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Little Haunts Club · Halloween 2026</title><style>
:root{font:16px/1.45 Segoe UI,Arial,sans-serif;color:#27362f;background:#f5f2eb}*{box-sizing:border-box}body{margin:0}header{background:#233a32;color:#fff;padding:17px 26px}header h1{margin:0;font-size:23px}header p{margin:3px 0;color:#dce8de}.wrap{max-width:1480px;margin:auto;padding:20px}nav{display:flex;gap:8px;margin:0 0 16px}.stepbtn,button{border:0;border-radius:8px;padding:9px 13px;cursor:pointer;font:inherit}.stepbtn{background:#e5e1d8;color:#26352f}.stepbtn.active{background:#385e4d;color:#fff}button{background:#395e4e;color:white}button.secondary{background:#e6e2d9;color:#26352f}button.danger{background:#a33e37}button:disabled{opacity:.45;cursor:not-allowed}.section{display:none}.section.active{display:block}.summary{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 16px}.pill{background:white;border:1px solid #ded9ce;border-radius:999px;padding:6px 12px}.notice,.panel{background:white;border:1px solid #e0ddd4;border-radius:12px;padding:15px;margin:12px 0}.notice{background:#eff4ee}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}.card{background:#fff;border:1px solid #dfdbd1;border-radius:12px;overflow:hidden}.card.selected{border:3px solid #55866a}.card img{width:100%;height:330px;object-fit:contain;background:#fff;cursor:zoom-in}.card .content{padding:12px}.card h2,.card h3{font-size:16px;margin:5px 0}.meta{font-size:13px;color:#655f56}.badge{display:inline-block;border-radius:999px;padding:3px 8px;background:#e9e4d8;font-size:12px}.badge.approved{background:#e1f2e6;color:#267246}.badge.pending{background:#fff0d8;color:#925a00}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.actions button{font-size:14px}.actions select,select,input{font:inherit;padding:8px;border:1px solid #ccc;border-radius:7px;max-width:100%}.checkrow{display:flex;align-items:center;gap:7px;margin:6px 0}.checkrow input{width:auto}.textarea{width:100%;min-height:62px;border:1px solid #ccc;border-radius:7px;padding:8px;font:inherit}textarea:focus,input:focus,select:focus{outline:2px solid #83aa91}.files a{margin-right:10px;display:inline-block}.preview{width:100%;height:auto;max-height:850px;object-fit:contain;background:white;border:1px solid #ddd;border-radius:8px;cursor:zoom-in}.listing{display:flex;gap:10px;flex-wrap:wrap}.listing img{width:190px;height:190px;object-fit:contain;border:1px solid #ddd;border-radius:8px;cursor:zoom-in}.statusline{font-size:14px;color:#3f493f}.error{color:#a33e37}.helper{font-size:13px;color:#625f57}details{margin:12px 0}summary{cursor:pointer;font-weight:600}.advanced{background:#f0eee8;padding:14px;border-radius:9px}.advanced pre{white-space:pre-wrap;max-height:260px;overflow:auto}.candidate{display:flex;flex-direction:column}.candidate .content{flex:1}dialog{border:0;border-radius:12px;padding:8px;max-width:96vw;max-height:96vh}dialog img{max-width:92vw;max-height:90vh}@media(max-width:600px){.wrap{padding:12px}header{padding:13px}.card img{height:300px}}
</style></head><body><header><h1>Little Haunts Club · A Very Odd Halloween</h1><p>Choose your pages, review the finished download, then prepare Etsy. Nothing is sent to Etsy until you choose it.</p></header><main class="wrap"><div class="summary" id="summary"></div><nav><button class="stepbtn active" data-step="choose">1 · Choose artwork</button><button class="stepbtn" data-step="finalize">2 · Finalize product</button><button class="stepbtn" data-step="publish">3 · Publish</button></nav><div class="notice" id="status" aria-live="polite"></div>
<section id="choose" class="section active"><div class="toolbar"><b id="approvalSummary"></b><button id="bulkKeep">Keep selected pages</button><label>Replace target <select id="replaceTarget"></select></label><span class="helper">Keep approves the shown art. Replace swaps in a reviewed candidate and asks for approval. Add to book increases its page count. Regenerate makes a new option using your saved note.</span></div><h2>Pages in your book</h2><div id="bookGrid" class="grid"></div><h2>Best extra candidates <span id="extraCount"></span></h2><p class="helper">Open any image full-size. Use a reviewed option to replace a page or add it as a new page.</p><div id="extras" class="grid"></div><details><summary>Advanced · rejected archive, scores, QA and run status</summary><div class="advanced"><h3>Run and package diagnostics</h3><pre id="diagnostics"></pre><h3>Unselected archive</h3><div id="archive" class="grid"></div><h3>Complete package QA</h3><pre id="qaDetails"></pre></div></details></section>
<section id="finalize" class="section"><div class="panel"><h2>Finished product preview</h2><p id="finalStatus"></p><img id="contactSheet" class="preview" alt="Rendered contact sheet" data-zoom=""><div class="listing" id="listingImages"></div><h3>Files included</h3><div class="files" id="packageFiles"></div><div class="actions"><button id="approveProduct">Approve Product</button></div><p class="helper">This records your approval of the current 17-page (or adjusted page-count) selection and its package QA. It does not upload or publish.</p></div><details><summary>Package QA checks</summary><pre id="qaFinal"></pre></details></section>
<section id="publish" class="section"><div class="panel"><h2>Prepare the Etsy listing</h2><p id="etsyStatus"></p><p class="helper">Etsy supports creating a digital draft and uploading the package through its API. Authorization uses Etsy PKCE and requests only listing and shop read/write access. The API app key and shared secret are stored encrypted with Windows DPAPI on this computer.</p><details open><summary>Connect Etsy (one-time setup)</summary><p class="helper">Create a Seller App in Etsy Developer Portal and register an exact HTTPS callback URL. Enter its API keystring, shared secret and the matching redirect URL here. Etsy returns the code to that HTTPS callback; copy the code and state back here to finish the secure exchange.</p><div class="actions"><input id="etsyKey" placeholder="Etsy API keystring" autocomplete="off"><input id="etsySecret" placeholder="Etsy shared secret" autocomplete="new-password"><input id="etsyRedirect" placeholder="Registered HTTPS callback URL" style="min-width:330px"><button id="etsyConnect">Save securely and connect</button></div><div class="actions"><input id="etsyCode" placeholder="Authorization code from callback"><input id="etsyState" placeholder="Returned state"><button id="etsyFinish">Finish Etsy authorization</button></div><div class="actions"><button class="secondary" id="etsyLoad">Load shops and listing categories</button><select id="etsyShop"><option value="">Choose your shop</option></select><select id="etsyTaxonomy"><option value="">Choose a listing category</option></select></div></details><div class="panel"><h3>Listing preview</h3><p id="etsyListingPreview"></p><div class="listing" id="etsyImages"></div><div id="etsyFiles"></div><label>Price in your shop currency <input id="etsyPrice" type="number" min="0.01" step="0.01" placeholder="Enter price"></label><p class="helper">API fees are not available before listing creation; Etsy will show applicable fees in its seller flow. Review the displayed listing and files before transferring anything.</p><p>To create an Etsy draft and upload its digital files and listing images, type <b>CREATE DRAFT AND UPLOAD</b>.</p><div class="actions"><input id="draftConfirm" placeholder="Type CREATE DRAFT AND UPLOAD"><button id="createDraft">Create Etsy draft and upload files</button></div><p id="draftDetails"></p></div><div class="panel"><h3>Final publication</h3><p id="publishDetails">Publishing requires all page approvals, an approved package and a complete Etsy draft. It will not happen automatically.</p><div class="actions"><input id="publishConfirm" placeholder="Type PUBLISH LISTING"><button class="danger" id="publishListing">Publish listing on Etsy</button></div></div></div></section><p class="helper">Notes and selections save locally as you work. The nine original approved pages retain their history; any changed artwork needs your approval.</p></main><dialog id="zoom"><img alt="Full-size artwork"></dialog><script>
let state,noteDrafts={};async function api(url,body){let r=await fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}function esc(s=''){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function say(s,err=false){let el=document.getElementById('status');el.className='notice'+(err?' error':'');el.textContent=s}function showErr(e){say(e.message,true)}function setStep(id){document.querySelectorAll('.stepbtn,.section').forEach(x=>x.classList.remove('active'));document.querySelector(`[data-step="${id}"]`).classList.add('active');document.getElementById(id).classList.add('active')}
function pageCard(p){let checked=(state.bulk_checked_pages||[]).includes(p.page),approved=p.human_approved;let options=(state.candidate_library.items||[]).filter(x=>x.surface!==false&&x.category!=='approved'&&x.review?.decision==='accept'&&x.qa?.pass&&x.file!==p.file);return `<article class="card selected"><img src="${p.image||''}" data-zoom="${p.image||''}" alt="Page ${p.page}: ${esc(p.label)}"><div class="content"><label class="checkrow"><input type="checkbox" data-check-page="${p.page}" ${checked?'checked':''}> Include in bulk approval</label><span class="badge ${approved?'approved':'pending'}">${approved?'Human approved':'Needs your review'}</span> <span class="badge">Selected page ${p.page}</span><h2>${String(p.page).padStart(2,'0')} · ${esc(String(p.label).replace(/^\d{1,2}\s*[·.:-]\s*/,''))}</h2><p class="meta">${esc(p.candidate_category||'candidate')} · ${p.locked?'Original approved art':''}</p><textarea class="textarea" id="note${p.page}" placeholder="Optional art direction; saved notes guide regeneration">${esc(p.note||'')}</textarea><div class="actions"><button data-keep="${p.page}">Keep</button><button class="secondary" data-save-note="${p.page}">Save note</button><select data-page-candidate="${p.page}">${options.map(x=>`<option value="${esc(x.file)}">${esc(x.title||x.id)}</option>`).join('')}</select><button class="secondary" data-replace-page="${p.page}" ${options.length?'':'disabled'}>Replace</button><button class="danger" data-remove="${p.page}">Remove</button><button class="secondary" data-regenerate="${p.page}">Regenerate</button></div></div></article>`}
function candidateCard(x){let used=state.pages.some(p=>p.file===x.file),ok=x.review?.decision==='accept'&&x.qa?.pass;return `<article class="card candidate"><img src="${x.image||''}" data-zoom="${x.image||''}" alt="${esc(x.title||x.id)}"><div class="content"><span class="badge">${esc(x.category==='new_scene'?'New scene':'Replacement')}</span> <span class="badge">${esc(x.status||'candidate')}</span><h3>${esc(x.title||x.id)}</h3><p class="meta">${esc(x.review?.summary||x.note||'Reviewed candidate')}${x.review?.score!==undefined?' · visual review '+esc(x.review.score):''}</p>${(x.review?.defects||[]).length?`<p class="meta">${esc(x.review.defects.join('; '))}</p>`:''}<div class="actions"><button data-use-candidate="${esc(x.id)}" ${!ok||used?'disabled':''}>Replace selected page</button><button class="secondary" data-add-candidate="${esc(x.id)}" ${!ok||used?'disabled':''}>Add to book</button></div>${used?'<p class="meta">Already selected in the book.</p>':''}</div></article>`}
function render(){let active=document.activeElement,focused=active&&active.matches('textarea[id^=note]')?active.id:null,caret=focused?active.selectionStart:null;document.querySelectorAll('textarea[id^=note]').forEach(t=>noteDrafts[t.id]=t.value);document.getElementById('summary').innerHTML=`<span class="pill">${esc(state.product.title)}</span><span class="pill">${state.total_pages} pages selected</span><span class="pill">${state.approved_pages}/${state.total_pages} human approved</span><span class="pill">Package QA ${state.qa_passed?'passed':'needs rebuild'}</span><span class="pill">Etsy published ${state.etsy?.published?'yes':'no'}</span>`;let st=state.status||{};if(st.status==='regenerating')say(`Generating and reviewing page ${st.pages?.join(', ')||''}; your current page remains saved.`);else if(st.error)say(st.error,true);else say(`Production: ${st.status||'ready'}${st.page_count?` · ${st.page_count} pages`:''}. Selection and notes save locally.`);document.getElementById('approvalSummary').textContent=`${state.total_pages} pages · ${state.approved_pages} approved · ${state.total_pages-state.approved_pages} still need review`;let replaceTarget=document.getElementById('replaceTarget'),previousTarget=replaceTarget.value;replaceTarget.innerHTML=state.pages.map(p=>`<option value="${p.page}">Page ${p.page}: ${esc(String(p.label).replace(/^\d{1,2}\s*[·.:-]\s*/,''))}</option>`).join('');replaceTarget.value=state.pages.some(p=>String(p.page)===previousTarget)?previousTarget:String((state.pages.find(p=>!p.human_approved)||state.pages[0]||{}).page||'');let grid=document.getElementById('bookGrid'),old=new Map([...grid.querySelectorAll('textarea[id^=note]')].map(t=>[t.id,t]));grid.innerHTML=state.pages.map(pageCard).join('');grid.querySelectorAll('textarea[id^=note]').forEach(t=>{let was=old.get(t.id);if(was){if(Object.prototype.hasOwnProperty.call(noteDrafts,t.id))was.value=noteDrafts[t.id];t.replaceWith(was)}else if(Object.prototype.hasOwnProperty.call(noteDrafts,t.id))t.value=noteDrafts[t.id]});if(focused){let f=document.getElementById(focused);if(f){f.focus();if(caret!==null)f.setSelectionRange(caret,caret)}}let curated=(state.candidate_library.items||[]).filter(x=>x.surface!==false&&x.category!=='approved').sort((a,b)=>(b.rank||0)-(a.rank||0));document.getElementById('extraCount').textContent=`(${curated.length})`;document.getElementById('extras').innerHTML=curated.map(candidateCard).join('');let archives=(state.candidate_library.items||[]).filter(x=>x.surface===false);document.getElementById('archive').innerHTML=archives.map(x=>`<article class="card"><img src="${x.image||''}" data-zoom="${x.image||''}" alt="Archived ${esc(x.title||x.id)}"><div class="content"><h3>${esc(x.title||x.id)}</h3><p>${esc(x.status||'archived')} · ${esc(x.review?.decision||'no review')} · score ${esc(x.review?.score??'—')}</p><p>${esc(x.review?.summary||'')}</p></div></article>`).join('')||'<p>No archived candidates.</p>';document.getElementById('diagnostics').textContent=JSON.stringify({production:state.candidate_library,status:state.status,package:state.package_directory,approved:state.approved_pages,total:state.total_pages},null,2);document.getElementById('qaDetails').textContent=JSON.stringify(state.qa,null,2);document.getElementById('qaFinal').textContent=JSON.stringify(state.qa,null,2);let cf=state.package_files.find(x=>x.name==='preview_print_ready_pages.jpg')||state.package_files.find(x=>x.name==='preview_original_pages.jpg');let sheet=document.getElementById('contactSheet');sheet.src=cf?.url||'';sheet.dataset.zoom=cf?.url||'';document.getElementById('finalStatus').textContent=state.qa_passed?`Rendered ${state.total_pages}-page package passed technical QA. Human artwork approval: ${state.approved_pages}/${state.total_pages}.`:`The latest package is not current for this selection. Choose or remove art to rebuild it.`;document.getElementById('packageFiles').innerHTML=state.package_files.map(f=>`<a href="${f.url}" target="_blank">${esc(f.name)}</a>`).join(' · ');document.getElementById('listingImages').innerHTML=state.package_files.filter(f=>f.name.startsWith('listing_')).map(f=>`<img src="${f.url}" data-zoom="${f.url}" alt="${esc(f.name)}">`).join('');let canApprove=state.approved_pages===state.total_pages&&state.qa_passed;document.getElementById('approveProduct').disabled=!canApprove||state.package_approved;document.getElementById('approveProduct').textContent=state.package_approved?'Product approved':'Approve Product';renderEtsy()}
function renderEtsy(){let e=state.etsy||{};document.getElementById('etsyStatus').textContent=`Connection: ${e.authorized?'authorized':'not authorized'} · Draft: ${e.draft?.listing_id||'none'} · Published: ${e.published?'yes':'no'}`;let l=state.product.listing||{};document.getElementById('etsyListingPreview').innerHTML=`<b>${esc(l.title||state.product.title)}</b><br>${esc(l.description||'')}<br><br>Tags: ${esc((l.tags||[]).join(', '))}`;document.getElementById('etsyImages').innerHTML=state.package_files.filter(f=>f.name.startsWith('listing_')).map(f=>`<img src="${f.url}" data-zoom="${f.url}" alt="${esc(f.name)}">`).join('');document.getElementById('etsyFiles').innerHTML=state.package_files.filter(f=>/\.pdf$|\.zip$/i.test(f.name)).map(f=>`<p><a href="${f.url}" target="_blank">${esc(f.name)}</a></p>`).join('');document.getElementById('draftDetails').textContent=e.draft?`Draft ${e.draft.listing_id} · ${e.draft.title} · ${e.draft.price} · ${e.draft.uploaded_files?.length||0} files and ${e.draft.uploaded_images?.length||0} images transferred.`:'';let ready=state.package_approved&&state.qa_passed&&state.approved_pages===state.total_pages&&e.authorized;document.getElementById('createDraft').disabled=!ready;document.getElementById('publishListing').disabled=!ready||!e.draft||e.published}
async function refresh(){let el=document.activeElement,focus=el&&el.matches('textarea[id^=note]')?el.id:null,caret=focus?el.selectionStart:null;state=await api('/api/state');render();if(focus){let input=document.getElementById(focus);if(input){input.focus();input.setSelectionRange(caret,caret)}}}document.querySelectorAll('.stepbtn').forEach(b=>b.onclick=()=>setStep(b.dataset.step));document.addEventListener('input',e=>{if(e.target.matches('textarea[id^=note]'))noteDrafts[e.target.id]=e.target.value});document.addEventListener('click',async e=>{let t=e.target,zoom=t.closest('[data-zoom]');if(zoom&&zoom.dataset.zoom){let d=document.getElementById('zoom');d.querySelector('img').src=zoom.dataset.zoom;d.showModal();return}try{if(t.matches('[data-keep]')){await api('/api/book/keep',{page:+t.dataset.keep});say(`Page ${t.dataset.keep} marked Keep. This is your human approval.`);await refresh()}else if(t.matches('[data-save-note]')){let p=+t.dataset.saveNote,n=document.getElementById('note'+p).value;await api('/api/book/note',{page:p,note:n});noteDrafts['note'+p]=n;t.textContent='Saved';say(`Note for page ${p} saved. Your text is still here.`);setTimeout(()=>t.textContent='Save note',1400)}else if(t.matches('[data-remove]')){await api('/api/book/remove',{page:+t.dataset.remove});await refresh();say('Page removed from the current book. Its image and decision history remain archived.')}else if(t.matches('[data-replace-page]')){let p=+t.dataset.replacePage,file=document.querySelector(`[data-page-candidate="${p}"]`).value;await api('/api/book/replace',{page:p,candidate_file:file});await refresh();say(`Page ${p} replaced. The new selection needs your approval.`)}else if(t.matches('[data-use-candidate]')){let id=t.dataset.useCandidate,x=state.candidate_library.items.find(y=>y.id===id),page=+document.getElementById('replaceTarget').value;await api('/api/book/replace',{page,candidate_file:x.file});await refresh();say(`Page ${page} replaced with ${x.title}; the new art needs your approval.`)}else if(t.matches('[data-add-candidate]')){let x=state.candidate_library.items.find(y=>y.id===t.dataset.addCandidate);await api('/api/book/add',{candidate_file:x.file});await refresh();say(`${x.title} added as a new page. Review it before product approval.`)}else if(t.matches('[data-regenerate]')){let p=+t.dataset.regenerate;if(!confirm(`Regenerate page ${p} using its saved note? This starts one local image generation and review.`))return;await api('/api/book/regenerate',{page:p});await refresh()} }catch(err){showErr(err)}});document.addEventListener('change',async e=>{if(e.target.matches('[data-check-page]')){let pages=[...document.querySelectorAll('[data-check-page]:checked')].map(x=>+x.dataset.checkPage);try{await api('/api/book/check',{pages});state.bulk_checked_pages=pages;document.getElementById('approvalSummary').textContent=`${state.total_pages} pages · ${state.approved_pages} approved · ${pages.length} selected for bulk approval`}catch(err){showErr(err)}}});document.getElementById('bulkKeep').onclick=async()=>{let pages=[...document.querySelectorAll('[data-check-page]:checked')].map(x=>+x.dataset.checkPage);if(!pages.length)return say('Check the pages you want to approve together.',true);await api('/api/book/bulk_keep',{pages});await refresh();say(`${pages.length} selected pages marked Keep. This records your human approvals.`)};document.getElementById('approveProduct').onclick=async()=>{if(!confirm(`Approve this ${state.total_pages}-page product package? This records your approval but does not send it to Etsy.`))return;try{await api('/api/book/approve',{});await refresh();say('Product approved locally. Etsy has not been contacted.')}catch(e){showErr(e)}};document.getElementById('etsyConnect').onclick=async()=>{try{let r=await api('/api/etsy/connect',{api_key:document.getElementById('etsyKey').value,shared_secret:document.getElementById('etsySecret').value,redirect_uri:document.getElementById('etsyRedirect').value});document.getElementById('etsyState').value=r.state;window.open(r.authorize_url,'_blank','noopener');say('Etsy authorization opened. After approval, copy the callback code here and submit its returned state.')}catch(e){showErr(e)}};document.getElementById('etsyFinish').onclick=async()=>{try{await api('/api/etsy/finish',{code:document.getElementById('etsyCode').value,state:document.getElementById('etsyState').value});await refresh();say('Etsy authorization saved securely.')}catch(e){showErr(e)}};document.getElementById('etsyLoad').onclick=async()=>{try{let [shopData,taxData]=await Promise.all([api('/api/etsy/shops'),api('/api/etsy/taxonomies')]),shops=shopData.results||[],tax=taxData.results||[];document.getElementById('etsyShop').innerHTML='<option value="">Choose your shop</option>'+shops.map(x=>`<option value="${x.shop_id}">${esc(x.shop_name)} · ${x.shop_id}</option>`).join('');let flat=[];function walk(nodes,path=[]){for(let n of nodes||[]){let p=[...path,n.name];if(!(n.children||[]).length)flat.push({id:n.id,name:p.join(' › ')});else walk(n.children,p)}}walk(tax);document.getElementById('etsyTaxonomy').innerHTML='<option value="">Choose a listing category</option>'+flat.map(x=>`<option value="${x.id}">${esc(x.name)} · ${x.id}</option>`).join('');say(`${shops.length} Etsy shop(s) and ${flat.length} listing categories loaded.`)}catch(e){showErr(e)}};document.getElementById('createDraft').onclick=async()=>{try{let shop=document.getElementById('etsyShop').value,tax=document.getElementById('etsyTaxonomy').value,price=document.getElementById('etsyPrice').value;if(!shop||!tax||!price)throw Error('Choose your Etsy shop and listing category, then enter the shop-currency price.');let r=await api('/api/etsy/draft',{shop_id:shop,taxonomy_id:tax,price,confirm:document.getElementById('draftConfirm').value});await refresh();say(`Etsy draft ${r.draft.listing_id} created and package files uploaded. It remains unpublished.`)}catch(e){showErr(e)}};document.getElementById('publishListing').onclick=async()=>{if(!confirm('Publish this listing on Etsy now? This is the final external publication action.'))return;try{let r=await api('/api/etsy/publish',{confirm:document.getElementById('publishConfirm').value});await refresh();say('Etsy listing is published.')}catch(e){showErr(e)}};document.getElementById('zoom').onclick=()=>document.getElementById('zoom').close();refresh().catch(showErr);setInterval(()=>refresh().catch(()=>{}),5000);
</script></body></html>'''


def make_handler(dashboard: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print("[dashboard] " + fmt % args)

        def send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                body = (BOOK_HTML if dashboard.book_path.is_file() else HTML).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif parsed.path == "/api/state":
                self.send_json(dashboard.data())
            elif parsed.path.startswith("/asset/"):
                try:
                    token = parsed.path.rsplit("/", 1)[1]
                    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
                    path = Path(raw).resolve()
                    roots = [dashboard.run_dir / "candidates", dashboard.run_dir / "candidate_library", dashboard.product_root / "pages", dashboard.final_dir,
                             dashboard.product_root / "qa", *dashboard.run_dir.glob("final_deliverables_*")]
                    if not contained(path, roots) or not path.is_file():
                        raise ValueError("not allowed")
                    body = path.read_bytes(); ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
                except Exception:
                    self.send_error(404)
            elif parsed.path == "/api/etsy/shops":
                try: self.send_json({"results": dashboard.etsy_shops()})
                except Exception as exc: self.send_json({"error": str(exc)}, 400)
            elif parsed.path == "/api/etsy/taxonomies":
                try: self.send_json({"results": dashboard.etsy_taxonomies()})
                except Exception as exc: self.send_json({"error": str(exc)}, 400)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 100_000:
                    raise ValueError("Request too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                route = urllib.parse.urlparse(self.path).path
                if route == "/api/decision": result = dashboard.decide(int(body["page"]), body["action"], str(body.get("note", "")), body.get("candidate_file"))
                elif route == "/api/note": result = dashboard.save_note(int(body["page"]), str(body.get("note", "")))
                elif route == "/api/library_replace": result = dashboard.replace_from_library(int(body["page"]), str(body["candidate_file"]))
                elif route == "/api/book/keep": result = dashboard.book_keep(int(body["page"]))
                elif route == "/api/book/bulk_keep": result = dashboard.book_bulk_keep(body.get("pages", []))
                elif route == "/api/book/check": result = dashboard.book_bulk_selection(body.get("pages", []))
                elif route == "/api/book/replace": result = dashboard.book_replace(int(body["page"]), str(body["candidate_file"]))
                elif route == "/api/book/add": result = dashboard.book_add(str(body["candidate_file"]))
                elif route == "/api/book/remove": result = dashboard.book_remove(int(body["page"]))
                elif route == "/api/book/note": result = dashboard.book_note(int(body["page"]), str(body.get("note", "")))
                elif route == "/api/book/regenerate": result = dashboard.book_regenerate(int(body["page"]))
                elif route == "/api/book/approve": result = dashboard.book_approve_product()
                elif route == "/api/etsy/connect": result = dashboard.etsy_connect(body)
                elif route == "/api/etsy/finish": result = dashboard.etsy_finish_auth(body)
                elif route == "/api/etsy/draft": result = dashboard.etsy_create_draft(body)
                elif route == "/api/etsy/publish": result = dashboard.etsy_publish(body)
                elif route == "/api/approve": result = dashboard.approve()
                elif route == "/api/rebuild": result = dashboard.rebuild_package()
                elif route == "/api/regenerate": result = dashboard.regenerate()
                else: self.send_json({"error": "Not found"}, 404); return
                self.send_json(result)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", default="products/little-haunts-club-spooky-neighbors-17")
    parser.add_argument("--run-id", default="factory-main")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Dashboard must bind to localhost only")
    product = Path(args.product)
    product = (ROOT / product).resolve() if not product.is_absolute() else product.resolve()
    if not (product / "prompts.json").is_file():
        parser.error(f"Product manifest not found: {product / 'prompts.json'}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Dashboard(product, args.run_id)))
    server.daemon_threads = True
    print(f"Human approval dashboard: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
