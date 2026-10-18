# Local art review and retry

## Persistent full-product factory

The one-page-batch launcher remains useful for experiments. For a full unattended Little Haunts run, start the supervisor once:

```powershell
.\start_product_factory.ps1 -InstallAtLogon
```

It creates a per-user Windows logon task and starts a persistent supervisor for the 15-page manifest. It first waits for a real multimodal vision provider, reviews the existing page candidates, retries only weak pages through the repository's FLUX.2 ComfyUI API workflow, then creates final deliverables after every page meets the configured vision threshold. It records progress/ETA in `products/little-haunts-club-spooky-neighbors-15/factory_status.json` and logs in `factory.log`. Use `.\start_product_factory.ps1 -Foreground` to run visibly while debugging, `.\cancel_product_factory.ps1` to cancel, and `.\retry_product_factory.ps1` after fixing a `needs_attention` condition.

The supervisor starts in `waiting_for_configuration` until `products/little-haunts-club-spooky-neighbors-15/factory_config.json` exists. Copy `factory_config.example.json` there and set a provider endpoint and model. The default example targets Ollama at `http://127.0.0.1:11434/v1` with `qwen3-vl:30b`. On Windows install Ollama and download the model:

```powershell
winget install --id Ollama.Ollama --exact
ollama pull qwen3-vl:30b
```

Use `scripts/test_vision.py` to verify the endpoint against a real coloring-page image before relying on it. `unload_endpoint` calls Ollama's native API to unload Qwen3-VL after each vision request, releasing VRAM before ComfyUI generates a retry. The worker also asks ComfyUI to unload FLUX models and checks that at least 22 GiB of VRAM is free before vision inference. For another local OpenAI-compatible multimodal server, update the model ID and endpoint and remove `unload_endpoint` if it does not implement Ollama's native API. For OpenAI, set `endpoint` to `https://api.openai.com/v1`, select a vision-capable model, set `api_key_env` to `OPENAI_API_KEY`, then store that key in the current user's environment (not the JSON file):

```powershell
$secure = Read-Host 'OpenAI API key' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr); [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $key, 'User') }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); Remove-Variable key -ErrorAction SilentlyContinue }
Copy-Item .\factory_config.example.json .\products\little-haunts-club-spooky-neighbors-15\factory_config.json
# Edit factory_config.json: endpoint=https://api.openai.com/v1, model=<vision-capable model>, api_key_env=OPENAI_API_KEY
```

To test a local Ollama setup against page 1 before the persistent service begins review:

```powershell
.\.venv\Scripts\python.exe scripts\test_vision.py --page 1
```

The supervisor polls the config and Windows user environment, verifies the model list, and runs a synthetic image-input probe before product review. It continues automatically when the configuration is saved. Provider failures are logged and surfaced in status; after a bounded retry exhaustion it waits for a config fix and `.\retry_product_factory.ps1` instead of looping against a broken service. To authorize another bounded generation round after a page exhausts its attempts, raise `generation.max_attempts` (up to 5) in the local config, then request a retry.

On completion, the final run folder contains `final_deliverables` with Letter/A4 interior PDFs, page ZIPs, listing images, listing draft, package QA, sampled rendered-PDF inspection sheets, and a completion report. `READY_FOR_FINAL_HUMAN_REVIEW` is not marketplace approval; the supervisor never uploads or publishes anything.

This controller runs a selected product manifest through local ComfyUI, stores each immutable generation attempt under `products/<product>/review_runs/<run-id>/candidates/`, and writes resumable state plus structured reviews. Existing `pages/`, manifests, and prior runs are never overwritten. No marketplace publishing is performed, and human approval remains required even when a vision model accepts a page.

## Windows quick start

1. Run `.\setup.ps1` once if the project virtual environment is not set up.
2. Start your existing ComfyUI installation and confirm its API is reachable at `http://127.0.0.1:8188`.
3. From the repository root run:

   ```powershell
   .\run_local_review.ps1
   ```

The default target is the six-candidate focused art batch, with at most two attempts per page. To select manifest pages, invoke the Python worker directly, for example `.\.venv\Scripts\python.exe scripts\product_review_worker.py --pages 1 4 --run-id scarecrow-check`.

## Vision provider

Without configuration, each generated candidate is labeled `manual_review`; the worker does not claim visual inspection and creates `REVIEW.md` and `reviews.json`. For an OpenAI-compatible multimodal endpoint, set both variables before launch:

```powershell
$env:VISION_API_BASE_URL = "http://127.0.0.1:1234/v1"
$env:VISION_MODEL = "your-vision-model"
$env:VISION_API_KEY = "your-local-key" # optional for local servers
.\run_local_review.ps1
```

The endpoint must accept chat completions with an image data URI and return JSON with `decision`, `score`, `summary`, `defects`, and `retry_prompt`. On retry, the next prompt incorporates the review summary and defect list. Each retry consumes one of the bounded attempts; after the limit, the best-known candidate is retained and the record is marked `exhausted`.

## Pause, resume, and approval

Ctrl+C pauses the current page and persists state. Rerun the same command with the same `--run-id` to resume. Each attempt has its own filename, seed, prompt ID, QA result, and review record. Candidate files are never cleaned up by the controller.

After inspecting the bundle and full-resolution images, record human approval explicitly:

```powershell
.\.venv\Scripts\python.exe scripts\product_review_worker.py --run-id targeted-review --approve 1 4
```

This records human approval in local run state only. It does not assemble or publish a product. Automated review is triage; final art direction and print readiness remain human decisions.

## Offline tests

Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`. These use mocked ComfyUI and vision providers and need neither a GPU nor network access.
