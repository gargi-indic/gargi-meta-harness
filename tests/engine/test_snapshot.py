"""RunSnapshot: output capping, round trip through an empty store, size under the ledger limit."""

import json

import pytest

from harness.engine import Done, rehydrate, to_snapshot
from harness.engine.model import RunSnapshot, cap_output
from harness.github.ledger import MAX_LEDGER_CHARS, parse_ledger, render_ledger
from harness.store.db import Store
from harness.store.repo import GateRow, insert_gate, steps_for_run
from tests.engine.helpers import CHECK, REPO, Scripted, make_issue, make_playbook


def big(i: int, size: int = 20_000) -> dict:
    return {"log": f"line {i}\n" * (size // 8), "i": i, "small": "kept?"}


async def build_long_run(env, steps: int = 30):
    pb = make_playbook(
        {"work": {**CHECK, "max_visits": steps + 5}}, {"work.fail": "work", "work.pass": "@done"}
    )

    def work(step, ctx):
        return Done(
            "pass" if step.visit == steps else "fail", {**big(step.visit), "correction": f"fix {step.visit}"}
        )

    runner = env.runner(Scripted({"work": [work]}))
    run_id = runner.start_run(REPO, make_issue(), pb, inputs={"k": 1}, agent="fake")
    for _ in range(steps):
        await runner.tick()
    assert env.run(run_id).status == "done"
    return run_id


async def test_30_step_run_with_large_outputs_fits_the_ledger(env):
    run_id = await build_long_run(env)
    snap = to_snapshot(env.store, run_id)
    assert len(snap["steps"]) == 30
    rendered = render_ledger(snap, "| Field | Value |\n| --- | --- |\n| Run | x |")
    assert len(rendered) < 60_000 < MAX_LEDGER_CHARS
    # Every big output was replaced by a marker; the full text is still in the local store.
    assert all(
        s["output"] == {"truncated": True, "keys": ["correction", "i", "log", "small"]} for s in snap["steps"]
    )
    assert snap["nodes"]["work"]["truncated"] is True
    assert all(v["truncated"] for v in snap["prev_for"].values() if v)
    assert len(env.steps(run_id)[0].output["log"]) > 8 * 1024
    assert parse_ledger(rendered) == snap


async def test_snapshot_round_trip_through_an_empty_store(env):
    run_id = await build_long_run(env, steps=12)
    # Add one small output and a gate so every field is exercised.
    env.store.execute(
        "UPDATE steps SET output_json = ? WHERE id = ?", (json.dumps({"ok": True}), f"{run_id}/work/1")
    )
    insert_gate(
        env.store,
        GateRow(
            id=f"{run_id}/work/1/gate",
            run_id=run_id,
            step_id=f"{run_id}/work/1",
            name="g",
            status="pending",
            note=None,
            decided_by=None,
            created_at="2027-01-15T08:00:00Z",
            decided_at=None,
        ),
    )
    first = to_snapshot(env.store, run_id)
    assert RunSnapshot.model_validate(first).v == 1
    assert first["gates"][0]["name"] == "g" and first["steps"][0]["output"] == {"ok": True}
    assert first["agent"] == "fake" and first["inputs"] == {"k": 1} and first["visits"] == {"work": 12}

    other = Store(":memory:")
    other.migrate()
    assert rehydrate(other, first) == run_id
    assert to_snapshot(other, run_id) == first
    # Idempotent: rehydrating again (or into the original store) changes nothing.
    assert rehydrate(other, first) == run_id
    assert rehydrate(env.store, first) == run_id
    assert to_snapshot(other, run_id) == first
    assert len(steps_for_run(other, run_id)) == 12
    other.close()


async def test_rehydrate_fills_in_only_missing_rows(env):
    run_id = await build_long_run(env, steps=3)
    snap = to_snapshot(env.store, run_id)
    other = Store(":memory:")
    other.migrate()
    partial = {**snap, "steps": snap["steps"][:1]}
    rehydrate(other, partial)
    other.execute("UPDATE steps SET status = 'waiting' WHERE id = ?", (snap["steps"][0]["id"],))
    rehydrate(other, snap)
    steps = steps_for_run(other, run_id)
    assert len(steps) == 3
    assert (
        next(s for s in steps if s.id == snap["steps"][0]["id"]).status == "waiting"
    )  # existing row untouched
    other.close()


def test_rehydrate_rejects_unknown_versions():
    store = Store(":memory:")
    store.migrate()
    with pytest.raises(ValueError):
        rehydrate(store, {"v": 2})
    store.close()


def test_cap_output():
    assert cap_output(None, 10) is None
    assert cap_output({"a": 1}, 100) == {"a": 1}
    assert cap_output({"b": "x" * 50, "a": 1}, 10) == {"truncated": True, "keys": ["a", "b"]}
    marker = {"truncated": True, "keys": ["a"]}
    assert cap_output(marker, 0) is marker  # idempotent, so truncation survives a round trip
