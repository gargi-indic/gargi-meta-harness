# Orchestrator Handover: gargi-meta-harness

The live state is in GitHub issue **#1 Orchestrator Logs**. If this file and #1 disagree, #1 wins.
To resume, use the `meta-harness-orchestrator` skill (`.claude/skills/meta-harness-orchestrator/SKILL.md`).

## Where everything lives
| Thing | Location |
|---|---|
| Repo | `gargi-indic/gargi-meta-harness` (private, `main`) |
| Local clone | `/Applications/code/gargi-meta-harness` |
| Spec | `docs/PLAN.md` |
| Lessons (read before reviewing) | `docs/LESSONS.md` |
| Live log and status tables | Issue **#1** |
| Jules helper | `.orchestrator/jules.py` (stdlib; `ORCH_REPO` defaults to this repo) |
| Jules API key | `~/.config/jules/env` (`JULES_API_KEY`). Never paste it anywhere. |
| Jules source | `sources/github/gargi-indic/gargi-meta-harness` (verified visible 2026-10-14) |

## Backlog
Epics are milestones. Tasks are real GitHub sub-issues with `blocked_by` links, and every task body has Scope, Acceptance criteria and Out of scope sections.

| Epic | Milestone | Tasks |
|---|---|---|
| #2 | M0 Foundations | #3–#9 |
| #10 | M1 Engine + Jules + reviewer (CLI) | #11–#21 |
| #22 | M2 Command center + playbook editor | #23–#32 |
| #33 | M3 Local agents + parallelism | #34–#42 |
| #43 | M4 Planner | #44–#50 |
| #51 | M5 Copilot, Antigravity, ops | #52–#59 |

`needs-discussion` (each states a proposed default; escalate on reaching it unless the human has confirmed): #40 (M3.7 parallel branch semantics), #44 (M4.1 what counts as plan approval), #52 (M5.1 Copilot route), #53 (M5.2 agy spike is half-manual: the human runs the probe locally).
The UI design system is in `docs/DESIGN.md` and `docs/design/tokens.css`.

## Decisions recorded
| Date | Decision | By |
|---|---|---|
| 2026-10-14 | Own small engine (not LangGraph); Python 3.12 + uv + FastAPI + pydantic v2; Vite + React + TS + React Flow UI | human |
| 2026-10-14 | Gates configurable per playbook; visual editor in v1; local web app; act as the human's `gh` token | human |
| 2026-10-14 | v1 adapters: Jules, Claude Code, Codex, Copilot, Antigravity | human |
| 2026-10-14 | Built by Jules via this orchestrator (one session at a time); the orchestrator merges after review | human |
| 2026-10-14 | When Jules is stuck, hand the task to a Sonnet subagent (same branch, same review) instead of escalating | human |

## State (handover 2026-10-16, end of the fourth orchestrator session)
**Done:**
- M0 and M1 complete (epics #2, #10 closed). The human gave the go-ahead for M2 on 2026-10-16; the sandbox e2e (`docs/RUNBOOK.md`) is still not run: `gargi-indic/harness-sandbox` doesn't exist and `~/.config/jules/env` has no `ANTHROPIC_API_KEY`.
- **M2 merged so far:** #23 M2.1 server skeleton (#81 `d5344d5`, Sonnet, one fix round), #24 M2.2 read-only API (#82 `cc39768`, Sonnet, one fix round). Suite 879 tests; `main` CI green.
- Binding specs written: `specs/m23-server-skeleton.txt`, `m24-read-api.txt`, `m26-web-scaffold.txt`, `m27-canvas.txt`. **None yet for #25, #30, #28, #29, #31, #32.**

**Running:**
- **#26 M2.4 web scaffold on Jules**, session `4367269268796800841` (IN_PROGRESS at handover). Plan approved with `specs/m26-web-scaffold.txt` sent as binding corrections (port 7777, exact npm pins, TS 5.9.3, `scripts/export_playbook_schema.py` at the repo root, CI `web` job with `actions/setup-node@v7.1.0`). No background wait is left running.
- Resume: `python3 .orchestrator/jules.py get 4367269268796800841`; then `wait` (background), review the PR against the spec (check the pins, the design-rules test, the drift step, and that `harness serve` serves `web/dist`), merge, `archive` the session.

**Next (in order; two at a time, non-overlapping files):**
1. Write `specs/m25-action-api.txt` and `specs/m30-playbook-api.txt`, then run #25 ∥ #30 (both unblocked now; both are Python and both touch `harness/server/app.py` router registration and `deps.py`, so either run them one after the other, or tell each to touch only its own router file plus a one-line registration and expect a trivial merge). #25 must reuse the Runner's existing pause/resume/cancel/retry methods (see `harness/cli.py` `_control`) and `harness/github/commands.py` `decide`/`resolve_gate`, not re-implement them.
2. #27 canvas (spec ready) as soon as #26 merges; it can run beside #25/#30 (web only).
3. Then #28, #29 (Run view: run ids contain `/` and `#`, e.g. `acme/widgets#5@1`; the UI must `encodeURIComponent` them; routes are `{run_id:path}`), #31, #32.

**#24 API facts for the web specs:** campaign id = repo full name, `/api/campaigns/{owner}/{repo}/graph`; `/api/board?campaign=owner/repo` has a `failed` column; inbox `kind` is an open string (`plan_approval|merge_approval|gate|question|escalation|<node kind>`); SSE types `run.updated|step.updated|activity|inbox.changed|issue.updated|log`; OpenAPI at `/api/openapi.json` (behind the cookie); seed fixture `tests/fixtures/server_seed.py` (`seed_store`, `seed_config`).

**Follow-ups found (not filed):**
- From M1: `harness sync` doesn't rehydrate runs (`rehydrate` unwired); the validator accepts an empty `nodes` map; `harness init` doesn't create the control issue; no dispatcher in the daemon; the human's `~/.config/gargi-harness/config.toml` needs a manual reset.
- From M2: a malformed `events.data_json` row makes `store.repo.events_since` raise, so the SSE tailer stalls on it; `store/repo.py`'s strict row models make one corrupt `runs.context_json` / `issues.labels_json` row 500 the read API (the #24 queries are already tolerant).

**Environment:** ~2.5 GB disk free. Use `git worktree add` in the scratchpad for Sonnet builders, never `gh repo clone`; remove each worktree after merge. A `.venv` now exists in the main checkout (created by a `uv run` there).

## Lessons from this build (in addition to docs/LESSONS.md)
1. **Archive every Jules session** (`jules.py archive`) after merge or handoff. Open sessions use up Jules's concurrency limit (new ones sit `PAUSED`), and an abandoned FAILED session came back to life and opened a duplicate PR (#65).
2. **The plan-approval message is the main lever.** Approve and send a numbered binding spec (exact signatures, semantics, a test list, references to the merged modules) in the same step.
3. **respx strictness:** use `pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)`. Assigning the attributes on the fixture does nothing.
4. **Tests must inject `sleep`/`clock` and use `write_spacing=0.0`** for `GitHub(...)`; otherwise they sleep for real.
5. **Jules gets stuck on pytest-only failures** (shared state, mutable defaults). Hand off to Sonnet after two unhelpful rounds.
6. **Sonnet first-pass reviews find real bugs:** a wrong endpoint, a lost update, reversed NULL ordering, a `force-include` that broke the wheel. Always verify the key findings in the diff.
7. **`gh issue edit` (GraphQL) failed intermittently;** use REST (`gh api ... --method PATCH`).
8. **The machine can sleep mid-task.** After waking, check GitHub and Jules connectivity, then resume interrupted subagents with SendMessage (their context survives).
9. **Small, well-specified tasks on the critical path** are much faster with a Sonnet subagent (about 3–5 min) than with Jules (30–90 min). Use Jules for larger tasks while Sonnet works on independent ones.
10. **Gate merges on the exit status of `gh pr checks`**, never on piped output: `gh pr checks N | tail -1 && gh pr merge` merged a red PR (#76), because `tail` succeeds. Use `if gh pr checks N >/dev/null; then gh pr merge ...; fi`.
11. **Cross-task contracts drift between specs.** When reviewing a merged PR, re-read the pending specs that consume its outputs: the session ref shape (#14 → #15) and `head_sha` in review outputs (#20 → #19) both had to be fixed in later specs.
12. **Sonnet subagents in parallel with one Jules session work well** on non-overlapping files. M1's last 7 tasks took one session; Jules needed a Sonnet finish on #15 after two incomplete rounds.
13. **Sonnet builder + a separate Sonnet first-pass reviewer works well for server tasks.** On #23 the reviewer found the launch token in uvicorn's access log (a real blocker) and a Ctrl-C hang with open SSE streams; on #24, corrupt-JSON 500s. Each took one small fix round, about 5 min.
14. **Typer help output is ANSI-coloured on GitHub Actions** (but not locally): CLI help tests must set `NO_COLOR`/`TERM=dumb` and strip ANSI.
