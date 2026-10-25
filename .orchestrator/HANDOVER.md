# Orchestrator Handover: gargi-meta-harness

The live state is in GitHub issue **#1 Orchestrator Logs**. If this file and #1 disagree, #1 wins.
To resume, use the `meta-harness-orchestrator` skill (`.claude/skills/meta-harness-orchestrator/SKILL.md`).

## Where everything lives
| Thing | Location |
|---|---|
| Repo | `gargi-indic/gargi-meta-harness` (private, `main`) |
| Local clone | `/home/gishnum/code/gargi-meta-harness` (WSL Ubuntu-24.04) |
| Spec | `docs/PLAN.md` |
| Lessons (read before reviewing) | `docs/LESSONS.md` |
| Live log and status tables | Issue **#1** |
| Jules helper | `.orchestrator/jules.py` (stdlib; `ORCH_REPO` defaults to this repo) |
| Jules API key | `~/.config/jules/env` (`JULES_API_KEY`). Never paste it anywhere. |
| Jules source | `sources/github/gargi-indic/gargi-meta-harness` |

## Backlog
Epics are milestones. Tasks are real GitHub sub-issues with `blocked_by` links, and every task body has Scope, Acceptance criteria and Out of scope sections.

| Epic | Milestone | Tasks |
|---|---|---|
| #2 | M0 Foundations | #3–#9 (closed) |
| #10 | M1 Engine + Jules + reviewer (CLI) | #11–#21 (closed) |
| #22 | M2 Command center + playbook editor | #23–#32 (#23–#30 done, #31 in progress, #32 remaining) |
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

## State (Handover 2026-10-25, end of eighth orchestrator session)
**Done:** M0-M4 complete. M5 (Epic #51): #52 Copilot adapter (#109), #56 cost/budget (#107), #57 daily cap + kill switch (#110, Jules scaffold + Sonnet), #58 notifications (#108), #59 Agents screen (#112, Sonnet after Jules stalled), #53 agy probe tooling (#111). Binding specs: `.orchestrator/specs/` m23..m59. `main` CI green.

**Waiting on the human (nothing else is buildable):**
- **#53 stays open:** the maintainer must run `uv run python scripts/spikes/agy_probe.py --agy "$(command -v agy)" --i-understand-this-calls-the-agent` with agy installed and logged in (spends quota), review captures for secrets, correct `AGY_EVENT_MAP`, fill `docs/spikes/antigravity.md`, commit, close #53.
- Then **#54 (Antigravity adapter)** and **#55 (handoff mode + handoff.yaml)** can start (write binding specs from the real findings; check #53 findings first). After #55 close Epic #51.

**Running:** nothing. No Jules session, no subagent, no cron job.

**Open follow-ups:** author check on planner proposal comments; slash-command plan approval; rendered markdown in the plan preview; real sandbox runs of tournament/decompose-epic; Copilot adapter: assignee-transport session that unassigns itself without a PR polls WORKING forever, old failure-marker comments mark FAILED; notifications: quiet-hours suppressed items are dropped, not deferred.

## How to Resume
1. Boot per the skill: `gh` only via `bash -ic '...'` (`gh issue view` is broken, use `gh api`); the orchestrator posts as the human's account, so identify human replies by body not starting with `**20`. Read #1's latest comments and check #53.
2. Method that works: binding spec first (facts verified on main), Sonnet subagent in a fresh scratchpad clone (Jules stalled on most tasks: use it only with plan polling every 2-3 minutes and send the spec as a message; hand to Sonnet if no PR after ~90 minutes). Jules `wait` returns early on a stale AWAITING state after you message it: poll until IN_PROGRESS.
