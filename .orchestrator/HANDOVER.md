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

## State (handover 2026-10-15, first orchestrator: Claude Opus 5.5)
- **M0 is complete** (#3–#9). In M1, these are merged: #11 `5e7055d`, #12 `67cddf4` (Sonnet), #13 `80b90ac` (Sonnet), #18 `378d76f` (Sonnet). Suite: 337 tests.
- **Active:** #14 (M1.4 engine runner), Jules session `18193645226373843116`. Plan v1 was approved together with the full binding spec in `.orchestrator/specs/m14-engine.txt`. Jules was implementing at handover.
- **Resume step:** `python3 .orchestrator/jules.py get 18193645226373843116`.
  - If it's still in progress, wait.
  - If it's COMPLETED, review the PR against the issue and `specs/m14-engine.txt`; this is the most important review in M1. Run a Sonnet first-pass review plus your own probes, and check the crash matrix especially.
  - If it's stuck or FAILED, hand it to a Sonnet subagent.
- **After #14:** #15, #16, #17, #19 and #20 all depend only on #14 (and earlier merged work). Run them **two at a time**, one with Jules and one with a Sonnet subagent, on non-overlapping files. Each gets a binding spec written first; `specs/` has the pattern.
  - They must register executors per the engine's `Executor` protocol: `advance(step, node, ctx) -> Waiting | Done`.
  - They must honour the **correction convention**: outcomes that route to a revise node carry `output.correction`, and revise messages use `{{ prev.correction }}`.
  - Gates and escalate request needs-you via `Waiting(output={"needs_you": True})`.
- #21 comes last; it wires everything together and adds the runbook. Then close epic #10 and post the M1 summary on #1. The human asked for M1 to run end to end without asking them.
- **Policies in force:**
  - At most one Jules session plus one Sonnet task at a time.
  - Archive every Jules session after merge or handoff.
  - Sonnet subagents implement in a `git worktree` from the local repo, never a fresh clone, because the **disk is nearly full** (about 0.5–1.4 GB free; the space is held outside our scratch). Remove worktrees afterwards.
  - Review everything Sonnet writes yourself; for Jules PRs, use a Sonnet first pass and then verify its key findings.
- **Helpers** (moved out of the scratchpad), all under `.orchestrator/tools/`:
  - `log.py row <issue> <emoji> [session] [pr]` and `log.py current <active> <session> <pr> <next>` edit #1's tables via REST.
  - `tick.sh <issue>` ticks the boxes and closes the issue.
  - `acts.py` pretty-prints `jules.py activities` output: `python3 .orchestrator/jules.py activities <s> 2 | python3 .orchestrator/tools/acts.py`.

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
