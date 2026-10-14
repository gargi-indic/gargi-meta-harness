# Lessons learned: orchestrating coding agents

These are carried over from the gargi-ckg build, where a Claude orchestrator drove Jules through about 20 tasks. They are written down because the harness's reviewer prompts (M1.9) and the Jules adapter quirks (M1.8) must encode them. Some examples are Go-specific, but the lesson behind each one is general.

1. **Jules over-scopes plans.** In #2 its plan defined interfaces that belonged to #10/#40/#16. Always compare the plan with the issue's scope.
2. **Jules reports steps as done without following them.** In #3 its notes said "verified", but the PR contradicted the approved plan (v1 instead of v2). Review the actual diff: versions, schemas, globs.
3. **Jules can confidently assert false facts** ("golangci-lint v2 doesn't exist"). Correct it with specifics and tell it to stop and ask rather than silently change direction.
4. **No local verification is possible** (CI now covers build/lint/test). The orchestrator Mac has **no Go toolchain**, and **Docker isn't responding** (`docker info` hangs). Until CI (#5) is merged, review by reading the diff. **The Jules API does not expose command output** (activity artifacts are only `changeSet`), and Jules's progress notes aren't evidence. When proof is needed (e.g. lint/test results, a rule firing), `message` Jules asking it to reply in chat with the raw terminal output. Read it from the `agentMessaged` activity and copy it into a PR comment. Don't install tooling on the human's machine without asking.
5. GitHub blocks `--request-changes` on these PRs because Jules opens them under the human's account. Use `gh pr comment` and send the same text with `jules.py message`.
6. `jules.py wait` returns immediately while a session sits in `AWAITING_PLAN_APPROVAL`. After a `message`, use `wait-reply` (for plans and answers) or `wait-push` (for PR fixes).
7. Jules works from the `main` commit it started on. Don't push changes to `main` that touch files Jules is editing mid-task; `.orchestrator/` and `.claude/` are safe.
8. Each Jules session takes ~5–35 min. Run waits in the background and never start a second session while one is open.
9. **Jules pins GitHub Action versions from stale memory.** In #5 it chose `actions/cache@v4.1.2`, which GitHub had deprecated, so the first CI run failed at "Set up job". Verify pinned tags exist (`gh api repos/<owner>/<repo>/git/ref/tags/<tag>`) and look at the first CI run. Same for Docker image tags (`curl https://hub.docker.com/v2/repositories/<repo>/tags/<tag>`).
10. **Jules re-asks for approval.** After you `approve` a plan it sometimes posts "Does this plan look good?" and parks in `AWAITING_USER_FEEDBACK`. Answer with a short `message` ("approved, proceed"), then `wait` again.
11. **`jules.py wait` can return at once** if it reads the stale `AWAITING_PLAN_APPROVAL` state right after an approval. Re-check with `get` and restart the wait.
12. **Jules plans over-reach and reinvent things that already exist.** In #6 it planned a duplicate ADR, a Store interface plus dummy adapters (belongs to #40), and viper. Always compare the plan to what is already on `main` (`docs/adr/`, existing packages) as well as to the issue.
13. **Plan rounds are the cheap place to fix things.** Every task so far needed plan v2; most also needed one PR fix round. Put exact versions, file lists, scope limits and "do not touch" lists in the first correction message.
14. **Jules claims verification it didn't do.** In #4 it could only run `docker compose config`. Ask it to state what it did and didn't run, and flag unverified runtime behaviour to the human in #67.
15. A run of `sleep N && ...` in the Bash tool is blocked; use `run_in_background` for the helper waits and the Monitor tool (`until ...; do sleep 5; done`) to wait for CI.
16. **Put concrete signatures, semantics and an explicit do-not-add list in the plan-rejection message.** For #12 this produced a PR needing no fix round. When Jules musings leak into a plan (as in #14's wait-for-signal), decide the design yourself and tell it not to reopen it.
17. **Review against the layer above, not just the issue text:** what the engine stamps (`ExpectedVersion`), what a valid mutation is (effect-less mutations are rejected), who owns status transitions. #14's nodes violated several.
18. **`Graph` accessors (and `Registry.Types`) return fresh slices by design.** Fetch once per node, not inside inner loops (#13's FindCycle was O(d²) until fixed).
19. **Jules adds stray `// Package engine` comments to new files** (#11) and examples that are not compiled. Require exactly one package comment (doc.go) and runnable `Example` functions.
20. **Nodes/instances must not write to their own config in `Execute`** (data race on shared instances); `-race` only catches it if a test executes concurrently, so request one.
21. **Jules' completion message usually has no command output**; rely on CI and on diff review, and ask for raw output in the plan-approval message anyway.
22. **Approving a plan and sending binding clarifications in the same breath works** (approve, then `message`); Jules folds them into a v2.1 plan.
23. **Table edits in the #67 body:** `str.replace` on a row template can hit every occurrence (an early edit corrupted the Epic #1 header). Anchor on the full row and re-read the body after every edit.
24. **Verify "message sent" (and the wait started) after every `jules.py message`.** A long inline message with shell metacharacters broke once (#21's first rejection never reached Jules; it surfaced only as a failed background task). Write long messages to a file in the scratchpad and pass `"$(cat file)"`.
25. **Untrusted-input parsers need a budget for every expansion mechanism**, not just input length and depth: YAML aliases (alias bomb) in #17, plus duplicate keys that `encoding/json` silently accepts. Review parsers adversarially.
26. **Review public types for method receivers and `errors.As` targets and JSON behaviour** (#21: `Unwrap` returned values so `errors.As(*ValidationIssue)` never matched; a pointer-receiver `MarshalJSON` produced `{}` for values).
27. **Reference examples must be checked against the real implementations** (#16's example had node configs the #14 built-ins reject); keep a test that instantiates them through the registry.
28. **Errors shown to API clients must not contain Go type names** (`float64`, `map[string]interface {}`); use JSON type names.
29. **Jules's first plan is usually a bullet list of intentions; the useful lever is a complete spec in the rejection message** (files, exact signatures, error codes, test list, out-of-scope list). It keeps rounds to two (plan v2 + one PR fix round).
30. **Don't leave a background `wait` running across a handover**; record the resume command in HANDOVER §4 instead.
