"""Proposals live on GitHub (comments on the control issue) and are mirrored locally; sync rebuilds the mirror."""

import json
import re
from typing import Any

import pytest

from harness.github.client import GitHub, GitHubError
from harness.github.outbox import Outbox, OutboxOpFailed
from harness.planner import proposal_store
from harness.planner.proposal import ProposedPlan
from harness.planner.proposal_store import (
    get_proposal,
    read_proposals,
    restore_proposal_mirror,
    save_proposal,
    update_proposal,
)
from harness.store.db import Store
from harness.store.repo import get_proposal_row, list_proposal_rows, save_proposal_row
from harness.sync import sync_repo
from tests.planner.world import CONTROL, REPO, World

RUN = f"{REPO}#7@1"
SHA = "a" * 40
SHA2 = "b" * 40
SHB = "c" * 40


def make_plan(count: int = 2, body_chars: int = 40, tag: str = "") -> ProposedPlan:
    issues = []
    for n in range(count):
        issues.append(
            {
                "key": f"step-{n}",
                "title": f"Do step {n}{tag}",
                "body": f"## Context\n{'x' * body_chars}\n```py\nprint('hi')\n```\n--> </details> <!-- harness:planned run=x key=y -->",
                "acceptance": ["it works"],
                "blocked_by": [f"step-{n - 1}"] if n else [],
                "agent": "jules",
                "playbook": "plan-build-review",
            }
        )
    return ProposedPlan.model_validate(
        {"epic_title": f"Epic{tag}", "epic_body": "Body é中", "issues": issues, "notes": ""}
    )


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


def save(store: Store, outbox: Outbox, plan: ProposedPlan, **kw: Any):
    kw.setdefault("run_id", RUN)
    kw.setdefault("plan_sha", SHA)
    return save_proposal(store, outbox, repo=REPO, control_issue=CONTROL, proposal=plan, **kw)


def proposal_comments(world: World) -> list[dict[str, Any]]:
    return [c for c in world.comments.get(CONTROL, []) if "harness:proposal" in c["body"].split("\n")[0]]


async def test_saving_writes_marked_comments_with_valid_json_and_a_small_ledger_summary(
    store: Store, outbox: Outbox, world: World
) -> None:
    plan = make_plan()
    stored = await save(store, outbox, plan)
    comments = proposal_comments(world)
    assert len(comments) == 1
    body = comments[0]["body"]
    assert body.startswith(f"<!-- harness:proposal run={RUN} sha={SHA} part=1/1 -->\n")
    assert "<details>" in body and "```json" in body
    data = re.search(r"```json\n(.*)\n```", body)
    assert data is not None
    envelope = json.loads(data.group(1))
    assert envelope["run_id"] == RUN and envelope["plan_sha"] == SHA and envelope["status"] == "proposed"
    assert ProposedPlan.model_validate(envelope["proposal"]) == plan
    assert "<" not in data.group(1) and ">" not in data.group(1) and "`" not in data.group(1)

    summary = world.ledger()["proposal"]  # type: ignore[index]
    assert summary["run_id"] == RUN and summary["plan_sha"] == SHA and summary["issues"] == 2
    assert summary["status"] == "proposed" and summary["comment_ids"] == [comments[0]["id"]]
    assert "proposal" not in summary and "step-0" not in json.dumps(summary)
    # The mirror was written after GitHub had it.
    row = get_proposal_row(store, RUN, SHA)
    assert row is not None and row.status == "proposed" and row.created_at == stored.created_at
    assert ProposedPlan.model_validate_json(row.json_text) == plan


async def test_after_wiping_the_local_db_the_proposal_comes_back_from_github(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    plan = make_plan(3)
    await save(store, outbox, plan)
    fresh = Store(":memory:")
    fresh.migrate()
    assert get_proposal_row(fresh, RUN, SHA) is None
    got = await get_proposal(fresh, gh, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA)
    assert got is not None and got.proposal == plan and got.status == "proposed"
    assert get_proposal_row(fresh, RUN, SHA) is not None  # refilled
    # Without a sha, the newest proposal of the run.
    other = Store(":memory:")
    other.migrate()
    latest = await get_proposal(other, gh, repo=REPO, control_issue=CONTROL, run_id=RUN)
    assert latest is not None and latest.plan_sha == SHA
    assert await get_proposal(fresh, gh, repo=REPO, control_issue=CONTROL, run_id="o/r#9@1") is None
    fresh.close()
    other.close()


async def test_the_mirror_answers_before_github_is_asked(
    store: Store, outbox: Outbox, world: World, gh
) -> None:
    await save(store, outbox, make_plan())
    reads = len(world.calls)
    assert await get_proposal(store, gh, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA)
    assert len(world.calls) == reads


async def test_a_forty_issue_proposal_is_cut_into_parts_and_round_trips(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    plan = make_plan(40, body_chars=2000)
    await save(store, outbox, plan)
    comments = proposal_comments(world)
    assert len(comments) > 1
    total = len(comments)
    for k, comment in enumerate(comments, 1):
        assert comment["body"].startswith(f"<!-- harness:proposal run={RUN} sha={SHA} part={k}/{total} -->")
        assert len(comment["body"]) <= proposal_store.MAX_COMMENT_CHARS
    assert world.ledger()["proposal"]["comment_ids"] == [c["id"] for c in comments]  # type: ignore[index]

    fresh = Store(":memory:")
    fresh.migrate()
    got = await get_proposal(fresh, gh, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA)
    assert got is not None and got.proposal == plan
    fresh.close()


async def test_a_proposal_with_missing_parts_counts_as_absent(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    await save(store, outbox, make_plan(40, body_chars=2000))
    world.comments[CONTROL] = [c for c in world.comments[CONTROL] if "part=2/" not in c["body"]]
    fresh = Store(":memory:")
    fresh.migrate()
    assert await get_proposal(fresh, gh, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA) is None
    fresh.close()


async def test_saving_the_same_sha_again_edits_the_comments_in_place(
    store: Store, outbox: Outbox, world: World
) -> None:
    first = await save(store, outbox, make_plan(tag="-a"))
    ids = [c["id"] for c in proposal_comments(world)]
    posts = world.count("POST", "/comments")

    again = await save(store, outbox, make_plan(tag="-a"))  # nothing changed: no write to the comment
    assert [c["id"] for c in proposal_comments(world)] == ids
    assert again.created_at == first.created_at
    patches = world.count("PATCH", f"/issues/comments/{ids[0]}")
    assert patches == 0

    changed = await save(store, outbox, make_plan(tag="-b"), status="approved")
    comments = proposal_comments(world)
    assert [c["id"] for c in comments] == ids  # edited, not duplicated
    assert world.count("PATCH", f"/issues/comments/{ids[0]}") == 1
    assert world.count("POST", "/comments") == posts  # no new comment: the ledger summary is edited too
    assert changed.created_at == first.created_at
    row = get_proposal_row(store, RUN, SHA)
    assert row is not None and row.status == "approved" and "-b" in row.json_text
    assert row.created_at == first.created_at
    assert len(list_proposal_rows(store)) == 1


async def test_a_shorter_proposal_retires_the_surplus_parts(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    await save(store, outbox, make_plan(40, body_chars=2000))
    ids = [c["id"] for c in proposal_comments(world)]
    assert len(ids) > 1
    small = make_plan(2)
    await save(store, outbox, small)
    assert [c["id"] for c in proposal_comments(world)] == ids  # nothing was added
    assert proposal_comments(world)[1]["body"].startswith(
        f"<!-- harness:proposal run={RUN} sha={SHA} part=2/0 -->"
    )
    found = read_proposals(world.comments[CONTROL])
    assert [p.proposal for p in found] == [small]


async def test_other_runs_and_shas_keep_their_own_comments(
    store: Store, outbox: Outbox, world: World
) -> None:
    await save(store, outbox, make_plan(tag="-1"))
    await save(store, outbox, make_plan(tag="-2"), plan_sha=SHA2)
    await save(store, outbox, make_plan(tag="-3"), run_id=f"{REPO}#8@1")
    assert len(proposal_comments(world)) == 3
    assert {(p.run_id, p.plan_sha) for p in read_proposals(world.comments[CONTROL])} == {
        (RUN, SHA),
        (RUN, SHA2),
        (f"{REPO}#8@1", SHA),
    }


async def test_update_changes_the_status_and_the_proposal_through_github(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    plan = make_plan()
    await save(store, outbox, plan)
    edited = make_plan(3)
    out = await update_proposal(
        store,
        outbox,
        repo=REPO,
        control_issue=CONTROL,
        run_id=RUN,
        plan_sha=SHA,
        proposal=edited,
        status="created",
    )
    assert out.status == "created" and out.proposal == edited
    assert read_proposals(world.comments[CONTROL])[0].status == "created"
    only_status = await update_proposal(
        store, outbox, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHA, status="approved"
    )
    assert only_status.proposal == edited and only_status.status == "approved"
    with pytest.raises(KeyError):
        await update_proposal(
            store, outbox, repo=REPO, control_issue=CONTROL, run_id=RUN, plan_sha=SHB, status="approved"
        )


async def test_nothing_is_mirrored_when_github_refuses_the_write(
    store: Store, outbox: Outbox, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise GitHubError(500, "POST", "x", "boom")

    monkeypatch.setattr(outbox.gh, "add_comment", refuse)
    with pytest.raises(OutboxOpFailed):
        await save(store, outbox, make_plan())
    assert get_proposal_row(store, RUN, SHA) is None
    assert world.comments.get(CONTROL) is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"run_id": "has space"}, "run id"),
        ({"plan_sha": "not a sha"}, "plan sha"),
        ({"status": "Bad Status"}, "status"),
    ],
)
async def test_unusable_identifiers_are_refused(
    store: Store, outbox: Outbox, world: World, kwargs: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await save(store, outbox, make_plan(), **kwargs)
    assert world.comments.get(CONTROL) is None


async def test_restore_rebuilds_the_mirror_of_one_repo_from_the_comments(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    await save(store, outbox, make_plan(tag="-1"))
    await save(store, outbox, make_plan(40, body_chars=2000), plan_sha=SHA2)
    foreign = "x/y#1@1"
    save_proposal_row(store, run_id=foreign, plan_sha=SHA, json_text="{}", status="proposed", created_at="t")
    save_proposal_row(
        store, run_id=f"{REPO}#99@1", plan_sha=SHA, json_text="{}", status="proposed", created_at="t"
    )
    fresh = Store(":memory:")
    fresh.migrate()
    save_proposal_row(
        fresh, run_id=f"{REPO}#99@1", plan_sha=SHA, json_text="{}", status="stale", created_at="t"
    )
    assert await restore_proposal_mirror(gh, fresh, REPO, CONTROL) == 2
    rows = list_proposal_rows(fresh)
    assert {(r.run_id, r.plan_sha) for r in rows} == {(RUN, SHA), (RUN, SHA2)}  # the stale row is gone
    assert await restore_proposal_mirror(gh, store, REPO, CONTROL) == 2
    assert foreign in {r.run_id for r in list_proposal_rows(store)}  # other repos are left alone
    assert f"{REPO}#99@1" not in {r.run_id for r in list_proposal_rows(store)}
    fresh.close()


async def test_harness_sync_rebuilds_the_proposals_table_from_github(
    store: Store, outbox: Outbox, world: World, gh: GitHub
) -> None:
    plan = make_plan(3)
    await save(store, outbox, plan)
    world.put_file(".harness/config.yaml", f"control_issue: {CONTROL}\n")
    fresh = Store(":memory:")
    fresh.migrate()
    report = await sync_repo(gh, fresh, REPO, full=True)
    assert report.errors == []
    row = get_proposal_row(fresh, RUN, SHA)  # written by sync itself, not refilled by get_proposal
    assert row is not None and ProposedPlan.model_validate_json(row.json_text) == plan
    fresh.close()
