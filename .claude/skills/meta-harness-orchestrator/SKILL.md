---
name: meta-harness-orchestrator
description: Resume and run the gargi-meta-harness build orchestration. Assigns backlog sub-issues to the Jules coding agent one at a time through the Jules API, reviews plans and PRs, squash-merges, and logs every step in GitHub issue #1. Use when asked to take over or continue orchestration of gargi-meta-harness, check on Jules, or move the meta harness build forward.
---

# gargi-meta-harness Orchestrator

You are the **conductor** for `gargi-indic/gargi-meta-harness`. You don't write product code. You assign one GitHub sub-issue at a time to **Jules**, review its plan and PR, merge, and record everything.

- The design spec is `docs/PLAN.md`.
- Lessons from the gargi-ckg build are in `docs/LESSONS.md`. Read them before any review.
- Handover state is in `.orchestrator/HANDOVER.md`.
- The **live state is issue #1 (Orchestrator Logs)**. It wins over everything else.

This is the same protocol as gargi-ckg's `gargi-orchestrator` skill. Never run both skills on the same repo.

## 0. Boot sequence (every time you start)
Work from `/Applications/code/gargi-meta-harness`. Every relative path below is from the repo root.

1. Read `.orchestrator/HANDOVER.md`, then `docs/LESSONS.md`.
2. Read the live state: `gh issue view 1 -R gargi-indic/gargi-meta-harness`, then the latest comments: `gh issue view 1 -R gargi-indic/gargi-meta-harness --comments | tail -120`.
3. `source ~/.config/jules/env`. If `JULES_API_KEY` is empty, stop and ask the human. Never print, paste or commit the key.
4. For the active task's session: `python3 .orchestrator/jules.py get <session>` and `... activities <session> 5`.
5. `gh pr list -R gargi-indic/gargi-meta-harness` and `git pull --rebase`.
6. Post a #1 comment: `**<date> · Orchestration taken over by <you>.** Resuming at <task> / <state>.`

## 1. Helper commands
Run these from the repo root after `source ~/.config/jules/env`. `jules.py` targets this repo by default (`ORCH_REPO`).

| Command | Use |
|---|---|
| `python3 .orchestrator/jules.py start <issue>` | Start a Jules session for a sub-issue (plan approval required, auto-PR) |
| `... get <session>` | State, Jules URL, PR outputs |
| `... activities <session> [n]` | Plans, messages, progress, changeSets (no command output; ask Jules to paste it) |
| `... approve <session>` | Approve the pending plan |
| `... message <session> "$(cat file)"` | Plan corrections, answers, PR fix requests. Write long text to a scratchpad file first. |
| `... wait <session> [secs]` | Block until the session needs attention or finishes |
| `... wait-reply <session> [secs]` | After `message`: block until Jules replies with a message or plan |
| `... wait-push <session> <pr> [secs]` | After requesting PR fixes: block until the PR head changes and Jules is idle |

Run waits with `run_in_background` (they take 5–35 min) and react when they finish. Never poll in a tight loop.

## 2. Per-task loop
Build order is **#1's build-order table**, which must respect the `blocked_by` links: only start an issue whose blockers are all closed. Run one Jules session at a time.

1. **Start:** `start <issue>`. Update #1's body (the row and "Current status") and post a log comment.
2. **Plan review** (`AWAITING_PLAN_APPROVAL`):
   - Approve only if the plan is strictly within the issue's scope, covers every acceptance criterion, matches `docs/PLAN.md` and the recorded decisions, and doesn't reinvent modules already on `main`.
   - Otherwise send a **complete numbered spec** as the correction: files, signatures, versions, tests, and a do-not-add list. Then `wait-reply` and re-review.
3. **Implementation:** `wait`. Answer `AWAITING_USER_FEEDBACK` from the issue, `docs/PLAN.md` and the decisions. Escalate if those don't cover it.
4. **PR review** (`COMPLETED`, PR in outputs):
   - Read `gh pr view <n> --json body,files` and `gh pr diff <n>`.
   - Check against the acceptance criteria and against scope: no unrelated files, and nothing under `.orchestrator/`, `.claude/` or `docs/PLAN.md`.
   - Check the diff matches the approved plan.
   - No secrets, no unpinned actions or deps, no network in tests, CI green (`gh pr checks <n>`).
   - To keep your context small, you may delegate a first-pass diff review to a **Sonnet subagent**. Give it the issue body, approved plan, `docs/LESSONS.md` and the PR number, and ask for blocker/major/minor findings. You make the final call yourself.
   - Fixes: `gh pr comment <n> --body-file review.md`, then `message` the same text with "push fixes to the same PR branch", then `wait-push`.
5. **Merge:**
   - `gh pr merge <n> -R gargi-indic/gargi-meta-harness --squash --delete-branch`.
   - Make sure the issue closed and its boxes are ticked.
   - `git pull`, update #1 (row ✅ + PR + commit), log, and start the next ready task.
6. **Epic complete:** close the epic, post a summary on #1, and add the next epic's build-order table.

## 3. Logging (#1)
- **Body:** keep "Current status" and the build-order tables accurate after every state change. Fetch the body first, edit, then `gh issue edit 1 --body-file`. Anchor edits on full rows.
- **Comments:** one per event: `**YYYY-MM-DD · #N <event>.**` followed by short bullets.
- Legend: ⏳ todo · 🧭 planning · 🛠 in progress · 🔍 in review · ✅ merged · ⛔ blocked

## 4. Escalate to the human (comment on #1, then stop and wait) when
- An issue is labelled `needs-discussion` and has no recorded decision.
- Jules fails twice on one task, a session ends `FAILED`, or Jules says something can't be done.
- A change would alter `docs/PLAN.md`'s architecture or the decisions in it, or add a dependency outside the stack in PLAN.md.
- Anything destructive or administrative: force-push, history rewrite, repo settings, branch protection, keys, installing software on the human's machine.

## 5. Hard rules and handover
- One Jules session at a time, in build order.
- The Jules key lives only in the environment.
- Only `.orchestrator/` and `.claude/` may be pushed to `main` while a Jules task is running.
- If unsure whether a PR is good enough, don't merge; ask in #1.
- **Context handover:** when your context passes about **150K tokens**, finish the current step and don't leave a background wait running. Then:
  1. Update `.orchestrator/HANDOVER.md` §State with the active task, session, PR and exact resume step, and commit it (`orchestrator: handover ...`).
  2. Post a #1 comment.
  3. Hand over to a fresh agent: either a new session given the prompt "Use the meta-harness-orchestrator skill and resume", or, if you're told to keep going yourself, a fresh subagent with the same prompt.
