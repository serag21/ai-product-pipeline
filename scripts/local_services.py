"""Safe readiness checks and launch of the user's already-installed local AI services."""
from __future__ import annotations
import os, shutil, subprocess, sys, time
from pathlib import Path
from typing import Callable
import requests

DEFAULT_COMFY = Path(r"D:\ComfyUI_NEW\ComfyUI")

def _matching_process(needles: tuple[str, ...]) -> bool:
    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                cmd=" ".join(proc.info.get("cmdline") or []).casefold()
                if all(n.casefold() in cmd for n in needles): return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        if os.name == "nt":
            query="Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"
            result=subprocess.run(["powershell","-NoProfile","-Command",query],capture_output=True,text=True,timeout=15)
            text=result.stdout.casefold()
            return all(n.casefold() in text for n in needles)
    return False

def _wait(check: Callable[[], bool], label: str, timeout: int) -> None:
    deadline=time.monotonic()+timeout; last=None
    while time.monotonic()<deadline:
        try:
            if check(): return
        except Exception as exc: last=exc
        time.sleep(3)
    raise RuntimeError(f"{label} did not become ready within {timeout}s"+(f": {last}" if last else ""))

def ensure_comfy(server: str="http://127.0.0.1:8188", install: str|Path=DEFAULT_COMFY, timeout: int=300) -> dict:
    def ready():
        r=requests.get(server.rstrip("/")+"/system_stats",timeout=5); r.raise_for_status(); return bool(r.json().get("system"))
    try:
        if ready(): return {"status":"reused","server":server}
    except Exception: pass
    root=Path(install).resolve(); exe=root.parent/"python_embeded"/"python.exe"; main=root/"main.py"
    if not exe.is_file() or not main.is_file(): raise RuntimeError(f"Configured ComfyUI installation is incomplete: {root}")
    if not _matching_process(("ComfyUI\\main.py",)):
        args=[str(exe),str(main),"--windows-standalone-build","--fp8_e4m3fn-text-enc","--disable-pinned-memory"]
        flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
        subprocess.Popen(args,cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         creationflags=flags,close_fds=True)
        launched=True
    else: launched=False
    _wait(ready,"ComfyUI",timeout)
    return {"status":"launched" if launched else "reused_existing_process","server":server}

def ensure_ollama(endpoint: str="http://127.0.0.1:11434", model: str="qwen3-vl:30b", timeout: int=120) -> dict:
    base=endpoint.rstrip("/").removesuffix("/v1")
    def ready():
        r=requests.get(base+"/api/tags",timeout=5); r.raise_for_status()
        models={str(x.get("name")) for x in r.json().get("models",[])}
        if model not in models: raise RuntimeError(f"Required Ollama model {model} is not installed; available: {', '.join(sorted(models))}")
        return True
    try:
        if ready(): return {"status":"reused","model":model}
    except RuntimeError as exc:
        if "not installed" in str(exc): raise
    except Exception: pass
    exe=shutil.which("ollama")
    if not exe: raise RuntimeError("Ollama is offline and the existing ollama executable is not on PATH")
    if not _matching_process(("ollama", "serve")):
        flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
        subprocess.Popen([exe,"serve"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         creationflags=flags,close_fds=True)
        launched=True
    else: launched=False
    _wait(ready,"Ollama with the configured vision model",timeout)
    return {"status":"launched" if launched else "reused_existing_process","model":model}
