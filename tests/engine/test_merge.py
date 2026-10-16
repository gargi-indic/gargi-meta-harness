"""Merge executor (M1.10): squash merge, finalize through the outbox. GitHub is mocked with respx."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import Response

from harness.adapters.base import AdapterRegistry
from harness.config import MergeConfig, RepoConfig
from harness.engine import Done, ExecContext, Waiting
from harness.engine.executors.merge import MergeExecutor
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.playbook.models import MergeNode
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow
from tests.engine.helpers import REPO, FakeClock, make_playbook

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

API = "https://api.github.com"
PR = 7
ISSUE = 1
HEAD = "a" * 40
MERGE_SHA = "c0ffee1" + "0" * 33
PULL_URL = f"{API}/repos/{REPO}/pulls/{PR}"
ISSUE_URL = f"{API}/repos/{REPO}/issues/{ISSUE}"
BODY = "Intro\n- [ ] one\n  * [ ] two\n- [x] three\nsee `- [ ]` here"
TICKED = "Intro\n- [x] one\n  * [x] two\n- [x] three\nsee `- [ ]` here"

PLAYBOOK = make_playbook(
    {
        "ci": {"kind": "check", "check": "ci"},
        "rev": {"kind": "review", "target": "pr", "checklist": ["ok"]},
        "plan_rev": {"kind": "review", "target": "plan", "checklist": ["ok"]},
        "m": {"kind": "merge"},
    },
    {"ci.pass": "rev", "ci.fail": "@failed", "rev.approve": "m", "m.merged": "@done", "m.failed": "@failed"},
)


@pytest.fixture
async def gh() -> AsyncIterator[GitHub]:
    async def no_sleep(_: float) -> None:
        return None

    client = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    client._max_retries = 0
    yield client
    await client.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock):
    s = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def outbox(store: Store, gh: GitHub, clock: FakeClock) -> Outbox:
    async def no_sleep(_: float) -> None:
        return None

    return Outbox(store, gh, clock=clock, sleep=no_sleep)


def make_ctx(
    gh: GitHub | None,
    outbox: Outbox | None,
    store: Store,
    clock: FakeClock,
    *,
    context: dict[str, Any] | None = None,
    config: RepoConfig | None = None,
    nodes: dict[str, dict[str, Any]] | None = None,
) -> ExecContext:
    run = RunRow(
        id="run1",
        repo=REPO,
        issue=ISSUE,
        playbook_id="mini",
        playbook_sha=None,
        status="running",
        context={"pr": {"number": PR, "url": "u"}} if context is None else context,
        ledger_comment_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    return ExecContext(
        store=store,
        outbox=outbox,
        gh=gh,
        adapters=AdapterRegistry(),
        repo_config=config or RepoConfig(),
        playbook=PLAYBOOK,
        run=run,
        template=Context(
            issue=IssueCtx(number=ISSUE, title="t", agent="jules"),
            run=RunCtx(id="run1"),
            nodes={"ci": {"head_sha": HEAD}, "rev": {"head_sha": HEAD}} if nodes is None else nodes,
        ),
        clock=clock,
    )


def make_step(ref: dict[str, Any] | None = None) -> StepRow:
    return StepRow(
        id="run1/m/1",
        run_id="run1",
        node_id="m",
        visit=1,
        status="running",
        outcome=None,
        ref=ref,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )


def pull_json(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": PR,
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "merge_commit_sha": None,
        "head": {"sha": HEAD, "ref": "harness/1", "repo": {"full_name": REPO}},
    }
    data.update(over)
    return data


def merged_pull(**over: Any) -> dict[str, Any]:
    return pull_json(state="closed", merged=True, merge_commit_sha=MERGE_SHA, **over)


def mock_pull(router, *responses: dict[str, Any]):
    return router.get(PULL_URL).mock(side_effect=[Response(200, json=r) for r in responses])


def queued_ops(store: Store) -> list[str]:
    return [r["op"] for r in store.query("SELECT op FROM outbox ORDER BY id")]


async def test_happy_path(respx_mock, gh, outbox, store, clock):
    ex, node, ctx = MergeExecutor(), MergeNode(), make_ctx(gh, outbox, store, clock)
    mock_pull(respx_mock, pull_json(), merged_pull())
    put = respx_mock.put(f"{PULL_URL}/merge").mock(return_value=Response(200, json={"merged": True}))
    respx_mock.delete(f"{API}/repos/{REPO}/git/refs/heads/harness/1").mock(return_value=Response(204))
    respx_mock.get(ISSUE_URL).mock(
        side_effect=[
            Response(200, json={"state": "open", "body": BODY}),  # executor: is it closed already?
            Response(200, json={"state": "open", "body": BODY}),  # tick_boxes: fresh body
        ]
    )
    patch = respx_mock.patch(ISSUE_URL).mock(return_value=Response(200, json={}))
    comment = respx_mock.post(f"{ISSUE_URL}/comments").mock(return_value=Response(201, json={"id": 5}))

    first = await ex.advance(make_step(), node, ctx)
    assert isinstance(first, Waiting)
    assert first.ref is not None and first.ref["phase"] == "merging" and first.ref["sha"] == HEAD
    assert queued_ops(store) == ["merge_pull"]
    assert await outbox.drain_once() == 1
    assert json.loads(put.calls.last.request.content) == {"merge_method": "squash", "sha": HEAD}

    second = await ex.advance(make_step(first.ref), node, ctx)
    assert second == Done("merged", {"pr": PR, "merge_sha": MERGE_SHA, "head_sha": HEAD})
    assert queued_ops(store) == ["merge_pull", "delete_branch", "tick_boxes", "close_issue", "add_comment"]
    assert await outbox.drain_once() == 4

    bodies = [json.loads(c.request.content) for c in patch.calls]
    assert {"body": TICKED} in bodies
    assert {"state": "closed"} in bodies
    comment_body = json.loads(comment.calls.last.request.content)["body"]
    assert comment_body.startswith("**2027-01-15 · #1 merged.**\n")
    assert "PR #7 merged as `c0ffee1`" in comment_body


async def test_already_merged_makes_no_merge_call(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, merged_pull())
    respx_mock.get(ISSUE_URL).mock(return_value=Response(200, json={"state": "closed", "body": BODY}))

    result = await MergeExecutor().advance(make_step(), MergeNode(), make_ctx(gh, outbox, store, clock))

    assert result == Done("merged", {"pr": PR, "merge_sha": MERGE_SHA, "head_sha": HEAD})
    assert "merge_pull" not in queued_ops(store)
    assert queued_ops(store) == ["delete_branch", "tick_boxes", "add_comment"]  # issue already closed


async def test_head_moved_since_review(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, pull_json(head={"sha": "b" * 40, "ref": "x", "repo": {"full_name": REPO}}))

    result = await MergeExecutor().advance(make_step(), MergeNode(), make_ctx(gh, outbox, store, clock))

    assert result == Done(
        "failed", {"note": "head changed since review", "head_sha": "b" * 40, "reviewed": [HEAD]}
    )
    assert queued_ops(store) == []


async def test_plan_review_sha_is_ignored_and_missing_sha_fails(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, pull_json(), pull_json())
    ex = MergeExecutor()
    stale_plan = {"plan_rev": {"head_sha": "9" * 40}, "ci": {"head_sha": HEAD}}
    ok = await ex.advance(make_step(), MergeNode(), make_ctx(gh, outbox, store, clock, nodes=stale_plan))
    assert isinstance(ok, Waiting) and ok.ref is not None  # plan review output is not compared

    none = await ex.advance(make_step(), MergeNode(), make_ctx(gh, outbox, store, clock, nodes={}))
    assert none == Done("failed", {"note": "no reviewed head sha"})


async def test_not_mergeable_fails_and_unknown_waits(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, pull_json(mergeable=False, mergeable_state="dirty"), pull_json(mergeable=None))
    ex, ctx = MergeExecutor(), make_ctx(gh, outbox, store, clock)

    conflict = await ex.advance(make_step(), MergeNode(), ctx)
    assert conflict == Done("failed", {"note": "not mergeable: dirty", "head_sha": HEAD})

    unknown = await ex.advance(make_step(), MergeNode(), ctx)
    assert unknown == Waiting(ref=None, poll_after=10)
    assert queued_ops(store) == []


async def test_failed_pr_states(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, pull_json(state="closed"))
    result = await MergeExecutor().advance(make_step(), MergeNode(), make_ctx(gh, outbox, store, clock))
    assert result == Done("failed", {"note": "PR closed without merge"})

    no_pr = await MergeExecutor().advance(
        make_step(), MergeNode(), make_ctx(gh, outbox, store, clock, context={})
    )
    assert no_pr == Done("failed", {"note": "no PR in run context"})

    with pytest.raises(RuntimeError):
        await MergeExecutor().advance(make_step(), MergeNode(), make_ctx(None, None, store, clock))


async def test_merge_op_failure_fails_the_step(respx_mock, gh, outbox, store, clock):
    ex, ctx = MergeExecutor(), make_ctx(gh, outbox, store, clock)
    mock_pull(respx_mock, pull_json(), pull_json(), pull_json())
    respx_mock.put(f"{PULL_URL}/merge").mock(return_value=Response(405, json={"message": "Not allowed"}))

    first = await ex.advance(make_step(), MergeNode(method="rebase"), ctx)
    assert isinstance(first, Waiting) and first.ref is not None
    for _ in range(5):
        assert await outbox.drain_once() == 0
        store.execute("UPDATE outbox SET not_before = NULL")

    # Still pending PR plus a failed outbox row: the step fails with the (redacted) error.
    second = await ex.advance(make_step(first.ref), MergeNode(method="rebase"), ctx)
    assert isinstance(second, Done) and second.outcome == "failed"
    assert second.output["note"].startswith("merge failed: ")
    assert "405" in second.output["note"]

    # While the row is still pending the step just waits.
    store.execute("UPDATE outbox SET status = 'pending'")
    third = await ex.advance(make_step(first.ref), MergeNode(), ctx)
    assert third == Waiting(ref=first.ref, poll_after=15)


async def test_method_comes_from_node_then_repo_config(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, pull_json(), pull_json())
    cfg = RepoConfig(merge=MergeConfig(method="rebase"))
    ex = MergeExecutor()

    await ex.advance(make_step(), MergeNode(), make_ctx(gh, outbox, store, clock, config=cfg))
    await ex.advance(make_step(), MergeNode(method="merge"), make_ctx(gh, outbox, store, clock, config=cfg))

    rows = store.query("SELECT payload_json FROM outbox")
    # Same coalesce key: the second enqueue replaced the first pending row.
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["method"] == "merge"


async def test_reentry_does_not_double_enqueue(respx_mock, gh, outbox, store, clock):
    mock_pull(respx_mock, merged_pull(), merged_pull())
    respx_mock.get(ISSUE_URL).mock(return_value=Response(200, json={"state": "open", "body": BODY}))
    ex, ctx = MergeExecutor(), make_ctx(gh, outbox, store, clock)

    await ex.advance(make_step(), MergeNode(), ctx)
    await ex.advance(make_step(), MergeNode(), ctx)  # crashed after finalize, runner re-advances

    assert queued_ops(store) == ["delete_branch", "tick_boxes", "close_issue", "add_comment"]


@pytest.mark.parametrize("fork", [True, False])
async def test_no_branch_deletion_for_forks_or_when_disabled(respx_mock, gh, outbox, store, clock, fork):
    head = {"sha": HEAD, "ref": "feat", "repo": {"full_name": "fork/r" if fork else REPO}}
    mock_pull(respx_mock, merged_pull(head=head))
    respx_mock.get(ISSUE_URL).mock(return_value=Response(200, json={"state": "closed"}))
    cfg = RepoConfig() if fork else RepoConfig(merge=MergeConfig(delete_branch=False))

    result = await MergeExecutor().advance(
        make_step(), MergeNode(), make_ctx(gh, outbox, store, clock, config=cfg)
    )

    assert isinstance(result, Done) and result.outcome == "merged"
    assert queued_ops(store) == ["tick_boxes", "add_comment"]
