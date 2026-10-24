import asyncio
import fnmatch
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from harness.config import RepoConfig, parse_repo_config
from harness.dag import blocked as open_blockers
from harness.engine.model import TERMINAL_STATUSES
from harness.engine.runner import RunExists, Runner
from harness.store.db import Store
from harness.store.repo import IssueRow, RunRow, get_repo, list_issues, list_runs


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


class StartGate(Protocol):
    def reason(self) -> str | None: ...


class Dispatcher:
    def __init__(
        self,
        *,
        store: Store,
        caps: Mapping[str, int],
        global_cap: int | None = None,
        gate: StartGate | None = None,
    ) -> None:
        self.store = store
        self.caps = caps
        self.global_cap = global_cap
        self.gate = gate

    def _repo_config(self, repo: str) -> RepoConfig:
        stored = get_repo(self.store, repo)
        try:
            return parse_repo_config(json.dumps((stored or {}).get("config") or {}))
        except ValueError:
            return RepoConfig()  # an unreadable stored config falls back to the defaults

    def plan(self, ready: Sequence[IssueRow], active: Sequence[RunRow]) -> list[DispatchDecision]:
        # Everything below is read once per repo per call, not once per issue.
        repos = {i.repo for i in ready} | {r.repo for r in active}
        configs = {repo: self._repo_config(repo) for repo in repos}
        known: dict[str, dict[int, IssueRow]] = {}
        for repo in repos:
            known[repo] = {i.number: i for i in list_issues(self.store, repo)}
            known[repo].update({i.number: i for i in ready if i.repo == repo})
        open_by_repo = {repo: open_blockers(list(issues.values())) for repo, issues in known.items()}

        active_issues = {(run.repo, run.issue) for run in active}
        active_per_agent = {name: 0 for name in self.caps}
        claimed: list[
            tuple[str, int, list[str]]
        ] = []  # (repo, issue, path hints) of active and selected work
        for run in active:
            agent = run.context.get("agent")
            if agent in active_per_agent:
                active_per_agent[agent] += 1
            issue = known[run.repo].get(run.issue)
            claimed.append((run.repo, run.issue, parse_path_hints(issue.body if issue else None)))

        def sort_key(issue: IssueRow) -> tuple[int, int, int]:
            value = _label_value(issue.labels, "priority:")
            priority = int(value) if value is not None and value.isdigit() else 100
            return (priority, len(issue.blocked_by), issue.number)

        def find_overlap(repo: str, hints: list[str]) -> int | None:
            for hint in hints:
                for other_repo, number, other_hints in claimed:
                    if other_repo == repo and any(_paths_overlap(hint, other) for other in other_hints):
                        return number
            return None

        decisions: list[DispatchDecision] = []
        total_active = len(active)
        for issue in sorted(ready, key=sort_key):
            config = configs[issue.repo]
            agent = _label_value(issue.labels, "agent:") or config.default_agent
            playbook = _label_value(issue.labels, "playbook:") or config.default_playbook

            def skip(
                reason: str, issue: IssueRow = issue, agent: str = agent, playbook: str = playbook
            ) -> None:
                decisions.append(DispatchDecision(issue.number, agent, playbook, reason))

            gate_reason = self.gate.reason() if self.gate is not None else None

            if gate_reason is not None:
                skip(gate_reason)
            elif agent not in self.caps:
                skip("agent_disabled")
            elif (issue.repo, issue.number) in active_issues:
                skip("running")
            elif open_by_repo[issue.repo][issue.number]:
                skip("blocked")
            elif self.global_cap is not None and total_active >= self.global_cap:
                skip("global_cap")
            elif active_per_agent[agent] >= self.caps[agent]:
                skip("cap")
            else:
                hints = parse_path_hints(issue.body)
                overlap = find_overlap(issue.repo, hints)
                if overlap is not None:
                    skip(f"overlap:#{overlap}")
                    continue
                skip("started")
                active_per_agent[agent] += 1
                total_active += 1
                claimed.append((issue.repo, issue.number, hints))

        return decisions

    async def tick(
        self,
        runner: Runner,
        ready_provider: Callable[[], Awaitable[Sequence[IssueRow]]],
        resolve_playbook: Callable[[str, str], Awaitable[tuple[Any, str | None]]],
    ) -> list[DispatchDecision]:
        ready = await ready_provider()
        active = [run for run in list_runs(self.store) if run.status not in TERMINAL_STATUSES]
        decisions = self.plan(ready, active)

        for decision in decisions:
            if decision.reason != "started":
                continue
            issue = next(i for i in ready if i.number == decision.issue)
            playbook, playbook_sha = await resolve_playbook(issue.repo, decision.playbook)
            try:
                runner.start_run(issue.repo, issue, playbook, playbook_sha=playbook_sha, agent=decision.agent)
            except RunExists:
                pass  # a non-terminal run appeared since the active runs were read

        return decisions
