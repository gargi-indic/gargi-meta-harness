import pytest
from pydantic import BaseModel

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine.dispatcher import (
    Dispatcher,
    _paths_overlap,
    parse_path_hints,
)
from harness.engine.executors import default_registry
from harness.engine.runner import Runner
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.llm.backend import FakeBackend
from harness.store.db import Store
from harness.store.repo import IssueRow, RunRow, list_runs, upsert_issue, upsert_repo


class FakePlaybook(BaseModel):
    id: str = "fake"
    nodes: dict = {"entry": {}}


def fake_runner(store: Store) -> Runner:
    adapters = AdapterRegistry()
    adapters.register(FakeAdapter())
    return Runner(
        store,
        Outbox(store, GitHub("token")),
        default_registry(review_backend=FakeBackend({})),
        adapters,
        repo_configs={"test/repo": RepoConfig()},
    )


def test_parse_path_hints():
    assert parse_path_hints(None) == []
    assert parse_path_hints("") == []

    # Comma list
    assert parse_path_hints("paths: a/b, c/ , `d/e`") == ["a/b", "c/", "d/e"]

    # Bullets
    body = """
paths:
- a/b
- c/
- `d/e`
"""
    assert parse_path_hints(body) == ["a/b", "c/", "d/e"]

    # Globs
    assert parse_path_hints("paths: *.py, tests/*") == ["*.py", "tests/*"]


def test_paths_overlap():
    assert _paths_overlap("a/b", "a/b")
    assert _paths_overlap("a/", "a/x.py")
    assert _paths_overlap("a/x.py", "a/")
    assert _paths_overlap("a/*.py", "a/x.py")
    assert _paths_overlap("a/x.py", "a/*.py")
    assert not _paths_overlap("a/b", "a/c")
    assert not _paths_overlap("a", "ab")


def issue_row(
    number: int, body: str = "", labels: list[str] = None, blocked_by: list[int] = None
) -> IssueRow:
    return IssueRow(
        repo="test/repo",
        number=number,
        id=number,
        title=f"Issue {number}",
        body=body,
        state="open",
        labels=labels or [],
        parent=None,
        blocked_by=blocked_by or [],
        updated_at="2023-01-01T00:00:00Z",
    )


def run_row(issue_num: int, agent: str, status: str = "running") -> RunRow:
    return RunRow(
        id=f"test/repo#{issue_num}@1",
        repo="test/repo",
        issue=issue_num,
        playbook_id="default",
        playbook_sha=None,
        status=status,
        context={"agent": agent},
        ledger_comment_id=None,
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )


def setup_store():
    store = Store(":memory:")
    store.migrate()
    upsert_repo(store, "test/repo", "main", None)
    return store


def test_plan_agent_cap():
    store = setup_store()
    dispatcher = Dispatcher(store=store, caps={"jules": 1, "claude": 2})

    ready = [issue_row(1), issue_row(2)]
    active = [run_row(3, "jules")]

    decisions = dispatcher.plan(ready, active)

    # RepoConfig's default agent is jules, which is at its cap.
    assert len(decisions) == 2
    for d in decisions:
        assert d.reason == "cap"


def test_plan_global_cap():
    store = setup_store()
    dispatcher = Dispatcher(store=store, caps={"claude": 5}, global_cap=1)

    ready = [issue_row(1, labels=["agent:claude"]), issue_row(2, labels=["agent:claude"])]
    active = []

    decisions = dispatcher.plan(ready, active)
    assert decisions[0].reason == "started"
    assert decisions[1].reason == "global_cap"


def test_plan_ordering_and_overlap():
    store = setup_store()
    dispatcher = Dispatcher(store=store, caps={"claude": 5})

    ready = [
        issue_row(2, body="paths: src/", labels=["agent:claude"]),
        issue_row(3, labels=["priority:2", "agent:claude"], body="paths: src/x.py", blocked_by=[]),
        issue_row(1, labels=["priority:1", "agent:claude"], body="paths: src/y.py", blocked_by=[]),
    ]
    # Order: #1 (priority 1), #3 (priority 2), #2 (no priority); #2's "src/" overlaps the earlier #1.

    decisions = dispatcher.plan(ready, [])
    assert decisions[0].issue == 1
    assert decisions[0].reason == "started"

    assert decisions[1].issue == 3
    assert decisions[1].reason == "started"

    assert decisions[2].issue == 2
    assert decisions[2].reason == "overlap:#1"


def test_mixed_agents():
    store = setup_store()
    dispatcher = Dispatcher(store=store, caps={"claude": 2, "jules": 1})

    ready = [
        issue_row(1, labels=["agent:claude"]),
        issue_row(2, labels=["agent:claude"]),
        issue_row(3, labels=["agent:claude"]),
        issue_row(4, labels=["agent:jules"]),
        issue_row(5, labels=["agent:jules"]),
    ]

    decisions = dispatcher.plan(ready, [])
    starts = [d for d in decisions if d.reason == "started"]
    caps = [d for d in decisions if d.reason == "cap"]

    assert len(starts) == 3
    assert len(caps) == 2


@pytest.mark.asyncio
async def test_tick_with_fakes():
    store = setup_store()

    runner = fake_runner(store)

    dispatcher = Dispatcher(store=store, caps={"fake": 2})

    issues = [
        issue_row(1, labels=["agent:fake"]),
        issue_row(2, labels=["agent:fake"]),
        issue_row(3, labels=["agent:fake"]),
    ]

    async def ready_provider():
        return issues

    async def resolve_playbook(repo, p_id):
        return FakePlaybook(), "sha123"

    decisions = await dispatcher.tick(runner, ready_provider, resolve_playbook)
    assert len([d for d in decisions if d.reason == "started"]) == 2
    assert decisions[2].reason == "cap"

    # Idempotent tick
    await dispatcher.tick(runner, ready_provider, resolve_playbook)
    assert sorted(r.issue for r in list_runs(store)) == [1, 2]  # no duplicates

    # One run finishes: the third issue starts, the others are not started again.
    store.execute("UPDATE runs SET status = 'done' WHERE issue = 1")
    issues.pop(0)  # a real ready set no longer contains the finished issue
    decisions = await dispatcher.tick(runner, ready_provider, resolve_playbook)
    assert sorted(r.issue for r in list_runs(store)) == [1, 2, 3]
    assert {d.issue: d.reason for d in decisions}[3] == "started"


def test_blocked_and_running_skipped():
    store = setup_store()
    dispatcher = Dispatcher(store=store, caps={"claude": 5})

    upsert_issue(store, issue_row(99))
    upsert_issue(
        store,
        IssueRow(
            repo="test/repo",
            number=100,
            id=100,
            title="Issue 100",
            body="",
            state="closed",
            labels=[],
            parent=None,
            blocked_by=[],
            updated_at="2023-01-01T00:00:00Z",
        ),
    )

    ready = [
        issue_row(1, labels=["agent:claude"], blocked_by=[99]),  # blocked by open issue
        issue_row(2, labels=["agent:claude"], blocked_by=[100]),  # blocked by closed issue -> ready
        issue_row(3, labels=["agent:claude"], blocked_by=[101]),  # blocked by missing issue -> blocked
        issue_row(4, labels=["agent:claude"]),  # running
    ]

    decisions = dispatcher.plan(ready, [run_row(4, "claude")])

    reasons = {d.issue: d.reason for d in decisions}
    assert reasons[1] == "blocked"
    assert reasons[2] == "started"
    assert reasons[3] == "blocked"
    assert reasons[4] == "running"


def test_restart_safety_counts_active_runs_from_rows_only():
    dispatcher = Dispatcher(store=setup_store(), caps={"jules": 1})

    decisions = dispatcher.plan([issue_row(2, labels=["agent:jules"])], [run_row(1, "jules")])

    assert [(d.issue, d.reason) for d in decisions] == [(2, "cap")]


@pytest.mark.asyncio
async def test_blocked_issue_is_reported_and_never_started_by_tick():
    store = setup_store()
    upsert_issue(store, issue_row(10))  # open blocker
    blocked_issue = issue_row(11, labels=["agent:fake"], blocked_by=[10])
    runner = fake_runner(store)
    dispatcher = Dispatcher(store=store, caps={"fake": 2})

    async def ready_provider():
        return [blocked_issue]

    async def resolve_playbook(repo, p_id):
        return FakePlaybook(), "sha"

    decisions = await dispatcher.tick(runner, ready_provider, resolve_playbook)

    assert [(d.issue, d.reason) for d in decisions] == [(11, "blocked")]
    assert list_runs(store) == []
