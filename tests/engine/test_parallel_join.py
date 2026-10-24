"""Parallel/join with real agent branches over the FakeAdapter: concurrency, all-failure cancel, crash recovery."""

import pytest

from harness.adapters.base import AgentSnapshot, AgentState, PrRef
from harness.engine import Done, ExecutorRegistry, SimulatedCrash
from harness.engine.runner import CRASH_POINTS
from tests.engine.helpers import (
    CHECK,
    REPO,
    CountingAdapter,
    Scripted,
    StubAgentExecutor,
    drive,
    make_issue,
    make_playbook,
)

RUN_ID = f"{REPO}#7@1"


def _agent(session: str) -> dict:
    return {"kind": "agent", "session": session, "action": "implement", "agent": "fake"}


def _pr(number: int) -> PrRef:
    return PrRef(number=number, url=f"https://example.invalid/pull/{number}", head_sha=f"sha{number}")


def _phases(delay: int, number: int, *, fail: bool = False) -> list[list[AgentSnapshot]]:
    """Plan ready, then ``delay`` working polls, then a PR (or a failure)."""
    end = (
        AgentSnapshot(state=AgentState.FAILED, error="boom")
        if fail
        else AgentSnapshot(state=AgentState.PR_READY, pr=_pr(number))
    )
    return [
        [AgentSnapshot(state=AgentState.PLAN_READY, plan="p")],
        [AgentSnapshot(state=AgentState.WORKING)] * delay + [end],
    ]


def _playbook(mode: str):
    return make_playbook(
        {
            "fork": {"kind": "parallel", "branches": ["a", "b"]},
            "a": _agent("sa"),
            "b": _agent("sb"),
            "join": {"kind": "join", "mode": mode},
            "check": CHECK,
        },
        {
            "a.pr_ready": "join",
            "a.failed": "join",
            "b.pr_ready": "join",
            "b.failed": "join",
            "join.done": "check",
            "join.failed": "@failed",
            "check.pass": "@done",
            "check.fail": "@failed",
        },
    )


def _runner(env, adapter, scripted=None):
    registry = ExecutorRegistry()
    registry.register("agent", StubAgentExecutor())
    registry.register("check", scripted or Scripted({"check": [Done("pass")]}))
    env.adapters.register(adapter)
    return env.runner(executors=registry)


def _status(env, node: str) -> str | None:
    return next((s.status for s in env.steps(RUN_ID) if s.node_id == node), None)


async def test_both_branches_are_in_flight_together_and_join_waits_for_both(env):
    adapter = CountingAdapter(script={f"{RUN_ID}/sa": _phases(1, 101), f"{RUN_ID}/sb": _phases(5, 102)})
    runner = _runner(env, adapter)
    runner.start_run(REPO, make_issue(), _playbook("all"), agent="fake")
    both_active = a_done_b_active_join_waiting = False
    for _ in range(40):
        await runner.tick()
        env.clock.advance(60)
        a, b, join, check = (_status(env, n) for n in ("a", "b", "join", "check"))
        both_active |= a == "waiting" and b == "waiting"
        if a == "done" and b == "waiting":
            assert (
                join in (None, "pending", "waiting") and check is None
            )  # the join does not fire on one arrival
            a_done_b_active_join_waiting |= join == "waiting"
        if env.run(RUN_ID).status == "done":
            break
    assert both_active and a_done_b_active_join_waiting
    assert env.run(RUN_ID).status == "done"
    assert adapter.start_invocations == 2
    assert [s.node_id for s in env.steps(RUN_ID)].count("join") == 1


async def test_all_join_with_a_failed_branch_cancels_the_still_running_sibling(env):
    adapter = CountingAdapter(
        script={f"{RUN_ID}/sa": _phases(1, 101, fail=True), f"{RUN_ID}/sb": _phases(50, 102)}
    )
    runner = _runner(env, adapter)
    runner.start_run(REPO, make_issue(), _playbook("all"), agent="fake")
    run = await drive(runner, env, RUN_ID)
    assert run.status == "failed"
    join = next(s for s in env.steps(RUN_ID) if s.node_id == "join")
    assert join.outcome == "failed"
    b = next(s for s in env.steps(RUN_ID) if s.node_id == "b")
    assert (b.status, b.outcome) == ("failed", "cancelled")
    assert adapter.count("cancel") == 1
    cancelled_session = next(c for name, c in adapter.calls if name == "cancel")["session"]
    assert cancelled_session == b.ref["id"]


async def test_any_join_does_not_cancel_the_slower_branch(env):
    adapter = CountingAdapter(script={f"{RUN_ID}/sa": _phases(1, 101), f"{RUN_ID}/sb": _phases(5, 102)})
    runner = _runner(env, adapter)
    runner.start_run(REPO, make_issue(), _playbook("any"), agent="fake")
    run = await drive(runner, env, RUN_ID)
    assert run.status == "done"
    assert adapter.count("cancel") == 0


@pytest.mark.parametrize("warmup", range(4))
@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_around_the_fan_out_leaves_one_join_and_no_duplicate_sessions(env, point, warmup):
    adapter = CountingAdapter(script={f"{RUN_ID}/sa": _phases(1, 101), f"{RUN_ID}/sb": _phases(3, 102)})
    first = _runner(env, adapter)
    first.start_run(REPO, make_issue(), _playbook("all"), agent="fake")
    for _ in range(warmup):
        await first.tick()
        env.clock.advance(60)
    first.crash_after = point
    with pytest.raises(SimulatedCrash):
        for _ in range(20):
            await first.tick()
            env.clock.advance(60)

    registry = ExecutorRegistry()
    registry.register("agent", StubAgentExecutor())
    registry.register("check", Scripted({"check": [Done("pass")]}))
    second = env.runner(executors=registry)  # same store and adapter: the "restarted" process
    run = await drive(second, env, RUN_ID)
    assert run.status == "done"
    nodes = [s.node_id for s in env.steps(RUN_ID)]
    assert nodes.count("join") == 1 and nodes.count("fork") == 1
    assert len({s.id for s in env.steps(RUN_ID)}) == len(nodes)
    assert adapter.start_invocations == 2
    assert adapter.count("start") == 2
