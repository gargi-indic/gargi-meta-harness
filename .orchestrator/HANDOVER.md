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

## State (Handover 2026-10-23, end of seventh orchestrator session)
**Done:**
- **M0, M1, M2:** complete (epics #2, #10, #22 closed).
- **M3 (epic #33) merged:** #34 worktree manager (PR #91), #35 commit/push/PR helper + `harness/scope.py` (PR #92), #36 protected-path guard (PR #93), #39 dispatcher (PR #94), #38 Codex adapter (PR #95), #37 Claude adapter (PR #96). Jules built #35, #38, #39; Sonnet built #34, #36, #37 and did the fix rounds for #38, #39. `main` CI is green.
- **Binding specs:** `.orchestrator/specs/` m23..m39.

**Waiting on the human:**
- **#40 M3.7 parallel/join is `needs-discussion`.** The escalation is on issue #1 (comment starting `**2026-10-23 · #39 merged; #40 needs a decision`). The issue's proposed default: keep one step per (node, visit); a `parallel` node completes with outcome `fork` and the runner schedules every target as independent steps in the same run; a `join` step stays `waiting` until its arrival condition is met (counted per `fork_id`). Do not start #40, #41, #42 until the human answers "accept default" or gives an alternative. #41 and #42 are blocked behind #40.
- Then M4 (#43), M5 (#51). #44, #52, #53 are also `needs-discussion`.

**Running:** nothing. No Jules session is open, no background wait, no cron job should be relied on.

## How to Resume
1. Boot per the skill (section 0): `gh` only via `bash -ic '...'`; `gh issue view` is broken, use `gh api`; read #1.
2. Check whether the human replied on #1 or #40 (a comment not starting with `**20`). If they accepted the default, write a binding spec to `.orchestrator/specs/m40-parallel-join.txt` (multiple active steps per run; fork_id; join all/any; select raises NotImplementedError until M3.8; existing runner tests unchanged), start #40 on Jules, and approve its plan within 3 minutes. Consider a Sonnet subagent directly: Jules stalled on 4 of the last 6 tasks.
3. After #40: #41 (join select), #42 (branch + tournament.yaml). Then close epic #33 and add the M4 table to #1.
