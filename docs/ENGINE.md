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
- `mode: select` (a judge picks one branch) is M3.8. The validator warns (`join_select_not_implemented`) and the
  executor raises `NotImplementedError`, which fails the step.
- The run is done only when routing reaches `@done`; a run cancel (`Runner.cancel`) cancels every waiting session.
