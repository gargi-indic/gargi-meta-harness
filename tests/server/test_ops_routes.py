"""Ops API: status, kill (literal confirmation), resume, budget override. Auth and validation included."""

import json

import httpx
import pytest

from harness.engine.model import TERMINAL_STATUSES
from harness.server import create_app
from harness.store.db import Store
from harness.store.repo import list_runs, upsert_repo
from tests.fixtures.server_seed import seed_config
from tests.server.helpers import AUTH, PORT, TOKEN, make_client, make_store


@pytest.fixture
def with_control(store: Store) -> Store:
    for entry in seed_config().repos:
        upsert_repo(store, entry.name, "main", {"control_issue": 1})
    return store


def queued_ops(store: Store) -> list[dict]:
    rows = store.query("SELECT payload_json FROM outbox WHERE op = 'merge_ledger' ORDER BY id")
    return [json.loads(r["payload_json"]) for r in rows]


def pending_requests(store: Store, kind: str) -> int:
    return len(store.query("SELECT 1 FROM control_requests WHERE kind = ? AND handled_at IS NULL", (kind,)))


async def test_status_defaults(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/ops/status")).json()
    assert body["halted"] is None
    assert set(body["budget"]) == {
        "limit_usd",
        "spent_usd",
        "remaining_usd",
        "resets_at",
        "exceeded",
        "unknown_cost_runs",
    }
    assert body["budget"]["limit_usd"] is None and body["budget"]["exceeded"] is False


async def test_kill_needs_the_literal_confirmation(client: httpx.AsyncClient, with_control: Store) -> None:
    for body in ({"confirm": "kill"}, {"confirm": ""}, {}, {"confirm": "KILL", "extra": 1}):
        assert (await client.post("/api/ops/kill", json=body)).status_code == 422
    assert queued_ops(with_control) == []


async def test_kill_halts_first_and_queues_a_cancel_per_active_run(
    client: httpx.AsyncClient, with_control: Store
) -> None:
    active = [r.id for r in list_runs(with_control) if r.status not in TERMINAL_STATUSES]
    assert active

    response = await client.post("/api/ops/kill", json={"confirm": "KILL"})

    assert response.status_code == 200
    assert response.json() == {"halted": True, "cancels_queued": len(active)}
    ops = queued_ops(with_control)
    assert len(ops) == len(seed_config().repos) and {o["key"] for o in ops} == {"ops"}
    assert all(
        o["value"]["halt"]["reason"] == "kill_switch" and o["value"]["halt"]["by"] == "api" for o in ops
    )
    assert pending_requests(with_control, "cancel") == len(active)
    halted = (await client.get("/api/ops/status")).json()["halted"]
    assert halted["reason"] == "kill_switch" and halted["by"] == "api"
    # a second kill queues nothing more
    again = await client.post("/api/ops/kill", json={"confirm": "KILL"})
    assert again.json()["cancels_queued"] == 0 and pending_requests(with_control, "cancel") == len(active)


async def test_resume_clears_the_halt(client: httpx.AsyncClient, with_control: Store) -> None:
    await client.post("/api/ops/kill", json={"confirm": "KILL"})
    response = await client.post("/api/ops/resume")
    assert response.status_code == 200 and response.json() == {"resumed_runs": []}
    assert (await client.get("/api/ops/status")).json()["halted"] is None
    assert queued_ops(with_control)[0]["value"]["halt"] is None


async def test_budget_set_and_clear(client: httpx.AsyncClient, with_control: Store) -> None:
    response = await client.post("/api/ops/budget", json={"daily_usd": 40.0})
    assert response.status_code == 200 and response.json()["limit_usd"] == 40.0
    assert (await client.get("/api/ops/status")).json()["budget"]["limit_usd"] == 40.0
    assert queued_ops(with_control)[0]["value"]["daily_usd"] == 40.0
    cleared = await client.post("/api/ops/budget", json={"daily_usd": None})
    assert cleared.json()["limit_usd"] is None


async def test_budget_validation(client: httpx.AsyncClient, with_control: Store) -> None:
    assert (await client.post("/api/ops/budget", json={"daily_usd": -1})).status_code == 422
    assert (await client.post("/api/ops/budget", json={"daily_usd": "lots"})).status_code == 422
    assert (await client.post("/api/ops/budget", json={})).status_code == 422
    assert queued_ops(with_control) == []


async def test_without_a_control_issue_the_writes_are_refused() -> None:
    app = create_app(store=make_store(), config=seed_config(), token=TOKEN, port=PORT, tail_events=False)
    async with make_client(app, headers=AUTH) as c:
        assert (await c.post("/api/ops/kill", json={"confirm": "KILL"})).status_code == 409
        assert (await c.post("/api/ops/budget", json={"daily_usd": 1})).status_code == 409


async def test_the_config_budget_is_the_default_limit(store: Store) -> None:
    cfg = seed_config()
    cfg.budget.daily_usd = 12.0
    app = create_app(store=store, config=cfg, token=TOKEN, port=PORT, tail_events=False)
    async with make_client(app, headers=AUTH) as c:
        assert (await c.get("/api/ops/status")).json()["budget"]["limit_usd"] == 12.0


async def test_ops_routes_need_the_cookie(anon: httpx.AsyncClient) -> None:
    assert (await anon.get("/api/ops/status")).status_code == 401
    assert (await anon.post("/api/ops/kill", json={"confirm": "KILL"})).status_code == 401
    assert (await anon.post("/api/ops/resume")).status_code == 401
    assert (await anon.post("/api/ops/budget", json={"daily_usd": 40.0})).status_code == 401
