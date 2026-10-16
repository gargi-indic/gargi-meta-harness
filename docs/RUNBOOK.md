# Runbook

How to set the harness up, prove it on a sandbox repo, take a repo over from the gargi-orchestrator skill, and fix the usual problems. Commands are CLI only (M1); the web UI comes in M2.

## 1. Setup

### Install

```bash
# as a tool on your PATH (from a checkout of this repo)
uv tool install .
# or from the checkout, without installing
uv sync --all-extras --dev
uv run harness --help
```

The reviewer (`review` nodes) uses the Claude Agent SDK, which is an optional extra. Install it wherever the daemon or `harness run` runs:

```bash
uv sync --extra claude          # in the checkout
uv tool install ".[claude]"
```

Without the extra everything else works; a `review` node then fails with "claude-agent-sdk is not installed", which the playbook escalates to you.

### Secrets and env files

Secrets live in env files, never in the repo or on the command line. The global config lists the files to read (default: `~/.config/jules/env`, then `~/.config/gargi-harness/env`); a variable already set in your shell wins over the files.

```bash
mkdir -p ~/.config/jules
cat > ~/.config/jules/env <<'EOF'
JULES_API_KEY=...            # registers the `jules` agent; without it only `fake` exists
ANTHROPIC_API_KEY=...        # the reviewer (Claude Agent SDK)
GH_TOKEN=...                 # optional, see below
EOF
chmod 600 ~/.config/jules/env
```

- `GH_TOKEN` (or `GITHUB_TOKEN`) is used as the GitHub token. If neither is set the harness runs `gh auth token`, so `gh auth login` is enough. The token needs `repo` scope (issues, pull requests, contents, labels).
- Logs, events, ledger comments and CLI output are redacted, but do not paste these values anywhere.
- Jules must have the repo connected as a source (the adapter uses `sources/github/<owner>/<repo>`).

### Register the repo

```bash
harness init gargi-indic/harness-sandbox
```

This adds the repo to `~/.config/gargi-harness/config.toml`, creates the `harness:*` labels (`ready`, `running`, `needs-you`, `review`, `blocked`, `done`, `failed`) and prints the default repo config. It does not create the control issue: open one yourself (title "Harness control", pinned if you like) and put its number in `.harness/config.yaml` as `control_issue`. The harness writes the status table into that issue's body between `<!-- harness:table:start -->` and `<!-- harness:table:end -->` and leaves the rest of the body alone. The run lock lives in a ledger comment on the same issue.

### `.harness/config.yaml`

Commit this to the repo's default branch. Every field is optional; these are the defaults except for `control_issue`.

```yaml
control_issue: 1              # issue holding the status table and the run lock; without it there is no lock
default_playbook: plan-build-review   # used when neither --playbook nor a playbook:<id> label is given
default_agent: jules          # used when neither --agent nor an agent:<name> label is given
base_branch: null             # null: the repo's default branch
context_files: []             # files added to the agent's brief, e.g. [CLAUDE.md, docs/ARCHITECTURE.md]
agents:                       # per-agent settings
  jules:
    enabled: true
    max_concurrency: 1
    model: null
protected_paths:              # the `scope` check fails a PR that touches these
  - .harness/
  - .claude/
  - .orchestrator/
escalate:
  labels: [needs-discussion]  # issues with these labels are not picked up
  failures: 2
merge:
  method: squash              # squash | merge | rebase
  delete_branch: true         # only for branches of the repo itself, never forks
command_users: []             # GitHub logins allowed to comment /harness commands; empty: the repo owner only
```

### Playbooks and checklists

- `.harness/playbooks/<id>.yaml` defines a playbook (schema: `harness playbook schema`; check one with `harness playbook validate <file>`). `harness run` looks the id up here first and falls back to the built-ins (`harness playbook list`).
- `.harness/checklists/<name>.md` overrides the built-in reviewer checklist of the same name (`scope`, `acceptance`, `decisions`, `existing-code`, `plan-match`, `pinned-versions`).
- Both are read from the default branch at the time a run starts or a review runs.

### Commands at a glance

| Command | What it does |
| --- | --- |
| `harness sync [repo] [--full]` | Refresh issues and the repo config in the local store. |
| `harness status [repo]` | The ready set and blockers. |
| `harness run <owner/repo> <issue> [--playbook ID] [--agent NAME] [--max-ticks N]` | Start the run (or continue the issue's open run) and drive it in the foreground. |
| `harness daemon [--force-unlock]` | Run all registered repos: runner, outbox, `/harness` comments, sync, lock heartbeats. |
| `harness runs [--repo R] [--status S]`, `harness show <run>` | List runs; show one run's steps and last events. |
| `harness gates`, `approve`, `reject`, `pause`, `resume`, `cancel`, `retry` | Controls. `approve`/`reject` take a gate id or an issue number with `--repo`. |

`harness run` exits 0 when the run is `done` or waiting for you (`needs-you`), and 1 when it `failed` or was `cancelled` (or could not start: unknown playbook, unavailable agent, closed issue). It prints one line per event, and for `needs-you` the reason and the gate id. Run it again after you resolved the gate: it prints `continuing <run id>` and carries on.

## 2. Sandbox end-to-end

Goal: prove the whole chain on a throwaway repo before any real one. Nothing here is automated; it is run by the orchestrator and a human after M1 merges.

### Prepare

1. Create a **private** repo `gargi-indic/harness-sandbox` with a `main` branch, a trivial Python package and a CI workflow that runs the tests on pull requests. Connect the repo to Jules.
2. Open the control issue (say #1) and commit `.harness/config.yaml`:

   ```yaml
   control_issue: 1
   default_agent: jules
   command_users: [<your login>]
   ```

3. Open the work issue (say #2), titled `Add slugify() with tests`:

   ```markdown
   ## Scope
   Add `slugify(text: str) -> str` to `sandbox/text.py`: lowercase, runs of non-alphanumerics become one `-`, no leading or trailing `-`.

   ## Acceptance criteria
   - [ ] `slugify("Hello, World!")` returns `hello-world`
   - [ ] `slugify("  --  ")` returns an empty string
   - [ ] tests in `tests/test_text.py` cover both, and CI is green

   ## Out of scope
   - unicode transliteration
   - any other module
   ```

4. The built-in `plan-build-review` playbook already has `gates: {plan: auto, merge: human}`. To pin it explicitly, copy it to `.harness/playbooks/plan-build-review.yaml`.
5. `harness init gargi-indic/harness-sandbox` and `harness sync`.

### Run 1: approve with the CLI

```bash
harness run gargi-indic/harness-sandbox 2
```

Expected, in order: `plan` (Jules plans) -> `plan_review` (reviewer approves) -> `plan_gate` (auto) -> `implement` (Jules opens the PR) -> `ci` -> `scope` -> `pr_review` -> `merge_gate`, where the command stops with `needs-you`, prints the gate id and exits 0. Then:

```bash
harness gates
harness approve 2 --repo gargi-indic/harness-sandbox --note "ship it"
harness run gargi-indic/harness-sandbox 2      # prints "continuing ...", merges, exits 0 at "done"
```

### Run 2: approve with a comment under the daemon

In M1 the daemon drives the runs that exist; it does not pick up new issues by itself. So start the run in the foreground first, let it do one tick, then hand over to the daemon (never have `harness run` and `harness daemon` driving the same run at once):

```bash
# open a second trivial issue (say #3, "Add `title_case()` with tests"), then:
harness run gargi-indic/harness-sandbox 3 --max-ticks 1
harness daemon                          # leave it running; Ctrl-C stops it cleanly
```

When the issue shows `harness:needs-you` (the daemon keeps polling the agent, CI and the reviewer meanwhile), comment `/harness approve ship it` on issue #3 as an allowed user. Within about 30 seconds the daemon replies "command applied", merges and closes the issue.

### What to verify (both runs)

- [ ] **Ledger**: issue has exactly one comment starting with `<!-- harness:ledger -->`, edited in place (its edit history shows many revisions), final `status: done`, and a state block that `harness sync` can parse.
- [ ] **Event comments**: `run started`, `plan ready`, `review approve`, `PR ready`, `review approve`, `needs you`, `merged`, `done` (and `command applied` for run 2), each once, and none for plain polls.
- [ ] **Labels**: `harness:running` while working, `harness:needs-you` at the gate, `harness:running` again after approval, `harness:done` at the end; never two `harness:*` labels at once.
- [ ] **Control table**: the control issue body has one row per issue with the right status (`RUNNING`, `REVIEW`, `NEEDS-YOU`, `DONE`), agent, run id and PR; the text around the table is untouched.
- [ ] **PR**: merged with a squash commit; the head sha equals the sha the review saw; one `Harness review: approve` comment on the PR; the branch is deleted.
- [ ] **Issue**: every `- [ ]` is now `- [x]`, nothing else in the body changed, and the issue is closed.
- [ ] **Jules**: one session per run, titled `#N: ... [h:<run id>/main]`; it was archived or finished, not left open.
- [ ] **Locks**: while the daemon runs, the control issue's ledger comment has `lock` with your host and pid, and its `heartbeat` moves; after Ctrl-C the `lock` key is gone.
- [ ] **Crash safety**: start a run on a new issue, `kill -9` the daemon (or the foreground `harness run`) while the agent is working, then `harness sync` and `harness daemon --force-unlock` (the dead process left its lock; it also goes stale after 15 minutes). The run continues from the local store: no second Jules session, no second PR, no repeated event comment, still one ledger comment. `harness show <run>` lists each step once per visit.

`harness sync` refreshes issues and the repo config; it does not (yet) rebuild runs from ledger comments, so it matters only for a lost local database (`~/.gargi-harness/harness.db`), where the ledger comments hold the state needed to resume.

## 3. Migrating a repo off the gargi-orchestrator skill

Never run both orchestrators on one repo: they would both start agent sessions and both write to the issue.

1. **Stop the skill.** Let nothing be mid-task: finish or park the open Jules task, then archive its Jules session (`jules.py archive <session>`) so the harness does not find it as a stray. Record in the skill's log issue that the skill is paused.
2. **Move the control issue.** The skill's log issue (gargi-ckg #67) keeps its history. Create or adopt one issue as the harness control issue: copy the status table into its body between the harness table markers (or leave it empty; rows appear as runs start). Set `control_issue` in `.harness/config.yaml`. Keep the old log issue as the archive and say so at its top.
3. **Initialise.** `harness init <owner/repo>`, commit `.harness/config.yaml` (with `context_files` for the repo's `CLAUDE.md` or equivalent and `command_users`), then `harness sync` and `harness status` to see the ready set.
4. **Take the lock.** `harness daemon` (see section 4 if it refuses). The lock is written to the control issue's ledger comment before anything else starts; from then on the skill must not be resumed.
5. **Start with one issue.** `harness run <owner/repo> <issue>`, compare the result with what the skill produced for the same kind of issue, and only then let the daemon take the rest.
6. **Roll back** by stopping the daemon (Ctrl-C releases the lock), cancelling open runs (`harness cancel <run>`), and resuming the skill.

## 4. Troubleshooting

### `lock held by <host>:<pid> since ... (last heartbeat ...)`

Another daemon (or the old skill's tooling) holds the repo's lock. If its host is alive, stop it. If it crashed, wait for the heartbeat to go stale (15 minutes) or take the lock over with `harness daemon --force-unlock`. The previous holder, if it is somehow still alive, notices at its next heartbeat (within a minute), records `lock_lost` and stops itself. A repo without `control_issue` has no lock: the daemon logs a `daemon_warning` event for it and runs anyway, so set `control_issue` before running two machines.

### Nothing is happening

- `harness runs` and `harness show <run>`: look at the active step and the last events (`tick_error`, `daemon_error`, `executor_error`, `outbox_failed` kinds).
- A `needs-you` run waits for `harness approve <issue> --repo R` / `/harness approve`, or for `harness resume <run> --to <node>` / `harness retry <run>` after an escalation.
- `agent 'jules' is not available`: `JULES_API_KEY` is not set in this process' environment or env files.
- `no GitHub token`: set `GH_TOKEN` or run `gh auth login`.

### Outbox failures

Every GitHub write goes through a local outbox and is retried with backoff (5 attempts, then `failed`). A failed write is recorded as an `outbox_failed` event on its issue: `harness run` prints it, and `harness show <run>` lists it among the last events. The rows themselves are in the `outbox` table of `~/.gargi-harness/harness.db` (`status = 'failed'`, with `last_error`; reading it with `sqlite3` is safe). `harness status` lists issues and the ready set only. Fix the cause (token scope, a deleted issue, a protected branch) and re-trigger the change that produced the write; a failed merge is reported by the merge step as `merge failed: ...` and escalated.

### Rate limits

The harness sends at most 40 writes a minute, spaces writes by one second and honours `Retry-After` and `x-ratelimit-reset`. Seeing `403`/`429` retries in the log is normal under load; repeated `outbox_failed` events with secondary-rate-limit text mean too many runs share one token: lower the number of concurrent runs.

### Slash commands are ignored

Only `command_users` (or the repo owner when the list is empty) may command; others get a "command ignored" reply. A command must be the first non-empty line of the comment and the run must still be active. Commands are read by the daemon, so with only `harness run` in the foreground they wait.
