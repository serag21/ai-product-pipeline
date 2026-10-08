# AI Product Pipeline

Local-first automation for AI-assisted digital products, starting with printable coloring products.

## Current milestone

Prove this end-to-end:

**ComfyUI -> local API -> deterministic Python product worker -> validated PNG pages -> printable product**

The repository already contains the proven FLUX.2 Dev API workflow at `workflows/flux2_coloring_api.json` plus the multi-reference workflow.

## Local setup

Clone once:

```powershell
cd C:\Users\serag\source\repos
git clone https://github.com/serag21/ai-product-pipeline.git
cd ai-product-pipeline
```

Run setup:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

Start the existing ComfyUI installation separately. The pipeline expects:

```text
http://127.0.0.1:8188
```

The proven FLUX.2 Dev API workflow is already committed as:

```text
workflows/flux2_coloring_api.json
```

## First production product

Product manifest:

```text
products/construction-vehicles-toddler-30/prompts.json
```

The production generator deliberately overrides the workflow's UI canvas to portrait printable dimensions (1536x1984), while keeping the proven model, sampler, 20 steps, and guidance 4 settings.

Run a five-page QA batch:

```powershell
.\.venv\Scripts\python.exe scripts\generate_product.py --count 5
```

Create the visual QA sheet:

```powershell
.\.venv\Scripts\python.exe scripts\contact_sheet.py
```

Generated pages are written under:

```text
products/construction-vehicles-toddler-30/pages/
```

and QA artifacts under:

```text
products/construction-vehicles-toddler-30/qa/
```

After the five-page visual gate passes, the same generator can produce the full 30-page product:

```powershell
.\.venv\Scripts\python.exe scripts\generate_product.py --count 30
```

## Design principles

- Reuse existing tools, APIs, and open-source implementations before writing custom infrastructure.
- Keep LLMs out of the long-running GPU generation loop.
- Use deterministic local Python orchestration for production jobs.
- Keep marketplace publishing human-controlled.
- Add research, visual QA, packaging, and listing automation only after the generation foundation is proven.

## License

This repository currently uses AGPLv3 because the initial ComfyUI client was adapted from the AGPLv3-licensed OpenMontage ComfyUI client. See `docs/open-source-reuse.md` and `LICENSE`.
