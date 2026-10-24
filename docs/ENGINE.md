# Engine notes

## Branches and tokens (parallel / join)

- A run has one step per node visit (`{run}/{node}/{visit}`). There is no separate token object: each non-terminal
  step is a live token, and several steps of one run may be active at once. `tick()` advances every due step.
- A `parallel` node lists its targets in `branches`. The runner handles it itself (no executor): the step ends with
  outcome `forked` and one pending step is created per branch, entered through the normal routing (so `max_visits`
  and `exhausted` still apply per branch).
- Branches route into a `join` through ordinary edges. The join's inputs are its explicit `inputs`, else every node
  with an edge into it. An arrival creates the join step unless one is already active (pending/running/waiting).
  Arrivals of a step that was part of the join's last firing are absorbed, so a late `any` branch never re-fires it.
- `mode: all` waits until every input's latest step is done or failed. Output is `{"inputs": {node: output}}` when all
  succeed, or outcome `failed` (with `failed: [...]`) as soon as one input failed. When an `all` join fails, the
  branch steps still active are cancelled: their adapter sessions are cancelled via the step ref and the steps end
  `failed`/`cancelled`.
- `mode: any` fires on the first successful input. Slower branches are left running and are not cancelled; their
  results are ignored by the join.
- `mode: select` (a judge picks one branch) waits like `all` for every input, then considers only the branches that
  finished without failing and produced a PR (`output.pr`). No candidate: outcome `failed`. One candidate: it wins
  without a model call. Several: `select_candidate` (`harness/llm/selector.py`) asks the review backend, with the
  same read-only tools, for `{winner, ranking, rationale, per_candidate}`, using the built-in rubric (acceptance
  criteria, scope, tests, smallest diff, CI) plus the node's optional `rubric: [..]`. Invalid JSON is retried once.
  If the reply stays invalid or names an unknown winner, the fallback picks the candidate with passing CI, then the
  smallest diff (additions + deletions), then the alphabetically first node name.
- After choosing, the join step stores the selection in its own `output`, then comments on the winner, and for each
  loser comments (`Not selected in tournament for #N (winner: PR #M): ...`), closes the PR and, when
  `merge.delete_branch` is set, deletes its branch. Comments carry `<!-- harness:select run=<id> step=<id> -->` and
  are skipped when present, closed PRs are not closed again, so a crash between choosing and closing resumes from
  the stored selection with no second model call, comment or close.
- The select output is `{winner, winner_pr, losers: [{node, pr, branch}], pr, rationale, ranking, failed}`. `pr` is
  the winner's PR, which the runner copies into the run context, so the following `ci` / `pr_review` / `merge` nodes
  work on the winner unchanged. `JoinExecutor` gets its judge from `ExecutorRegistry.select_backend`
  (`default_registry(review_backend=...)` sets it); without one, a select join with 2+ candidates fails the step.
- `harness/playbooks/tournament.yaml` uses this: `fork` -> `a`/`b` -> `pick` (select). A node after the join continues the winner's session with `session: "{{ pick.winner }}"` (the agent executor renders `session` as a template).
- The run is done only when routing reaches `@done`; a run cancel (`Runner.cancel`) cancels every waiting session.

## Plan gate (campaign scope)

- A `gate` node with `scope: campaign` (only allowed on the gate named `plan`) waits for the approval of the campaign's plan document instead of a per-issue decision. There is no `gates` row; the step stays `waiting` with `needs_you` (inbox kind `plan_approval`) until the plan is approved. `gates.plan: auto` still approves at once.
- The plan is a repo file or an issue: the run inputs `plan_path` or `plan_issue` (and optionally `plan_ref`) name it. `PlanDoc.sha` is the git blob sha for a file and `sha256(title + "\n" + body)` for an issue.
- GitHub is the state store. The approval is a `plan` record in the control issue's ledger (`plan_sha`, `approved_by`, `via`, ...; never the plan text). It is written through the outbox (`merge_ledger`, which keeps the lock and every other ledger key) and mirrored into the local store only after GitHub has it; `harness sync` rebuilds the mirror from the ledger.
- Signals (owner or `command_users` only): the label `harness:plan-approved` on the control issue (added after the plan's last change), the merge of the PR that last modified the plan file, or `harness plan approve`. The executor turns a fresh signal into the ledger record and resolves `approved` on the next poll.
- If the plan's sha differs from the approved `plan_sha` the status is `STALE`: the gate asks again (one request comment per sha, label `harness:needs-you`) and nothing already created is cancelled. A label event is used up by the record it produced, so approving a changed plan needs a new label event (remove and re-add the label) or a merged PR for the new text.
- The inbox cannot approve a campaign plan item: it answers 422 and points to the label or the CLI.

## Creating the issues of a plan (`harness plan create`)

- `harness.planner.materialize.materialize(plan, ctx)` turns a validated `ProposedPlan` into an epic, sub-issues (attached to the epic), `blocked_by` links and labels (`agent:<x>`, `playbook:<y>`, `harness:planned`, `harness:ready` or `harness:blocked`). Issues are created blockers first.
- Every write goes through the outbox (new ops `create_issue`, `add_sub_issue`, `add_blocked_by`, `ensure_label`; the control table rows use `edit_issue_body`, the event comment `add_comment`). `Outbox.settle(op_ids)` drains through `drain_once` and sleeps while the per-minute window is full, so the throttle applies; the number and id of a created issue are read back with `Outbox.result(op_id)`.
- GitHub is the store. Each issue body ends with `<!-- harness:planned run=<run_key> key=<key> -->` (the epic uses key `@epic`). Before creating, `materialize` lists the issues labelled `harness:planned` and reuses the ones whose marker matches; the `create_issue` op repeats that search when it is sent, so an op left queued by a crash cannot duplicate an issue. Links are read first and only missing ones are added. A finished run that is repeated makes no write call.
- `run_key` is chosen by the caller (`harness plan create` derives it from the plan's sha256 unless `--run-key` is given): the same key resumes, a new key is a new materialization.
- Bodies keep the proposed text, replace its `Acceptance criteria` section by `- [ ]` boxes from `acceptance` (the merge executor ticks them), and add a `paths: a/, b.py` line that the dispatcher's `parse_path_hints` reads.
- With a control issue, one table row per sub-issue (anchored upsert) and one `**date · #<epic> planned.**` comment are added, each only if missing.

## The decompose node (`decompose`)

- Node: `{kind: decompose, plan: "docs/PLAN.md" | "#12" | "{{ inputs.plan_path }}", guidance: "<template>", create: false, max_issues: 40}`. Without `plan`, the run inputs `plan_path` / `plan_issue` / `plan_ref` name the plan, as for the plan gate. Ports: `proposed`, `created` (only when `create: true`), `invalid`, `failed` (and `exhausted` with `max_visits`). The validator requires a gate named `plan` upstream of a `create: true` node.
- `DecomposeExecutor` (`harness/engine/executors/decompose.py`, registered in `default_registry` with the review backend) loads the plan, then looks for a proposal: first the Planner's validated draft for the same plan sha (`find_planner_draft`, any run id `<repo>#planner-*`; it is copied to this run and created under `plan_run_key`), then the proposal of this run and plan sha (the mirror first, then GitHub). Only when there is none does it call `decompose()`; a node entered again after a rejection (visit 2+, `create: false`) asks again, with `guidance` rendered from `prev.correction`. A plan that changed gets a new decomposition; the same plan never asks the model twice, so a crash after the answer re-advances into the stored proposal.
- GitHub is the state store. The proposal is written through the outbox as comments on the control issue that start with `<!-- harness:proposal run=<run_id> sha=<plan_sha> part=<k>/<n> -->` (JSON in a fenced block inside `<details>`, cut into parts of at most 60 000 characters, edited in place when saved again, surplus parts retired as `part=<k>/0`). The ledger of the control issue gets a small `proposal` summary (sha, run, issue count, status, comment ids; never the proposal). The local `proposals` table is a mirror written only after GitHub took the writes; `harness sync` rebuilds it from the comments. API: `harness/planner/proposal_store.py` (`save_proposal`, `get_proposal`, `update_proposal`, `restore_proposal_mirror`).
- Step output: `proposal`, `plan_sha`, `plan`, `status`, `issue_count`, `reused` (and `draft`, the Planner run id, when a Planner draft was used), so templates can use `{{ decompose.proposal.issues | length }}`. `invalid` keeps `errors: [{code, key, message}]`; `failed` is an unreadable plan, GitHub or outbox trouble, or a backend that was down on every attempt.
- `create: true` then calls `materialize` (injectable through the executor's `materialize_fn`) with a run key derived from the run id and plan sha, and sets the proposal status to `created`. Repeating the step resumes the same creation.
- `harness/playbooks/decompose-epic.yaml` ties these together: `decompose` -> campaign `plan` gate -> `decompose` with `create: true` -> `notify` (see docs/PLAYBOOKS.md).

## The planner API (`/api/planner/*`, screen 6)

- Routes (`harness/server/routes/planner.py`, logic in `harness/server/planner_service.py`; the cookie guard applies, 503 without GitHub): `GET /plan?repo=&path=|issue=` (plan text, sha and the approval read from the plan gate; this API never approves a plan), `GET /options?repo=` (agents with `enabled` and capability names, playbook ids), `POST /decompose` (202 `{proposal_id, job_id}`; 409 while the repo has a decomposition running), `GET`/`PUT /proposals/{id}`, `POST /proposals/{id}/validate` (writes nothing), `POST /proposals/{id}/approve` (202 `{job_id, ...}`) and `GET /jobs/{id}`.
- GitHub is the state store. A proposal id is `<owner>~<name>~<token>` (run id `<owner>/<name>#planner-<token>`); the draft is a `proposal_store` proposal on the control issue, so `harness sync` restores it. Its `status` is `drafting` (a placeholder proposal until the decomposer answers), `ready` / `invalid` (the draft passes / fails `validate_proposal`), `creating` or `created`. `rev` counts changes of the draft and `meta` keeps the plan source, the decomposition errors, the creation error and the `MaterializeResult`; both are stored in the proposal envelope on GitHub (and mirrored in the `meta` key-value table, because the `proposals` table has no column for them).
- Jobs are views of the status, not of memory: `<id>.decompose` is running while `drafting`, failed when `meta` holds decomposition errors; `<id>.create` is running while `creating`, done when `created` (with the result), failed when a `ready` proposal holds the error of the last attempt. Asyncio tasks only run the work; the first read of a `drafting` or `creating` proposal after a restart starts the task again (the model is asked again; `materialize` resumes by its run key `plan_run_key`).
- Every change is first a pending draft in the local `meta` table (`planner:pending:<run id>`), saved to GitHub by one background task per proposal that always saves the newest pending draft, so rapid edits coalesce into one write. Reads answer from the pending draft (`saved: false`, `save_error` after a failed save, which is retried on later reads), `rev` only grows and `If-Match` / 412 is checked against it. The pending draft exists only in the local database until GitHub has it.
- `PUT` validates the whole draft and stores it even when invalid (a body that is not a `ProposedPlan` at all is a 422). Approve needs a clean validation, the plan approval `APPROVED` for the plan sha the proposal was made for, and the current `rev` to be the one that was validated (the optional `rev` in the body must match it); anything else is a 409 with a `reason`. All outbox use of the service is serialised, as `Outbox.drain_once` is not re-entrant.
- Every change appends a `planner_updated` event-log row, published as the SSE type `planner.updated`.
