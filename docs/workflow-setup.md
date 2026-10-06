# ComfyUI workflow setup

## Important: API format

The workflow pasted into the project handoff is ComfyUI's normal UI/save format (`nodes`, `links`, `widgets_values`). The local `/prompt` API expects API-format workflow JSON instead.

In ComfyUI:

1. Open the Flux.2 workflow.
2. Use **File -> Export Workflow (API)**.
3. Save the resulting JSON as `workflows/flux2_coloring_api.json`.

The API-format file should be a flat object keyed by node ID, with `class_type` and `inputs` fields.

## Expected controls

Based on the supplied UI workflow and ComfyUI's current graph-to-API serialization:

- `6` — positive prompt text
- `25` — random noise seed
- `50` / `51` — frontend PrimitiveNode width/height controls. These are virtual frontend nodes and are not sent as execution nodes.
- `47` — EmptyFlux2LatentImage receives the resolved width/height values
- `48` — Flux2Scheduler receives the resolved width/height values and steps
- `26` — Flux guidance
- `16` — sampler selection
- `9` — SaveImage output node

The smoke test validates the actual API export before submitting anything and fails clearly if the graph differs.

## Server

Start the existing ComfyUI installation normally. The pipeline expects the local server at `http://127.0.0.1:8188` by default.
