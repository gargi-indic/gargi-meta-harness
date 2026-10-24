"""POST /api/planner/proposals/{id}/approve and the create job: the gates, the spy, idempotence, restarts (M4.5)."""

import asyncio
from typing import Any

import pytest

from harness.github.outbox import OutboxOpFailed
from harness.planner.materialize import MaterializeContext, MaterializeError, materialize, plan_run_key
from harness.planner.proposal import ProposalIssue, ProposedPlan
from harness.planner.proposal_store import read_proposals
from harness.server.events import EVT_PLANNER_UPDATED, EventBus, EventLogTailer, types_for_kind
from tests.server.helpers import make_store
from tests.server.planner_helpers import CONTROL, good_plan, make_rig

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)


class Spy:
    """Stands in for ``materialize``: records the calls, can fail or wait, and otherwise runs the real one."""

    def __init__(self) -> None:
        self.fail: list[Exception] = []
        self.calls: list[tuple[ProposedPlan, MaterializeContext]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, plan: ProposedPlan, ctx: MaterializeContext) -> Any:
        self.calls.append((plan, ctx))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise self.fail.pop(0)
        return await materialize(plan, ctx)


@pytest.fixture
def spy() -> Spy:
    return Spy()


@pytest.fixture
async def rig(respx_mock, spy):
    r = make_rig(respx_mock, materialize_fn=spy)
    r.hub.approve_plan()
    yield r
    await r.aclose()


@pytest.fixture
async def ready(rig):
    return await rig.drafted()


def detail(response) -> dict[str, Any]:
    return response.json()["detail"]


async def test_approving_creates_the_issues_once_and_the_job_reaches_done(rig, ready, spy) -> None:
    response = await rig.approve(ready)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert job_id == f"{ready}.create" and response.json()["status"] == "running"
    await rig.settle()

    job = await rig.job(job_id)
    assert job["status"] == "done" and job["error"] is None and job["kind"] == "create"
    result = job["result"]
    assert set(result["issues"]) == {"a", "b"} and result["created"] == ["a", "b"] and result["reused"] == []
    assert result["links_created"] == 3  # two sub-issue links and one dependency
    assert len(spy.calls) == 1
    plan, ctx = spy.calls[0]
    assert plan == ProposedPlan.model_validate(good_plan("a", "b"))
    assert ctx.run_key == plan_run_key(plan) and ctx.control_issue == CONTROL and ctx.repo == "acme/repo0"
    assert ctx.agents == ["jules"] and "plan-build-review" in (ctx.playbooks or [])

    # The issues exist on GitHub, the proposal is `created` there and in the mirror.
    assert [i["title"] for i in rig.hub.created()] == ["The epic", "Do a", "Do b"]
    [stored] = read_proposals(rig.hub.comments[CONTROL])
    assert stored.status == "created" and stored.meta["result"] == result
    assert rig.hub.ledger()["proposal"]["status"] == "created"
    proposal = await rig.proposal(ready)
    assert proposal["status"] == "created" and proposal["saved"] is True
    assert [e["what"] for e in rig.events()][-2:] == ["creating", "created"]


async def test_approving_twice_returns_the_same_job_and_creates_nothing_twice(rig, ready, spy) -> None:
    first = await rig.approve(ready)
    again = await rig.approve(ready)  # while it runs
    assert again.status_code == 202 and again.json()["job_id"] == first.json()["job_id"]
    await rig.settle()
    after = await rig.approve(ready)  # once it is done
    assert after.status_code == 202 and after.json()["job_id"] == first.json()["job_id"]
    assert after.json()["status"] == "done" and after.json()["result"]["created"] == ["a", "b"]
    assert len(spy.calls) == 1 and len(rig.hub.created()) == 3


async def test_a_created_proposal_cannot_be_edited(rig, ready) -> None:
    await rig.approve(ready)
    await rig.settle()
    response = await rig.put(ready, good_plan("a"))
    assert response.status_code == 409 and detail(response)["reason"] == "proposal_created"


async def test_validation_errors_block_the_approval(rig, ready, spy) -> None:
    broken = good_plan("a", "b")
    broken["issues"][1]["blocked_by"] = ["ghost"]
    await rig.put(ready, broken)
    response = await rig.approve(ready)
    assert response.status_code == 409 and detail(response)["reason"] == "validation"
    assert [i["code"] for i in detail(response)["issues"]] == ["unknown_dep"]
    assert spy.calls == []


async def test_a_pending_plan_approval_blocks_with_the_reason(respx_mock, spy) -> None:
    rig = make_rig(respx_mock, materialize_fn=spy)  # the plan was never approved
    try:
        proposal_id = await rig.drafted()
        response = await rig.approve(proposal_id)
        assert response.status_code == 409
        assert detail(response)["reason"] == "plan_pending" and "pending" in detail(response)["message"]
        assert spy.calls == []
    finally:
        await rig.aclose()


async def test_a_plan_changed_after_its_approval_blocks(rig, ready, spy) -> None:
    rig.hub.put_plan("# The plan\n\nChanged.\n", version=2)
    response = await rig.approve(ready)
    # The proposal was made for the old plan: that is the first thing to say.
    assert response.status_code == 409 and detail(response)["reason"] == "plan_changed"

    # Decompose again for the new plan: its approval is stale until someone approves that version.
    rig.backend._responses.append(good_plan("a", "b"))
    second = await rig.drafted()
    stale = await rig.approve(second)
    assert stale.status_code == 409 and detail(stale)["reason"] == "plan_stale"
    assert spy.calls == []

    rig.hub.approve_plan()
    rig.store.execute("DELETE FROM meta WHERE key LIKE 'ledger:%'")  # the mirror of the old approval
    assert (await rig.approve(second)).status_code == 202


async def test_a_rev_that_moved_on_after_the_validation_blocks(rig, ready, spy) -> None:
    validated = (await rig.validate(ready))["rev"]
    await rig.put(ready, good_plan("a", "b", "c"))  # someone edits after the human looked
    response = await rig.approve(ready, rev=validated)
    assert response.status_code == 409 and detail(response)["reason"] == "rev_changed"
    assert spy.calls == []
    assert (await rig.approve(ready, rev=validated + 1)).status_code == 202


async def test_a_draft_nobody_validated_must_be_validated_first(rig, ready, spy) -> None:
    rig.store.execute("DELETE FROM meta WHERE key LIKE 'planner:validated:%'")  # e.g. a new database
    response = await rig.approve(ready)
    assert response.status_code == 409 and detail(response)["reason"] == "not_validated"
    await rig.validate(ready)
    assert (await rig.approve(ready)).status_code == 202


async def test_a_proposal_that_is_still_drafting_cannot_be_approved(respx_mock, spy) -> None:
    rig = make_rig(respx_mock, gated=True, materialize_fn=spy)
    try:
        proposal_id = (await rig.decompose()).json()["proposal_id"]
        drafting = await rig.approve(proposal_id)
        assert drafting.status_code == 409 and detail(drafting)["reason"] == "proposal_drafting"
        assert (await rig.approve("acme~repo0~0123456789ab")).status_code == 404
    finally:
        await rig.aclose()


async def test_a_failed_decomposition_cannot_be_approved(respx_mock, spy) -> None:
    bad = good_plan("a")
    bad["issues"][0]["blocked_by"] = ["ghost"]
    rig = make_rig(respx_mock, responses=[bad, bad, bad], materialize_fn=spy)
    try:
        proposal_id = await rig.drafted()
        response = await rig.approve(proposal_id)
        assert response.status_code == 409 and detail(response)["reason"] == "no_proposal"
    finally:
        await rig.aclose()


async def test_the_control_issue_can_be_given(rig, ready, spy) -> None:
    other = rig.hub._add_issue("Other control", "Control issue.\n\nIntro.\n\nOutro.\n", [])["number"]
    rig.hub.approve_plan(number=other)
    assert (await rig.approve(ready, control_issue=other)).status_code == 202
    await rig.settle()
    assert spy.calls[0][1].control_issue == other


async def test_a_failed_creation_is_a_failed_job_and_approving_again_retries(rig, ready, spy) -> None:
    spy.fail = [OutboxOpFailed("outbox op 7 (create_issue) failed: boom")]
    job_id = (await rig.approve(ready)).json()["job_id"]
    await rig.settle()
    job = await rig.job(job_id)
    assert job["status"] == "failed" and "boom" in job["error"] and job["result"] is None
    proposal = await rig.proposal(ready)
    assert proposal["status"] == "ready" and proposal["saved"] is True  # editable again
    assert rig.hub.ledger()["proposal"]["status"] == "ready"

    retry = await rig.approve(ready)
    assert retry.status_code == 202 and retry.json()["job_id"] == job_id
    await rig.settle()
    assert (await rig.job(job_id))["status"] == "done" and len(spy.calls) == 2
    assert len(rig.hub.created()) == 3
    assert [e["what"] for e in rig.events()][-4:] == ["creating", "create_failed", "creating", "created"]


async def test_a_refused_plan_reports_the_problems(rig, ready, spy) -> None:
    spy.fail = [
        MaterializeError("the plan is not valid", [ProposalIssue("unknown_agent", "a", "no such agent")])
    ]
    job_id = (await rig.approve(ready)).json()["job_id"]
    await rig.settle()
    job = await rig.job(job_id)
    assert job["status"] == "failed" and [i["code"] for i in job["issues"]] == ["unknown_agent"]


async def test_an_unexpected_crash_is_recorded_in_the_job_not_lost(rig, ready, spy) -> None:
    spy.fail = [RuntimeError("kaboom")]
    job_id = (await rig.approve(ready)).json()["job_id"]
    await rig.settle()
    job = await rig.job(job_id)
    assert job["status"] == "failed" and "kaboom" in job["error"]
    assert (await rig.proposal(ready))["status"] == "ready"


async def test_a_restart_during_the_creation_resumes_it_without_duplicates(
    rig, ready, spy, respx_mock
) -> None:
    spy.gate = asyncio.Event()  # the process dies inside materialize
    job_id = (await rig.approve(ready)).json()["job_id"]
    for _ in range(100):
        await asyncio.sleep(0)
    assert len(spy.calls) == 1
    await rig.service.aclose()
    assert rig.store.query("SELECT status FROM proposals")[0]["status"] == "creating"

    again_spy = Spy()
    again = make_rig(respx_mock, hub=rig.hub, store=rig.store, materialize_fn=again_spy)
    try:
        assert (await again.job(job_id))["status"] == "running"  # the read resumed the creation
        await again.settle()
        job = await again.job(job_id)
        assert job["status"] == "done" and job["result"]["created"] == ["a", "b"]
        assert len(again_spy.calls) == 1 and again_spy.calls[0][1].run_key == spy.calls[0][1].run_key
        assert len(rig.hub.created()) == 3
        repeated = await again.approve(ready)
        assert repeated.json()["job_id"] == job_id and len(again_spy.calls) == 1
    finally:
        await again.aclose()


async def test_the_job_is_read_from_github_when_the_database_is_gone(rig, ready, respx_mock) -> None:
    job_id = (await rig.approve(ready)).json()["job_id"]
    await rig.settle()
    fresh = make_rig(respx_mock, hub=rig.hub, store=make_store())
    try:
        job = await fresh.job(job_id)
        assert job["status"] == "done" and set(job["result"]["issues"]) == {"a", "b"}
    finally:
        await fresh.aclose()


def test_planner_rows_publish_planner_updated_over_sse() -> None:
    assert types_for_kind("planner_updated") == (EVT_PLANNER_UPDATED,) == ("planner.updated",)


async def test_the_tailer_publishes_the_planner_events_of_a_job(rig, ready) -> None:
    bus = EventBus()
    tailer = EventLogTailer(rig.store, bus, start_after=0)
    await rig.approve(ready)
    await rig.settle()
    await tailer.poll_once()
    bus.close()
    events = [e async for e in bus.subscribe(0)]
    planner = [e for e in events if e.type == "planner.updated"]
    assert [e.data["data"]["what"] for e in planner] == ["drafting", "drafted", "creating", "created"]
    assert all(e.data["repo"] == "acme/repo0" and e.data["kind"] == "planner_updated" for e in planner)
