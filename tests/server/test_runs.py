"""GET /api/runs and /api/runs/{id} (M2.2)."""

from harness.store.repo import get_run, update_run
from tests.server.conftest import run_path


async def test_lists_runs_newest_first(client, seed) -> None:
    response = await client.get("/api/runs")
    assert response.status_code == 200
    runs = response.json()
    assert [r["id"] for r in runs] == [seed.run_b, seed.run_a]
    assert runs[0] == {
        "id": seed.run_b,
        "repo": "acme/widgets",
        "issue": 6,
        "title": "Widget import",
        "playbook_id": "plan-build-review",
        "status": "needs-you",
        "agent": "jules",
        "pr": 42,
        "current_node": "escalate",
        "created_at": "2026-01-01T00:20:00Z",
        "updated_at": "2026-01-01T00:29:00Z",
    }
    assert runs[1]["pr"] is None
    assert runs[1]["current_node"] == "plan_gate"


async def test_filters(client, store, seed) -> None:
    update_run(store, seed.run_a, status="done")
    assert [r["id"] for r in (await client.get("/api/runs", params={"status": "done"})).json()] == [
        seed.run_a
    ]
    assert [r["id"] for r in (await client.get("/api/runs", params={"status": "needs-you"})).json()] == [
        seed.run_b
    ]
    assert [r["id"] for r in (await client.get("/api/runs", params={"issue": 5})).json()] == [seed.run_a]
    assert (await client.get("/api/runs", params={"repo": "acme/widgets"})).json()[0]["id"] == seed.run_b
    assert (await client.get("/api/runs", params={"repo": "acme/other"})).json() == []
    assert (await client.get("/api/runs", params={"status": "paused", "issue": 5})).json() == []


async def test_unknown_status_is_422(client) -> None:
    assert (await client.get("/api/runs", params={"status": "bogus"})).status_code == 422
    assert (await client.get("/api/runs", params={"issue": "x"})).status_code == 422


async def test_run_detail(client, seed) -> None:
    response = await client.get(f"/api/runs/{run_path(seed.run_a)}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["run"]["id"] == seed.run_a
    assert detail["playbook"]["id"] == "plan-build-review"
    assert "plan_gate" in detail["playbook"]["nodes"]
    assert [(s["node"], s["visit"]) for s in detail["steps"]] == [
        ("plan", 1),
        ("plan_review", 1),
        ("revise_plan", 1),
        ("plan_review", 2),
        ("plan_gate", 1),
    ]
    assert detail["visit_counts"] == {"plan": 1, "plan_review": 2, "revise_plan": 1, "plan_gate": 1}
    gate = detail["steps"][-1]
    assert gate == {
        "id": seed.plan_gate_step,
        "node": "plan_gate",
        "visit": 1,
        "status": "waiting",
        "outcome": None,
        "ref": {"gate_id": seed.plan_gate_id},
        "started_at": "2026-01-01T00:15:00Z",
        "finished_at": None,
    }
    assert "context" not in detail["run"]
    assert "context" not in detail


async def test_detail_falls_back_to_the_builtin_playbook(client, store, seed) -> None:
    run = get_run(store, seed.run_a)
    assert run is not None
    update_run(store, seed.run_a, context={"agent": "jules"})
    detail = (await client.get(f"/api/runs/{run_path(seed.run_a)}")).json()
    assert detail["playbook"]["id"] == "plan-build-review"
    assert detail["playbook"]["nodes"]["plan_gate"]["kind"] == "gate"


async def test_detail_without_any_playbook_is_null(client, store, seed) -> None:
    store.execute(
        "UPDATE runs SET playbook_id = 'custom-gone', context_json = '{}' WHERE id = ?", (seed.run_a,)
    )
    assert (await client.get(f"/api/runs/{run_path(seed.run_a)}")).json()["playbook"] is None


async def test_pending_steps_sort_last(client, store, seed) -> None:
    from harness.store.repo import StepRow, insert_step

    insert_step(
        store,
        StepRow(
            id=f"{seed.run_a}/implement/1",
            run_id=seed.run_a,
            node_id="implement",
            visit=1,
            status="pending",
            outcome=None,
            ref=None,
            output=None,
            poll_after=None,
            started_at=None,
            ended_at=None,
        ),
    )
    detail = (await client.get(f"/api/runs/{run_path(seed.run_a)}")).json()
    assert detail["steps"][-1]["node"] == "implement"
    assert detail["run"]["current_node"] == "implement"


async def test_unknown_run_is_404(client) -> None:
    response = await client.get("/api/runs/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown run"}


async def test_requires_the_cookie(anon, seed) -> None:
    assert (await anon.get("/api/runs")).status_code == 401
    assert (await anon.get(f"/api/runs/{run_path(seed.run_a)}")).status_code == 401
