"""ScriptedExecutor and scripted_registry (M2.8)."""

import pytest

from harness.engine.fakes import ScriptedExecutor, TraceEntry, scripted_registry
from harness.playbook.models import Playbook
from harness.store.repo import StepRow
from tests.engine.helpers import make_playbook

PLAYBOOK = make_playbook(
    {
        "review": {"kind": "review", "target": "plan", "checklist": ["scope"], "max_visits": 5},
        "gate": {"kind": "gate", "gate": "merge"},
        "done": {"kind": "notify", "message": "x"},
    },
    {
        "review.approve": "gate",
        "review.changes": "review",
        "review.escalate": "@failed",
        "review.exhausted": "@failed",
        "gate.approved": "done",
        "gate.rejected": "@failed",
        "done.sent": "@done",
    },
)


def step(node: str, visit: int) -> StepRow:
    return StepRow(
        id=f"r/{node}/{visit}",
        run_id="r",
        node_id=node,
        visit=visit,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at="2026-01-01T00:00:00Z",
        ended_at=None,
    )


async def advance(executor: ScriptedExecutor, node_id: str, visit: int):
    return await executor.advance(step(node_id, visit), PLAYBOOK.nodes[node_id], None)  # type: ignore[arg-type]


async def test_outcomes_are_taken_per_visit_then_the_primary_port() -> None:
    trace: list[TraceEntry] = []
    executor = ScriptedExecutor({"review": ["changes", "escalate"]}, trace, [])
    results = [await advance(executor, "review", v) for v in (1, 2, 3, 4)]
    assert [r.outcome for r in results] == ["changes", "escalate", "approve", "approve"]
    assert trace == [
        TraceEntry(node="review", visit=1, outcome="changes"),
        TraceEntry(node="review", visit=2, outcome="escalate"),
        TraceEntry(node="review", visit=3, outcome="approve"),
        TraceEntry(node="review", visit=4, outcome="approve"),
    ]


async def test_the_result_has_an_empty_output() -> None:
    result = await advance(ScriptedExecutor({}, [], []), "done", 1)
    assert (result.outcome, result.output) == ("sent", {})


async def test_gates_taking_the_default_are_recorded() -> None:
    gates: list[dict] = []
    executor = ScriptedExecutor({}, [], gates)
    await advance(executor, "gate", 1)
    await advance(executor, "gate", 2)
    assert gates == [
        {"node": "gate", "visit": 1, "gate": "merge"},
        {"node": "gate", "visit": 2, "gate": "merge"},
    ]


async def test_a_scripted_gate_outcome_is_not_an_auto_resolution() -> None:
    gates: list[dict] = []
    trace: list[TraceEntry] = []
    executor = ScriptedExecutor({"gate": ["rejected"]}, trace, gates)
    assert (await advance(executor, "gate", 1)).outcome == "rejected"
    assert gates == []
    assert trace[0].outcome == "rejected"


def test_the_registry_covers_each_kind_but_join_and_parallel() -> None:
    registry = scripted_registry(PLAYBOOK, {}, [], [])
    assert registry.kinds() == ["gate", "notify", "review"]
    playbook = Playbook.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "fan",
            "nodes": {
                "fork": {"kind": "parallel", "branches": ["a"]},
                "a": {"kind": "notify", "message": "x"},
                "join": {"kind": "join", "mode": "all"},
            },
            "edges": {"fork.exhausted": "@done"},
        }
    )
    assert scripted_registry(playbook, {}, [], []).kinds() == ["notify"]


def test_one_executor_serves_every_kind() -> None:
    registry = scripted_registry(PLAYBOOK, {}, [], [])
    assert registry.get("gate") is registry.get("review")
    with pytest.raises(KeyError):
        registry.get("merge")
