# Local human approval dashboard

Start the dashboard from the repository root on Windows:

```powershell
.\run_human_approval_dashboard.ps1
```

It serves `http://127.0.0.1:8765/` and binds only to loopback. Use `-Product` and `-RunId` to open another existing factory run. The dashboard reads the run's manifest, candidate state and history, package outputs, and QA report. Human decisions are stored separately in `review_runs/<run-id>/dashboard_state.json`; the original candidate archive and final-deliverables folder are kept intact.

Keep marks one selected page approved. Reject / Regenerate queues only rejected scenes; the existing worker appends candidates and arbitrates ComfyUI generation and Ollama visual review sequentially. New candidates return to human review. Replace with candidate is offered only for alternatives with a current-policy vision acceptance and passing image QA; it requires fresh human approval and makes package QA stale. Needs changes preserves a note and holds product approval.

After all 15 selected pages have explicit human approval, **Rebuild package & run QA** creates a new uniquely named package folder under the factory run. This avoids overwriting the current ready-to-review package. The package builder verifies both PDFs and outputs the PNG archives, listing images, listing copy, and QA report. **Approve whole product** becomes available only when all pages remain approved and package QA matches the selected page files. Approval is a local record only. This tool has no upload or publish endpoint; `published` remains false.

The current implementation uses the factory's existing OpenAI-compatible Ollama vision reviewer and the product worker's sequential GPU release flow. A future local coding-agent adapter can use a localhost OpenAI-compatible chat-completions endpoint configured independently from the visual reviewer. If agent orchestration is added, it should acquire the same explicit GPU scheduling/resource interface before loading a model, so it cannot contend with FLUX.2 or Qwen3-VL. Neither adapter nor coding-agent feature is required by this approval UI.
