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
- **M0, M1, M2, M3, M4:** complete (epics #2, #10, #22, #33, #43 closed). M3 PRs #91-#99, M4 PRs #100-#106 (#44 plan gate, #45 decomposer, #47 materialize, #46 decompose executor, #48 planner API, #50 decompose-epic playbook, #49 Planner screen). Jules built #35, #38, #39; Sonnet subagents built everything else. `main` CI is green.
- **Binding specs:** `.orchestrator/specs/` m23..m56.
- **Rules from the human:** GitHub is the state store (SQLite is only a rebuildable cache/outbox/event log: write to GitHub first via the control-issue ledger / marker comments / outbox, mirror locally, and make `harness sync` restore it). Product direction (not scheduled): canvas Plan node (attach an issue, pick an agent, freeze) -> Orchestrator node -> Claude/Antigravity worker nodes with an eval-check loop; optional human gate on every node with an editable brief/scope; generated from the plan.

**M5 (Epic #51) status:**
- **#56 M5.5 cost tracking + per-run budget: MERGED** (PR #107, 2026-10-24). Spend rides on the run ledger snapshot (`RunSnapshot.usage`), `limits.budget_usd` is enforced in the runner, `harness costs`, `/api/costs/summary`, `cost` on run detail. Known gaps: placeholder prices in `harness/ops/pricing.toml`; assumed SDK `total_cost_usd` is cumulative per session (unverified); `web/src/api/types.ts` has no `cost` field yet (needed by #59). No agent is running now.
- **Then #58 M5.7 notifications** (spec not yet written; issue body in `gh api .../issues/58`): no notification code exists; the maintainer is on WSL/Linux so `NullNotifier` is the default off darwin and `MacOSNotifier` (osascript with argv only, optional terminal-notifier) must be correct and tested with a fake subprocess runner; the `notify` executor routes through the service with idempotency key = step id; config under `[notifications]` (declare fields; `save_global` is a hand-written TOML writer; RepoConfig/AgentConfig are extra=forbid); redaction via `harness/secrets.py`; `harness notify-test`. Write the spec first (verify facts with `git show origin/main:<path>`), push it to main, delegate to a Sonnet subagent on branch `claude-58-notifications`.

**Waiting on the human (separately, do NOT start before they answer; a human reply is a comment on #1/#52/#53 NOT starting with `**20`):**
- **#52 Copilot adapter** (needs-discussion): proposed default = assignee route behind a `CopilotTransport` protocol, optional second `tasks` transport (`agents.copilot.transport`, default `assignee`); needs the user's GitHub token.
- **#53 agy spike** (needs-discussion, half-manual): an agent writes the probe script, parser and findings template with synthetic fixtures; the human runs the probe locally with `agy` installed and fills `docs/spikes/antigravity.md`. #54 and #55 depend on it.
- After #58: ask whether to continue with #57 (daily cap + kill switch, needs #56) and #59 (Agents screen, needs #56 and #57).

**Open follow-ups:** author check on planner proposal comments (a session chip was raised: `harness/planner/proposal_store.py` trusts proposal comment authors); slash-command plan approval (`/harness approve plan`); rendered markdown in the plan preview; real sandbox runs of the tournament and decompose-epic playbooks (manual, `docs/PLAN.md` "Verification").

**Running:** nothing. No Jules session, no cron job, no subagent.

## How to Resume
1. Boot per the skill (sections 0 and 7): `gh` only via `bash -ic '...'`; `gh issue view` is broken, use `gh api`; the orchestrator posts from the human's account, so identify human replies by body not starting with `**20`. The local checkout is on an old branch: always `git fetch origin` and read with `git show origin/main:<path>`; push orchestrator files only from a clean clone (`git clone` into the scratchpad, `git pull --rebase origin main`, commit, `git push origin HEAD:main`; only `.orchestrator/` and `.claude/` may go to main).
2. Read #1 (its body has the build-order tables for M3-M5 and the latest comments), then start #58 (see the M5 status above); #56 is already merged.
3. Working method that worked in M3-M4: binding spec first (`.orchestrator/specs/mNN-*.txt`, facts verified against `origin/main`, because issue texts are partly stale; correct them), delegate to a Sonnet subagent with a fresh scratchpad clone and "stop and report on spec conflicts", review every PR with `gh pr view --json files`, the diff and CI (check stray root files and pre-existing test edits), squash-merge with `gh pr merge N --squash --delete-branch`, `sh .orchestrator/tools/tick.sh N`, `python3 .orchestrator/tools/log.py row N ...`, a dated comment on #1. Jules stalled on 4 of 6 tasks: use it only with plan polling every 2-3 minutes (`JULES_API_KEY` in `~/.config/jules/env`).
