"""``find_planner_draft`` (M4.7): the Planner API's usable draft for a plan sha, found across runs on GitHub."""

import pytest

from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.planner.proposal_store import find_planner_draft, save_proposal
from harness.store.db import Store
from tests.planner.test_proposal_store import SHA, SHA2, make_plan
from tests.planner.world import CONTROL, REPO, World

DRAFT_A = f"{REPO}#planner-aaaa"
DRAFT_B = f"{REPO}#planner-bbbb"


@pytest.fixture
def store():
    s = Store(":memory:")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def outbox(store: Store, gh: GitHub) -> Outbox:
    async def no_sleep(_: float) -> None:
        return None

    return Outbox(store, gh, sleep=no_sleep)


async def put(store, outbox, run_id, *, sha=SHA, status="ready", rev=1, tag="") -> None:
    await save_proposal(
        store,
        outbox,
        repo=REPO,
        control_issue=CONTROL,
        run_id=run_id,
        plan_sha=sha,
        proposal=make_plan(tag=tag),
        status=status,
        rev=rev,
    )


async def find(gh: GitHub, sha: str = SHA):
    return await find_planner_draft(gh, repo=REPO, control_issue=CONTROL, plan_sha=sha)


async def test_no_draft_is_none(world: World, gh: GitHub) -> None:
    assert await find(gh) is None


async def test_only_validated_planner_drafts_of_the_same_plan_count(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    await put(store, outbox, DRAFT_A, status="drafting")
    await put(store, outbox, f"{REPO}#planner-iiii", status="invalid")
    await put(store, outbox, f"{REPO}#7@1")  # a run's own proposal
    await put(store, outbox, "other/repo#planner-cccc")
    await put(store, outbox, DRAFT_B, sha=SHA2)
    assert await find(gh) is None
    for status in ("ready", "creating", "created"):
        await put(store, outbox, DRAFT_A, status=status)
        found = await find(gh)
        assert found is not None and (found.run_id, found.status) == (DRAFT_A, status)


async def test_the_most_edited_draft_wins_and_a_tie_goes_to_the_newest(
    world: World, gh: GitHub, store: Store, outbox: Outbox
) -> None:
    await put(store, outbox, DRAFT_A, rev=3, tag=" A")
    await put(store, outbox, DRAFT_B, rev=1, tag=" B")
    found = await find(gh)
    assert found is not None and found.run_id == DRAFT_A and found.rev == 3
    await put(store, outbox, DRAFT_B, rev=3, tag=" B")
    found = await find(gh)
    assert found is not None and found.run_id == DRAFT_B
