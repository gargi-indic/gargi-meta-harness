"""The executor contract.

An executor performs one node's work for one step. The runner calls ``advance`` repeatedly: a ``Waiting``
result means "come back after ``poll_after`` seconds", a ``Done`` result carries the outcome (a port name)
that the runner routes on.

Executors must be idempotent. ``advance`` may be called again with the same step after a crash, and the
``ref`` returned last time is in ``step.ref``: use it to resume, never to restart. Executors never write
``set_labels``, ``edit_issue_body`` or ``upsert_ledger`` to the outbox; the runner owns those.
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
    """Not finished: poll again after ``poll_after`` seconds.

    ``ref`` and ``output`` replace the step's stored values when given (None keeps the stored value).
    ``output={"needs_you": True, "reason": ..., "links": [...]}`` asks the runner to put the run in
    ``needs-you`` and post one "needs you" comment.
    """

    ref: dict[str, Any] | None = None
    poll_after: float = 60.0
    output: dict[str, Any] | None = None


@dataclass
class Done:
    """Finished with ``outcome`` (a port of the node)."""

    outcome: str
    output: dict[str, Any] = field(default_factory=dict)


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
    # Crash hook: ``crash("name")`` raises ``SimulatedCrash`` when the runner was told to crash there.
    crash: Callable[[str], None]


class Executor(Protocol):
    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done: ...


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, kind: str, executor: Executor) -> None:
        self._executors[kind] = executor

    def get(self, kind: str) -> Executor:
        try:
            return self._executors[kind]
        except KeyError:
            known = ", ".join(sorted(self._executors)) or "(none)"
            raise KeyError(f"no executor registered for node kind {kind!r}; known kinds: {known}") from None

    def kinds(self) -> list[str]:
        return sorted(self._executors)
