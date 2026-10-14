# Gargi Meta Harness: design and build plan

This is the spec for this repo. Each backlog issue references the section it implements. Where an issue and this file disagree, the issue wins, because it records a later decision.

## Context
gargi-ckg is being built by a Claude "conductor" (the `gargi-orchestrator` skill). It feeds GitHub sub-issues to Jules **one at a time**, reviews plans and PRs, merges, and logs to issue #67. That works, but:
- the protocol lives in a skill and in one session's memory;
- it drives only one agent;
- there is no UI.

The goal is a project-independent **meta harness** with four properties:
- **GitHub is the state.** Issues, sub-issues, `blocked_by` dependencies, labels, PRs and comments are canonical. The daemon can be killed and rebuilt from GitHub at any time.
- **You design how a problem is attacked.** *Playbooks* (constructs) are graphs of agent steps, reviews, checks, loops and gates. You draw them in a visual editor and they are stored as YAML in the repo.
- **Many agents behind one interface:** Jules, Claude Code, Codex, Copilot and Antigravity.
- **A local command center.** A web UI on localhost shows the issue DAG, live runs and an inbox of things waiting on you, and lets you steer.

**Decisions made:**
- Gates are configurable per playbook.
- v1 adapters: Jules, Claude Code (local), Codex, Copilot and Antigravity.
- The visual graph editor ships in v1.
- Local web app.
- Our own small engine, not LangGraph.
- Python 3.12 + uv + FastAPI + pydantic v2 backend; Vite + React + TS + `@xyflow/react` UI.
- Act as your `gh auth token` (Copilot accepts only user tokens).
- Repo: `gargi-indic/gargi-meta-harness` (private).

## Architecture

```
 you ─▶ localhost UI (React Flow) ──REST/SSE──▶ harness daemon (FastAPI + asyncio)
 you ─▶ GitHub comment "/harness approve" ─┐       │
                                           ▼       ├─ dispatcher: ready set from blocked_by DAG, per-agent concurrency caps
 GitHub (canonical state) ◀── poll w/ ETag ┤       ├─ engine: runs playbooks as reconcile loops (one per active run)
   issues · sub-issues · deps · labels     │       ├─ adapters: jules | claude | codex | copilot | antigravity | fake
   ledger comments · PRs · checks  ◀── outbox (throttled writes)
                                                   └─ SQLite: cache + outbox + event log (rebuildable via `harness sync`)
```

There are two layers, both visible in the UI:
1. **Issue DAG ("what").** A plan doc goes through a plan gate, then a decomposer turns it into an epic with sub-issues, each carrying acceptance criteria, `blocked_by` links, `agent:<x>` and `playbook:<y>` labels. The dispatcher starts a run for every *ready* issue: open, all blockers closed, not already running, and under the agent's cap.
2. **Playbook ("how").** Each run executes one playbook against one issue. A playbook can also contain a `decompose` node, which creates child issues that the dispatcher then picks up. This is how one construct can attack a whole problem.

## Playbook schema (`harness/v1`)
Built-ins ship in `playbooks/`. Repo-specific ones live in `<target repo>/.harness/playbooks/*.yaml`.

**Node kinds and their outcome ports:**

| Node kind | Ports |
|---|---|
| `agent` (`action: plan \| implement \| revise`; named `session` slot so plan, implement and fix rounds reuse one agent session) | `plan_ready`, `pr_ready`, `needs_input`, `failed`, `timeout` |
| `review` (LLM judge via Claude Agent SDK, read-only; `target: plan \| pr \| question`; `checklist` refs) | `approve`, `changes`, `escalate` |
| `check` (deterministic: `ci`, `scope` for protected paths and allowlist, `pinned_versions`, `command`) | `pass`, `fail` |
| `gate` (resolves `gates.<name>` to `human \| auto \| auto_if: <expr>`; human gates appear in the Inbox) | `approved`, `rejected` |
| `branch` (`when` expressions) | named ports |
| `parallel` / `join` (`mode: all \| any \| select`; `select` runs a judge review over the candidate PRs and closes the losers) | (not specified) |
| `decompose`, `merge` (squash, delete branch, tick acceptance boxes, update the control table), `escalate`, `notify` | (not specified) |

**Rules:**
- Any node can set `max_visits: N`. When that is exceeded, the node emits `exhausted`.
- The validator **requires** `max_visits` on every node in a cycle.
- Templating and conditions use a **Jinja2 sandbox** over `issue`, `pr`, `inputs` and the outputs of earlier nodes. That is one small language for both.
- The validator also checks that every port is wired (or covered by a `*.failed` wildcard), that every node is reachable, that every named gate is defined, and that each agent can do what the node asks. For example, Copilot has no plan step, so `action: plan` on Copilot is an error.
- The pydantic models export JSON Schema, which is generated into TS types and node inspector forms for the editor.

**Default playbook:** `playbooks/plan-build-review.yaml`, a port of SKILL.md §2.
```yaml
apiVersion: harness/v1
id: plan-build-review
gates: {plan: auto, merge: human}            # per-playbook autonomy
limits: {budget_usd: 20, timeout: 6h}
nodes:
  plan:        {kind: agent, session: main, action: plan, agent: "{{ issue.agent }}"}
  plan_review: {kind: review, target: plan, checklist: [scope, acceptance, decisions, existing-code]}
  revise_plan: {kind: agent, session: main, action: revise, message: "{{ plan_review.correction }}", max_visits: 3}
  plan_gate:   {kind: gate, gate: plan}
  implement:   {kind: agent, session: main, action: implement}
  ci:          {kind: check, check: ci}
  scope:       {kind: check, check: scope}
  pr_review:   {kind: review, target: pr, checklist: [acceptance, scope, plan-match, pinned-versions]}
  fix:         {kind: agent, session: main, action: revise, message: "{{ pr_review.correction }}", max_visits: 3}
  merge_gate:  {kind: gate, gate: merge}
  merge:       {kind: merge, method: squash}
  escalate:    {kind: escalate}
edges:
  plan.plan_ready: plan_review        ;  plan_review.approve: plan_gate   ;  plan_review.changes: revise_plan
  revise_plan.plan_ready: plan_review ;  plan_gate.approved: implement    ;  plan_gate.rejected: revise_plan
  implement.pr_ready: ci   ;  ci.pass: scope  ;  ci.fail: fix  ;  scope.pass: pr_review  ;  scope.fail: fix
  pr_review.approve: merge_gate ; pr_review.changes: fix ; fix.pr_ready: ci ; merge_gate.approved: merge
  merge_gate.rejected: fix ; "*.exhausted": escalate ; "*.failed": escalate ; "*.needs_input": escalate
```

**Tournament playbook:** `playbooks/tournament.yaml`. A `parallel` node feeds `a` (Claude `implement`) and `b` (Codex `implement`). Then a `join mode: select` judge picks one, and the winner goes through `ci`, `pr_review`, `merge_gate` and `merge`.

## Engine semantics
- **Data model** (SQLite):
  - **Run:** issue, playbook id plus git sha, status, counters, context.
  - **Step:** run, node, visit number, status (`pending`, `running`, `waiting`, `done`, `failed`), the external ref (session id, PR number or gate id), outcome, and output JSON.
- **Executors are idempotent reconcilers.** `advance(step) -> Waiting(ref, poll_after) | Done(outcome, output)`.
  - The intent is written to SQLite before any external call, and the ref is written after it.
  - Creation is idempotent:
    - Jules session titles carry `[h:<run>/<step>]`, and the adapter looks that tag up before creating a session.
    - PRs are found by the head branch `harness/<issue>-<slug>`.
    - Local agents resume by their saved session id.
- **Single asyncio process.**
  - Per-adapter semaphores enforce the caps.
  - Poll cadence comes from the adapter: Jules 60s, local process events immediately, GitHub 30–60s with ETag (304 responses are free).
- **Mirror to GitHub, through a throttled outbox.** GitHub allows about 80 content writes a minute, so ledger edits are coalesced. Four things get written:
  - One harness-owned **ledger comment** per issue, edited in place. It holds the human-readable status plus `<!-- harness:state {json snapshot} -->`.
  - One **event comment** per meaningful event, in the #67 style: `**YYYY-MM-DD · #N <event>.**` plus bullets.
  - **Labels:** `harness:ready|running|needs-you|review|blocked|done|failed`.
  - A pinned **control issue** per campaign holding the status table. The table is edited by anchored row replacement, per lesson 23.
- **`harness sync`** finds issues labelled `harness:*`, parses their ledgers, rebuilds runs and steps, and resumes reconciling. Killing the daemon at any point must not create a duplicate session, PR or comment.
- **Run lock:** a lock in the control issue's ledger stops two orchestrators (for example the old skill and the harness) from driving one repo.

## Agent adapters (`harness/adapters/`)
```python
class AgentAdapter(Protocol):
    name: str; caps: Capabilities  # plan_approval, messaging, opens_pr, local_workspace, max_concurrency
    async def start(self, req: StartRequest) -> SessionRef      # repo, base, branch, brief, mode, worktree
    async def poll(self, ref) -> AgentSnapshot                  # normalized state + new activity + plan + PR
    async def approve_plan(self, ref, note: str | None) -> None
    async def send(self, ref, text: str) -> None                # corrections, answers, PR-fix requests
    async def cancel(self, ref) -> None
```
**Normalized states:** `STARTING`, `PLANNING`, `PLAN_READY`, `WORKING`, `NEEDS_INPUT`, `PR_READY`, `FAILED`.

For agents that don't open PRs themselves, the harness commits, pushes and opens the PR. The brief is built from:
- the issue and its parent epic;
- the repo's `context_files`;
- the playbook's prompt template;
- the standing rules: stay in scope, don't touch protected paths, title the PR `#N: title`, and start the body with `Closes #N`.

| Adapter | Mechanism | Plan step | Revise |
|---|---|---|---|
| **jules** | Port of `gargi-ckg/.orchestrator/jules.py` to async httpx, with the repo configurable. Built-in quirks: auto-reply when Jules re-asks for approval (lesson 10); re-check stale state after approving (11); detect PR-head changes the way `wait-push` does (6). | Native: `requirePlanApproval` | `:sendMessage`, then watch for a reply or a push |
| **claude** | `claude-agent-sdk` `ClaudeSDKClient` in a git worktree (API key auth). Tool allowlist, `max_turns` and budget. A PreToolUse hook denies writes to protected paths; reuse the Bash-write detection in `gargi-modular-harness/gargi_code/host/hooks.py`. | `permission_mode="plan"`. The `ExitPlanMode` input is captured as the plan; approving switches to `acceptEdits`. | Resume the session with the findings |
| **codex** | `codex exec --json` subprocess in a worktree | Emulated: a read-only run with `--output-schema` producing a plan JSON | `codex exec resume <id>` |
| **copilot** | `POST /agents/repos/{o}/{r}/tasks`, or assign `copilot-swe-agent[bot]` with `custom_instructions` set to the brief. Needs the user token. | None (`caps.plan_approval=false`) | A PR comment mentioning `@copilot` |
| **antigravity** | `agy -p --output-format stream-json` under a **pty**, because it reportedly hangs without a TTY. Raise `--print-timeout`, and parse silently denied tools into `NEEDS_INPUT`. Fallback `mode: handoff`: the harness prepares the worktree and `.harness/briefs/<n>.md`, adds an Inbox item for you to start it in the IDE, and watches for the branch or PR. | Emulated, like Codex, via `--conversation` | Resume the conversation |
| **fake** | Scripted state sequences for tests and for the editor's dry run. Follows the `FakeProvider` pattern in `gargi-modular-harness/gargi_code/engine/providers/base.py`. | Scripted | Scripted |

**Reviewer (`review` node):**
- Runs Claude Agent SDK with read-only tools (`Read`, `Grep`, `Glob`, `Bash(git diff*|gh pr diff*|gh pr checks*)`) on a worktree at the PR head, and returns structured JSON: `{verdict, findings[], correction}`.
- Its system prompt generalizes HANDOVER §7. It compares the plan with the issue scope and with existing code, reviews the actual diff rather than the agent's notes, checks that pinned versions exist, and writes the `correction` as a complete numbered spec (lessons 13, 16 and 29).
- Repo checklists live in `.harness/checklists/*.md`.
- It posts its review as a PR comment, because `--request-changes` fails on PRs opened under your own account.

## Configuration
- **Per target repo, `.harness/config.yaml`:**
  - `control_issue`, `default_playbook`, `context_files` (e.g. `docs/SPEC.md`, `docs/adr/`)
  - `agents.<name>: {enabled, max_concurrency, model}`
  - `protected_paths: [.harness/, .claude/, .orchestrator/]`
  - `escalate: {labels: [needs-discussion], failures: 2}`
  - `merge: {method: squash, delete_branch: true}`
- **Global, `~/.config/gargi-harness/config.toml`:** registered repos and the port.
- **Secrets** come from env files (mode 600) or the Keychain: `JULES_API_KEY` from `~/.config/jules/env`, `ANTHROPIC_API_KEY`, `CODEX_API_KEY`. They are redacted from logs, SSE streams, ledgers and comments.
- **Slash commands** are accepted only from the repo owner or an allowlist. Issue and PR text is treated as untrusted input to the agents.

## Repo layout
```
gargi-meta-harness/
  pyproject.toml  uv.lock  .github/workflows/ci.yml
  harness/
    cli.py                 # typer: init, sync, status, next, run <issue>, gates, approve/reject, daemon, serve, playbook validate
    config.py  secrets.py
    github/                # client.py (httpx REST+GraphQL, ETag cache, rate limits), outbox.py, ledger.py, control_table.py, commands.py (slash cmds)
    store/                 # SQLite schema + migrations, repositories
    playbook/              # models.py (pydantic), validate.py, templating.py (Jinja2 sandbox), schema export
    engine/                # runner.py (reconcile loop), executors/{agent,review,check,gate,branch,parallel,join,decompose,merge,escalate,notify}.py, dispatcher.py
    adapters/              # base.py, jules.py, claude.py, codex.py, copilot.py, antigravity.py, fake.py
    workspaces.py          # git worktree create/lock/clean under ~/.gargi-harness/worktrees/<repo>/<issue>
    llm/                   # reviewer.py, decomposer.py, prompts/ (lessons, checklists)
    server/                # FastAPI app, REST /api/*, SSE /api/events, static web build
  playbooks/               # plan-build-review.yaml, tournament.yaml, handoff.yaml, decompose-epic.yaml
  web/                     # Vite + React + TS; @xyflow/react, ELK layout, TanStack Query, rjsf inspector; tokens.css copied from gargi/web/styles
  tests/                   # pytest + respx GitHub fixtures + FakeAdapter engine tests
```

**UI screens:** all in the Gargi Labs design system (`/Applications/code/gargi/DESIGN.md`: Archivo and JetBrains Mono, 0 radius, one red accent, mono status tags, no emojis).
1. **Campaigns:** the issue DAG on a React Flow canvas with ELK layout, coloured by status.
2. **Board:** Ready / Running / Needs you / Review / Blocked / Done.
3. **Inbox:** plan approvals, merge approvals with the PR, checks and findings, escalations, handoffs. Actions: approve, reject with a message, reroute to another agent.
4. **Run view:** the playbook graph with a live node overlay and visit counts, a step timeline, the SSE agent activity stream, and controls for pause, cancel, retry node and send message.
5. **Playbook editor:** the same canvas component in edit mode, with a palette, a schema-driven inspector, a validation panel, a dry run on FakeAdapter, and save, which opens a PR to `.harness/playbooks/`.
6. **Planner:** preview the plan, decompose it, edit the proposed issue DAG (draw dependency edges, set agent and playbook per issue), then approve to create everything.
7. **Agents:** health, concurrency in use, active sessions, and cost per run.

The server binds to 127.0.0.1 with a per-launch token cookie.

## Milestones: thin vertical slices
**How it is built:** an orchestrator (Claude Code, running the `meta-harness-orchestrator` skill in `.claude/skills/`) assigns the backlog sub-issues to **Jules** through the Jules API. It reviews each plan and PR, then merges, the same loop gargi-ckg uses. The live log is the pinned **Orchestrator Logs** issue. Once M1 is merged, the harness takes over its own backlog (dogfooding).

**M0 Foundations**
1. Scaffold: uv, Python 3.12, ruff, pytest, the typer entrypoint, `web/` Vite skeleton with tokens, and CI. Then `git init` and create the private `gargi-indic/gargi-meta-harness`.
2. Config and secrets models and loaders, with redaction.
3. GitHub client covering issues, comments, labels, sub-issues, `blocked_by`, PRs, checks and merge. Includes the ETag cache, rate limiting and the throttled outbox, with respx fixture tests.
4. Ledger encode/decode, the event comment format, and anchored control-table edits.
5. SQLite store and migrations.
6. `harness sync` and `harness status`: rebuild from ledgers and print the ready set.

**M1 First slice, which replaces the gargi-orchestrator skill (CLI only)**
7. Playbook models, validator and JSON Schema export.
8. Engine runner: reconcile loop, executor registry, templating, `max_visits`/`exhausted`, and a crash-recovery test.
9. Adapter protocol and FakeAdapter. Engine tests drive every path of `plan-build-review`.
10. Jules adapter (the port) with idempotent start and the quirks above.
11. Review node: Claude SDK reviewer, prompts built from the lessons, checklists, and the correction renderer.
12. Check nodes: `ci`, `scope`, `pinned_versions`.
13. Gate node: `harness gates`, `harness approve|reject`, and the `/harness approve|reject` comment parser.
14. Merge node: squash, tick the boxes, close the issue, update the control table.
15. `harness run <issue>` and `harness daemon`. Run end-to-end on the sandbox repo, then on gargi-ckg, after pausing the skill and moving #67's state into the control issue.

**M2 Command center and editor.** FastAPI, SSE, and screens 1–5. The run view and the editor share one canvas component.

**M3 Local agents and parallelism.** Worktree manager, the Claude Code and Codex adapters, a dispatcher with caps over the `blocked_by` DAG, `parallel`/`join`, and the tournament playbook.

**M4 Planner.** Plan gate, decomposer, `decompose` node, screen 6, and creating sub-issues and dependencies.

**M5 Copilot and Antigravity.** Start with a spike of `agy` in non-TTY mode, then build the adapter with the handoff fallback. Also: the agents panel and cost tracking, a global daily budget cap with a kill switch, and macOS notifications.

## Verification
- **Unit:** pytest with FakeAdapter covers every playbook path, including exhausted loops, gate rejection and agent failure. respx fixtures pin GitHub request shapes. Validator tests cover bad graphs.
- **Crash safety:** kill the daemon at each step boundary (via a test hook), run `harness sync`, and assert the run resumes with **no duplicate** sessions, PRs or comments.
- **Sandbox end-to-end:** create a private sandbox repo, `gargi-indic/harness-sandbox`, with a trivial issue ("add `slugify()` with tests"). Run `harness run 1` with Jules, `plan: auto` and `merge: human`. Then approve via the CLI, and again via a `/harness approve` comment. Check the ledger, labels, control table, merged PR and ticked boxes.
- **Real repo:** after the active gargi-ckg task finishes and the skill is paused, run gargi-ckg #18 through the harness and compare the result with the skill's #67 log.
- **UI:** check the screens in the browser pane, including the live overlay during a FakeAdapter run, approving from the Inbox, and saving an edited playbook (which opens a PR). Add a Playwright smoke test in CI.
- **M3:** run the tournament on the sandbox, with Claude and Codex in parallel worktrees, and confirm the losing PR is closed.

## Top risks and mitigations
- **GitHub write limits:** outbox throttle, coalesced ledger edits, and event comments only for meaningful events.
- **Agents touching `.harness/` or CI config:** protected paths in the brief, the `scope` check node, and a Claude PreToolUse veto.
- **Parallel worktree conflicts:** the dispatcher avoids issues with overlapping `paths:` hints and merges in DAG order. On conflict, it sends the agent a rebase request.
- **Cost runaway:** per-node and per-run budgets, `max_visits` required on every cycle, a global daily cap and a UI kill switch.
- **Unstable external APIs** (Jules is v1alpha, Copilot is in preview, Antigravity's CLI hangs without a TTY): adapters are isolated behind capability flags, with recorded-fixture contract tests and a handoff fallback.
- **Two orchestrators on one repo:** the control-issue run lock, plus an explicit migration step off the skill.
