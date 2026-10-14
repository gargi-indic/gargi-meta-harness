# Gargi Meta Harness

A local orchestrator that drives coding agents (Jules, Claude Code, Codex, Copilot and Antigravity) through playbooks you design. GitHub issues hold the state, and a localhost web command center shows and steers the work.

- **Design and build plan:** [`docs/PLAN.md`](docs/PLAN.md)
- **Backlog:** GitHub issues. Each milestone (M0–M5) is an epic, with its tasks attached as sub-issues; `blocked by` links set the build order.
- **How it's being built:** an orchestrator agent assigns sub-issues to Jules one at a time, reviews each plan and PR, and merges. See the pinned *Orchestrator Logs* issue.
