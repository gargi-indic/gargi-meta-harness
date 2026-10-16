"""POST /api/playbooks/dry-run (M2.8): scripted executors on a throwaway store, no network."""

import asyncio

import pytest

import harness.server.dry_run as dry_run_module
from harness.engine.executors.base import ExecutorRegistry
from harness.engine.fakes import ScriptedExecutor
from harness.playbook.load import load_playbook
from harness.server.dry_run import dry_run
from tests.server.playbooks_helpers import builtin_text, fixture_text, make_playbook_app, playbook_client

# No routes at all: any request that leaves the process fails the test.
pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

URL = "/api/playbooks/dry-run"
DEFAULT_TRACE = [
    ("plan", 1, "plan_ready"),
    ("plan_review", 1, "approve"),
    ("plan_gate", 1, "approved"),
    ("implement", 1, "pr_ready"),
    ("ci", 1, "pass"),
    ("scope", 1, "pass"),
    ("pr_review", 1, "approve"),
    ("merge_gate", 1, "approved"),
    ("merge", 1, "merged"),
]


def trace_of(body: dict) -> list[tuple[str, int, str | None]]:
    return [(t["node"], t["visit"], t["outcome"]) for t in body["trace"]]


@pytest.fixture
def app():
    return make_playbook_app()


@pytest.fixture
async def client(app, respx_mock):
    async with playbook_client(app) as c:
        yield c


async def test_default_scenario_reaches_done(client) -> None:
    response = await client.post(URL, json={"yaml": builtin_text()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["terminated"] == "done"
    assert body["error"] is None
    assert trace_of(body) == DEFAULT_TRACE
    # `plan_gate` is `auto` and `merge_gate` is `human`; both took the default port without a human.
    assert body["gates_auto_resolved"] == [
        {"node": "plan_gate", "visit": 1, "gate": "plan"},
        {"node": "merge_gate", "visit": 1, "gate": "merge"},
    ]


async def test_repeated_changes_escalate_via_exhausted(client) -> None:
    scenario = {"outcomes": {"plan_review": ["changes", "changes", "changes", "changes"]}}
    body = (await client.post(URL, json={"yaml": builtin_text(), "scenario": scenario})).json()
    assert body["terminated"] == "escalated"
    trace = trace_of(body)
    # revise_plan allows 3 visits; the 4th entry is redirected through its `exhausted` port to escalate.
    assert [t for t in trace if t[0] == "revise_plan"] == [
        ("revise_plan", v, "plan_ready") for v in (1, 2, 3)
    ]
    assert [t for t in trace if t[0] == "plan_review"] == [
        ("plan_review", v, "changes") for v in (1, 2, 3, 4)
    ]
    assert trace[-1] == ("escalate", 1, None)
    assert trace.count(("escalate", 1, None)) == 1
    assert "revise_plan" not in {t[0] for t in trace[trace.index(("plan_review", 4, "changes")) :]}


async def test_a_scripted_escalate_port_stops_at_the_escalate_node(client) -> None:
    scenario = {"outcomes": {"ci": ["fail"], "fix": ["failed"]}}
    body = (await client.post(URL, json={"yaml": builtin_text(), "scenario": scenario})).json()
    assert body["terminated"] == "escalated"
    assert trace_of(body)[-2:] == [("fix", 1, "failed"), ("escalate", 1, None)]


async def test_a_loop_is_stopped_by_max_steps(client) -> None:
    body = (
        await client.post(URL, json={"yaml": fixture_text("looping.yaml"), "scenario": {"max_steps": 37}})
    ).json()
    assert body["terminated"] == "max_steps"
    assert len(body["trace"]) == 37
    assert [t["node"] for t in body["trace"][:4]] == ["ping", "pong", "ping", "pong"]


async def test_the_default_cap_is_200(client) -> None:
    body = (await client.post(URL, json={"yaml": fixture_text("looping.yaml")})).json()
    assert (body["terminated"], len(body["trace"])) == ("max_steps", 200)


@pytest.mark.parametrize("cap", [0, 1001])
async def test_max_steps_is_bounded(client, cap: int) -> None:
    response = await client.post(URL, json={"yaml": builtin_text(), "scenario": {"max_steps": cap}})
    assert response.status_code == 422


async def test_a_run_routed_to_failed_ends_in_error(client) -> None:
    # The looping nodes exhaust their visits and the `exhausted` edge routes to @failed.
    short = fixture_text("looping.yaml").replace("1000", "3")
    body = (await client.post(URL, json={"yaml": short})).json()
    assert body["terminated"] == "error"
    assert body["error"] == "failed"
    assert len(body["trace"]) == 6


async def test_invalid_yaml_is_a_422_with_the_issues(client) -> None:
    response = await client.post(URL, json={"yaml": fixture_text("unrouted_port.yaml")})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "invalid playbook"
    assert [i["code"] for i in body["issues"]] == ["unrouted_port"]


@pytest.mark.parametrize(
    ("outcomes", "needle"),
    [
        ({"ghost": ["approve"]}, "unknown node 'ghost'"),
        ({"plan_review": ["approve", "maybe"]}, "node 'plan_review' has no port 'maybe'"),
    ],
)
async def test_a_bad_scenario_is_a_422(client, outcomes: dict, needle: str) -> None:
    response = await client.post(URL, json={"yaml": builtin_text(), "scenario": {"outcomes": outcomes}})
    assert response.status_code == 422
    assert response.json() == {"detail": f"bad scenario: {needle}"}


async def test_a_timeout_returns_the_trace_so_far(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dry_run_module, "DRY_RUN_TIMEOUT_S", 0.0)
    response = await client.post(
        URL, json={"yaml": fixture_text("looping.yaml"), "scenario": {"max_steps": 1000}}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["terminated"], body["error"]) == ("error", "timeout")
    assert len(body["trace"]) < 1000


class SlowExecutor(ScriptedExecutor):
    async def advance(self, step, node, ctx):
        await asyncio.sleep(0.01)
        return await super().advance(step, node, ctx)


def slow_registry(playbook, outcomes, trace, gates):
    registry = ExecutorRegistry()
    executor = SlowExecutor(outcomes, trace, gates)
    for kind in {node.kind for node in playbook.nodes.values()}:
        registry.register(kind, executor)
    return registry


async def test_a_slow_run_times_out(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dry_run_module, "DRY_RUN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(dry_run_module, "scripted_registry", slow_registry)
    body = (
        await client.post(URL, json={"yaml": fixture_text("looping.yaml"), "scenario": {"max_steps": 1000}})
    ).json()
    assert (body["terminated"], body["error"]) == ("error", "timeout")
    assert 0 < len(body["trace"]) < 1000


async def test_the_app_store_is_untouched(client, app) -> None:
    store = app.state.store
    tables = [r["name"] for r in store.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert {"runs", "steps", "events", "outbox"} <= set(tables)

    def counts() -> dict[str, int]:
        return {t: store.query(f'SELECT COUNT(*) AS n FROM "{t}"')[0]["n"] for t in tables}

    before = counts()
    for scenario in (None, {"outcomes": {"plan_review": ["changes"] * 4}}):
        assert (
            await client.post(URL, json={"yaml": builtin_text(), "scenario": scenario})
        ).status_code == 200
    assert counts() == before


async def test_the_dry_run_function_directly() -> None:
    playbook = load_playbook(builtin_text())
    result = await dry_run(playbook, outcomes={})
    assert result.terminated == "done"
    assert [(t.node, t.outcome) for t in result.trace][-1] == ("merge", "merged")
