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

## State (Handover 2026-10-20, end of sixth orchestrator session)
**Done:**
- **M0 and M1:** complete (epics #2, #10 closed).
- **M2 merged:**

  | Task | PR | Commit | Built by | Fix rounds |
  |---|---|---|---|---|
  | #23 server skeleton | #81 | `d5344d5` | Sonnet | 1 |
  | #24 read API | #82 | `cc39768` | Sonnet | 1 |
  | #26 web scaffold | #83 | `ffd7760` | Jules | 2, then a Sonnet pin fix |
  | #25 action API | #84 | `0cb0473` | Sonnet | 1 |
  | #30 playbook API | #85 | `fbfc9ef` | Sonnet | 1 |
  | #27 canvas | #86 | `48d5ec4` | Jules | 1 |
  | #28 screens (campaigns/board/inbox) | #87 | `7632bf6` | Jules | 1 |
  | #29 run view | #88 | `ef61d1d` | Jules | 1 |

  `main` CI is completely green (python lint & unit tests, and `web` job).
- **Binding specs:** `specs/m23..m27`, `m28-screens.txt`, `m29-run-view.txt`, `m30-playbook-api.txt`, `m31-playbook-editor.txt`.

**Running / In Progress:**
- **#31 M2.9 Playbook Editor on Jules**, session `9885139017106979480`.
  - Session URL: https://jules.google.com/session/9885139017106979480
  - State: `IN_PROGRESS` (resumed at `03:49:20Z`).
  - Plan was approved with binding spec `.orchestrator/specs/m31-playbook-editor.txt`.
  - Exact pinned packages `@rjsf/core@6.11.0`, `@rjsf/utils@6.11.0`, `@rjsf/validator-ajv8@6.11.0` were pre-installed and committed to `main` (`1dcc0f6`).
  - Jules hit a Vitest timeout on `EditorPage.test.tsx` because `POST /api/playbooks/validate` was unmocked (the debounced validator was firing 400ms after render), and `DOMMatrixReadOnly` was missing for React Flow. We messaged the exact fix to Jules, and Jules resumed coding.
  - **No background wait is left running.**

## How to Resume Tomorrow
1. Check the session status in WSL:
   ```bash
   source ~/.config/jules/env
   cd /home/gishnum/code/gargi-meta-harness
   python3 .orchestrator/jules.py get 9885139017106979480
   ```
2. If Jules has opened a PR:
   - Check if the PR created `package.json` / `package-lock.json` at repo root (a known Jules quirk). If so, checkout the PR branch, `git rm package.json package-lock.json`, commit and push to the PR branch.
   - Run tests locally in WSL: `uv run ruff check .`, `uv run pytest`, and `cd web && npm test && npm run lint && npm run typecheck`.
   - Verify CI checks: `gh pr checks <pr-number> --watch -R gargi-indic/gargi-meta-harness`.
   - Squash-merge: `gh pr merge <pr-number> -R gargi-indic/gargi-meta-harness --squash --delete-branch`.
   - Close & tick issue: `bash .orchestrator/tools/tick.sh 31`.
   - Archive session: `python3 .orchestrator/jules.py archive 9885139017106979480`.
   - Pull `main` (`git pull origin main`), update Issue #1 table and comment.
3. If Jules is still working:
   - Run the background monitor:
     ```bash
     python3 .orchestrator/jules.py wait 9885139017106979480 600
     ```
4. **Advance to #32 (M2.10 Demo seed & Playwright smoke test):**
   - Write binding spec `.orchestrator/specs/m32-demo-smoke.txt`.
   - Seed data for screens 1-5 (Campaigns, Board, Inbox, Run view, Playbook Editor).
   - Playwright smoke test covering screens 1-5 in CI.
   - Merging #32 completes Epic #22!
