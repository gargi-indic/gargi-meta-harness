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
| (see #1) | M2–M5 | created after M0/M1 |

## Decisions recorded
| Date | Decision | By |
|---|---|---|
| 2026-10-14 | Own small engine (not LangGraph); Python 3.12 + uv + FastAPI + pydantic v2; Vite + React + TS + React Flow UI | human |
| 2026-10-14 | Gates configurable per playbook; visual editor in v1; local web app; act as the human's `gh` token | human |
| 2026-10-14 | v1 adapters: Jules, Claude Code, Codex, Copilot, Antigravity | human |
| 2026-10-14 | Built by Jules via this orchestrator (one session at a time); the orchestrator merges after review | human |

## State
- 2026-10-14: seed pushed (README and `docs/PLAN.md`), log issue #1 created, M0 and M1 backlog created, orchestrator files committed. The next step is to start #3 (M0.1 scaffold + CI).
