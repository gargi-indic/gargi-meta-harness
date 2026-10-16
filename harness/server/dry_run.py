"""Playbook dry run (M2.8): the real Runner on a throwaway in-memory store with every executor scripted.

The dry run never touches the app's store, the network or an agent. Design decision: it does not drive
``FakeAdapter`` (whose states only cover agent ports); every node kind gets a ``ScriptedExecutor`` instead, so
a scenario can choose any port of any node. ``FakeAdapter`` is still registered, which satisfies the Runner.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.engine.fakes import TraceEntry, scripted_registry
from harness.engine.runner import Runner
from harness.playbook.models import Playbook
from harness.store.db import Store
from harness.store.repo import IssueRow, get_run, upsert_repo

# Seconds before a dry run is abandoned; module-level so tests can monkeypatch it.
DRY_RUN_TIMEOUT_S = 10.0

DRY_REPO = "dry-run/dry-run"
_FAILURE_EVENTS = ("no_route", "executor_error", "unknown_node", "routing_loop")


class DryRunResult(BaseModel):
    terminated: Literal["done", "escalated", "max_steps", "error"] = Field(description="How the run ended")
    trace: list[TraceEntry] = Field(description="Every step that ran, in order")
    gates_auto_resolved: list[dict[str, Any]] = Field(description="Gate steps that took their default port")
    error: str | None = Field(default=None, description="Why the run ended in error, else null")


class _Clock:
    """Strictly increasing fake time, one second per reading; feeds both the Store and the Runner."""

    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self(), UTC)


def _failure_reason(store: Store, run_id: str) -> str:
    rows = store.query(
        "SELECT kind FROM events WHERE run_id = ? AND kind IN (?, ?, ?, ?) ORDER BY id DESC LIMIT 1",
        (run_id, *_FAILURE_EVENTS),
    )
    return rows[0]["kind"] if rows else "failed"


async def dry_run(
    playbook: Playbook,
    *,
    outcomes: dict[str, list[str]],
    max_steps: int = 200,
    trace: list[TraceEntry] | None = None,
    gates: list[dict[str, Any]] | None = None,
) -> DryRunResult:
    """Run ``playbook`` to a terminal state. ``trace`` and ``gates`` may be passed in to read partial results."""
    trace = trace if trace is not None else []
    gates = gates if gates is not None else []
    clock = _Clock()
    store = Store(":memory:", clock=clock.datetime)
    try:
        store.migrate()
        upsert_repo(store, DRY_REPO, "main", None)
        issue = IssueRow(
            repo=DRY_REPO,
            number=1,
            id=1,
            title="Dry run",
            body=None,
            state="open",
            labels=[],
            parent=None,
            blocked_by=[],
            updated_at="2026-01-01T00:00:00Z",
        )
        adapters = AdapterRegistry()
        adapters.register(FakeAdapter())
        runner = Runner(
            store,
            None,
            scripted_registry(playbook, outcomes, trace, gates),
            adapters,
            gh=None,
            clock=clock,
            playbooks={playbook.id: playbook},
        )
        run_id = runner.start_run(DRY_REPO, issue, playbook)

        def result(terminated: str, error: str | None = None) -> DryRunResult:
            return DryRunResult(
                terminated=terminated,  # type: ignore[arg-type]
                trace=list(trace),
                gates_auto_resolved=list(gates),
                error=error,
            )

        escalate_ids = [node_id for node_id, node in playbook.nodes.items() if node.kind == "escalate"]

        def check() -> DryRunResult | None:
            if escalate_ids:
                marks = ", ".join("?" for _ in escalate_ids)
                rows = store.query(
                    f"SELECT node_id, visit FROM steps WHERE run_id = ? AND node_id IN ({marks}) "
                    "ORDER BY started_at, id LIMIT 1",
                    (run_id, *escalate_ids),
                )
                if rows:
                    # Stop before the escalate node would park the run waiting for a human.
                    trace.append(TraceEntry(node=rows[0]["node_id"], visit=rows[0]["visit"], outcome=None))
                    return result("escalated")
            run = get_run(store, run_id)
            assert run is not None
            if run.status == "done":
                return result("done")
            if run.status == "failed":
                return result("error", _failure_reason(store, run_id))
            if len(trace) >= max_steps:
                return result("max_steps")
            return None

        while True:
            if (finished := check()) is not None:
                return finished
            advanced = await runner.tick()
            # Yield so that the caller's timeout can fire between ticks.
            await asyncio.sleep(0)
            if advanced == 0:
                return result("error", "stalled")
    finally:
        store.close()


async def dry_run_with_timeout(
    playbook: Playbook, *, outcomes: dict[str, list[str]], max_steps: int = 200
) -> DryRunResult:
    """:func:`dry_run` under ``DRY_RUN_TIMEOUT_S``; a timeout is an ``error`` result with the trace so far."""
    trace: list[TraceEntry] = []
    gates: list[dict[str, Any]] = []
    try:
        return await asyncio.wait_for(
            dry_run(playbook, outcomes=outcomes, max_steps=max_steps, trace=trace, gates=gates),
            timeout=DRY_RUN_TIMEOUT_S,
        )
    except TimeoutError:
        return DryRunResult(
            terminated="error", trace=list(trace), gates_auto_resolved=list(gates), error="timeout"
        )
