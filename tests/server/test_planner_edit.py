"""PUT/GET/validate of a proposal: invalid drafts are stored, rev and If-Match, saves that are still queued (M4.5)."""

import pytest

from harness.planner.proposal_store import read_proposals
from harness.server import planner_service
from tests.server.planner_helpers import CONTROL, good_issue, good_plan, make_rig

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)


@pytest.fixture
async def rig(respx_mock):
    r = make_rig(respx_mock)
    yield r
    await r.aclose()


@pytest.fixture
async def drafted(rig):
    return await rig.drafted()


def edit_of(*keys: str, **over):
    plan = good_plan(*keys)
    plan.update(over)
    return plan


def github_rev(rig) -> int:
    [stored] = read_proposals(rig.hub.comments[CONTROL])
    return stored.rev


async def test_a_valid_edit_bumps_the_rev_and_is_saved_to_github(rig, drafted) -> None:
    assert (await rig.proposal(drafted))["rev"] == 2
    response = await rig.put(drafted, edit_of("a", "b", "c"), **{"If-Match": '"2"'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rev"] == 3 and body["status"] == "ready" and body["issues_validation"] == []
    assert response.headers["etag"] == '"3"'
    assert [i["key"] for i in body["proposal"]["issues"]] == ["a", "b", "c"]
    await rig.settle()
    assert (await rig.proposal(drafted))["saved"] is True
    assert github_rev(rig) == 3
    [stored] = read_proposals(rig.hub.comments[CONTROL])
    assert [i.key for i in stored.proposal.issues] == ["a", "b", "c"]
    assert rig.hub.ledger()["proposal"]["issues"] == 3


async def test_an_invalid_draft_is_stored_with_its_validation_issues(rig, drafted) -> None:
    broken = edit_of("a", "b")
    broken["issues"][1]["blocked_by"] = ["ghost"]
    broken["issues"][0]["agent"] = "nobody"
    response = await rig.put(drafted, broken)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "invalid" and body["rev"] == 3
    assert {(i["code"], i["key"]) for i in body["issues_validation"]} == {
        ("unknown_dep", "b"),
        ("unknown_agent", "a"),
    }
    await rig.settle()
    # It is the draft now: read again, it is still there, and on GitHub.
    again = await rig.proposal(drafted)
    assert again["status"] == "invalid" and again["proposal"]["issues"][1]["blocked_by"] == ["ghost"]
    [stored] = read_proposals(rig.hub.comments[CONTROL])
    assert stored.status == "invalid" and stored.proposal.issues[1].blocked_by == ["ghost"]
    # A fix makes it ready again.
    assert (await rig.put(drafted, edit_of("a", "b"))).json()["status"] == "ready"


async def test_validate_reports_the_issues_and_writes_nothing(rig, drafted) -> None:
    broken = edit_of("a", "b")
    broken["issues"][1]["acceptance"] = []
    await rig.put(drafted, broken)
    await rig.settle()
    before = len(rig.hub.calls)
    writes_before = [c for c in rig.hub.calls if c[0] != "GET"]
    result = await rig.validate(drafted)
    assert result["ok"] is False and result["rev"] == 3
    assert [(i["code"], i["key"]) for i in result["issues"]] == [("no_acceptance", "b")]
    assert [c for c in rig.hub.calls if c[0] != "GET"] == writes_before
    assert len(rig.hub.calls) >= before
    assert (await rig.proposal(drafted))["rev"] == 3
    assert len(rig.events()) == 3  # drafting, drafted, one edit: validate added none


async def test_if_match_must_carry_the_current_rev(rig, drafted) -> None:
    stale = await rig.put(drafted, edit_of("a", "b", "c"), **{"If-Match": '"1"'})
    assert stale.status_code == 412
    assert (await rig.proposal(drafted))["rev"] == 2
    assert (await rig.put(drafted, edit_of("a", "b", "c"), **{"If-Match": "2"})).json()["rev"] == 3
    assert (await rig.put(drafted, edit_of("a"), **{"If-Match": 'W/"2"'})).status_code == 412
    assert (await rig.put(drafted, edit_of("a"), **{"If-Match": "abc"})).status_code == 400
    assert (await rig.put(drafted, edit_of("a"), **{"If-Match": "*"})).json()["rev"] == 4
    get = await rig.client.get(f"/api/planner/proposals/{drafted}")
    assert get.headers["etag"] == '"4"'


async def test_putting_the_same_draft_again_changes_nothing(rig, drafted) -> None:
    first = await rig.put(drafted, edit_of("a", "b", "c"))
    await rig.settle()
    events = len(rig.events())
    writes = rig.hub.count("PATCH", "")
    again = await rig.put(drafted, edit_of("a", "b", "c"))
    assert again.json()["rev"] == first.json()["rev"] == 3
    await rig.settle()
    assert len(rig.events()) == events and rig.hub.count("PATCH", "") == writes


async def test_a_structurally_broken_body_is_a_422_and_stores_nothing(rig, drafted) -> None:
    for body in (
        {"epic_title": "x"},
        {**good_plan("a"), "surprise": 1},
        {**good_plan("a"), "issues": []},
        {**good_plan("a"), "issues": [{**good_issue("a"), "key": "Not A Key"}]},
    ):
        response = await rig.put(drafted, body)
        assert response.status_code == 422, body
        assert isinstance(response.json()["detail"], list)
    assert (await rig.client.put(f"/api/planner/proposals/{drafted}", content="[]")).status_code == 422
    assert (await rig.proposal(drafted))["rev"] == 2


async def test_only_a_ready_or_invalid_proposal_can_be_edited(respx_mock) -> None:
    rig = make_rig(respx_mock, gated=True)
    try:
        proposal_id = (await rig.decompose()).json()["proposal_id"]
        response = await rig.put(proposal_id, good_plan("a"))
        assert response.status_code == 409 and response.json()["detail"]["reason"] == "proposal_drafting"
        assert (await rig.put("acme~repo0~0123456789ab", good_plan("a"))).status_code == 404
    finally:
        await rig.aclose()


async def test_a_failed_decomposition_can_be_replaced_by_a_hand_written_draft(respx_mock) -> None:
    bad = good_plan("a")
    bad["issues"][0]["blocked_by"] = ["ghost"]
    rig = make_rig(respx_mock, responses=[bad, bad, bad])
    try:
        proposal_id = await rig.drafted()
        failed = await rig.proposal(proposal_id)
        assert failed["status"] == "invalid" and failed["proposal"] is None
        response = await rig.put(proposal_id, good_plan("a", "b"))
        assert response.status_code == 200 and response.json()["status"] == "ready"
        assert (await rig.job(f"{proposal_id}.decompose"))["status"] == "done"
    finally:
        await rig.aclose()


# ---- reads and edits while a save is still queued ---------------------------------------------------------


async def test_reads_show_the_pending_draft_until_github_has_it(rig, drafted) -> None:
    async with rig.service._write_lock:  # the saver cannot run, as when the outbox is throttled
        response = await rig.put(drafted, edit_of("a", "b", "c"))
        assert response.json()["saved"] is False and response.json()["rev"] == 3
        pending = await rig.proposal(drafted)
        assert pending["saved"] is False and pending["rev"] == 3
        assert [i["key"] for i in pending["proposal"]["issues"]] == ["a", "b", "c"]
        assert github_rev(rig) == 2  # GitHub and the mirror still have the old draft
        assert rig.store.query("SELECT 1 FROM meta WHERE key LIKE 'planner:pending:%'")
        # The rev keeps growing and If-Match is checked against the pending draft.
        assert (await rig.put(drafted, edit_of("a", "b"), **{"If-Match": "2"})).status_code == 412
        assert (await rig.put(drafted, edit_of("a", "b"), **{"If-Match": "3"})).json()["rev"] == 4
    await rig.settle()
    done = await rig.proposal(drafted)
    assert done["saved"] is True and done["rev"] == 4 and len(done["proposal"]["issues"]) == 2
    assert github_rev(rig) == 4
    assert rig.store.query("SELECT 1 FROM meta WHERE key LIKE 'planner:pending:%'") == []


async def test_rapid_edits_coalesce_into_one_save_of_the_newest_draft(rig, drafted) -> None:
    before = rig.hub.count("PATCH", "")
    async with rig.service._write_lock:
        for n in range(1, 8):
            plan = edit_of("a", "b")
            plan["epic_title"] = f"Edit {n}"
            assert (await rig.put(drafted, plan)).json()["rev"] == 2 + n
    await rig.settle()
    after = await rig.proposal(drafted)
    assert after["rev"] == 9 and after["proposal"]["epic_title"] == "Edit 7" and after["saved"] is True
    [stored] = read_proposals(rig.hub.comments[CONTROL])
    assert stored.rev == 9 and stored.proposal.epic_title == "Edit 7"
    # One comment edit and one ledger edit for seven edits, not fourteen.
    assert rig.hub.count("PATCH", "") - before == 2


async def test_a_failed_save_keeps_the_draft_pending_and_is_retried(rig, drafted, monkeypatch) -> None:
    monkeypatch.setattr(planner_service, "RETRY_FLUSH_AFTER_S", 0.0)
    rig.hub.down.add(("PATCH", ""))
    response = await rig.put(drafted, edit_of("a", "b", "c"))
    assert response.status_code == 200 and response.json()["rev"] == 3
    await rig.settle()
    failed = await rig.proposal(drafted)  # the read retries once, and fails again
    await rig.settle()
    failed = await rig.proposal(drafted)
    assert failed["saved"] is False and failed["rev"] == 3 and failed["save_error"]
    assert [i["key"] for i in failed["proposal"]["issues"]] == ["a", "b", "c"]
    assert github_rev(rig) == 2
    # An edit on top of it works against the pending rev ...
    rig.hub.down.clear()
    assert (await rig.put(drafted, edit_of("a", "b", "c", "d"), **{"If-Match": "3"})).json()["rev"] == 4
    await rig.settle()
    done = await rig.proposal(drafted)
    assert done["saved"] is True and done["save_error"] is None and github_rev(rig) == 4


async def test_a_pending_draft_survives_a_restart(rig, drafted, respx_mock) -> None:
    async with rig.service._write_lock:
        await rig.put(drafted, edit_of("a", "b", "c"))
    await rig.service.aclose()  # dies with the save unfinished
    assert github_rev(rig) == 2

    again = make_rig(respx_mock, hub=rig.hub, store=rig.store)
    try:
        read = await again.proposal(drafted)
        assert read["saved"] is False and read["rev"] == 3  # still the user's draft
        await again.settle()
        assert (await again.proposal(drafted))["saved"] is True
        assert github_rev(again) == 3
    finally:
        await again.aclose()
