# AI Product Pipeline

Local-first automation for AI-assisted digital products, starting with printable coloring products.

## Current milestone

Prove this end-to-end:

ComfyUI -> local API -> Python worker -> PNG

The current production-target smoke test uses **FLUX.2 [klein] 4B Base**, a commercially usable Apache-2.0 open-weight model. The worker patches the prompt, seed, resolution, scheduler steps, guidance, and output prefix, waits for ComfyUI to finish, and downloads the result.

The original FLUX.2 [dev] workflow is still kept in the repository as a legacy/reference workflow.

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

### FLUX.2 Klein 4B model

The project targets the **FLUX.2 [klein] 4B Base** workflow:

- diffusion model: `flux-2-klein-base-4b.safetensors`
- text encoder: `qwen_3_4b.safetensors`
- VAE: `flux2-vae.safetensors`

ComfyUI's official workflow template is the source of truth for the model graph. The Comfy-Org repackaged model files place these assets under `models/diffusion_models`, `models/text_encoders`, and `models/vae`.

Official references:

- BFL FLUX.2 repo: https://github.com/black-forest-labs/flux2
- Official ComfyUI Klein template: https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_flux2_klein_text_to_image.json
- Comfy-Org model package: https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b

Then run:

```powershell
.\.venv\Scripts\python.exe scripts\test_comfy.py
```

The first test generates one image at 1536x1984 by default. This is intentionally a test resolution with the final 8.5x11 / 300-DPI packaging step kept separate.

## Workflow files

`workflows/flux2_klein_4b_coloring_api.json` is the default API workflow for the pipeline.

`workflows/flux2_coloring_api.json` remains available as the original FLUX.2 Dev smoke-test workflow.

The repository also retains the multi-reference Dev workflow as a reference for the later consistency/editing stage.

## Design principles

- Reuse existing tools, APIs, and open-source implementations before writing custom infrastructure.
- Keep LLMs out of the long-running GPU generation loop.
- Use deterministic local Python orchestration for production jobs.
- Keep marketplace publishing human-controlled.
- Add research, prompt planning, visual QA, packaging, and listing automation only after the generation foundation is proven.

## Product goals

The intended pipeline remains:

**Etsy products -> KDP books -> paid downloads -> commercial customer work**

This model swap does not change that scope; it only changes the local image-generation engine underneath it.

## License

This repository currently uses AGPLv3 because the initial ComfyUI client was adapted from the AGPLv3-licensed OpenMontage ComfyUI client. See `docs/open-source-reuse.md` and `LICENSE`.
