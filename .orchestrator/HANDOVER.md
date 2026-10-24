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
- **M0, M1, M2, M3, M4:** complete (epics #2, #10, #22, #33, #43 closed).
- **M3 (PRs #91-#99), M4:** #44 plan gate (#100), #45 decomposer (#101), #47 materialize (#102), #46 decompose executor (#103), #48 planner API (#104), #50 decompose-epic playbook (#105), #49 Planner screen (#106). Jules built #35, #38, #39; Sonnet subagents built everything else. `main` CI is green (1740+ Python tests, web tests, Playwright).
- **Binding specs:** `.orchestrator/specs/` m23..m50.
- **Design rule recorded by the human:** GitHub is the state store; SQLite is only a rebuildable cache/outbox/event log. Write to GitHub first (control-issue ledger, marker comments, outbox) and mirror locally; `harness sync` must restore any new state.
- **Product direction from the human (not yet scheduled):** canvas Plan node (attach an issue, pick an agent, freeze) -> Orchestrator node -> Claude/Antigravity worker nodes with an eval-check loop back to the orchestrator; optional human gate on every node with an editable brief/scope; everything generated from the plan. Plan freeze = the plan gate approval of #44 (sha-bound). Not built: an `orchestrator` node kind, a gate on any node, editable brief/scope, plan node on the canvas.

**Waiting on the human (escalated on #1, comment `**2026-10-24 · #49 merged; Epic #43 (M4) complete`):**
- **#52 Copilot adapter:** proposed default = assignee route behind a `CopilotTransport` protocol, optional second `tasks` transport (`agents.copilot.transport`, default `assignee`).
- **#53 agy spike (half-manual):** agent writes the probe script, parser and findings template with synthetic fixtures; the human runs the probe locally (needs `agy` installed and logged in), commits real captures and fills `docs/spikes/antigravity.md`. #54 (Antigravity adapter) and #55 depend on it.
- Not blocked: #56 cost tracking, #58 notifications, then #57, #59.

**Open follow-ups:** a session chip exists for the author check of proposal comments (see `harness/planner/proposal_store.py`); slash-command plan approval; rendered markdown in the plan preview; real sandbox runs of the tournament and decompose-epic playbooks (manual, `docs/PLAN.md` "Verification").

**Running:** nothing. No Jules session, no cron job, no subagent.

## How to Resume
1. Boot per the skill (sections 0 and 7): `gh` only via `bash -ic '...'`; `gh issue view` is broken, use `gh api`; a human reply is a comment NOT starting with `**20` (the orchestrator posts from the human's account).
2. Read #1; check for answers on #52/#53. M5 order: #56, #58 can start now; #52/#53 after the decisions; #54 after real agy findings; #55; #57, #59 after #56.
3. Working method that worked in M3-M4: write a binding spec first (`.orchestrator/specs/mNN-*.txt`, facts verified with `git show origin/main:<path>` because issue texts are partly stale), push it to main from a clean clone, delegate to a Sonnet subagent with a fresh scratchpad clone (branch `claude-NN-...`, stop and report on spec conflicts), review every PR with `gh pr view --json files`, the diff and CI, check for stray root files and pre-existing test edits, then squash-merge and tick. Jules stalled on 4 of 6 tasks; use it only with plan polling every 2-3 minutes.
