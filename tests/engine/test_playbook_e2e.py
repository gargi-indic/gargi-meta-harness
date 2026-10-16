import pytest

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine.executors.agent import AgentExecutor
from harness.engine.executors.base import Done, ExecContext, Executor, ExecutorRegistry, Waiting
from harness.engine.executors.branch import BranchExecutor
from harness.engine.executors.escalate import EscalateExecutor
from harness.engine.executors.notify import NotifyExecutor
from harness.engine.runner import Runner
from harness.playbook.load import builtin_playbooks
from harness.store.db import Store


class ScriptedExecutor(Executor):
    def __init__(self, script: list[tuple[str, dict]]):
        self.script = script
        self.visits = 0

    async def advance(self, step, node, ctx: ExecContext) -> Waiting | Done:
        idx = min(self.visits, len(self.script) - 1)
        self.visits += 1
        outcome, output = self.script[idx]
        return Done(outcome, output)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.migrate()
    with s.tx():
        s.execute(
            "INSERT INTO issues (repo, number, id, title, body, state, labels_json, parent, blocked_by_json, updated_at) VALUES ('owner/repo', 1, 100, 'Test', 'body', 'open', '[]', NULL, '[]', '2023')"
        )
    return s


def make_runner(store, script_review, script_check, script_gate, script_merge, adapter):
    executors = ExecutorRegistry()
    executors.register("agent", AgentExecutor())
    executors.register("branch", BranchExecutor())
    executors.register("escalate", EscalateExecutor())
    executors.register("notify", NotifyExecutor())
    executors.register("review", ScriptedExecutor(script_review))
    executors.register("check", ScriptedExecutor(script_check))
    executors.register("gate", ScriptedExecutor(script_gate))
    executors.register("merge", ScriptedExecutor(script_merge))

    adapters = AdapterRegistry()
    adapters.register(adapter)

    playbooks = builtin_playbooks()

    return Runner(
        store,
        outbox=None,
        executors=executors,
        adapters=adapters,
        playbooks=playbooks,
        gh=None,
        repo_configs={"owner/repo": RepoConfig()},
        clock=lambda: 1000.0,
    )


def init_run(runner, playbook_id="plan-build-review") -> str:
    from harness.store.repo import get_issue

    issue = get_issue(runner.store, "owner/repo", 1)
    playbook = runner.playbooks[playbook_id]
    return runner.start_run("owner/repo", issue, playbook, agent="fake")


async def step_until_idle(runner):
    for i in range(100):
        runner.clock = lambda i=i: 1000.0 + i * 1000
        advanced = await runner.tick()
        if advanced == 0:
            break


@pytest.mark.asyncio
async def test_e2e_happy_path(store):
    adapter = FakeAdapter.happy_path()
    runner = make_runner(
        store,
        script_review=[("approve", {})],
        script_check=[("pass", {})],
        script_gate=[("approved", {})],
        script_merge=[("merged", {})],
        adapter=adapter,
    )

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status, context_json FROM runs")[0]
    steps = store.query("SELECT node_id, status, outcome FROM steps")
    assert run["status"] == "done", f"Steps: {steps}"

    # check adapter calls
    starts = [c for c in adapter.calls if c[0] == "start"]
    assert len(starts) == 1
    approves = [c for c in adapter.calls if c[0] == "approve_plan"]
    assert len(approves) == 1


@pytest.mark.asyncio
async def test_e2e_plan_rejected_once(store):
    adapter = FakeAdapter.plan_rejected_once()
    runner = make_runner(
        store,
        script_review=[("changes", {"correction": "no good"}), ("approve", {})],
        script_check=[("pass", {})],
        script_gate=[("approved", {})],
        script_merge=[("merged", {})],
        adapter=adapter,
    )

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status FROM runs")[0]
    assert run["status"] == "done"
    sends = [c for c in adapter.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "no good" in sends[0][1]["text"]


@pytest.mark.asyncio
async def test_e2e_plan_rejected_exhausted(store):
    from harness.adapters.base import AgentSnapshot, AgentState

    adapter = FakeAdapter(
        script={
            "*": [
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v1")],
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v2")],
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v3")],
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="plan v4")],
            ]
        }
    )
    runner = make_runner(
        store,
        script_review=[("changes", {"correction": "no good"})] * 5,
        script_check=[("pass", {})],
        script_gate=[("approved", {})],
        script_merge=[("merged", {})],
        adapter=adapter,
    )

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status FROM runs")[0]
    assert run["status"] == "needs-you"


@pytest.mark.asyncio
async def test_e2e_ci_fails_then_passes(store):
    adapter = FakeAdapter.pr_fix_round()
    runner = make_runner(
        store,
        script_review=[("approve", {})],
        script_check=[("fail", {"correction": "fix ci"}), ("pass", {})],
        script_gate=[("approved", {})],
        script_merge=[("merged", {})],
        adapter=adapter,
    )

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status FROM runs")[0]
    assert run["status"] == "done"
    sends = [c for c in adapter.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "fix ci" in sends[0][1]["text"]


@pytest.mark.asyncio
async def test_e2e_pr_review_changes(store):
    adapter = FakeAdapter.pr_fix_round()
    runner = make_runner(
        store,
        script_review=[("approve", {})],
        script_check=[("pass", {})],
        script_gate=[("approved", {})],
        script_merge=[("merged", {})],
        adapter=adapter,
    )
    # The fix round uses revise_pr essentially, we will use script_review to reject pr once
    # Then approve
    runner.executors._executors["review"].script = [
        ("approve", {}),  # plan review
        ("changes", {"correction": "pr fix this"}),  # pr review 1
        ("approve", {}),  # pr review 2
    ]

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status FROM runs")[0]
    assert run["status"] == "done"
    sends = [c for c in adapter.calls if c[0] == "send"]
    assert len(sends) == 1
    assert "pr fix this" in sends[0][1]["text"]


@pytest.mark.asyncio
async def test_e2e_agent_fails(store):
    adapter = FakeAdapter.fails("boom")
    runner = make_runner(
        store, script_review=[], script_check=[], script_gate=[], script_merge=[], adapter=adapter
    )

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status FROM runs")[0]
    assert run["status"] == "needs-you"


@pytest.mark.asyncio
async def test_e2e_agent_needs_input(store):
    adapter = FakeAdapter.asks_question("what do?")
    runner = make_runner(
        store, script_review=[], script_check=[], script_gate=[], script_merge=[], adapter=adapter
    )

    init_run(runner)
    await step_until_idle(runner)

    run = store.query("SELECT status FROM runs")[0]
    assert run["status"] == "needs-you"
