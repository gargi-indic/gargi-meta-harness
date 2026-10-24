"""Per-run cost tracking and ``limits.budget_usd`` enforcement (M5.5), driven through the real agent executor."""

import json
from datetime import UTC, datetime

import pytest
from httpx import Response

from harness.adapters.base import AgentSnapshot, AgentState, Usage
from harness.adapters.fake import FakeAdapter
from harness.engine import Runner, rehydrate
from harness.engine.executors import default_registry
from harness.engine.runner import BUDGET_NODE
from harness.github.ledger import parse_ledger
from harness.llm.backend import FakeBackend
from harness.ops.costs import UsageRepo, budget_state, recorded_total
from harness.playbook.models import Playbook
from harness.server.derive import inbox_item, needs_human
from harness.store.db import Store
from harness.store.repo import get_run, steps_for_run
from harness.sync import sync_repo
from tests.engine.helpers import REPO, FakeClock, make_issue

BUDGET = 10.0


def playbook(*, budget: float | None = BUDGET, review: bool = False) -> Playbook:
    nodes: dict = {"plan": {"kind": "agent", "session": "main", "action": "plan", "agent": "fake"}}
    edges = {"plan.plan_ready": "@done", "*.failed": "@failed"}
    if review:
        nodes["rev"] = {"kind": "review", "target": "plan", "checklist": []}
        edges.update({"plan.plan_ready": "rev", "rev.approve": "@done", "rev.changes": "@failed"})
    raw = {"apiVersion": "harness/v1", "id": "budgeted", "nodes": nodes, "edges": edges}
    if budget is not None:
        raw["limits"] = {"budget_usd": budget}
    return Playbook.model_validate(raw)


def working(cost: float | None, **extra) -> AgentSnapshot:
    """A poll snapshot whose cumulative session spend is ``cost`` (None: this adapter reports nothing)."""
    usage = (
        None
        if cost is None
        else Usage(input_tokens=int(cost * 100), output_tokens=int(cost * 10), cost_usd=cost)
    )
    return AgentSnapshot(state=AgentState.WORKING, usage=usage, poll_after=0, **extra)


def adapter_with(*costs: float | None) -> FakeAdapter:
    return FakeAdapter(script={"*": [[working(c) for c in costs]]})


def runner_for(env, adapter: FakeAdapter, pb: Playbook, backend: FakeBackend | None = None) -> Runner:
    env.adapters.register(adapter)
    registry = default_registry(review_backend=backend or FakeBackend([]))
    return env.runner(executors=registry, playbooks={pb.id: pb})


async def tick(runner: Runner, env, n: int = 1) -> None:
    for _ in range(n):
        await runner.tick()
        env.clock.advance(60)


def titles(env) -> list[str]:
    return env.outbox.comment_titles()


async def test_spend_is_recorded_once_per_reading_even_when_polls_repeat(env):
    pb = playbook()
    runner = runner_for(env, adapter_with(1.0, 2.5, 2.5, 2.5), pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 6)
    assert UsageRepo(env.store).total_for_run(run_id) == pytest.approx(2.5)
    assert recorded_total(env.run(run_id).context) == pytest.approx(2.5)
    assert env.run(run_id).status == "running"
    # The spend travels in the ledger snapshot the runner writes to GitHub.
    snapshot = parse_ledger(env.outbox_rows("upsert_ledger")[-1]["payload"]["body"])
    assert snapshot["usage"]["steps"][f"{run_id}/plan/1"]["cost_usd"] == pytest.approx(2.5)


async def test_a_restarted_runner_does_not_count_the_same_reading_twice(env):
    pb = playbook()
    adapter = adapter_with(3.0)
    run_id = runner_for(env, adapter, pb).start_run(REPO, make_issue(), pb, agent="fake")
    await tick(
        env.runner(executors=default_registry(review_backend=FakeBackend([])), playbooks={pb.id: pb}), env, 2
    )
    again = env.runner(executors=default_registry(review_backend=FakeBackend([])), playbooks={pb.id: pb})
    await tick(again, env, 3)
    assert UsageRepo(env.store).total_for_run(run_id) == pytest.approx(3.0)


async def test_crossing_80_percent_warns_once(env):
    pb = playbook()
    runner = runner_for(env, adapter_with(5.0, 8.5), pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 6)
    assert [e["data"] for e in env.events("budget_warning")] == [{"spent_usd": 8.5, "budget_usd": BUDGET}]
    assert titles(env).count("budget warning") == 1
    assert env.run(run_id).status == "running"
    # A new runner (restart) reads the flag from the run record and does not warn again.
    again = env.runner(executors=default_registry(review_backend=FakeBackend([])), playbooks={pb.id: pb})
    await tick(again, env, 3)
    assert len(env.events("budget_warning")) == 1


async def test_crossing_100_percent_cancels_once_fails_the_run_and_escalates(env):
    pb = playbook()
    adapter = adapter_with(4.0, 8.5, 10.5)
    runner = runner_for(env, adapter, pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 8)

    run = env.run(run_id)
    assert run.status == "failed"
    assert [e["data"]["reason"] for e in env.events("run_failed")] == ["budget_exceeded"]
    assert sum(1 for name, _ in adapter.calls if name == "cancel") == 1
    assert budget_state(run.context) == {"warned": True, "unknown_warned": False, "exceeded": True}
    assert "budget exceeded" in titles(env)
    by_node = {s.node_id: s for s in env.steps(run_id)}
    plan, escalation = by_node["plan"], by_node[BUDGET_NODE]
    assert (plan.status, plan.outcome) == ("failed", "cancelled")
    assert escalation.node_id == BUDGET_NODE and needs_human(escalation)
    item = inbox_item(run, escalation, env.steps(run_id), "t")
    assert item.kind == "escalation" and "Budget exceeded" in item.summary
    labels = [p["add"] for p in env.outbox.of("set_labels")]
    assert labels[-1] == ["harness:failed"]

    # No second cancel, comment or event after more ticks or a restart.
    again = env.runner(executors=default_registry(review_backend=FakeBackend([])), playbooks={pb.id: pb})
    await tick(again, env, 3)
    assert sum(1 for name, _ in adapter.calls if name == "cancel") == 1
    assert len(env.events("budget_exceeded")) == 1


async def test_a_jump_past_the_budget_goes_straight_to_exceeded(env):
    pb = playbook()
    runner = runner_for(env, adapter_with(12.0), pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 4)
    assert env.run(run_id).status == "failed"
    assert env.events("budget_warning") == []


async def test_a_run_without_usage_is_never_cancelled_and_warns_once(env):
    pb = playbook(budget=0.01)
    adapter = adapter_with(None)
    runner = runner_for(env, adapter, pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 8)
    assert env.run(run_id).status == "running"
    assert sum(1 for name, _ in adapter.calls if name == "cancel") == 0
    assert len(env.events("budget_unknown")) == 1
    assert recorded_total(env.run(run_id).context) is None
    again = env.runner(executors=default_registry(review_backend=FakeBackend([])), playbooks={pb.id: pb})
    await tick(again, env, 3)
    assert len(env.events("budget_unknown")) == 1


async def test_a_playbook_without_a_budget_is_not_enforced(env):
    pb = playbook(budget=None)
    runner = runner_for(env, adapter_with(1000.0), pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 4)
    assert env.run(run_id).status == "running"
    assert UsageRepo(env.store).total_for_run(run_id) == pytest.approx(1000.0)


async def test_reviewer_spend_is_added_to_the_run(env):
    pb = playbook(review=True)
    plan_ready = AgentSnapshot(state=AgentState.PLAN_READY, plan="1. do it", usage=Usage(cost_usd=1.0))
    backend = FakeBackend(
        [{"verdict": "approve", "findings": [], "correction": "", "summary": "ok"}],
        usage=[Usage(input_tokens=10, output_tokens=5, cost_usd=0.5)],
    )
    runner = runner_for(env, FakeAdapter(script={"*": [[plan_ready]]}), pb, backend)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 5)
    assert env.run(run_id).status == "done"
    cost = UsageRepo(env.store).run_cost(run_id)
    assert cost.by_node == {"plan": pytest.approx(1.0), "rev": pytest.approx(0.5)}
    assert cost.usd == pytest.approx(1.5)


async def test_retrying_after_the_budget_stop_resolves_the_escalation(env):
    pb = playbook()
    runner = runner_for(env, adapter_with(11.0), pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 4)
    assert env.run(run_id).status == "failed"
    runner.retry_node(run_id, "plan")
    assert env.run(run_id).status == "running"
    assert [s.status for s in env.steps(run_id) if s.node_id == BUDGET_NODE] == ["done"]


@pytest.mark.respx(assert_all_called=False)
async def test_sync_rebuilds_totals_and_the_budget_state_after_the_database_is_wiped(env, respx_mock):
    api = f"https://api.github.com/repos/{REPO}"
    respx_mock.get(api).mock(return_value=Response(200, json={"default_branch": "main"}))
    pb = playbook()
    runner = runner_for(env, adapter_with(5.0, 10.5), pb)
    run_id = runner.start_run(REPO, make_issue(), pb, agent="fake")
    await tick(runner, env, 6)
    before = UsageRepo(env.store)
    assert env.run(run_id).status == "failed"
    ledger_body = env.outbox_rows("upsert_ledger")[-1]["payload"]["body"]
    expected_total = before.total_for_run(run_id)
    expected_by_node = before.run_cost(run_id).by_node
    assert expected_total == pytest.approx(10.5)

    # Lose the local database; GitHub still has the run's ledger comment.
    clock = FakeClock(env.clock.t)
    wiped = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    wiped.migrate()
    assert UsageRepo(wiped).total_for_run(run_id) == 0.0
    respx_mock.get(f"{api}/contents/.harness/config.yaml?ref=main").mock(return_value=Response(404))
    issue = {
        "number": 7,
        "id": 1007,
        "title": "Fix it",
        "state": "open",
        "labels": [{"name": "harness:failed"}],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    respx_mock.get(f"{api}/issues?state=all&per_page=100").mock(return_value=Response(200, json=[issue]))
    respx_mock.get(f"{api}/issues/7/parent").mock(return_value=Response(404))
    respx_mock.get(f"{api}/issues/7/dependencies/blocked_by?per_page=100").mock(
        return_value=Response(200, json=[])
    )
    respx_mock.get(f"{api}/issues/7/comments?per_page=100").mock(
        return_value=Response(
            200, json=[{"id": 1, "body": ledger_body, "created_at": "2026-01-01T00:00:00Z"}]
        )
    )
    report = await sync_repo(env.gh, wiped, REPO, full=True)
    assert report.errors == []

    # Totals come back from the ledger; the run (with its budget_exceeded state) rehydrates from the same snapshot.
    restored = UsageRepo(wiped)
    assert restored.total_for_run(run_id) == pytest.approx(expected_total)
    assert restored.run_cost(run_id).by_node == {k: pytest.approx(v) for k, v in expected_by_node.items()}
    assert rehydrate(wiped, report.ledgers[7]) == run_id
    run = get_run(wiped, run_id)
    assert run is not None and run.status == "failed"
    assert budget_state(run.context)["exceeded"] is True
    assert recorded_total(run.context) == pytest.approx(expected_total)
    assert [s.node_id for s in steps_for_run(wiped, run_id) if needs_human(s)] == [BUDGET_NODE]
    wiped.close()
    json.dumps(report.ledgers[7])  # the snapshot stays plain JSON
