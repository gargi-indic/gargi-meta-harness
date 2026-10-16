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

## State (handover 2026-10-15, end of the third orchestrator session)
**Done:**
- M0 complete (epic #2 closed).
- **M1 complete (epic #10 closed).** Merged: #11 `5e7055d`, #12 `67cddf4`, #13 `80b90ac`, #18 `378d76f`, #14 `7782fa2`, #17 `a1d80d1`, #20 `eb59950`, #16 `e4d2f89` (+ #77 `dd52d66` CI fix), #19 `28b83bd`, #15 `225623f`, #21 `51640d9`.
- Suite: 745 tests; `main` CI green at `51640d9`. Binding specs for every M1 task are in `.orchestrator/specs/`.

**Nothing is running:** no Jules sessions (all archived), no subagents, no worktrees, no background waits. Stale PR #72 (resurrected Jules #14 session) was closed.

**Next (needs the human's go-ahead; the M1 mandate ended here):**
1. Sandbox end-to-end per `docs/RUNBOOK.md`. This also verifies the real `claude-agent-sdk` API names in `harness/llm/backend.py` (never installed: no disk).
2. M2 (epic #22): the build order and parallel pairs are in #1's body. Write specs first (none exist for M2).

**Follow-ups found in M1 (not filed as issues):**
- `harness sync` doesn't rehydrate runs from ledger snapshots: `engine.model.rehydrate` is unwired, and repo playbooks need to be in the runner's lookup.
- The validator accepts an empty `nodes` map, and `Runner.start_run` then raises `StopIteration`.
- `harness init` doesn't create the control issue. The daemon doesn't start runs for new issues (no dispatcher).
- The human's `~/.config/gargi-harness/config.toml` holds junk written by the old `tests/test_cli.py` leak (fixed in #21). It needs a manual reset.

**Environment:** the disk is ~300 MB free (the uv cache was pruned once with `uv cache prune`). Use `git worktree add` in the scratchpad, never `gh repo clone`, and remove each worktree right after its PR is reviewed.

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
