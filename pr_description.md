Closes #38

This pull request implements the M3.5 task: Codex adapter.

Acceptance criteria met:
- [x] Fake `ProcessRunner` replays fixture transcripts; no subprocess in unit tests (one test asserts the argv list for plan and implement runs exactly, including `--output-schema` path and no shell string).
- [x] Plan emulation: valid plan JSON -> `PLAN_READY` with rendered plan; invalid JSON retried once then `FAILED`; `approve_plan` then implement prompt contains the approved plan.
- [x] Implement: replayed transcript with edits in a real temp worktree -> `PR_READY` through the publisher and respx; rerun is idempotent (no second PR).
- [x] Session id extraction works for both field names; resume run uses `codex exec resume <id>` argv.
- [x] Edits under `.harness/` result in `NEEDS_INPUT` via the publisher violation; the API key string never appears in stored activity (assert).
- [x] Cancel terminates the process once and is idempotent; non-zero exit -> `FAILED` with last stderr/error event as reason.
- [x] Adapter conformance suite from M1.3 (if present) passes (there was none to hook into explicitly here but our own tests pass the constraints).
- [x] Tests added under `tests/` (pytest, no network; GitHub mocked with respx, agents/LLMs faked) and `uv run ruff check`, `uv run ruff format --check`, `uv run pytest` are green in CI.
