"""Crash matrix: kill the runner at every named point, recover with a fresh Runner on the same store."""

from datetime import UTC, datetime

import pytest

from harness.adapters.base import AdapterRegistry
from harness.engine import Runner, SimulatedCrash, rehydrate, to_snapshot
from harness.engine.runner import CRASH_POINTS
from harness.github.client import GitHub
from harness.github.ledger import parse_ledger
from harness.store.db import Store
from tests.engine.helpers import (
    REPO,
    CountingAdapter,
    FakeClock,
    RecordingOutbox,
    StubAgentExecutor,
    agent_playbook,
    drive,
    make_issue,
    registry_for,
)


def assert_exactly_once(env, adapter, run_id):
    run = env.run(run_id)
    assert run.status == "done"
    assert run.context["pr"]["number"] == 101
    assert adapter.start_invocations == 1
    assert adapter.count("start") == 1
    assert adapter.count("approve_plan") == 1
    ids = [s.id for s in env.steps(run_id)]
    assert len(ids) == len(set(ids)) == 2
    assert sorted(i.removeprefix(run_id + "/") for i in ids) == ["implement/1", "plan/1"]
    assert run.context["visits"] == {"plan": 1, "implement": 1}


def fresh_runner(env):
    return env.runner(StubAgentExecutor())


@pytest.mark.parametrize("warmup", range(5))
@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_matrix(env, point, warmup):
    """`warmup` clean ticks, then crash once at `point` on the next tick, then a new Runner finishes."""
    adapter = CountingAdapter.happy_path()
    env.adapters.register(adapter)
    first = fresh_runner(env)
    run_id = first.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    for _ in range(warmup):
        await first.tick()
        env.clock.advance(60)

    first.crash_after = point
    with pytest.raises(SimulatedCrash) as crash:
        for _ in range(10):
            await first.tick()
            env.clock.advance(60)
    assert str(crash.value) == point

    second = fresh_runner(env)  # same store, same FakeAdapter instance (the "remote agent")
    run = await drive(second, env, run_id)
    assert run.status == "done"
    assert_exactly_once(env, adapter, run_id)


@pytest.mark.parametrize("point", ["after_step_update", "after_route"])
async def test_crash_inside_the_transition_leaves_the_old_state(env, point):
    """Hooks inside the step transition fire with the new rows written; the tx rollback hides them."""
    adapter = CountingAdapter.happy_path()
    env.adapters.register(adapter)
    runner = fresh_runner(env)
    run_id = runner.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    await runner.tick()
    env.clock.advance(60)
    before_steps = [s.model_dump() for s in env.steps(run_id)]
    before_run = env.run(run_id).model_dump()
    before_outbox = env.store.query("SELECT id, op, payload_json FROM outbox ORDER BY id")
    before_events = env.store.query("SELECT id FROM events")

    runner.crash_after = point
    with pytest.raises(SimulatedCrash):
        await runner.tick()  # plan reports PLAN_READY: done + routed to implement, all in one tx

    assert [s.model_dump() for s in env.steps(run_id)] == before_steps
    assert env.run(run_id).model_dump() == before_run
    assert env.store.query("SELECT id, op, payload_json FROM outbox ORDER BY id") == before_outbox
    assert env.store.query("SELECT id FROM events") == before_events


async def test_crash_after_mirror_leaves_the_new_state_committed(env):
    adapter = CountingAdapter.happy_path()
    env.adapters.register(adapter)
    runner = fresh_runner(env)
    run_id = runner.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    await runner.tick()
    env.clock.advance(60)
    runner.crash_after = "after_mirror"
    with pytest.raises(SimulatedCrash):
        await runner.tick()
    assert env.step_ids(run_id) == ["implement/1", "plan/1"]
    assert env.steps(run_id)[1].status == "done"
    snapshot = parse_ledger(env.outbox_rows("upsert_ledger")[0]["payload"]["body"])
    assert snapshot["cursor"] == [f"{run_id}/implement/1"]  # the mirror committed with the transition


@pytest.mark.parametrize("warmup", [1, 2, 3, 4])
async def test_rebuild_from_the_ledger_after_losing_the_database(env, warmup):
    """Crash, throw the local store away, rehydrate from the last ledger snapshot, finish: still once."""
    adapter = CountingAdapter.happy_path()
    env.adapters.register(adapter)
    first = fresh_runner(env)
    pb = agent_playbook()
    run_id = first.start_run(REPO, make_issue(), pb, agent="fake")
    for _ in range(warmup):
        await first.tick()
        env.clock.advance(60)
    first.crash_after = "after_advance"
    with pytest.raises(SimulatedCrash):
        await first.tick()
    ledger = parse_ledger(env.outbox_rows("upsert_ledger")[0]["payload"]["body"])

    clock = FakeClock(env.clock.t)
    store = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    store.migrate()

    async def no_sleep(_):
        return None

    gh = GitHub("t", write_spacing=0.0, sleep=no_sleep)
    try:
        assert rehydrate(store, ledger) == run_id
        outbox = RecordingOutbox(store, gh, clock=clock, sleep=no_sleep)
        adapters = AdapterRegistry()
        adapters.register(adapter)
        runner = Runner(
            store,
            outbox,
            registry_for(StubAgentExecutor()),
            adapters,
            clock=clock,
            playbooks={pb.id: pb},
        )
        for _ in range(50):
            await runner.tick()
            clock.advance(60)
            if to_snapshot(store, run_id)["run"]["status"] == "done":
                break
        final = to_snapshot(store, run_id)
        assert final["run"]["status"] == "done" and final["pr"]["number"] == 101
        assert adapter.start_invocations == 1
        assert adapter.count("approve_plan") == 1
        assert len({s["id"] for s in final["steps"]}) == 2
    finally:
        await gh.aclose()
        store.close()
