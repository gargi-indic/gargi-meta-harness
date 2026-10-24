"""``rev`` and ``meta`` of a stored proposal: on GitHub in the envelope, mirrored in the ``meta`` table (M4.5)."""

import json
import re

import pytest

from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.planner.proposal import ProposedPlan
from harness.planner.proposal_store import (
    get_proposal,
    read_proposals,
    restore_proposal_mirror,
    save_proposal,
    update_proposal,
)
from harness.store.db import Store
from tests.planner.test_proposal_store import RUN, SHA, make_plan
from tests.planner.world import CONTROL, REPO, World


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


def envelope_of(world: World) -> dict:
    [body] = [b for b in world.bodies() if "harness:proposal" in b]
    return json.loads(re.search(r"```json\n(.*)\n```", body).group(1))


async def save(store: Store, outbox: Outbox, plan: ProposedPlan, **kw):
    return await save_proposal(
        store, outbox, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA, proposal=plan, **kw
    )


async def test_the_defaults_leave_the_envelope_as_it_was(store, outbox, world) -> None:
    stored = await save(store, outbox, make_plan())
    assert stored.rev == 1 and stored.meta == {}
    assert set(envelope_of(world)) == {"v", "run_id", "plan_sha", "status", "created_at", "proposal"}
    assert store.query("SELECT 1 FROM meta WHERE key LIKE 'proposal:ext:%'") == []


async def test_rev_and_meta_are_written_to_github_and_the_mirror(store, outbox, world, gh) -> None:
    await save(store, outbox, make_plan(), rev=4, meta={"plan": "docs/PLAN.md", "n": [1, 2]})
    envelope = envelope_of(world)
    assert envelope["rev"] == 4 and envelope["meta"] == {"plan": "docs/PLAN.md", "n": [1, 2]}
    got = await get_proposal(store, gh, repo=REPO, control_issue=CONTROL, run_id=RUN)
    assert got is not None and got.rev == 4 and got.meta["n"] == [1, 2]
    [read] = read_proposals(world.comments[CONTROL])
    assert read.rev == 4 and read.meta == {"plan": "docs/PLAN.md", "n": [1, 2]}


async def test_update_keeps_rev_and_meta_unless_given(store, outbox, world) -> None:
    await save(store, outbox, make_plan(), rev=3, meta={"a": 1})
    kept = await update_proposal(
        store, outbox, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA, status="created"
    )
    assert kept.rev == 3 and kept.meta == {"a": 1} and kept.status == "created"
    changed = await update_proposal(
        store, outbox, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA, rev=5, meta={"b": 2}
    )
    assert changed.rev == 5 and changed.meta == {"b": 2} and changed.status == "created"


async def test_the_mirror_is_rebuilt_from_github_with_rev_and_meta(store, outbox, world, gh) -> None:
    await save(store, outbox, make_plan(), rev=6, meta={"k": "v"})
    fresh = Store(":memory:")
    fresh.migrate()
    assert await restore_proposal_mirror(gh, fresh, REPO, CONTROL) == 1
    got = await get_proposal(fresh, gh, repo=REPO, control_issue=CONTROL, run_id=RUN)
    assert got is not None and got.rev == 6 and got.meta == {"k": "v"}
    # Restoring over a mirror that knows other values replaces them.
    store.execute(
        "UPDATE meta SET value = ? WHERE key LIKE 'proposal:ext:%'", (json.dumps({"rev": 99, "meta": {}}),)
    )
    await restore_proposal_mirror(gh, store, REPO, CONTROL)
    again = await get_proposal(store, gh, repo=REPO, control_issue=CONTROL, run_id=RUN)
    assert again is not None and again.rev == 6 and again.meta == {"k": "v"}
    fresh.close()


async def test_going_back_to_the_defaults_drops_the_mirrored_extras(store, outbox, world, gh) -> None:
    await save(store, outbox, make_plan(), rev=2, meta={"k": 1})
    await save(store, outbox, make_plan(), rev=1, meta={})
    assert store.query("SELECT 1 FROM meta WHERE key LIKE 'proposal:ext:%'") == []
    got = await get_proposal(store, gh, repo=REPO, control_issue=CONTROL, run_id=RUN)
    assert got is not None and got.rev == 1 and got.meta == {}


@pytest.mark.parametrize("bad", [{"rev": 0}, {"rev": "2"}, {"rev": True}, {"meta": [1]}])
async def test_an_envelope_with_a_bad_rev_or_meta_is_not_a_proposal(store, outbox, world, bad) -> None:
    await save(store, outbox, make_plan(), rev=2)
    [comment] = [c for c in world.comments[CONTROL] if "harness:proposal" in c["body"]]
    data = re.search(r"```json\n(.*)\n```", comment["body"]).group(1)
    comment["body"] = comment["body"].replace(
        data, json.dumps({**json.loads(data), **bad}, separators=(",", ":"))
    )
    assert read_proposals(world.comments[CONTROL]) == []
