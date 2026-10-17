# Autonomous Coloring-Book Business System

Status: implementation blueprint; do not treat future stages as working until their acceptance checks pass.

## Requirement

Routine operation must not depend on Serag manually running command chains, copying prompts, moving images, packaging files, writing listing copy, or uploading digital files. The only intended recurring human decisions are (1) visual approval/rejection of candidate and final page sheets, and (2) a final go/no-go before a listing goes live. One-time account/API onboarding is expected. No system can guarantee sales.

## Chosen architecture

1. **Persistent operator:** pilot the self-hosted M.U.S.E (Multi-Use Synaptic Entity) agent as the scheduler, task-graph orchestrator, researcher, and approval notifier. Keep the product pipeline useful without M.U.S.E; the agent is not the source of truth for generated artifacts. Pin and smoke-test an inspected version before relying on it. Its own Windows bridge guide calls native Windows early beta and recommends WSL2 as the more battle-tested Windows path. Do not run it with broad permission-bypass flags.
2. **Deterministic production engine:** retain the local Python + ComfyUI workflow under this repository. Agent tasks may prepare validated manifests and call a narrow set of scripts; they should not freestyle arbitrary shell commands for routine production. Keep seeds, manifests, QA reports, package hashes, run logs, and state transitions.
3. **Computer-local execution:** run the operator/worker persistently on the Windows PC, start it at login/startup, health-check ComfyUI, resume incomplete jobs, serialize GPU work, and retry transient failures with bounded attempts. No public inbound port is required. The PC must be powered on and the GPU/server available for image generation.
4. **Marketplace:** use Etsy's official Open API for digital listings and files. Do not screen-scrape Etsy. Create drafts automatically; publish only after the user gives the final go-ahead. Use a Seller App scoped to the owner's shop, with tokens kept in Windows Credential Manager or an environment file outside Git.
5. **KDP is a separate path:** automatically build and validate interiors/covers and prepare all metadata. KDP's published workflow requires uploading and previewing inside its website; no official public publishing API has been verified here. Do not promise unattended KDP submission until a compliant, reliable integration is proven.
6. **Operational boundary:** this repository is public. Never commit API keys, Etsy OAuth tokens, private sales data, or secret configuration. Before production launch, separate private job state, unpublished manifests, customer/sales data, and credentials from this public code repository.

## Weekly product cycle

1. Research marketplace demand and competing products using permitted public sources. Capture dated source URLs, title/price/review signals, keyword hypotheses, gaps, and confidence. Do not present a guessed keyword volume as measured data.
2. Design a coherent, original book concept and page-by-page scene map. Track subject, scene composition, motif frequency, readability, and novelty. Never copy competitor characters, artwork, titles, or branding.
3. Generate candidates via the existing FLUX.2 + ComfyUI pipeline. Create alternatives for risky scenes and preserve candidate provenance; do not quietly substitute failed images.
4. Run deterministic image/print checks plus image-capable semantic QA for every page. Detect style drift, heavy fills, malformed anatomy/props, duplicates, clipping, empty compositions, unsafe content, and scene/prompt mismatch. Retry/repair within a fixed budget, then escalate unresolved failures instead of lowering standards.
5. Present one contact sheet with labeled alternatives and concise findings. Wait at the user's visual checkpoint. Record accepted page/candidate IDs and rationale in a selection manifest.
6. Package only approved pages: US Letter/A4 PDFs, split PNG archives under Etsy file limits, listing previews, README/instructions, listing metadata, disclosure, checksums, and a machine-readable QA report.
7. Create the Etsy draft through the official API, upload files/images, validate the returned listing, then pause for the user's explicit publish approval.
8. Once live, collect available shop/listing/order metrics weekly. Keep experiments small, record changes and outcomes, and use observed results—not invented claims—to inform the next book.

## Acceptance gates before calling the system autonomous

- Startup/restart test: agent and worker recover after reboot without duplicate jobs.
- ComfyUI health test: no generation starts until the local API is healthy; queued work is serialized.
- Idempotency: retrying a job cannot duplicate pages, listings, or uploads.
- Recovery: transient network/model failures retry within a fixed budget; hard failures surface a useful alert and preserve artifacts.
- Quality: no package can be marked ready unless every required page is present, readable at print size, QA reports pass, and the visual checkpoint is approved.
- Publishing: Etsy API writes are tested first on drafts; no live publish without explicit approval.
- Resilience: complete three end-to-end test runs, including one interrupted/restarted run, before weekly unattended operation.
- Cost/safety: per-run budget, API limits, logging, least-privilege credentials, and an emergency stop are configured.

## Current work vs. target

Already present: ComfyUI API generation, image dimensions/contrast checks, contact sheets, detailed quadrant sheets, thresholded print candidates, Etsy-oriented PDFs/ZIPs/previews, listing-copy draft, upload-size checks.

Still to build and verify: persistent job scheduler/operator, evidence-backed research workflow, image-semantic QA, approval/resume state machine, Etsy OAuth/API publisher and metrics reader, automated health/recovery/alerts, and repeatable end-to-end acceptance tests.
