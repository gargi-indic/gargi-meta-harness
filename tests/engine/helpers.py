"""Shared builders for the engine tests: fake clock, mini playbooks, stub executors, a recording outbox."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from harness.adapters.base import AdapterRegistry, AgentState, SessionRef, StartRequest
from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine import Done, ExecContext, ExecutorRegistry, Runner, Waiting
from harness.engine.model import TERMINAL_STATUSES
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.playbook.models import Node, Playbook
from harness.playbook.templating import render
from harness.store.db import Store
from harness.store.repo import IssueRow, RunRow, StepRow, get_run, steps_for_run

REPO = "o/r"

CHECK = {"kind": "check", "check": "command", "command": "true"}
NOTIFY = {"kind": "notify", "message": "hi"}
GATE = {"kind": "gate", "gate": "g"}
AGENT_KINDS = ("agent", "review", "check", "gate", "notify", "merge", "escalate", "decompose", "branch")


class FakeClock:
    """Seconds since the epoch, moved by hand. Also drives the Store and Outbox clocks."""

    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float = 60.0) -> None:
        self.t += seconds

    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)


def make_playbook(nodes: dict[str, dict[str, Any]], edges: dict[str, str], id: str = "mini") -> Playbook:
    return Playbook.model_validate({"apiVersion": "harness/v1", "id": id, "nodes": nodes, "edges": edges})


def make_issue(number: int = 7, labels: tuple[str, ...] = (), title: str = "Fix it") -> IssueRow:
    return IssueRow(
        repo=REPO,
        number=number,
        id=1000 + number,
        title=title,
        body="body",
        state="open",
        labels=list(labels),
        parent=None,
        blocked_by=[],
        updated_at="2026-01-01T00:00:00Z",
    )


def agent_playbook() -> Playbook:
    """The happy-path / crash-matrix playbook of the addendum (section G)."""
    return make_playbook(
        {
            "plan": {
                "kind": "agent",
                "session": "main",
                "action": "plan",
                "agent": "{{ issue.agent }}",
            },
            "implement": {
                "kind": "agent",
                "session": "main",
                "action": "implement",
                "agent": "{{ issue.agent }}",
            },
        },
        {
            "plan.plan_ready": "implement",
            "plan.pr_ready": "@done",
            "implement.pr_ready": "@done",
            "*.failed": "@failed",
        },
        id="agent-mini",
    )


class Scripted:
    """Stub executor returning scripted results per node id.

    Each script entry is a Done/Waiting, an Exception instance (raised), or a callable
    ``(step, ctx) -> Done | Waiting``. The last entry repeats once the list is used up.
    """

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []
        self.contexts: dict[str, ExecContext] = {}

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done:
        self.calls.append(step.id)
        self.contexts[step.id] = ctx
        items = self.script[step.node_id]
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(step, ctx)
        return item

    def step_calls(self, node_id: str) -> int:
        return sum(1 for c in self.calls if c.split("/")[-2] == node_id)


def registry_for(executor: Any, kinds: tuple[str, ...] = AGENT_KINDS) -> ExecutorRegistry:
    registry = ExecutorRegistry()
    for kind in kinds:
        registry.register(kind, executor)
    return registry


class CountingAdapter(FakeAdapter):
    """FakeAdapter that counts ``start`` invocations even when the key is deduplicated."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.start_invocations = 0

    async def start(self, req: StartRequest) -> SessionRef:
        self.start_invocations += 1
        return await super().start(req)

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)


class StubAgentExecutor:
    """Test-only stand-in for the real agent executor (#15): find/start by key, poll, approve on implement."""

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done:
        adapter = ctx.adapters.get(render(node.agent, ctx.template, path=f"nodes.{step.node_id}.agent"))  # type: ignore[union-attr]
        key = f"{ctx.run.id}/{node.session}"  # type: ignore[union-attr]
        ref = dict(step.ref) if step.ref else None
        if ref is None:
            found = await adapter.find(key)
            if found is None:
                found = await adapter.start(
                    StartRequest(
                        repo=ctx.run.repo,
                        base_branch="main",
                        branch=f"harness/{ctx.run.issue}",
                        issue=ctx.run.issue,
                        title=ctx.template.issue.title,
                        brief="do the thing",
                        mode="plan_first",
                        idempotency_key=key,
                    )
                )
            ref = found.model_dump(mode="json")
        session = SessionRef(**{k: ref[k] for k in ("agent", "id", "url", "data") if k in ref})
        if node.action == "implement" and not ref.get("approved"):  # type: ignore[union-attr]
            # Observe, then act: a previous attempt may have approved just before a crash.
            if (await adapter.poll(session)).state == AgentState.PLAN_READY:
                await adapter.approve_plan(session)
            return Waiting(ref={**ref, "approved": True}, poll_after=0)
        snap = await adapter.poll(session)
        if snap.state == AgentState.PLAN_READY and node.action == "plan":  # type: ignore[union-attr]
            return Done("plan_ready", {"plan": snap.plan})
        if snap.state == AgentState.PR_READY and snap.pr is not None:
            return Done("pr_ready", {"pr": snap.pr.model_dump()})
        if snap.state == AgentState.FAILED:
            return Done("failed", {"error": snap.error or "agent failed"})
        return Waiting(ref=ref, poll_after=0)


class RecordingOutbox(Outbox):
    """A real Outbox over the in-memory store (never drained) that also records every enqueue call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.enqueued: list[tuple[str, dict[str, Any], str | None]] = []

    def enqueue(self, op: Any, payload: dict[str, Any], coalesce_key: str | None = None) -> int:
        self.enqueued.append((op, payload, coalesce_key))
        return super().enqueue(op, payload, coalesce_key)

    def of(self, op: str) -> list[dict[str, Any]]:
        return [p for o, p, _ in self.enqueued if o == op]

    def comment_titles(self) -> list[str]:
        titles = []
        for body in (p["body"] for p in self.of("add_comment")):
            match = re.match(r"\*\*\S+ · #\d+ (.*)\.\*\*", body)
            titles.append(match.group(1) if match else body)
        return titles


@dataclass
class Env:
    store: Store
    clock: FakeClock
    gh: GitHub
    outbox: RecordingOutbox
    adapters: AdapterRegistry

    def runner(
        self,
        executor: Any = None,
        *,
        executors: ExecutorRegistry | None = None,
        repo_configs: dict[str, RepoConfig] | None = None,
        **kwargs: Any,
    ) -> Runner:
        registry = executors if executors is not None else registry_for(executor or Scripted({}))
        return Runner(
            self.store,
            self.outbox,
            registry,
            self.adapters,
            gh=self.gh,
            repo_configs=repo_configs,
            clock=self.clock,
            **kwargs,
        )

    def run(self, run_id: str) -> RunRow:
        run = get_run(self.store, run_id)
        assert run is not None
        return run

    def steps(self, run_id: str) -> list[StepRow]:
        return sorted(steps_for_run(self.store, run_id), key=lambda s: s.id)

    def step_ids(self, run_id: str) -> list[str]:
        return [s.id.removeprefix(run_id + "/") for s in self.steps(run_id)]

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM events ORDER BY id")
        out = [{**r, "data": json.loads(r["data_json"])} for r in rows]
        return [e for e in out if kind is None or e["kind"] == kind]

    def outbox_rows(self, op: str) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM outbox WHERE op = ? ORDER BY id", (op,))
        return [{**r, "payload": json.loads(r["payload_json"])} for r in rows]


async def drive(runner: Runner, env: Env, run_id: str, *, seconds: float = 60.0, limit: int = 50) -> RunRow:
    """Tick (advancing the fake clock) until the run is terminal; fail if it never gets there."""
    for _ in range(limit):
        await runner.tick()
        run = env.run(run_id)
        if run.status in TERMINAL_STATUSES:
            return run
        env.clock.advance(seconds)
    raise AssertionError(f"run {run_id} not terminal after {limit} ticks: {env.run(run_id).status}")
