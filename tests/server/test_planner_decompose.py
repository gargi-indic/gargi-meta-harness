"""POST /api/planner/decompose: the job lifecycle, failures, the one-job-per-repo rule and restarts (M4.5)."""

import pytest

from harness.llm.backend import BackendError
from harness.planner.proposal import ProposedPlan
from harness.planner.proposal_store import read_proposals
from tests.server.helpers import make_store
from tests.server.planner_helpers import CONTROL, PLAN_PATH, REPO, good_plan, make_rig

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)


@pytest.fixture
async def rig(respx_mock):
    r = make_rig(respx_mock, gated=True)
    yield r
    await r.aclose()


async def test_a_decompose_job_goes_from_drafting_to_ready(rig) -> None:
    response = await rig.decompose(guidance="keep it small")
    assert response.status_code == 202, response.text
    proposal_id = response.json()["proposal_id"]
    job_id = response.json()["job_id"]
    assert job_id == f"{proposal_id}.decompose"

    drafting = await rig.proposal(proposal_id)
    assert drafting["status"] == "drafting" and drafting["proposal"] is None
    assert drafting["plan_sha"] == rig.hub.plan_sha() and drafting["plan"] == PLAN_PATH
    assert drafting["issues_validation"] == [] and drafting["saved"] is True
    assert (await rig.job(job_id))["status"] == "running"
    # The drafting state is already on GitHub, as a marked comment and in the ledger summary.
    assert len(rig.hub.proposal_comments()) == 1
    assert rig.hub.ledger()["proposal"]["status"] == "drafting"

    rig.backend.release()
    await rig.settle()
    ready = await rig.proposal(proposal_id)
    assert ready["status"] == "ready" and ready["saved"] is True and ready["issues_validation"] == []
    assert [i["key"] for i in ready["proposal"]["issues"]] == ["a", "b"]
    assert ready["rev"] == 2
    assert ProposedPlan.model_validate(ready["proposal"]) == ProposedPlan.model_validate(good_plan("a", "b"))
    job = await rig.job(job_id)
    assert job["status"] == "done" and job["error"] is None and job["kind"] == "decompose"
    assert rig.hub.ledger()["proposal"]["status"] == "ready"
    assert "keep it small" in rig.backend.calls[0]["prompt"]

    # GitHub holds the finished draft, with its rev, so nothing is lost with the local database.
    [stored] = read_proposals(rig.hub.comments[CONTROL])
    assert stored.status == "ready" and stored.rev == 2 and stored.proposal.epic_title == "The epic"


async def test_every_status_change_appends_a_planner_event(rig) -> None:
    proposal_id = await rig.drafted(gate=True)
    events = rig.events()
    assert [e["what"] for e in events] == ["drafting", "drafted"]
    assert [e["status"] for e in events] == ["drafting", "ready"]
    assert all(e["proposal_id"] == proposal_id for e in events)
    assert events[-1]["rev"] == 2


async def test_a_second_decompose_for_the_same_repo_is_refused_while_one_runs(rig) -> None:
    first = (await rig.decompose()).json()
    second = await rig.decompose()
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "decompose_running"
    assert second.json()["detail"]["proposal_id"] == first["proposal_id"]
    assert len(rig.hub.proposal_comments()) == 1  # nothing was written for the refused call
    rig.backend.release()
    await rig.settle()
    # Once it is finished, the next one is accepted.
    rig.backend._responses.append(good_plan("c"))
    assert (await rig.decompose()).status_code == 202


async def test_a_failing_decomposition_is_invalid_with_its_issue_list(respx_mock) -> None:
    bad = good_plan("a", "b")
    bad["issues"][1]["blocked_by"] = ["ghost"]
    rig = make_rig(respx_mock, responses=[bad, bad, bad])  # the first answer and both repairs
    try:
        proposal_id = await rig.drafted()
        proposal = await rig.proposal(proposal_id)
        assert proposal["status"] == "invalid" and proposal["proposal"] is None
        assert [(i["code"], i["key"]) for i in proposal["issues_validation"]] == [("unknown_dep", "b")]
        job = await rig.job(f"{proposal_id}.decompose")
        assert job["status"] == "failed" and "ghost" in job["error"]
        assert [i["code"] for i in job["issues"]] == ["unknown_dep"]
        assert len(rig.backend.calls) == 3
        # The failure is on GitHub too; a stopped decomposition does not block the next one.
        assert rig.hub.ledger()["proposal"]["status"] == "invalid"
        rig.backend._responses.append(good_plan("a"))
        assert (await rig.decompose()).status_code == 202
    finally:
        await rig.aclose()


async def test_a_model_that_is_down_is_an_invalid_proposal_with_a_backend_error(respx_mock) -> None:
    rig = make_rig(
        respx_mock, responses=[BackendError("no model"), BackendError("no model"), BackendError("no model")]
    )
    try:
        proposal_id = await rig.drafted()
        proposal = await rig.proposal(proposal_id)
        assert proposal["status"] == "invalid"
        assert {i["code"] for i in proposal["issues_validation"]} == {"backend_error"}
        assert (await rig.job(f"{proposal_id}.decompose"))["status"] == "failed"
    finally:
        await rig.aclose()


async def test_an_unreadable_plan_is_refused_before_anything_is_written(rig) -> None:
    response = await rig.decompose(path="docs/NOPE.md")
    assert response.status_code == 404
    assert rig.hub.proposal_comments() == []
    assert (await rig.client.post("/api/planner/decompose", json={"repo": REPO})).status_code == 422
    both = await rig.client.post("/api/planner/decompose", json={"repo": REPO, "path": PLAN_PATH, "issue": 1})
    assert both.status_code == 422
    unknown = await rig.client.post("/api/planner/decompose", json={"repo": "x/y", "path": PLAN_PATH})
    assert unknown.status_code == 404
    extra = await rig.client.post("/api/planner/decompose", json={"repo": REPO, "path": PLAN_PATH, "x": 1})
    assert extra.status_code == 422


async def test_without_a_control_issue_it_asks_for_one(respx_mock) -> None:
    rig = make_rig(respx_mock, control_issue=None)
    try:
        refused = await rig.decompose()
        assert refused.status_code == 422 and refused.json()["detail"]["reason"] == "no_control_issue"
        accepted = await rig.decompose(control_issue=CONTROL)
        assert accepted.status_code == 202
        await rig.settle()
        assert (await rig.proposal(accepted.json()["proposal_id"]))["status"] == "ready"
    finally:
        await rig.aclose()


async def test_an_issue_can_be_decomposed(rig) -> None:
    issue = rig.hub._add_issue("Plan issue", "# Plan issue\n\n## Goal\n", [])
    response = await rig.client.post("/api/planner/decompose", json={"repo": REPO, "issue": issue["number"]})
    assert response.status_code == 202, response.text
    rig.backend.release()
    await rig.settle()
    proposal = await rig.proposal(response.json()["proposal_id"])
    assert proposal["status"] == "ready" and proposal["plan"] == f"#{issue['number']}"


async def test_a_plan_that_changes_while_it_is_decomposed_ends_invalid(rig) -> None:
    proposal_id = (await rig.decompose()).json()["proposal_id"]
    await rig.service.aclose()
    rig.hub.put_plan("# Changed\n", version=2)
    await rig.proposal(proposal_id)  # the read starts the decomposition again
    rig.backend.release()
    await rig.settle()
    proposal = await rig.proposal(proposal_id)
    assert proposal["status"] == "invalid"
    assert proposal["issues_validation"][0]["code"] == "plan_unreadable"


async def test_a_restart_during_the_decomposition_resumes_it(rig, respx_mock) -> None:
    proposal_id = (await rig.decompose()).json()["proposal_id"]
    await rig.service.aclose()  # the process dies with the job unfinished
    assert rig.store.query("SELECT status FROM proposals")[0]["status"] == "drafting"

    again = make_rig(respx_mock, hub=rig.hub, store=rig.store, responses=[good_plan("a", "b")])
    try:
        assert (await again.job(f"{proposal_id}.decompose"))["status"] == "running"  # the read resumed it
        await again.settle()
        assert (await again.proposal(proposal_id))["status"] == "ready"
        assert (await again.job(f"{proposal_id}.decompose"))["status"] == "done"
        assert len(again.backend.calls) == 1
    finally:
        await again.aclose()


async def test_a_second_decompose_after_a_restart_is_refused_and_resumes_the_first(rig, respx_mock) -> None:
    first = (await rig.decompose()).json()["proposal_id"]
    await rig.service.aclose()
    again = make_rig(respx_mock, hub=rig.hub, store=rig.store)
    try:
        refused = await again.decompose()
        assert refused.status_code == 409 and refused.json()["detail"]["proposal_id"] == first
        await again.settle()
        assert (await again.proposal(first))["status"] == "ready"
    finally:
        await again.aclose()


async def test_with_the_local_database_gone_the_proposal_is_read_from_github(rig, respx_mock) -> None:
    proposal_id = await rig.drafted(gate=True)
    fresh = make_store()
    again = make_rig(respx_mock, hub=rig.hub, store=fresh)
    try:
        assert fresh.query("SELECT 1 FROM proposals") == []
        proposal = await again.proposal(proposal_id)
        assert proposal["status"] == "ready" and proposal["rev"] == 2 and proposal["saved"] is True
        assert [i["key"] for i in proposal["proposal"]["issues"]] == ["a", "b"]
        assert fresh.query("SELECT status FROM proposals")[0]["status"] == "ready"  # the mirror is refilled
        assert (await again.job(f"{proposal_id}.decompose"))["status"] == "done"
    finally:
        await again.aclose()


async def test_unknown_ids_are_404(rig) -> None:
    for path in (
        "/api/planner/proposals/nonsense",
        "/api/planner/proposals/acme~repo0~0123456789ab",
        "/api/planner/proposals/other~repo~0123456789ab",
        "/api/planner/jobs/nonsense",
        "/api/planner/jobs/acme~repo0~0123456789ab.decompose",
        "/api/planner/jobs/acme~repo0~0123456789ab.other",
    ):
        assert (await rig.client.get(path)).status_code == 404, path


async def test_a_job_that_was_never_started_is_unknown(rig) -> None:
    proposal_id = await rig.drafted(gate=True)
    assert (await rig.client.get(f"/api/planner/jobs/{proposal_id}.create")).status_code == 404
