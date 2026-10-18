# Automation quick start

This adds a lightweight, persistent Windows worker to the existing pipeline. It reuses the existing Python/ComfyUI scripts instead of requiring a large orchestration deployment.

## One-time setup

Run this single line in PowerShell on the PC that has ComfyUI. It updates the canonical checkout, installs a separate worker checkout on D:, installs the existing requirements, and registers a worker task that starts at login.

    Set-Location 'C:\Users\serag\source\repos\ai-product-pipeline'; git pull; powershell -ExecutionPolicy Bypass -File .\scripts\install_local_worker.ps1

The installer does not need ComfyUI to be running. Keep the PC logged in and start ComfyUI normally before generation jobs need to execute. The worker waits if ComfyUI is unavailable.

The task runs as your Windows user. Git needs to be able to push to this repository without an interactive password prompt; normal authenticated pushes from this PC should already satisfy this.

A no-GPU health check is already queued as [issue #2](https://github.com/serag21/ai-product-pipeline/issues/2). Once the worker starts, it should process that issue automatically and commit its result under test_outputs/automation_jobs/job-000002/. You do not need to create this test job yourself.

## Daily use

After setup, queue work by asking ChatGPT to create an issue in this repository with the exact job schema. You should not create the issue or copy the JSON yourself. Example request to ChatGPT:

    Queue a review run for products/little-haunts-club-monster-workshop-lab-3, generate all 3 candidates, and use automatic review sheets.

The assistant creates an open issue titled with the prefix [AI-PIPELINE JOB] and a body like this:

<job-json>
{
  "type": "generate_review_set",
  "product": "products/little-haunts-club-monster-workshop-lab-3",
  "count": 3,
  "start": 1,
  "sheet_mode": "auto"
}
</job-json>

The worker checks the queue approximately every 90 seconds. It accepts only issues authored by serag21, only allow-listed job types, only product paths under products/, and only a local ComfyUI endpoint. The JSON is treated as data, never as a shell command.

## Results and review

Review results are written to:

    test_outputs/automation_jobs/job-000123/

The actual job number replaces 000123. A result.json, selected prompt/provenance manifest, overview, and (for small sets) detail sheets are committed to main. The local full-resolution generated pages remain in the dedicated worker checkout at:

    D:\AIProductPipelineWorker\products\worker-runs\worker-job-123\pages\

The assistant can inspect the published review sheets from GitHub. A generation result is not visual approval; no candidate set is automatically packaged or published.

## Packaging gate

A second allow-listed job type, package_approved_product, exists for an already-curated final product only. It requires both owner_approved: true and selection_confirmed: true, a complete set of generated pages, and passing generation QA when a generation manifest exists. The request must include product and title; optional fields include brand and listing_title.

Do not point this job at a variant lab or candidate set. First create a final manifest with one accepted illustration per intended scene. Packaging produces print files locally under that product's final/ folder; only its listing draft and package QA report are published to test_outputs. It does not create or publish an Etsy listing.

## Logs and control

- View logs: Get-Content "$env:LOCALAPPDATA\AIProductPipelineWorker\worker.log" -Tail 60
- Stop worker: Stop-ScheduledTask -TaskName 'AI Product Pipeline Worker'
- Start worker: Start-ScheduledTask -TaskName 'AI Product Pipeline Worker'
- Task status: Get-ScheduledTask -TaskName 'AI Product Pipeline Worker' | Select-Object TaskName, State

Worker state and logs live outside the public repository under %LOCALAPPDATA%\AIProductPipelineWorker.

## Current boundaries

Implemented in this quick-start slice: persistent polling, owner-filtered issue triggers, a validated local job schema, isolated ComfyUI candidate generation, bounded generation and push retries, review sheets, local state/logs, and the approval gate for packaging an already-curated product.

Not yet automated by this worker: building a final book from choices across multiple candidate labs; semantic image review/repair; Etsy OAuth, draft creation/file upload/publish; marketing attribution; and KDP submission. Etsy API automation requires one-time Etsy developer-app/shop authorization. The worker's first real end-to-end run on your Windows/ComfyUI machine is still needed to confirm local behavior and authentication; don't describe the whole business as autonomous until that validation and the remaining stages pass.
