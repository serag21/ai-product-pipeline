# AI Product Pipeline

Local-first automation for AI-assisted digital products, starting with printable coloring products.

## Current milestone

Prove this end-to-end:

ComfyUI -> local API -> Python worker -> PNG

The first smoke test patches the prompt, seed, resolution, scheduler steps, guidance, and output prefix, waits for ComfyUI to finish, and downloads the result.

## Local setup

Clone:

```powershell
git clone https://github.com/serag21/ai-product-pipeline.git
cd ai-product-pipeline
```

Run the setup script:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

Start the existing ComfyUI installation separately. The pipeline expects:

```text
http://127.0.0.1:8188
```

Then, in ComfyUI, open the Flux.2 workflow and use:

**File -> Export Workflow (API)**

Save the API-format JSON as:

`workflows/flux2_coloring_api.json`

Finally run:

```powershell
.\.venv\Scripts\python.exe scripts\test_comfy.py
```

The first test generates one image at 1536x1984 by default. This is intentionally a test resolution with the final 8.5x11 / 300-DPI packaging step kept separate.

## Design principles

- Reuse existing tools, APIs, and open-source implementations before writing custom infrastructure.
- Keep LLMs out of the long-running GPU generation loop.
- Use deterministic local Python orchestration for production jobs.
- Keep marketplace publishing human-controlled.
- Add research, prompt planning, visual QA, packaging, and listing automation only after the generation foundation is proven.

## License

This repository currently uses AGPLv3 because the initial ComfyUI client was adapted from the AGPLv3-licensed OpenMontage ComfyUI client. See `docs/open-source-reuse.md` and `LICENSE`.