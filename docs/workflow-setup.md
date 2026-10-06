# ComfyUI workflow setup

## Current default: FLUX.2 Klein 4B Base

The project now targets the **FLUX.2 [klein] 4B Base** workflow for the image-generation foundation.

Required model files:

- `flux-2-klein-base-4b.safetensors` in `ComfyUI/models/diffusion_models/`
- `qwen_3_4b.safetensors` in `ComfyUI/models/text_encoders/`
- `flux2-vae.safetensors` in `ComfyUI/models/vae/`

The official ComfyUI text-to-image template is:

https://github.com/Comfy-Org/workflow_templates/blob/main/templates/image_flux2_klein_text_to_image.json

Comfy-Org's repackaged model files are documented here:

https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b

The repository's API-format implementation is:

`workflows/flux2_klein_4b_coloring_api.json`

It is intentionally close to the existing Flux.2 API graph: UNETLoader -> CLIPLoader -> Flux2 latent -> Flux2Scheduler -> CFGGuider -> SamplerCustomAdvanced -> VAE decode -> SaveImage.

## API format

The ComfyUI UI/save workflow format contains `nodes`, `links`, and `widgets_values`. The local `/prompt` API expects API-format workflow JSON instead.

For the official UI template, use:

**File -> Export Workflow (API)**

The exported API workflow should be a flat object keyed by node ID, with `class_type` and `inputs` fields.

The repository already contains the API workflow needed for the smoke test, so no manual re-export is required for the current default path.

## Current API controls

For `flux2_klein_4b_coloring_api.json`:

- `9` — positive prompt text
- `15` — random noise seed
- `7` — EmptyFlux2LatentImage width/height
- `8` — Flux2Scheduler steps/width/height
- `14` — CFG guidance
- `16` — sampler selection
- `19` — SaveImage output

The smoke test patches these values before submission.

## Prompting

The coloring workflow uses positive-state descriptions rather than a traditional negative prompt. The current seed prompt emphasizes:

- clean black contour line art
- bold smooth outlines
- large open areas for coloring
- simple rounded shapes
- white page background
- uncluttered composition
- clear separation of subjects

This keeps the generator aligned with current FLUX.2 prompting guidance.

## Server

Start the existing ComfyUI installation normally. The pipeline expects the local server at:

`http://127.0.0.1:8188`

## Legacy workflow

`workflows/flux2_coloring_api.json` is the original FLUX.2 Dev API workflow.

It remains in the repository so we can compare the new Klein path against the known-good smoke-test graph without losing the previous benchmark.
