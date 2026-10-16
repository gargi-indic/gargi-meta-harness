"""Executor protocol, result types and registry.

An executor is an idempotent reconciler for one node kind. ``advance`` may be called again with the same
step after a crash; the ``ref`` an executor returned last time is in ``step.ref`` and must be used to resume,
never to restart. Executors never write ``set_labels`` / ``edit_issue_body`` / ``upsert_ledger`` outbox ops:
the runner owns the mirroring to GitHub.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.playbook.models import Node, Playbook
from harness.playbook.templating import Context
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow


@dataclass
class Waiting:
    """The step is not finished. ``poll_after`` is a delay in seconds, relative to the runner's clock."""

    ref: dict[str, Any] | None = None
    poll_after: float = 60.0
    output: dict[str, Any] | None = None


@dataclass
class Done:
    """The step finished with ``outcome``, which the runner routes through the playbook's edges."""

    outcome: str
    output: dict[str, Any] = field(default_factory=dict)


def _no_crash(point: str) -> None:
    return None


@dataclass
class ExecContext:
    store: Store
    outbox: Outbox | None
    gh: GitHub | None
    adapters: AdapterRegistry
    repo_config: RepoConfig
    playbook: Playbook
    run: RunRow
    template: Context
    clock: Callable[[], float]
    # Crash-injection hook: ``crash("name")`` raises SimulatedCrash when the runner was armed with that name.
    crash: Callable[[str], None] = field(default=_no_crash)


class Executor(Protocol):
    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done: ...


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, kind: str, executor: Executor) -> None:
        if kind in self._executors:
            raise ValueError(f"executor already registered for kind {kind!r}")
        self._executors[kind] = executor

    def get(self, kind: str) -> Executor:
        try:
            return self._executors[kind]
        except KeyError:
            known = ", ".join(sorted(self._executors)) or "(none)"
            raise KeyError(f"no executor registered for kind {kind!r}; known kinds: {known}") from None

    def kinds(self) -> list[str]:
        return sorted(self._executors)
