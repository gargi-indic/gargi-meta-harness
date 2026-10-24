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
