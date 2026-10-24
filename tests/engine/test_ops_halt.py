"""Kill switch, daily cap pause and the dispatcher gate (M5.6), driven through the real Runner with FakeAdapters."""

from datetime import UTC

from harness.adapters.base import SessionRef
from harness.engine.dispatcher import Dispatcher
from harness.ops.budget import DailyBudget
from harness.ops.costs import UsageRepo
from harness.ops.halt import HaltState, OpsGate, enforce_daily_cap, kill_all, queue_resume
from tests.engine.helpers import REPO, CountingAdapter, StubAgentExecutor, agent_playbook, make_issue

CONTROL = 99


class FlakyCancelAdapter(CountingAdapter):
    """Cancelling the sessions in ``fail`` raises, like an agent API that is down."""

    def __init__(self, *args, fail: set[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail = fail or set()

    async def cancel(self, ref: SessionRef) -> None:
        await super().cancel(ref)
        if ref.id in self.fail:
            raise RuntimeError("agent API down")


async def start_runs(env, adapter, numbers=(7, 8, 9)):
    env.adapters.register(adapter)
    runner = env.runner(StubAgentExecutor())
    ids = [runner.start_run(REPO, make_issue(n), agent_playbook(), agent="fake") for n in numbers]
    await runner.tick()
    return runner, ids


def halt_state(env) -> HaltState:
    return HaltState(env.store, env.outbox, [(REPO, CONTROL)])


def halted_comments(env) -> list[str]:
    return [t for t in env.outbox.comment_titles() if t == "halted"]


async def test_kill_all_cancels_each_session_once_tolerates_one_failure_and_logs_once(env) -> None:
    adapter = FlakyCancelAdapter.happy_path()
    adapter.fail = {"fake-2"}
    runner, ids = await start_runs(env, adapter)
    state = halt_state(env)

    report = await kill_all(runner, env.store, state, by="cli")

    cancels = [c["session"] for name, c in adapter.calls if name == "cancel"]
    assert sorted(cancels) == ["fake-1", "fake-2", "fake-3"]  # each exactly once
    assert [env.run(i).status for i in ids] == ["cancelled"] * 3  # the failing one too
    assert len(report.errors) == 1 and "agent API down" in report.errors[0][1]
    assert report.errors[0][0] in ids
    assert state.halted().reason == "kill_switch"  # type: ignore[union-attr]
    assert len(halted_comments(env)) == 1  # ONE ledger event
    assert len(env.events("ops_halted")) == 1
    (op,) = [r for r in env.outbox_rows("merge_ledger") if r["payload"]["key"] == "ops"]
    assert op["payload"]["value"]["halt"]["reason"] == "kill_switch"


async def test_a_second_kill_all_does_nothing(env) -> None:
    adapter = CountingAdapter.happy_path()
    runner, _ = await start_runs(env, adapter, (7,))
    state = halt_state(env)
    await kill_all(runner, env.store, state, by="cli")
    cancels = adapter.count("cancel")
    comments, events = len(halted_comments(env)), len(env.events("ops_halted"))
    writes = len(env.outbox.enqueued)

    report = await kill_all(runner, env.store, state, by="cli")

    assert report.errors == []
    assert adapter.count("cancel") == cancels
    assert (len(halted_comments(env)), len(env.events("ops_halted"))) == (comments, events)
    assert len(env.outbox.enqueued) == writes  # nothing queued at all


def budget_for(env, limit: float | None = 5.0, state: HaltState | None = None) -> DailyBudget:
    return DailyBudget(
        UsageRepo(env.store),
        limit_usd=limit,
        override=state.get_budget_override if state else None,
        tz=UTC,
        clock=env.clock.dt,
    )


def spend(env, usd: float) -> None:
    UsageRepo(env.store).add(run_id="x", step_id="x/s/1", source="agent", adapter="fake", cost_usd=usd)


def pending(env, run_id: str, kind: str) -> int:
    return len(
        env.store.query(
            "SELECT 1 FROM control_requests WHERE run_id = ? AND kind = ? AND handled_at IS NULL",
            (run_id, kind),
        )
    )


async def test_the_daily_cap_halts_and_pauses_each_running_run_once_with_an_escalation(env) -> None:
    runner, ids = await start_runs(env, CountingAdapter.happy_path(), (7, 8))
    state = halt_state(env)
    budget = budget_for(env, 5.0, state)

    assert enforce_daily_cap(runner, env.store, state, budget).run_ids == []  # under the cap: nothing
    spend(env, 6.0)
    assert sorted(enforce_daily_cap(runner, env.store, state, budget).run_ids) == sorted(ids)
    assert enforce_daily_cap(runner, env.store, state, budget).run_ids == []  # pending: not queued twice
    assert [pending(env, i, "pause") for i in ids] == [1, 1]
    halt = state.halted()
    assert halt is not None and (halt.reason, halt.by) == ("daily_budget", "system")
    for i in ids:
        step = next(s for s in env.steps(i) if s.node_id == "budget")
        assert step.id == f"{i}/budget/1" and step.status == "waiting" and step.output["needs_you"] is True

    await runner.tick()  # the daemon's Runner applies the pauses
    assert [env.run(i).status for i in ids] == ["paused", "paused"]
    assert enforce_daily_cap(runner, env.store, state, budget).run_ids == []  # paused: left alone
    assert [pending(env, i, "pause") for i in ids] == [0, 0]
    assert len(env.events("ops_halted")) == 1


async def test_resume_lifts_the_halt_and_resumes_only_the_runs_the_cap_paused(env) -> None:
    runner, (capped, manual) = await start_runs(env, CountingAdapter.happy_path(), (7, 8))
    state = halt_state(env)
    runner.pause(manual)  # a human pause that the cap must not undo
    spend(env, 6.0)
    enforce_daily_cap(runner, env.store, state, budget_for(env, 5.0, state))
    await runner.tick()
    assert env.run(capped).status == "paused" and env.run(manual).status == "paused"

    assert queue_resume(env.store, state, by="cli") == [capped]
    assert state.halted() is None
    await runner.tick()
    assert env.run(capped).status == "running"
    assert env.run(manual).status == "paused"
    assert next(s for s in env.steps(capped) if s.node_id == "budget").status == "done"  # escalation resolved
    assert len(env.events("ops_resumed")) == 1


async def test_a_raised_cap_or_a_new_day_does_not_clear_the_halt(env) -> None:
    runner, _ = await start_runs(env, CountingAdapter.happy_path(), (7,))
    state = halt_state(env)
    spend(env, 6.0)
    enforce_daily_cap(runner, env.store, state, budget_for(env, 5.0, state))
    state.set_daily_budget(100.0)
    env.clock.advance(2 * 86400)
    assert not budget_for(env, 5.0, state).status().exceeded
    assert enforce_daily_cap(runner, env.store, state, budget_for(env, 5.0, state)).run_ids == []
    assert state.halted().reason == "daily_budget"  # type: ignore[union-attr]


def ready(*numbers: int):
    return [make_issue(n, labels=("agent:claude",)) for n in numbers]


def test_the_dispatcher_gate_holds_back_starts_while_halted_or_over_the_cap_and_resume_allows_them(
    env,
) -> None:
    state = halt_state(env)
    budget = budget_for(env, 5.0, state)
    dispatcher = Dispatcher(store=env.store, caps={"claude": 5}, gate=OpsGate(state, budget))
    assert [d.reason for d in dispatcher.plan(ready(1, 2), [])] == ["started", "started"]

    state.halt("kill_switch", "cli")
    assert [d.reason for d in dispatcher.plan(ready(1, 2), [])] == ["halted", "halted"]
    state.resume("cli")
    assert [d.reason for d in dispatcher.plan(ready(1), [])] == ["started"]  # resume allows

    spend(env, 6.0)
    assert [d.reason for d in dispatcher.plan(ready(1, 2), [])] == ["daily_budget", "daily_budget"]
    state.set_daily_budget(50.0)  # a raised cap allows starts again
    assert [d.reason for d in dispatcher.plan(ready(1), [])] == ["started"]


def test_without_a_gate_the_dispatcher_is_unchanged(env) -> None:
    dispatcher = Dispatcher(store=env.store, caps={"claude": 5})
    assert [d.reason for d in dispatcher.plan(ready(1, 2), [])] == ["started", "started"]


def test_the_gate_only_replaces_would_be_starts(env) -> None:
    state = halt_state(env)
    state.halt("kill_switch", "cli")
    dispatcher = Dispatcher(store=env.store, caps={"claude": 5}, gate=OpsGate(state, budget_for(env, None)))
    decisions = dispatcher.plan([make_issue(1, labels=("agent:nope",))], [])
    assert [d.reason for d in decisions] == ["agent_disabled"]  # not a start: its own reason stays
