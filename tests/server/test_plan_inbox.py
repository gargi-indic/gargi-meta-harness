"""A pending campaign plan gate (M4.1) is an inbox item of kind plan_approval that cannot be approved from the UI."""

from urllib.parse import quote

from harness.store.repo import steps_for_run, update_step
from tests.server.test_actions import snapshot


def as_campaign_gate(store, step_id: str) -> None:
    step = next(
        s
        for run in store.query("SELECT id FROM runs")
        for s in steps_for_run(store, run["id"])
        if s.id == step_id
    )
    output = {k: v for k, v in (step.output or {}).items() if k != "gate_id"}
    update_step(store, step_id, output={**output, "plan_sha": "a" * 40, "plan_status": "pending"})


async def test_a_campaign_plan_gate_is_a_plan_approval_item(client, store, seed) -> None:
    as_campaign_gate(store, seed.plan_gate_step)
    items = (await client.get("/api/inbox")).json()
    item = next(i for i in items if i["step_id"] == seed.plan_gate_step)
    assert item["kind"] == "plan_approval" and item["gate_id"] is None


async def test_approving_it_in_the_ui_is_refused_and_changes_nothing(client, store, seed) -> None:
    as_campaign_gate(store, seed.plan_gate_step)
    before = snapshot(store)
    response = await client.post(f"/api/inbox/{quote(seed.plan_gate_step, safe='')}/approve")
    assert response.status_code == 422
    assert "harness:plan-approved" in response.json()["detail"]
    assert snapshot(store) == before
