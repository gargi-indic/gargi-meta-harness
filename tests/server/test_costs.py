"""Run cost in GET /api/runs/{id}, GET /api/costs/summary and the inbox item of a run stopped for its budget."""

import pytest

from harness.ops.costs import UsageRepo, mark_budget
from harness.store.repo import get_run, update_run, update_step
from tests.server.conftest import run_path


def spend(store, seed) -> None:
    repo = UsageRepo(store)
    repo.add(
        run_id=seed.run_a,
        step_id=f"{seed.run_a}/plan/1",
        source="agent",
        adapter="claude",
        cost_usd=1.5,
        created_at="2026-01-01T00:10:00Z",
    )
    repo.add(
        run_id=seed.run_a,
        step_id=f"{seed.run_a}/plan_review/1",
        source="review",
        adapter="reviewer",
        cost_usd=0.25,
        estimated=True,
        created_at="2026-01-01T00:20:00Z",
    )
    repo.add(
        run_id=seed.run_b,
        step_id=None,
        source="agent",
        adapter="codex",
        cost_usd=4.0,
        created_at="2026-01-02T00:00:00Z",
    )


async def test_run_detail_has_the_cost(client, store, seed) -> None:
    spend(store, seed)
    cost = (await client.get(f"/api/runs/{run_path(seed.run_a)}")).json()["cost"]
    assert cost == {"usd": 1.75, "estimated": True, "by_node": {"plan": 1.5, "plan_review": 0.25}}


async def test_run_detail_cost_is_null_when_nothing_was_reported(client, seed) -> None:
    cost = (await client.get(f"/api/runs/{run_path(seed.run_a)}")).json()["cost"]
    assert cost == {"usd": None, "estimated": False, "by_node": {}}


async def test_summary_aggregates_by_adapter_and_run(client, store, seed) -> None:
    spend(store, seed)
    body = (await client.get("/api/costs/summary")).json()
    assert body["total_usd"] == pytest.approx(5.75)
    assert body["by_adapter"] == {"claude": 1.5, "codex": 4.0, "reviewer": 0.25}
    assert [(r["run_id"], r["issue"], r["usd"]) for r in body["by_run"]] == [
        (seed.run_b, 6, 4.0),
        (seed.run_a, 5, 1.75),
    ]


async def test_summary_since_filters(client, store, seed) -> None:
    spend(store, seed)
    body = (await client.get("/api/costs/summary", params={"since": "2026-01-01T12:00:00Z"})).json()
    assert body["total_usd"] == pytest.approx(4.0)
    assert body["by_adapter"] == {"codex": 4.0}
    empty = (await client.get("/api/costs/summary", params={"since": "2999-01-01"})).json()
    assert empty == {"total_usd": 0.0, "by_adapter": {}, "by_run": []}


async def test_summary_relative_since_is_measured_from_the_app_clock(store, seed) -> None:
    from datetime import UTC, datetime

    from harness.server import create_app
    from tests.fixtures.server_seed import seed_config
    from tests.server.helpers import AUTH, PORT, TOKEN, make_client

    spend(store, seed)
    now = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
    app = create_app(
        store=store, config=seed_config(), token=TOKEN, port=PORT, tail_events=False, clock=lambda: now
    )
    async with make_client(app, headers=AUTH) as client:
        body = (await client.get("/api/costs/summary", params={"since": "24h"})).json()
    assert body["by_adapter"] == {"codex": 4.0}  # since 2026-01-01T12:00


async def test_summary_rejects_a_bad_since(client) -> None:
    assert (await client.get("/api/costs/summary", params={"since": "yesterday"})).status_code == 422


async def test_summary_requires_the_cookie(anon) -> None:
    assert (await anon.get("/api/costs/summary")).status_code == 401


async def test_a_run_stopped_for_its_budget_stays_in_the_inbox(client, store, seed) -> None:
    run = get_run(store, seed.run_b)
    assert run is not None
    update_step(
        store, seed.escalate_step, output={"needs_you": True, "reason": "Budget exceeded: spent $11.00"}
    )
    ctx = dict(run.context)
    mark_budget(ctx, exceeded=True)
    update_run(store, seed.run_b, status="failed", context=ctx)
    items = (await client.get("/api/inbox")).json()
    assert seed.run_b in [i["run_id"] for i in items]


async def test_a_failed_run_without_the_budget_flag_is_not_in_the_inbox(client, store, seed) -> None:
    update_run(store, seed.run_b, status="failed")
    assert seed.run_b not in [i["run_id"] for i in (await client.get("/api/inbox")).json()]
