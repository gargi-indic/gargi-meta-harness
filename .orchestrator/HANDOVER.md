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

## State (Handover 2026-10-24, end of seventh orchestrator session)
**Done:**
- **M0, M1, M2, M3:** complete (epics #2, #10, #22, #33 closed).
- **M3 merged:** #34 (PR #91), #35 (#92), #36 (#93), #39 (#94), #38 (#95), #37 (#96), #40 (#97), #41 (#98), #42 (#99). Jules built #35, #38, #39; Sonnet built #34, #36, #37, #40, #41, #42 and did the fix rounds. `main` CI is green (1407 tests, web checks).
- **Binding specs:** `.orchestrator/specs/` m23..m42.

**Waiting on the human:**
- **#44 M4.1 plan gate is `needs-discussion`.** Escalation is on issue #1 (comment `**2026-10-24 · #42 merged; Epic #33 (M3) complete`). Issue's proposed default: approved when EITHER the label `harness:plan-approved` is on the control issue (set by repo owner/allowlisted user) OR the PR that last modified the plan doc is merged; bound to the plan doc blob sha, stale if the doc changes. Do not start #44 or #50 until the human answers "accept default" or an alternative.
- **M4 not started.** The M4 build-order table is in the #1 body (#44 ⛔, #45-#50 ⏳). #45 (decomposer), #47 (issue creation) and #46 do not depend on #44; only #50 does. Ask the human whether to proceed with #45 before the decision on #44.
- M5 (#51): #52, #53 are also `needs-discussion`.

**Running:** nothing. No Jules session, no background wait, no cron job.

## How to Resume
1. Boot per the skill (section 0 and 7): `gh` only via `bash -ic '...'`; `gh issue view` is broken, use `gh api`; the orchestrator's comments are posted from the human's account, so a human reply is a comment NOT starting with `**20`.
2. Read #1 and check for the human's answer on #44 (and whether to start #45).
3. Jules stalled on 4 of the last 6 tasks: for new tasks write the binding spec first (facts verified against `git show origin/main:<path>`; the issue texts are partly stale), then delegate to a Sonnet subagent with a fresh scratchpad clone, or use Jules only with plan polling every 2-3 minutes. Review every PR with `gh pr view --json files` and the diff before merging; check for stray root files and for pre-existing test edits.
