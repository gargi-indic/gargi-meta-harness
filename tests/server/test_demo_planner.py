"""Demo mode planner (M4.6): the demo app decomposes the demo plan, takes an edit and creates five issues."""

import httpx

from harness.server.demo import create_demo_app
from harness.server.demo_planner import DEMO_PLAN_PATH, DemoPlannerHub
from harness.server.demo_seed import REPO

PORT = 8799


async def test_demo_planner_decompose_edit_approve_creates_five_issues() -> None:
    app, token = create_demo_app(token="t", port=PORT, interval_s=0)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"Cookie": f"harness_token={token}"},
    )
    service = app.state.planner
    hub: DemoPlannerHub = app.state.demo_planner_hub
    try:
        options = (await client.get("/api/planner/options", params={"repo": REPO})).json()
        assert [a["name"] for a in options["agents"]] == ["claude", "codex", "jules"]
        assert "plan-build-review" in options["playbooks"]

        plan = (await client.get("/api/planner/plan", params={"repo": REPO, "path": DEMO_PLAN_PATH})).json()
        assert plan["approval"]["status"] == "approved"
        assert plan["headings"][0] == "Widget platform"

        accepted = await client.post("/api/planner/decompose", json={"repo": REPO, "path": DEMO_PLAN_PATH})
        assert accepted.status_code == 202
        proposal_id = accepted.json()["proposal_id"]
        await service.idle()

        proposal = (await client.get(f"/api/planner/proposals/{proposal_id}")).json()
        assert proposal["status"] == "ready"
        assert proposal["saved"] is True
        assert len(proposal["proposal"]["issues"]) == 5
        assert proposal["issues_validation"] == []

        draft = proposal["proposal"]
        draft["issues"][4]["blocked_by"] = ["model"]
        edited = await client.put(
            f"/api/planner/proposals/{proposal_id}", json=draft, headers={"If-Match": str(proposal["rev"])}
        )
        assert edited.status_code == 200
        await service.idle()

        approved = await client.post(
            f"/api/planner/proposals/{proposal_id}/approve", json={"rev": edited.json()["rev"]}
        )
        assert approved.status_code == 202, approved.text
        await service.idle()

        job = (await client.get(f"/api/planner/jobs/{approved.json()['job_id']}")).json()
        assert job["status"] == "done"
        assert len(job["result"]["issues"]) == 5
        created = (await client.get("/api/demo/planner-issues")).json()
        assert len(created) == 6  # the epic and five issues
        assert len(hub.created()) == 6
    finally:
        await service.aclose()
        await client.aclose()
