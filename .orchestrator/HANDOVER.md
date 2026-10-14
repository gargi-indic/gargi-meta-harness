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

## State
- 2026-10-14: **M0 complete** (#3–#9 merged; epic #2 closed). M1 is in progress, starting with #11 (M1.1). The human said to continue through M1 without asking.
- The live table is in #1. Helpers in the orchestrator scratchpad aren't persisted; the commands are all in the skill.

## Lessons from this build (in addition to docs/LESSONS.md)
1. **Archive every Jules session** (`jules.py archive`) after merge or handoff. Open sessions use up Jules's concurrency limit (new sessions then sit `PAUSED`), and an abandoned FAILED session came back to life and opened a duplicate PR (#65).
2. **The plan-approval message is the main lever.** Every M0 plan was a list of intentions. Approving it together with a numbered binding spec (exact signatures, semantics, test list, references to existing modules) worked well.
3. **respx strictness:** use `pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)`. Assigning the attributes on the fixture does nothing.
4. **Tests must inject `sleep`/`clock` and use `write_spacing=0.0`** for `GitHub(...)`; otherwise tests sleep for real.
5. **Jules gets stuck on pytest-only failures** (shared state, mutable defaults). Hand off to the Sonnet fallback after two unhelpful rounds.
6. **First-pass reviews by a Sonnet subagent** (read-only, with the issue, the binding spec and a local test run) find real bugs: a wrong endpoint, a lost update, reversed NULL ordering. Always verify the key findings in the diff before sending them.
7. **`gh issue edit` (GraphQL) failed intermittently**; use REST (`gh api ... --method PATCH`) for issue body edits.
