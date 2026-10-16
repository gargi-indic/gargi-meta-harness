import pytest

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.engine.executors.base import ExecutorRegistry
from harness.engine.model import rehydrate
from harness.engine.runner import CRASH_POINTS, Runner, SimulatedCrash
from harness.github.ledger import parse_ledger
from harness.github.outbox import Outbox
from harness.store.db import Store
from harness.store.repo import get_run, steps_for_run
from tests.engine.support import (
    REPO,
    StubAgentExecutor,
    drive,
    events,
    make_issue,
    make_playbook,
    outbox_rows,
    pending_outbox,
)

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

PLAYBOOK = make_playbook(
    {
        "plan": {"kind": "agent", "session": "main", "action": "plan", "agent": "{{ issue.agent }}"},
        "implement": {
            "kind": "agent",
            "session": "main",
            "action": "implement",
            "agent": "{{ issue.agent }}",
        },
    },
    {
        "plan.plan_ready": "implement",
        "plan.pr_ready": "@done",
        "implement.pr_ready": "@done",
        "*.failed": "@failed",
    },
)


def build_runner(store, outbox, adapter, clock, **kwargs) -> Runner:
    """A fresh Runner, as after a process restart. The adapter models the remote agent and is shared."""
    adapters = AdapterRegistry()
    adapters.register(adapter)
    executors = ExecutorRegistry()
    executors.register("agent", StubAgentExecutor())
    return Runner(store, outbox, executors, adapters, clock=clock, **kwargs)


def count(adapter: FakeAdapter, name: str) -> int:
    return sum(1 for call, _ in adapter.calls if call == name)


def assert_completed_once(store, adapter, run_id, *, restored=False):
    run = get_run(store, run_id)
    assert run.status == "done"
    assert count(adapter, "start") == 1  # one session slot, started exactly once
    assert count(adapter, "approve_plan") == 1
    assert run.context["pr"]["number"] == 101
    ids = [s.id for s in steps_for_run(store, run_id)]
    assert len(ids) == len(set(ids)) == 2
    # The transactional mirror means no event comment is ever posted twice.
    titles = [r["body"].splitlines()[0] for r in outbox_rows(store, "add_comment")]
    assert len(titles) == len(set(titles)), titles
    # Events are local and not part of the snapshot, so a restored store has no run_started.
    assert len(events(store, "run_started")) == (0 if restored else 1)


@pytest.mark.parametrize("nth", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_matrix(point, nth, store, outbox, clock):
    adapter = FakeAdapter.happy_path()
    crashed = build_runner(store, outbox, adapter, clock, crash_after=f"{point}#{nth}")
    run_id = crashed.start_run(REPO, make_issue(), PLAYBOOK, agent="fake")

    died = False
    try:
        await drive(crashed, run_id, clock)
    except SimulatedCrash:
        died = True
    if nth == 1:
        assert died, f"{point} was never reached"

    fresh = build_runner(store, outbox, adapter, clock)  # same Store, brand-new Runner
    await drive(fresh, run_id, clock)
    assert_completed_once(store, adapter, run_id)


@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_inside_a_transition_never_leaves_a_mix(point, store, outbox, clock):
    adapter = FakeAdapter.happy_path()
    runner = build_runner(store, outbox, adapter, clock, crash_after=f"{point}#2")
    run_id = runner.start_run(REPO, make_issue(), PLAYBOOK, agent="fake")

    def state():
        run = get_run(store, run_id)
        return (
            run.status,
            repr(run.context),
            [
                (s.id, s.status, s.outcome, s.ref, s.output, s.poll_after)
                for s in steps_for_run(store, run_id)
            ],
            len(events(store)),
            len(outbox_rows(store)),
            [r["body"] for r in pending_outbox(store, "upsert_ledger")],
        )

    before = state()
    with pytest.raises(SimulatedCrash):
        for _ in range(10):
            before = state()
            await runner.tick()
            clock.t += 61
    after = state()
    if point == "after_mirror":
        # The transition committed completely before the crash: the ledger shows the new state.
        assert after != before
        snapshot = parse_ledger(after[5][0])
        assert [s["status"] for s in snapshot["steps"]] == [s[1] for s in after[2]]
    else:
        # Rolled back (or never started): the old state, with no stray rows.
        assert after == before


async def test_one_shot_crash_and_unknown_points(store, outbox, clock):
    adapter = FakeAdapter.happy_path()
    runner = build_runner(store, outbox, adapter, clock, crash_after="before_advance")
    run_id = runner.start_run(REPO, make_issue(), PLAYBOOK, agent="fake")
    with pytest.raises(SimulatedCrash):
        await runner.tick()
    await drive(runner, run_id, clock)  # the same Runner no longer crashes
    assert get_run(store, run_id).status == "done"

    with pytest.raises(ValueError, match="unknown crash point"):
        build_runner(store, outbox, adapter, clock, crash_after="whenever")


async def test_recovery_from_ledger_snapshot_alone(store, outbox, clock):
    """The local DB is lost: rebuild the run from its last ledger comment and finish it."""
    adapter = FakeAdapter.happy_path()
    runner = build_runner(store, outbox, adapter, clock)
    run_id = runner.start_run(REPO, make_issue(), PLAYBOOK, agent="fake")
    for _ in range(3):  # plan done, implement approved and polling
        await runner.tick()
        clock.t += 61
    assert get_run(store, run_id).status == "running"
    (ledger,) = pending_outbox(store, "upsert_ledger")
    snapshot = parse_ledger(ledger["body"])
    assert count(adapter, "start") == 1

    new_store = Store(":memory:", clock=clock.datetime)
    new_store.migrate()
    rehydrate(new_store, snapshot)
    new_outbox = Outbox(new_store, outbox.gh)
    adapters = AdapterRegistry()
    adapters.register(adapter)
    executors = ExecutorRegistry()
    executors.register("agent", StubAgentExecutor())
    revived = Runner(
        new_store, new_outbox, executors, adapters, clock=clock, playbooks={PLAYBOOK.id: PLAYBOOK}
    )
    await drive(revived, run_id, clock)

    assert_completed_once(new_store, adapter, run_id, restored=True)
    new_store.close()
