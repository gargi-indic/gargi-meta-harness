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
async def test_agent_revise_idempotency(empty_store, ctx_factory):
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

    # Make fake adapter crash on send
    class Crash(Exception):
        pass

    original_send = adapter.send

    async def crash_send(sess, text):
        raise Crash()

    adapter.send = crash_send

    try:
        await executor.advance(step, node, ctx)
    except Crash:
        pass

    # The step in db should now have 'sent': True
    db_step = empty_store.query("SELECT ref_json FROM steps WHERE id = 's1'")[0]
    import json

    ref = json.loads(db_step["ref_json"])
    assert ref.get("sent") is True

    # restore and advance
    adapter.send = original_send
    send_count_before = len([c for c in adapter.calls if c[0] == "send"])
    step.ref = ref
    res = await executor.advance(step, node, ctx)
    assert res.__class__.__name__ == "Waiting"
    send_count_after = len([c for c in adapter.calls if c[0] == "send"])
    assert send_count_before == send_count_after  # Did not send again


@pytest.mark.asyncio
async def test_agent_implement_idempotency(empty_store, ctx_factory):
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

    class Crash(Exception):
        pass

    original_approve = adapter.approve_plan

    async def crash_approve(sess, note):
        raise Crash()

    adapter.approve_plan = crash_approve

    try:
        await executor.advance(step, node, ctx)
    except Crash:
        pass

    db_step = empty_store.query("SELECT ref_json FROM steps WHERE id = 's1'")[0]
    import json

    ref = json.loads(db_step["ref_json"])
    assert ref.get("approved") is True

    # restore and advance
    adapter.approve_plan = original_approve
    approve_count_before = len([c for c in adapter.calls if c[0] == "approve_plan"])
    step.ref = ref
    await executor.advance(step, node, ctx)
    approve_count_after = len([c for c in adapter.calls if c[0] == "approve_plan"])
    assert approve_count_before == approve_count_after


@pytest.mark.asyncio
async def test_agent_notify_executor(empty_store, ctx_factory):
    from harness.engine.executors.notify import NotifyExecutor
    from harness.github.client import GitHub
    from harness.github.outbox import Outbox
    from harness.playbook.models import NotifyNode

    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)
    ctx.outbox = Outbox(empty_store, GitHub(None))

    node = NotifyNode(kind="notify", message="test message")
    step = StepRow(
        id="s1",
        run_id=run.id,
        node_id="notify_1",
        visit=1,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )

    executor = NotifyExecutor()
    res = await executor.advance(step, node, ctx)

    assert res.__class__.__name__ == "Done"

    ops = empty_store.query("SELECT * FROM outbox")
    assert len(ops) == 1
    import json

    op = ops[0]
    payload = json.loads(op["payload_json"])
    assert op["op"] == "add_comment"
    assert payload["repo"] == "owner/repo"
    assert payload["issue"] == 1
    assert "test message" in payload["body"]
    assert op["coalesce_key"] == "notify:s1"

    res = await executor.advance(step, node, ctx)
    ops = empty_store.query("SELECT * FROM outbox")
    assert len(ops) == 1  # Coalesced


@pytest.mark.asyncio
async def test_agent_plan_re_advance(empty_store, ctx_factory):
    # a session that already exists is not restarted
    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)

    key = "run-1/session_1"
    await adapter.start(
        StartRequest(
            repo="owner/repo",
            base_branch="main",
            branch="harness/1-test",
            issue=1,
            title="Test",
            brief="",
            mode="plan_first",
            idempotency_key=key,
        )
    )

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

    starts_after_advance = len([c for c in adapter.calls if c[0] == "start"])
    assert starts_after_advance == 1


@pytest.mark.asyncio
async def test_agent_timeout(empty_store, ctx_factory):
    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)

    node = AgentNode(kind="agent", action="plan", agent="fake", session="session_1", timeout="1h")
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
    step.ref = res1.ref

    ctx.clock = lambda: 1000.0 + 3601

    res2 = await executor.advance(step, node, ctx)
    assert res2.__class__.__name__ == "Done"
    assert res2.outcome == "timeout"
    assert "timed out" in res2.output["error"].lower()


@pytest.mark.asyncio
async def test_agent_revise_empty_message(empty_store, ctx_factory):
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

    node = AgentNode(
        kind="agent", action="revise", agent="fake", session="session", expect="plan", message="   "
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

    executor = AgentExecutor()
    res1 = await executor.advance(step, node, ctx)
    assert res1.__class__.__name__ == "Done"
    assert res1.outcome == "failed"
    assert "empty revise message" in res1.output["error"]


@pytest.mark.asyncio
async def test_agent_revise_no_session(empty_store, ctx_factory):
    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)

    node = AgentNode(
        kind="agent", action="revise", agent="fake", session="session", expect="plan", message="hello"
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

    executor = AgentExecutor()
    res1 = await executor.advance(step, node, ctx)
    assert res1.__class__.__name__ == "Done"
    assert res1.outcome == "failed"
    assert "no session" in res1.output["error"]


@pytest.mark.asyncio
async def test_agent_implement_no_session(empty_store, ctx_factory):
    adapter = FakeAdapter.happy_path()
    ctx, run = ctx_factory(adapter)

    node = AgentNode(kind="agent", action="implement", agent="fake", session="session")
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
    starts = [c for c in adapter.calls if c[0] == "start"]
    assert len(starts) == 1
    assert starts[0][1]["idempotency_key"] == "run-1/session"


@pytest.mark.asyncio
async def test_agent_needs_input(empty_store, ctx_factory):
    adapter = FakeAdapter.asks_question("what do?")
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
    step.ref = res1.ref

    res2 = await executor.advance(step, node, ctx)
    assert res2.__class__.__name__ == "Done"
    assert res2.outcome == "needs_input"
    assert res2.output["question"] == "what do?"


@pytest.mark.asyncio
async def test_agent_failed(empty_store, ctx_factory):
    adapter = FakeAdapter.fails("boom")
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
    step.ref = res1.ref

    res2 = await executor.advance(step, node, ctx)
    assert res2.__class__.__name__ == "Done"
    assert res2.outcome == "failed"
    assert "boom" in res2.output["error"]


@pytest.mark.asyncio
async def test_agent_revise_plan_ready(empty_store, ctx_factory):
    from harness.adapters.base import AgentSnapshot, AgentState

    adapter = FakeAdapter(
        script={
            "*": [
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v1")],
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v2")],
            ]
        }
    )
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

    node = AgentNode(
        kind="agent", action="revise", agent="fake", session="session", expect="plan", message="fix"
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

    executor = AgentExecutor()
    res1 = await executor.advance(step, node, ctx)
    step.ref = res1.ref
    assert res1.__class__.__name__ == "Waiting"

    res2 = await executor.advance(step, node, ctx)
    assert res2.__class__.__name__ == "Done"
    assert res2.outcome == "plan_ready"
