#!/usr/bin/env python3
"""Local-only human approval dashboard for an existing product factory run."""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
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
        roots = [self.run_dir / "candidates", self.product_root / "pages", self.final_dir,
                 self.product_root / "qa", *self.run_dir.glob("final_deliverables_human_approved_*")]
        if not contained(candidate, roots) or not candidate.is_file():
            return None
        token = base64.urlsafe_b64encode(str(candidate).encode()).decode().rstrip("=")
        return "/asset/" + token

    def data(self) -> dict[str, Any]:
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
            "package_files": self.package_files(current_package), "published": False}

    def package_files(self, package_dir: Path | None = None) -> list[dict[str, Any]]:
        package_dir = package_dir or self.final_dir
        paths = [*sorted((package_dir / "etsy_uploads").glob("*.pdf")),
                 *sorted((package_dir / "etsy_uploads").glob("*.zip")),
                 *sorted((package_dir / "listing_images").glob("*.jpg")),
                 package_dir / "etsy_listing_draft.md", package_dir / "package_qa.json"]
        return [{"name": p.name, "url": self.asset_url(p)} for p in paths if p.is_file()]

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
        with self.lock:
            data = self.data()
            if data["approved_pages"] != data["total_pages"]:
                raise ValueError(f"Approve all 15 pages individually first ({data['approved_pages']}/{data['total_pages']} approved)")
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
                if not contained(source, [self.run_dir / "candidates", self.product_root / "pages"]) or not source.is_file():
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
            for page in pages:
                rec = state.get("candidates", {}).get(str(page))
                if not rec:
                    raise ValueError(f"No candidate history for page {page}")
                max_attempts = int(self.load_json(self.product_root / "factory_config.json", {}).get("generation", {}).get("max_attempts", 3))
                if len(rec.get("attempts", [])) >= max_attempts:
                    raise ValueError(f"Page {page} reached the configured attempt limit ({max_attempts}); raise the factory limit before retrying")
                rec["status"] = "pending"
            tmp = self.state_path.with_suffix(".dashboard.tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
            config = self.load_json(self.product_root / "factory_config.json", {})
            vision = config.get("vision", {})
            endpoint, model = vision.get("endpoint"), vision.get("model")
            if not endpoint or not model:
                raise ValueError("Configure the local vision endpoint and model in factory_config.json first")
            command = [sys.executable, str(ROOT / "scripts" / "product_review_worker.py"), "--product", str(self.product_root), "--run-id", self.run_id,
                "--pages", *map(str, pages), "--max-attempts", str(config.get("generation", {}).get("max_attempts", 3)),
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
            human = self.human()
            for page in pages:
                rec = self.load_json(self.state_path, {}).get("candidates", {}).get(str(page), {})
                entry = human.get("pages", {}).setdefault(str(page), {})
                if rec.get("status") == "accepted_by_vision":
                    entry["status"] = "needs_review"
                    entry["candidate_file"] = rec.get("best_candidate")
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
let state;async function api(url,body){let r=await fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}function esc(s=''){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function showErr(e){document.getElementById('status').innerHTML='<span class="error">'+esc(e.message)+'</span>'}function reviewMarkup(p){let r=p.ai_review||{};let checks=r.checks||{};return `<div class="checks"><b>AI verdict: ${esc(r.decision||'none')} · score ${esc(r.score??'—')}</b><br>${esc(r.summary||'No assessment recorded')}<br><b>AI checks:</b> ${Object.entries(checks).map(([k,v])=>`${esc(k.replaceAll('_',' '))}: ${v?'pass':'fail'}`).join(' · ')||'not available'}${(r.defects||[]).length?'<br><b>Reasons:</b> '+esc(r.defects.join('; ')):''}</div>`}function card(p){return `<article class="card"><img src="${p.image||''}" data-zoom="${p.image||''}" alt="Page ${p.page}: ${esc(p.label)}"><div class="content"><h2>${String(p.page).padStart(2,'0')} · ${esc(p.label)}</h2><div class="meta">AI: ${esc(p.ai_status)} · Human: <span class="status ${esc(p.human_status)}">${esc(p.human_status.replaceAll('_',' '))}</span> · ${p.attempt_count} candidates</div>${reviewMarkup(p)}<textarea id="note${p.page}" placeholder="Optional note / change request">${esc(p.human_note)}</textarea><div class="actions"><button data-action="keep" data-page="${p.page}">Keep</button><button class="danger" data-action="reject" data-page="${p.page}">Reject / Regenerate</button><button class="secondary" data-action="needs_changes" data-page="${p.page}">Needs changes</button></div>${p.promotable.length?`<div class="actions"><select id="promote${p.page}">${p.promotable.map(c=>`<option value="${esc(c.candidate_file)}">Attempt ${c.attempt} · score ${c.score}</option>`).join('')}</select><button class="secondary" data-action="replace" data-page="${p.page}">Replace with candidate</button></div>`:''}</div></article>`}function render(){document.getElementById('summary').innerHTML=`<span class="pill">${esc(state.product.title)}</span><span class="pill">${state.approved_pages}/${state.total_pages} pages approved</span><span class="pill">Package QA: ${state.qa_passed?'passed':'not current'}</span><span class="pill">Published: no</span>`;let st=state.status||{};document.getElementById('status').textContent=`Factory status: ${st.status||'idle'}${st.message?' · '+st.message:''}${st.pages?' · pages '+st.pages.join(', '):''}${st.error?' · '+st.error:''}`;document.getElementById('grid').innerHTML=state.pages.map(card).join('');let groups=state.pages.map(p=>p.alternatives.length?`<div class="panel"><h2>Page ${p.page} · ${esc(p.label)}</h2><div class="alternatives">${p.alternatives.map(a=>`<div class="alt"><img src="${a.image||''}" data-zoom="${a.image||''}" alt="Candidate ${a.attempt}"><b>Attempt ${a.attempt} · score ${a.score??'—'} · ${esc(a.decision||'unreviewed')}</b><p>${esc(a.summary)}</p><p class="meta">${esc((a.defects||[]).join('; '))}</p></div>`).join('')}</div></div>`:'').join('');document.getElementById('alts').innerHTML=groups||'<div class="panel">No rejected alternatives recorded.</div>';document.getElementById('qa').textContent=state.qa_passed?'Latest package QA passed for '+state.total_pages+' pages.':'Package QA must be rebuilt after approving all selected pages.';document.getElementById('files').innerHTML=state.package_files.map(f=>`<a href="${f.url}" target="_blank">${esc(f.name)}</a>`).join('');document.getElementById('approveAll').disabled=state.approved_pages!==state.total_pages||!state.qa_passed;document.getElementById('regenerate').disabled=!state.pages.some(p=>p.human_status==='rejected')||['regenerating','packaging'].includes(st.status);document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{try{let page=+b.dataset.page,action=b.dataset.action,body={page,action,note:document.getElementById('note'+page)?.value||''};if(action==='replace')body.candidate_file=document.getElementById('promote'+page).value;await api('/api/decision',body);await refresh()}catch(e){showErr(e)}});document.querySelectorAll('[data-zoom]').forEach(i=>i.onclick=()=>{let d=document.getElementById('zoom');d.querySelector('img').src=i.dataset.zoom;d.showModal()})}async function refresh(){state=await api('/api/state');render()}document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.section').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')});document.getElementById('rebuild').onclick=async()=>{try{document.getElementById('rebuild').disabled=true;await api('/api/rebuild',{});await refresh()}catch(e){showErr(e)}finally{document.getElementById('rebuild').disabled=false}};document.getElementById('regenerate').onclick=async()=>{try{await api('/api/regenerate',{});await refresh()}catch(e){showErr(e)}};document.getElementById('approveAll').onclick=async()=>{if(confirm('Record human approval for this complete product? This will not upload or publish.'))try{await api('/api/approve',{});await refresh()}catch(e){showErr(e)}};refresh().catch(showErr);setInterval(()=>refresh().catch(()=>{}),5000);
</script></body></html>'''

HTML = HTML.replace("</style>", ".listingimages{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.listingimages img{width:160px;height:160px;object-fit:contain;background:#f7f6f2;border-radius:6px}.copyPreview{background:#f7f6f2;padding:12px;border-radius:8px;margin-top:14px;white-space:pre-wrap}\n</style>")
HTML = HTML.replace('<div class="filelinks" id="files"></div>', '<div class="filelinks" id="files"></div><div class="listingimages" id="listingImages"></div><div class="copyPreview" id="copyPreview"></div>')
HTML = HTML.replace("document.getElementById('files').innerHTML=state.package_files.map(f=>`<a href=\"${f.url}\" target=\"_blank\">${esc(f.name)}</a>`).join('');", "document.getElementById('files').innerHTML=state.package_files.map(f=>`<a href=\"${f.url}\" target=\"_blank\">${esc(f.name)}</a>`).join('');document.getElementById('listingImages').innerHTML=state.package_files.filter(f=>f.name.startsWith('listing_')).map(f=>`<a href=\"${f.url}\" target=\"_blank\"><img src=\"${f.url}\" alt=\"${esc(f.name)}\"></a>`).join('');let l=state.product.listing||{};document.getElementById('copyPreview').innerHTML=`<b>${esc(l.title||'Listing title')}</b>\\n${esc(l.tagline||'')}\\n\\n${esc(l.description||'')}\\n\\n<b>Tags:</b> ${esc((l.tags||[]).join(', '))}`;")


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
                body = HTML.encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif parsed.path == "/api/state":
                self.send_json(dashboard.data())
            elif parsed.path.startswith("/asset/"):
                try:
                    token = parsed.path.rsplit("/", 1)[1]
                    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
                    path = Path(raw).resolve()
                    roots = [dashboard.run_dir / "candidates", dashboard.product_root / "pages", dashboard.final_dir,
                             dashboard.product_root / "qa", *dashboard.run_dir.glob("final_deliverables_human_approved_*")]
                    if not contained(path, roots) or not path.is_file():
                        raise ValueError("not allowed")
                    body = path.read_bytes(); ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
                except Exception:
                    self.send_error(404)
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
    parser.add_argument("--product", default="products/little-haunts-club-spooky-neighbors-15")
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
