# Local worker request queue

The local worker watches this directory on the configured branch and accepts only a small allowlist of typed requests. Do not put credentials, secrets, private customer information, or arbitrary shell commands here; this repository is public.

The assistant/agent creates one new JSON file per job. The filename (without .json) must equal job_id. Commit the product manifest before the request file so the worker can pull both.

Example request:

```json
{
  "schema_version": 1,
  "job_id": "2026-10-17-little-haunts-review-001",
  "action": "generate_for_review",
  "product_id": "little-haunts-club-spooky-neighbors-15",
  "count": 15,
  "start": 1,
  "branch": "main",
  "server": "http://127.0.0.1:8188",
  "requested_at": "2026-10-17T22:00:00Z"
}
```

Omit `count` to generate from `start` through the end of the manifest. For a complete book, use `start: 1` and omit `count`. `count` and `start` refer to the manifest's array positions, not arbitrary page labels.

Allowed action in this first worker milestone: `generate_for_review`. It generates locally, creates a clearly labeled contact sheet and run report under `test_outputs/automated_reviews/<job_id>/`, and commits/pushes only those review files. It cannot publish marketplace listings. The output is a candidate, not approved artwork.

The worker keeps de-duplication and recovery state outside the repository in the current Windows user's LocalAppData directory. It serializes GPU jobs and never evaluates shell commands supplied through the queue.
