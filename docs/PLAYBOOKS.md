# Built-in playbooks

Built-in playbooks live in `harness/playbooks/`. A repo can override one by shipping `.harness/playbooks/<id>.yaml`.

## tournament

Two agents implement the same issue in parallel and a judge keeps the better pull request. A `parallel` node starts
`a` (Claude, `implement`) and `b` (Codex, `implement`). A `join` with `mode: select` waits for both, asks the review
backend to pick a winner (rubric: acceptance, scope, tests, smallest diff, CI), comments on both PRs and closes the
loser. The winner then goes through `ci`, `scope`, `pr_review`, `merge_gate` (human) and `merge`, like in
`plan-build-review`.

- **Cost:** every issue runs two agents plus the judge, so expect roughly twice the agent spend of
  `plan-build-review` (the playbook budget is 40 USD). Use it for issues where the extra attempt is worth it.
- **Failures:** a branch that fails, asks a question or times out is dropped by the join and the other branch can
  still win. If neither branch produces a PR, the run escalates.
- **Fixes:** the `fix` node continues the winner's own session (`session: "{{ pick.winner }}"`).
- **Run it:** `harness run <issue> --playbook tournament`. It needs the `claude` and `codex` adapters.

## decompose-epic

Turns a plan document into an epic with sub-issues and hands them to the dispatcher. It runs on the epic (or control)
issue and costs one decomposer conversation. Nodes: `decompose` (proposes, `create: false`), `plan_gate` (the
campaign-scoped `plan` gate, human), `create` (a second `decompose` node with `create: true`) and `notify`.

- **Start:** `harness run <owner/repo> <epic issue> --playbook decompose-epic --input plan_path=docs/PLAN.md`.
  `--input KEY=VALUE` can be repeated and is used only when a new run starts; without it the playbook's own default
  `plan_path: docs/PLAN.md` applies. The plan is a repo file (`plan_path`, optionally `plan_ref`); the gate and both
  `decompose` nodes read the same inputs. The repo needs a `control_issue`: the proposal and the approval live there.
- **Flow:** `decompose` stores a proposal for the plan's sha (comments on the control issue) -> `plan_gate` waits ->
  once the plan is approved, `create` reuses the stored proposal (no second model call), creates the epic, the
  sub-issues, their sub-issue and `blocked_by` links and the labels (`harness:ready` for issues without blockers,
  `harness:blocked` for the others, plus `agent:` and `playbook:`) -> `notify` comments the count on the run's issue.
  The dispatcher then starts the ready issues as their blockers close.
- **Approval:** the gate asks once per plan sha (a comment and the `harness:needs-you` label on the control issue).
  Approve by adding the label `harness:plan-approved` to the control issue, by merging the PR that last changed the
  plan file, or with `harness plan approve`. Only the repo owner and `command_users` count. GitHub is the store: the
  approval is a record in the control issue's ledger, so a restarted harness (or a wiped database) picks up where
  it was. If the plan changes after approval the gate asks again; issues already created are not touched.
- **Rejection:** the gate can send the run back to `decompose` (at most one more time: `max_visits: 2`), with the
  rejection text available to the decomposer as `{{ prev.correction }}` (the `guidance` of the node). After that the
  run escalates, like for `invalid`, `failed` and `exhausted`. No approval signal produces a rejection yet (the
  label, the merged PR and the CLI only approve), so today the loop is reachable only once a signal for it exists.
- **Planner screen:** a draft that was edited and validated in the Planner for the same plan sha is used instead of
  asking the model again (lookup order: Planner draft, then this run's stored proposal, then the model). Such a draft
  is created under the Planner's own key, so issues the Planner already made are found again, never duplicated.
- **Crash safety:** every issue carries a `harness:planned` marker; a run repeated after a crash reuses what exists
  and creates only what is missing.
- `notify` reads `{{ create.created.issues | length }}` and `{{ create.created.epic_number }}`: the step output
  `created` is the `MaterializeResult` (`epic_number`, `issues`, `created`, `reused`, ...).
