"""Gate executor (M1.6): auto, auto_if, human; plus an end-to-end run through a real Runner."""

from typing import Any

import pytest

from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig
from harness.engine import Done, ExecContext, ExecutorRegistry, Waiting
from harness.engine.executors.gate import GateExecutor
from harness.github.commands import decide
from harness.playbook.models import GateNode, Playbook
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.repo import RunRow, StepRow, get_gate
from tests.engine.helpers import REPO, Env, drive, make_issue

NODE = GateNode(gate="g")


def playbook(mode: Any) -> Playbook:
    return Playbook.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "gated",
            "gates": {"g": mode},
            "nodes": {"g": {"kind": "gate", "gate": "g"}},
            "edges": {"g.approved": "@done", "g.rejected": "@failed"},
        }
    )


def make_ctx(env: Env, mode: Any, *, context: dict[str, Any] | None = None, labels: list[str] | None = None):
    run = RunRow(
        id="run1",
        repo=REPO,
        issue=7,
        playbook_id="gated",
        playbook_sha=None,
        status="running",
        context=context or {},
        ledger_comment_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    return ExecContext(
        store=env.store,
        outbox=env.outbox,
        gh=None,
        adapters=AdapterRegistry(),
        repo_config=RepoConfig(),
        playbook=playbook(mode),
        run=run,
        template=Context(
            issue=IssueCtx(number=7, title="t", agent="jules", labels=labels or []),
            run=RunCtx(id="run1"),
        ),
        clock=env.clock,
    )


def make_step() -> StepRow:
    return StepRow(
        id="run1/g/1",
        run_id="run1",
        node_id="g",
        visit=1,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )


async def test_auto_approves(env: Env) -> None:
    result = await GateExecutor().advance(make_step(), NODE, make_ctx(env, "auto"))
    assert result == Done("approved", {"note": None, "decided_by": "auto"})
    assert get_gate(env.store, "run1/g/1/gate") is None


async def test_auto_if_true_approves(env: Env) -> None:
    ctx = make_ctx(env, {"auto_if": "'small' in issue.labels"}, labels=["small"])
    result = await GateExecutor().advance(make_step(), NODE, ctx)
    assert result == Done("approved", {"note": None, "decided_by": "auto_if"})
    assert get_gate(env.store, "run1/g/1/gate") is None


async def test_auto_if_false_falls_back_to_human(env: Env) -> None:
    ctx = make_ctx(env, {"auto_if": "'small' in issue.labels"}, labels=["big"])
    result = await GateExecutor().advance(make_step(), NODE, ctx)
    assert isinstance(result, Waiting)
    assert result.output is not None and result.output["needs_you"] is True
    gate = get_gate(env.store, "run1/g/1/gate")
    assert gate is not None and gate.status == "pending"


async def test_unknown_gate_fails(env: Env) -> None:
    result = await GateExecutor().advance(make_step(), GateNode(gate="nope"), make_ctx(env, "human"))
    assert result == Done("failed", {"error": "unknown gate nope"})


async def test_human_creates_one_gate_and_waits(env: Env) -> None:
    executor = GateExecutor()
    ctx = make_ctx(env, "human", context={"pr": {"number": 9, "url": "https://github.com/o/r/pull/9"}})
    first = await executor.advance(make_step(), NODE, ctx)
    second = await executor.advance(make_step(), NODE, ctx)
    assert first == second
    assert isinstance(first, Waiting)
    assert first.ref == {"gate_id": "run1/g/1/gate"}
    assert first.poll_after == 15.0
    out = first.output or {}
    assert out["needs_you"] is True
    assert out["gate_id"] == "run1/g/1/gate"
    assert out["links"] == ["https://github.com/o/r/pull/9"]
    assert "Gate 'g'" in out["reason"]
    assert "/harness approve" in out["reason"] and "/harness reject" in out["reason"]
    assert "harness approve run1/g/1/gate" in out["reason"]
    rows = env.store.query("SELECT * FROM gates")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending" and rows[0]["name"] == "g" and rows[0]["step_id"] == "run1/g/1"
    assert rows[0]["run_id"] == "run1"


async def test_human_without_pr_has_no_links(env: Env) -> None:
    result = await GateExecutor().advance(make_step(), NODE, make_ctx(env, "human"))
    assert isinstance(result, Waiting) and (result.output or {})["links"] == []


async def test_approved_returns_note_and_decider(env: Env) -> None:
    executor, ctx = GateExecutor(), make_ctx(env, "human")
    await executor.advance(make_step(), NODE, ctx)
    decide(env.store, "run1/g/1/gate", "approved", "ship it", "alice")
    result = await executor.advance(make_step(), NODE, ctx)
    assert result == Done("approved", {"note": "ship it", "decided_by": "alice"})


async def test_rejected_sets_note_and_correction(env: Env) -> None:
    executor, ctx = GateExecutor(), make_ctx(env, "human")
    await executor.advance(make_step(), NODE, ctx)
    decide(env.store, "run1/g/1/gate", "rejected", "wrong approach", "cli")
    result = await executor.advance(make_step(), NODE, ctx)
    assert result == Done(
        "rejected", {"note": "wrong approach", "decided_by": "cli", "correction": "wrong approach"}
    )


async def test_executor_enqueues_nothing(env: Env) -> None:
    executor, ctx = GateExecutor(), make_ctx(env, "human", context={"pr": {"number": 1, "url": "u"}})
    await executor.advance(make_step(), NODE, ctx)
    decide(env.store, "run1/g/1/gate", "approved", None, "cli")
    await executor.advance(make_step(), NODE, ctx)
    assert env.outbox.enqueued == []
    assert env.store.query("SELECT * FROM outbox") == []


# ---- end to end through a real Runner ------------------------------------------------------------------


def registry() -> ExecutorRegistry:
    reg = ExecutorRegistry()
    reg.register("gate", GateExecutor())
    return reg


@pytest.mark.parametrize(
    ("status", "final", "outcome"), [("approved", "done", "approved"), ("rejected", "failed", "rejected")]
)
async def test_runner_end_to_end(env: Env, status: str, final: str, outcome: str) -> None:
    runner = env.runner(executors=registry())
    run_id = runner.start_run(REPO, make_issue(7), playbook("human"))
    await runner.tick()
    assert env.run(run_id).status == "needs-you"
    assert [e["data"]["node"] for e in env.events("needs_you")] == ["g"]
    assert env.outbox.comment_titles().count("needs you") == 1

    # polling again neither comments nor changes anything
    env.clock.advance(60)
    await runner.tick()
    assert env.run(run_id).status == "needs-you"
    assert env.outbox.comment_titles().count("needs you") == 1

    gate_id = f"{run_id}/g/1/gate"
    decide(env.store, gate_id, status, "because", "tester")  # type: ignore[arg-type]
    env.clock.advance(60)
    result = await drive(runner, env, run_id)
    assert result.status == final
    step = env.steps(run_id)[0]
    assert step.outcome == outcome
    assert (step.output or {})["note"] == "because"
    assert env.run(run_id).context["nodes"]["g"]["decided_by"] == "tester"
