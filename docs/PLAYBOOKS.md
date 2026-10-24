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
