import pytest

from harness.engine.executors.base import Done
from harness.engine.model import MAX_OUTPUT_BYTES, cap_output, rehydrate, to_snapshot
from harness.github.ledger import render_ledger
from harness.store.db import Store
from harness.store.repo import get_run, steps_for_run, update_step
from tests.engine.support import REPO, Scripted, check, drive, events, make_issue, make_playbook

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

LINEAR = make_playbook(
    {"a": check(), "b": check(), "c": check()},
    {"a.pass": "b", "b.pass": "c", "c.pass": "@done"},
)


@pytest.fixture
def empty_store(clock):
    db = Store(":memory:", clock=clock.datetime)
    db.migrate()
    yield db
    db.close()


async def test_snapshot_round_trips_through_an_empty_store(store, empty_store, make_runner, clock):
    pr = {"number": 101, "url": "https://example/pull/101", "head_sha": "a1"}
    ex = Scripted({"a": [Done("pass", {"x": 1})], "b": [Done("pass", {"pr": pr})], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR, inputs={"k": "v"}, agent="codex", playbook_sha="s1")
    await drive(runner, run_id, clock)

    snapshot = to_snapshot(store, run_id)
    assert snapshot["v"] == 1
    assert snapshot["run"]["status"] == "done" and snapshot["run"]["pr"] == pr
    assert [s["node"] for s in snapshot["steps"]] == ["a", "b", "c"]
    assert snapshot["visits"] == {"a": 1, "b": 1, "c": 1}
    assert snapshot["nodes"]["a"] == {"x": 1}

    assert rehydrate(empty_store, snapshot) == run_id
    assert to_snapshot(empty_store, run_id) == snapshot
    run = get_run(empty_store, run_id)
    assert run.context["inputs"] == {"k": "v"} and run.context["agent"] == "codex"
    assert run.playbook_sha == "s1" and run.ledger_comment_id is None


async def test_rehydrate_is_idempotent_and_leaves_existing_rows_alone(store, empty_store, make_runner, clock):
    ex = Scripted({"a": [Done("pass")], "b": [Done("pass")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)
    snapshot = to_snapshot(store, run_id)

    rehydrate(empty_store, snapshot)
    update_step(empty_store, f"{run_id}/b/1", output={"local": "edit"})
    rehydrate(empty_store, snapshot)  # nothing duplicated, nothing overwritten
    steps = steps_for_run(empty_store, run_id)
    assert len(steps) == 3
    assert [s for s in steps if s.node_id == "b"][0].output == {"local": "edit"}

    # A partially restored store gets only the missing steps.
    partial = Store(":memory:", clock=clock.datetime)
    partial.migrate()
    rehydrate(partial, {**snapshot, "steps": snapshot["steps"][:1]})
    rehydrate(partial, snapshot)
    assert [s.id for s in steps_for_run(partial, run_id)] == [s["id"] for s in snapshot["steps"]]
    partial.close()


async def test_rehydrate_rejects_unknown_version(empty_store, store, make_runner):
    runner = make_runner(Scripted({}))
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    with pytest.raises(ValueError, match="version"):
        rehydrate(empty_store, {**to_snapshot(store, run_id), "v": 2})
    with pytest.raises(KeyError):
        to_snapshot(store, "missing")


def test_cap_output_boundary():
    at_limit = {"s": "x" * (MAX_OUTPUT_BYTES - len('{"s":""}'))}
    assert cap_output(at_limit) == at_limit
    over = {"s": "x" * (MAX_OUTPUT_BYTES - len('{"s":""}') + 1), "other": 1}
    assert cap_output(over) == {"truncated": True, "keys": ["other", "s"]}
    assert cap_output(None) is None and cap_output({}) == {}


async def test_thirty_steps_with_large_outputs_stay_under_the_ledger_limit(
    store, empty_store, make_runner, clock
):
    pb = make_playbook({"a": check(), "b": check()}, {"a.pass": "b", "b.pass": "a", "*.end": "@done"})
    huge = {"text": "lorem ipsum " * 2000, "n": 1}  # about 24 KB
    ex = Scripted(
        {
            "a": [Done("pass", dict(huge)) for _ in range(15)] + [Done("end", {"last": True})],
            "b": [Done("pass", dict(huge)) for _ in range(15)],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock, limit=60)

    steps = steps_for_run(store, run_id)
    assert len(steps) == 31
    snapshot = to_snapshot(store, run_id)
    body = render_ledger(snapshot, "summary")
    assert len(body) < 60_000

    truncated = [s for s in snapshot["steps"] if (s["output"] or {}).get("truncated")]
    assert len(truncated) == 30
    assert truncated[0]["output"] == {"truncated": True, "keys": ["n", "text"]}
    assert snapshot["nodes"]["b"] == {"truncated": True, "keys": ["n", "text"]}
    # The full output is still in the local store.
    assert len([s for s in steps if s.output and len(s.output.get("text", "")) > MAX_OUTPUT_BYTES]) == 30

    # Rehydrating the truncated snapshot and snapshotting again changes nothing further.
    rehydrate(empty_store, snapshot)
    assert to_snapshot(empty_store, run_id) == snapshot
    assert events(store, "ledger_too_large") == []


async def test_rehydrated_run_needs_its_playbook_to_run(empty_store, store, make_runner, clock):
    ex = Scripted({"a": [Done("pass")], "b": [Done("pass")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.tick()
    rehydrate(empty_store, to_snapshot(store, run_id))

    from harness.adapters.base import AdapterRegistry
    from harness.engine.executors.base import ExecutorRegistry
    from harness.engine.runner import Runner

    executors = ExecutorRegistry()
    executors.register("check", ex)
    bare = Runner(empty_store, None, executors, AdapterRegistry(), clock=clock)
    assert await bare.tick() == 0
    assert await bare.tick() == 0
    assert len([e for e in events(empty_store, "playbook_missing")]) == 1  # reported once, not every tick

    bare.register_playbook(LINEAR)
    await drive(bare, run_id, clock)
    assert get_run(empty_store, run_id).status == "done"
