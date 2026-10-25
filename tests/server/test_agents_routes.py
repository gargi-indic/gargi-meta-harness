"""GET /api/agents and /api/agents/costs: in-use counts, active sessions, health, auth, cost reuse."""

import importlib.util
import inspect
import shutil

import httpx
import pytest

from harness.ops.costs import UsageRepo
from harness.server.routes import agents as agents_route
from harness.store.repo import update_run, update_step
from tests.server.test_costs import spend


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("JULES_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)


def by_name(body: list[dict]) -> dict[str, dict]:
    return {a["name"]: a for a in body}


async def test_requires_the_cookie(anon: httpx.AsyncClient) -> None:
    assert (await anon.get("/api/agents")).status_code == 401
    assert (await anon.get("/api/agents/costs")).status_code == 401


async def test_lists_the_known_agents_with_caps_and_health(client, monkeypatch) -> None:
    monkeypatch.setenv("JULES_API_KEY", "x")
    agents = by_name((await client.get("/api/agents")).json())
    assert list(agents) == ["jules", "claude", "codex", "copilot"]
    assert agents["jules"]["health"]["status"] == "ok" and agents["jules"]["enabled"] is True
    assert agents["claude"]["health"]["status"] == "down"
    assert agents["copilot"]["health"]["status"] == "disabled" and agents["copilot"]["enabled"] is False
    assert agents["jules"]["caps"] == {
        "plan_approval": True,
        "messaging": True,
        "opens_pr": True,
        "local_workspace": False,
    }
    assert agents["jules"]["max_concurrency"] == 1
    assert "x" not in agents["jules"]["health"]["detail"].split("JULES_API_KEY")[0]


async def test_in_use_and_active_sessions_come_from_the_store(client, store, seed) -> None:
    update_step(
        store,
        f"{seed.run_a}/plan_gate/1",
        ref={"agent": "jules", "id": "sess-1", "gate_id": seed.plan_gate_id},
    )
    update_run(store, seed.run_b, context={"agent": "claude"})
    spend(store, seed)

    agents = by_name((await client.get("/api/agents")).json())

    assert agents["jules"]["in_use"] == 1 and agents["claude"]["in_use"] == 1
    assert agents["codex"]["in_use"] == 0
    [session] = agents["jules"]["active_sessions"]
    assert session["run_id"] == seed.run_a and session["issue"] == 5
    assert session["step"] == f"{seed.run_a}/plan_gate/1" and session["state"] == "waiting"
    assert session["cost_usd"] == 1.75
    assert agents["claude"]["active_sessions"] == []


async def test_a_terminal_run_is_not_in_use(client, store, seed) -> None:
    update_run(store, seed.run_a, status="done")
    update_run(store, seed.run_b, status="cancelled")
    agents = by_name((await client.get("/api/agents")).json())
    assert all(a["in_use"] == 0 and a["active_sessions"] == [] for a in agents.values())


async def test_today_cost_is_per_adapter_since_midnight(client, store, seed) -> None:
    UsageRepo(store).add(run_id=seed.run_a, step_id=None, source="agent", adapter="codex", cost_usd=2.5)
    agents = by_name((await client.get("/api/agents")).json())
    assert agents["codex"]["today_cost_usd"] == 2.5
    assert agents["jules"]["today_cost_usd"] is None


async def test_costs_per_run_with_agent_and_estimated(client, store, seed) -> None:
    spend(store, seed)
    rows = (await client.get("/api/agents/costs")).json()
    assert rows == [
        {"run_id": seed.run_b, "issue": 6, "agent": "jules", "usd": 4.0, "estimated": False},
        {"run_id": seed.run_a, "issue": 5, "agent": "jules", "usd": 1.75, "estimated": True},
    ]
    since = (await client.get("/api/agents/costs", params={"since": "2026-01-02"})).json()
    assert [r["run_id"] for r in since] == [seed.run_b]
    assert (await client.get("/api/agents/costs", params={"since": "nonsense"})).status_code == 422


def test_the_route_adds_no_sql_of_its_own() -> None:
    source = inspect.getsource(agents_route)
    assert "SELECT" not in source.upper().replace("SELECTED", "") and ".query(" not in source
    assert "UsageRepo" in source


async def test_demo_mode_scripts_health_and_cost() -> None:
    from harness.server.demo import create_demo_app
    from tests.server.helpers import make_client

    app, token = create_demo_app(token="t", port=7777)
    async with make_client(app, headers={"Cookie": "harness_token=t"}) as c:
        agents = by_name((await c.get("/api/agents")).json())
        assert [agents[n]["health"]["status"] for n in ("jules", "claude", "codex", "copilot")] == [
            "ok",
            "degraded",
            "down",
            "disabled",
        ]
        costs = (await c.get("/api/agents/costs")).json()
        assert any(r["estimated"] for r in costs)
        assert (await c.get("/api/ops/status")).status_code == 200
