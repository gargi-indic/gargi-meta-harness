"""Shared test helpers for the engine tests: fake clock, scripted executors, outbox inspection."""

import json
from datetime import UTC, datetime
from typing import Any

from harness.adapters.base import AgentState, SessionRef, StartRequest
from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.engine.runner import TERMINAL_STATUSES, Runner
from harness.playbook.models import Node, Playbook
from harness.playbook.templating import render
from harness.store.db import Store
from harness.store.repo import IssueRow, StepRow, get_run, steps_for_run

REPO = "acme/widgets"


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)


def make_issue(number: int = 7, labels: list[str] | None = None, title: str = "Fix the widget") -> IssueRow:
    return IssueRow(
        repo=REPO,
        number=number,
        id=1000 + number,
        title=title,
        body="body",
        state="open",
        labels=labels or [],
        parent=None,
        blocked_by=[],
        updated_at="2026-01-01T00:00:00Z",
    )


def make_playbook(nodes: dict[str, dict[str, Any]], edges: dict[str, str], pid: str = "mini") -> Playbook:
    return Playbook.model_validate({"apiVersion": "harness/v1", "id": pid, "nodes": nodes, "edges": edges})


def check(**extra: Any) -> dict[str, Any]:
    return {"kind": "check", "check": "ci", **extra}


class Scripted:
    """Stub executor returning scripted results per node id; the last result of a script repeats."""

    def __init__(self, script: dict[str, list[Waiting | Done | Exception]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []
        self.templates: dict[str, Any] = {}

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done:
        self.calls.append(step.id)
        self.templates[step.id] = ctx.template
        seq = self.script[step.node_id]
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item


class StubAgentExecutor:
    """Test-only stand-in for the real agent executor (#15), built on FakeAdapter."""

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done:
        adapter = ctx.adapters.get(render(node.agent, ctx.template, path=f"nodes.{step.node_id}.agent"))
        key = f"{ctx.run.id}/{node.session}"
        if step.ref:
            ref_data = dict(step.ref)
        else:
            found = await adapter.find(key)
            if found is None:
                found = await adapter.start(
                    StartRequest(
                        repo=ctx.run.repo,
                        base_branch="main",
                        branch=f"harness/{ctx.run.issue}",
                        issue=ctx.run.issue,
                        title="t",
                        brief="b",
                        mode="plan_first",
                        idempotency_key=key,
                    )
                )
            ref_data = found.model_dump(mode="json")
        ref = SessionRef(**{k: v for k, v in ref_data.items() if k in SessionRef.model_fields})
        snap = await adapter.poll(ref)
        if node.action == "implement" and not ref_data.get("approved"):
            # Approve only a plan that is still waiting: a crash after the call must not approve twice.
            if snap.state == AgentState.PLAN_READY:
                await adapter.approve_plan(ref)
            return Waiting(ref={**ref_data, "approved": True}, poll_after=0)
        if snap.state == AgentState.PLAN_READY and node.action == "plan":
            return Done("plan_ready", {"plan": snap.plan})
        if snap.state == AgentState.PR_READY and snap.pr is not None:
            return Done("pr_ready", {"pr": snap.pr.model_dump()})
        if snap.state == AgentState.FAILED:
            return Done("failed", {"error": snap.error})
        return Waiting(ref=ref_data, poll_after=0)


def outbox_rows(store: Store, op: str | None = None) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM outbox ORDER BY id")
    out = []
    for r in rows:
        if op is not None and r["op"] != op:
            continue
        out.append(
            {"op": r["op"], "key": r["coalesce_key"], "status": r["status"], **json.loads(r["payload_json"])}
        )
    return out


def pending_outbox(store: Store, op: str | None = None) -> list[dict[str, Any]]:
    return [r for r in outbox_rows(store, op) if r["status"] == "pending"]


def flush_outbox(store: Store) -> None:
    """Mark everything sent (as a drain would), so the next assertions only see new writes."""
    store.execute("UPDATE outbox SET status = 'sent', coalesce_key = NULL WHERE status = 'pending'")


def events(store: Store, kind: str | None = None) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM events ORDER BY id")
    out = [{"kind": r["kind"], "run_id": r["run_id"], "data": json.loads(r["data_json"])} for r in rows]
    return [e for e in out if kind is None or e["kind"] == kind]


def step_map(store: Store, run_id: str) -> dict[str, StepRow]:
    return {s.id: s for s in steps_for_run(store, run_id)}


async def drive(runner: Runner, run_id: str, clock: FakeClock, limit: int = 50) -> None:
    """Tick (moving the fake clock 61 seconds each time) until the run is terminal."""
    for _ in range(limit):
        run = get_run(runner.store, run_id)
        assert run is not None
        if run.status in TERMINAL_STATUSES:
            return
        await runner.tick()
        clock.t += 61
    raise AssertionError(f"run {run_id} did not finish in {limit} ticks")
