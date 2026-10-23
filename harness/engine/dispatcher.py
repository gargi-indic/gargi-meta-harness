import asyncio
import fnmatch
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from harness.config import RepoConfig
from harness.engine.model import TERMINAL_STATUSES
from harness.engine.runner import RunExists, Runner
from harness.store.db import Store
from harness.store.repo import IssueRow, RunRow


@dataclass(frozen=True)
class DispatchDecision:
    issue: int
    agent: str
    playbook: str
    reason: str


def parse_path_hints(body: str | None) -> list[str]:
    """Parse a 'paths: a/b, c/' line or a 'paths:' line followed by '- item' bullets. Strip backticks/whitespace."""
    if not body:
        return []

    hints = []
    lines = body.splitlines()
    in_paths_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.lower().startswith("paths:") and not in_paths_list:
            value = stripped[6:].strip()
            if value:
                for item in value.split(","):
                    clean = item.strip(" \t`")
                    if clean:
                        hints.append(clean)
                in_paths_list = False
            else:
                in_paths_list = True
            continue

        if in_paths_list:
            if stripped.startswith("-"):
                clean = stripped[1:].strip(" \t`")
                if clean:
                    hints.append(clean)
            else:
                in_paths_list = False

    return hints


def _paths_overlap(a: str, b: str) -> bool:
    """True if either is a prefix of the other, they are equal, or fnmatch matches either way."""
    if a == b:
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    if b.endswith("/") and a.startswith(b):
        return True
    if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    return False


def make_semaphores(caps: Mapping[str, int]) -> dict[str, asyncio.Semaphore]:
    return {name: asyncio.Semaphore(cap) for name, cap in caps.items()}


def _label_value(labels: list[str], prefix: str) -> str | None:
    for label in labels:
        if label.startswith(prefix) and len(label) > len(prefix):
            return label[len(prefix) :]
    return None


class Dispatcher:
    def __init__(
        self,
        *,
        store: Store,
        caps: Mapping[str, int],
        global_cap: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.caps = caps
        self.global_cap = global_cap
        self.clock = clock

    def plan(self, ready: Sequence[IssueRow], active: Sequence[RunRow]) -> list[DispatchDecision]:
        active_per_agent = {a: 0 for a in self.caps}

        # Track active runs per repo to check `running` state correctly
        active_issues = {(run.repo, run.issue) for run in active}

        # We need active run's issue numbers and their hints. Track per repo.
        active_hints_map: list[tuple[str, int, list[str]]] = []

        for run in active:
            agent = run.context.get("agent")
            if agent and agent in active_per_agent:
                active_per_agent[agent] += 1

            issue_rows = self.store.query(
                "SELECT body FROM issues WHERE repo = ? AND number = ?", (run.repo, run.issue)
            )
            if issue_rows:
                active_hints_map.append((run.repo, run.issue, parse_path_hints(issue_rows[0]["body"])))

        def sort_key(issue: IssueRow) -> tuple[int, int, int]:
            priority_val = _label_value(issue.labels, "priority:")
            priority = 100
            if priority_val and priority_val.isdigit():
                priority = int(priority_val)
            depth = len(issue.blocked_by)
            return (priority, depth, issue.number)

        sorted_ready = sorted(ready, key=sort_key)

        decisions: list[DispatchDecision] = []
        selected_hints_map: list[tuple[str, int, list[str]]] = []
        total_started = len(active)

        def find_overlap(repo: str, hints: list[str]) -> int | None:
            for h1 in hints:
                for r, active_issue_num, h_list in active_hints_map:
                    if r != repo:
                        continue
                    for h2 in h_list:
                        if _paths_overlap(h1, h2):
                            return active_issue_num
                for r, selected_issue_num, h_list in selected_hints_map:
                    if r != repo:
                        continue
                    for h2 in h_list:
                        if _paths_overlap(h1, h2):
                            return selected_issue_num
            return None

        # Determine which blockers are open.
        # Wait, blocked_by is a list of issue IDs or numbers. We can query the store to see if they are open.
        # But actually, the prompt says "An issue is ready if it is open, every blocker is closed".
        # Let's check `blocked` here.
        all_issues = self.store.query("SELECT number, state, repo FROM issues")
        open_issues = {(r["repo"], r["number"]) for r in all_issues if r["state"] == "open"}

        for issue in sorted_ready:
            repo_row = self.store.query("SELECT config_json FROM repos WHERE full_name = ?", (issue.repo,))
            repo_config = RepoConfig()
            if repo_row and repo_row[0]["config_json"]:
                try:
                    repo_config = RepoConfig.model_validate(
                        json.loads(repo_row[0]["config_json"])
                        if isinstance(repo_row[0]["config_json"], str)
                        else repo_row[0]["config_json"]
                    )
                except Exception:
                    pass

            agent = _label_value(issue.labels, "agent:") or repo_config.default_agent
            playbook = _label_value(issue.labels, "playbook:") or repo_config.default_playbook

            if agent not in self.caps:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "agent_disabled"))
                continue

            if (issue.repo, issue.number) in active_issues:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "running"))
                continue

            # Blocked check
            is_blocked = False
            for blocker in issue.blocked_by:
                # If blocker doesn't exist in our DB, we might treat it as open or closed.
                # Actually dag.py treats blocker as open if it's missing or state is open.
                # So if it's in open_issues OR not in all_issues.
                # Let's check if it's in open_issues.
                if (issue.repo, blocker) in open_issues or (issue.repo, blocker) not in {
                    (r["repo"], r["number"]) for r in all_issues
                }:
                    is_blocked = True
                    break
            if is_blocked:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "blocked"))
                continue

            if self.global_cap is not None and total_started >= self.global_cap:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "global_cap"))
                continue

            if active_per_agent.get(agent, 0) >= self.caps[agent]:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "cap"))
                continue

            issue_hints = parse_path_hints(issue.body)
            overlap_num = find_overlap(issue.repo, issue_hints)

            if overlap_num is not None:
                decisions.append(DispatchDecision(issue.number, agent, playbook, f"overlap:#{overlap_num}"))
                continue

            decisions.append(DispatchDecision(issue.number, agent, playbook, "started"))
            active_per_agent[agent] += 1
            total_started += 1
            selected_hints_map.append((issue.repo, issue.number, issue_hints))

        return decisions

    async def tick(
        self,
        runner: Runner,
        ready_provider: Callable[[], Awaitable[Sequence[IssueRow]]],
        resolve_playbook: Callable[[str, str], Awaitable[tuple[Any, str | None]]],
    ) -> list[DispatchDecision]:
        ready = await ready_provider()

        runs = []
        for run_dict in self.store.query("SELECT * FROM runs"):
            try:
                # context could be string if SQLite
                if isinstance(run_dict["context"], str):
                    run_dict = dict(run_dict)
                    run_dict["context"] = json.loads(run_dict["context"])
                run_row = RunRow.model_validate(run_dict)
                if run_row.status not in TERMINAL_STATUSES:
                    runs.append(run_row)
            except Exception:
                pass

        decisions = self.plan(ready, runs)

        for decision in decisions:
            if decision.reason == "started":
                issue = next(i for i in ready if i.number == decision.issue)
                playbook, playbook_sha = await resolve_playbook(issue.repo, decision.playbook)

                try:
                    runner.start_run(
                        issue.repo, issue, playbook, playbook_sha=playbook_sha, agent=decision.agent
                    )
                except RunExists:
                    pass

        return decisions
