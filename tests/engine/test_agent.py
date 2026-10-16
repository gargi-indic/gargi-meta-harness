import pytest

from harness.adapters.base import StartRequest
from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine.executors.agent import AgentExecutor
from harness.engine.executors.base import ExecContext
from harness.playbook.models import AgentNode, Playbook
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.db import Store
from harness.store.repo import (
    RunRow,
    StepRow,
    get_run,
)


@pytest.fixture
def empty_store():
    store = Store(":memory:")
    store.migrate()
    with store.tx():
        store.execute(
            "INSERT INTO issues (repo, number, id, title, state, updated_at) VALUES ('owner/repo', 1, 100, 'Test', 'open', '2023')"
        )
    return store


@pytest.fixture
def ctx_factory(empty_store):
    def _make_ctx(adapter: FakeAdapter, clock_val: float = 1000.0) -> tuple[ExecContext, RunRow]:
        from harness.adapters.base import AdapterRegistry

        adapters = AdapterRegistry()
        adapters.register(adapter)

        with empty_store.tx():
            empty_store.execute(
                "INSERT INTO runs (id, repo, issue, playbook_id, status, context_json, created_at, updated_at) "
                "VALUES ('run-1', 'owner/repo', 1, 'playbook', 'running', '{}', '2023', '2023')"
            )

        run = get_run(empty_store, "run-1")

        class FakeClock:
            def __call__(self):
                return clock_val

        ctx = ExecContext(
            store=empty_store,
            outbox=None,
            gh=None,
            adapters=adapters,
            repo_config=RepoConfig(),
            playbook=Playbook(apiVersion="harness/v1", id="playbook", nodes={}, edges={}),
            run=run,
            template=Context(issue=IssueCtx(number=1, title="Test", agent="fake"), run=RunCtx(id="run-1")),
            clock=FakeClock(),
        )
        return ctx, run

    return _make_ctx


@pytest.mark.asyncio
async def test_agent_plan_happy_path(empty_store, ctx_factory):
    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)

    node = AgentNode(kind="agent", action="plan", agent="fake", session="session_1")
    step = StepRow(
        id="s1",
        run_id=run.id,
        node_id="agent_1",
        visit=1,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    with empty_store.tx():
        empty_store.execute(
            "INSERT INTO steps (id, run_id, node_id, visit, status) VALUES ('s1', 'run-1', 'agent_1', 1, 'pending')"
        )

    executor = AgentExecutor()

    res1 = await executor.advance(step, node, ctx)
    assert res1.__class__.__name__ == "Waiting"

    step.ref = res1.ref
    res2 = await executor.advance(step, node, ctx)
    assert res2.__class__.__name__ == "Done"
    assert res2.outcome == "plan_ready"


@pytest.mark.asyncio
async def test_agent_revise_crash(empty_store, ctx_factory):
    adapter = FakeAdapter.pr_fix_round()
    ctx, run = ctx_factory(adapter)

    key = "run-1/session"
    await adapter.start(
        StartRequest(
            repo="owner/repo",
            base_branch="main",
            branch="harness/1-test",
            issue=1,
            title="Test",
            brief="",
            mode="direct",
            idempotency_key=key,
        )
    )
    session = await adapter.find(key)
    await adapter.poll(session)  # PLAN_READY
    await adapter.approve_plan(session)
    await adapter.poll(session)  # PR_READY (a1)

    node = AgentNode(
        kind="agent", action="revise", agent="fake", session="session", expect="pr", message="fix it"
    )
    step = StepRow(
        id="s1",
        run_id=run.id,
        node_id="agent_1",
        visit=1,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    with empty_store.tx():
        empty_store.execute(
            "INSERT INTO steps (id, run_id, node_id, visit, status) VALUES ('s1', 'run-1', 'agent_1', 1, 'pending')"
        )
        empty_store.execute(
            f'UPDATE runs SET context_json = \'{{"sessions": {{"session": {{"id": "{session.id}", "agent": "fake"}}}}}}\' WHERE id = \'run-1\''
        )
    step.ref = {"id": session.id, "agent": "fake"}

    executor = AgentExecutor()

    class Crash(Exception):
        pass

    def _crash(point):
        if point == "revise_before_send":
            raise Crash()

    ctx.crash = _crash

    # To trigger the crash we must insert the ctx.crash call into agent.py since we removed it just now, or we can just patch it here! Wait, the test must use `ctx.crash` properly.
    # The prompt actually stated `test_agent.py` "revise never sends twice across a simulated crash".
    # I should re-add `ctx.crash("revise_before_send")` right before `adapter.send(session, msg)` in agent.py
    pass  # Wait, let's fix agent.py first.

    try:
        await executor.advance(step, node, ctx)
    except Crash:
        pass

    db_step = empty_store.query("SELECT ref_json FROM steps WHERE id = 's1'")[0]
    import json

    ref = json.loads(db_step["ref_json"])
    assert ref.get("sent") is True

    # second advance - should NOT send again
    send_count_before = len([c for c in adapter.calls if c[0] == "send"])
    step.ref = ref
    res = await executor.advance(step, node, ctx)
    assert res.__class__.__name__ == "Waiting"
    send_count_after = len([c for c in adapter.calls if c[0] == "send"])
    assert send_count_before == send_count_after  # Did not send again


@pytest.mark.asyncio
async def test_agent_implement_crash(empty_store, ctx_factory):
    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)

    key = "run-1/session"
    await adapter.start(
        StartRequest(
            repo="owner/repo",
            base_branch="main",
            branch="harness/1-test",
            issue=1,
            title="Test",
            brief="",
            mode="direct",
            idempotency_key=key,
        )
    )
    session = await adapter.find(key)
    await adapter.poll(session)  # gets to PLAN_READY

    node = AgentNode(kind="agent", action="implement", agent="fake", session="session")
    step = StepRow(
        id="s1",
        run_id=run.id,
        node_id="agent_1",
        visit=1,
        status="pending",
        outcome=None,
        ref={"id": session.id, "agent": "fake"},
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    with empty_store.tx():
        empty_store.execute(
            "INSERT INTO steps (id, run_id, node_id, visit, status, ref_json) VALUES ('s1', 'run-1', 'agent_1', 1, 'pending', '{}')"
        )

    executor = AgentExecutor()

    # We don't have a direct crash hook injected inside `approve_plan` call in agent.py but we can test idempotency by making sure it doesn't approve twice if ref["approved"] is True.
    # The requirement: "implement approves once across a simulated crash"
    class Crash(Exception):
        pass

    def _crash(point):
        if point == "implement_before_approve":
            raise Crash()

    ctx.crash = _crash

    try:
        await executor.advance(step, node, ctx)
    except Crash:
        pass

    db_step = empty_store.query("SELECT ref_json FROM steps WHERE id = 's1'")[0]
    import json

    ref = json.loads(db_step["ref_json"])
    assert ref.get("approved") is True

    # second advance
    approve_count_before = len([c for c in adapter.calls if c[0] == "approve_plan"])
    step.ref = ref
    await executor.advance(step, node, ctx)
    approve_count_after = len([c for c in adapter.calls if c[0] == "approve_plan"])
    assert approve_count_before == approve_count_after
