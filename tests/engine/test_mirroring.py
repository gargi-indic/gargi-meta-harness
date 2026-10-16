import random

import pytest

from harness.config import RepoConfig
from harness.engine.executors.base import Done, ExecutorRegistry, Waiting
from harness.engine.model import to_snapshot
from harness.engine.runner import Runner
from harness.github.ledger import LEDGER_MARKER, parse_ledger
from harness.store.repo import get_run, upsert_issue
from tests.engine.support import (
    REPO,
    Scripted,
    check,
    drive,
    flush_outbox,
    make_issue,
    make_playbook,
    outbox_rows,
    pending_outbox,
)

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

LINEAR = make_playbook(
    {"a": check(), "b": check()},
    {"a.pass": "b", "b.pass": "@done", "*.fail": "@failed"},
)

PIPELINE = make_playbook(
    {
        "plan": {"kind": "agent", "session": "main", "action": "plan"},
        "rev": {"kind": "review", "target": "plan", "checklist": ["x"]},
        "impl": {"kind": "agent", "session": "main", "action": "implement"},
    },
    {
        "plan.plan_ready": "rev",
        "rev.approve": "impl",
        "impl.pr_ready": "@done",
        "*.failed": "@failed",
    },
)


def comment_titles(store):
    return [r["body"].splitlines()[0] for r in pending_outbox(store, "add_comment")]


async def test_start_run_enqueues_ledger_label_and_comment(store, make_runner):
    runner = make_runner(Scripted({}))
    run_id = runner.start_run(REPO, make_issue(7), LINEAR)

    (ledger,) = pending_outbox(store, "upsert_ledger")
    assert ledger["key"] == f"ledger:{REPO}#7" and ledger["issue"] == 7
    assert ledger["body"].startswith(LEDGER_MARKER)
    assert f"`{run_id}`" in ledger["body"] and "| running |" in ledger["body"]
    assert parse_ledger(ledger["body"]) == to_snapshot(store, run_id)

    (labels,) = pending_outbox(store, "set_labels")
    assert labels["add"] == ["harness:running"]
    assert "harness:running" not in labels["remove"] and "harness:done" in labels["remove"]

    assert comment_titles(store) == ["**2027-01-15 · #7 run started.**"]
    # No control issue configured: no control-table write.
    assert pending_outbox(store, "edit_issue_body") == []
    assert run_id


async def test_ledger_is_coalesced_and_always_matches_the_store(store, make_runner, clock):
    runner = make_runner(Scripted({"a": [Done("pass")], "b": [Done("pass")]}))
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)

    (ledger,) = pending_outbox(store, "upsert_ledger")
    snapshot = parse_ledger(ledger["body"])
    assert snapshot == to_snapshot(store, run_id)
    assert snapshot["run"]["status"] == "done"
    assert [s["status"] for s in snapshot["steps"]] == ["done", "done"]
    (labels,) = pending_outbox(store, "set_labels")
    assert labels["add"] == ["harness:done"]


async def test_labels_only_change_with_status_and_polls_write_nothing(store, make_runner, clock):
    ex = Scripted(
        {
            "a": [
                Waiting(ref={"id": "s1"}, poll_after=0),
                Waiting(poll_after=0),  # same state again
                Waiting(ref={"id": "s1", "n": 2}, poll_after=0),  # progress, still running
                Done("pass"),
            ],
            "b": [Done("pass")],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    flush_outbox(store)

    await runner.tick()  # pending -> waiting
    assert [r["op"] for r in pending_outbox(store)] == ["upsert_ledger"]
    flush_outbox(store)

    clock.t += 1
    await runner.tick()  # plain re-poll: no mirror, no comment
    assert pending_outbox(store) == []

    clock.t += 1
    await runner.tick()  # ref changed: ledger only
    assert [r["op"] for r in pending_outbox(store)] == ["upsert_ledger"]
    flush_outbox(store)

    clock.t += 1
    await runner.tick()  # a done: ledger only (b is pending, the run is still running)
    assert [r["op"] for r in pending_outbox(store)] == ["upsert_ledger"]
    flush_outbox(store)

    await drive(runner, run_id, clock)
    ops = [r["op"] for r in pending_outbox(store)]
    assert ops.count("set_labels") == 1  # only the move to done
    assert comment_titles(store) == ["**2027-01-15 · #7 done.**"]


async def test_event_comments_only_for_meaningful_transitions(store, make_runner, clock):
    pr = {"number": 101, "url": "https://example/pull/101", "head_sha": "a1"}
    ex = Scripted(
        {
            "plan": [Waiting(poll_after=0), Waiting(poll_after=0), Done("plan_ready", {"plan": "p"})],
            "rev": [Done("approve")],
            "impl": [Waiting(poll_after=0), Done("pr_ready", {"pr": pr})],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), PIPELINE)
    await drive(runner, run_id, clock)

    bodies = [r["body"] for r in outbox_rows(store, "add_comment")]
    titles = [b.splitlines()[0].split(" ", 3)[3] for b in bodies]
    assert titles == ["run started.**", "plan ready.**", "review: approve.**", "PR opened.**", "done.**"]
    assert "- https://example/pull/101" in bodies[3]
    assert get_run(store, run_id).context["pr"] == pr


async def test_failure_comments_are_not_duplicated(store, make_runner, clock):
    runner = make_runner(Scripted({"a": [RuntimeError("boom")], "b": [Done("pass")]}))
    pb = make_playbook(
        {"a": check(), "b": check()}, {"a.pass": "b", "b.pass": "@done", "*.failed": "@failed"}
    )
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "failed"
    assert comment_titles(store) == [
        "**2027-01-15 · #7 run started.**",
        "**2027-01-15 · #7 executor error.**",
    ]
    (labels,) = pending_outbox(store, "set_labels")
    assert labels["add"] == ["harness:failed"]


async def test_plain_failed_route_is_announced_once(store, make_runner, clock):
    runner = make_runner(Scripted({"a": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)
    assert comment_titles(store)[-1] == "**2027-01-15 · #7 failed.**"
    assert comment_titles(store).count("**2027-01-15 · #7 failed.**") == 1


async def test_cancel_and_pause_labels(store, make_runner, clock):
    runner = make_runner(Scripted({"a": [Waiting(poll_after=0)]}))
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.tick()
    flush_outbox(store)

    runner.pause(run_id)
    (labels,) = pending_outbox(store, "set_labels")
    assert labels["add"] == ["harness:blocked"]
    assert comment_titles(store) == []
    flush_outbox(store)

    await runner.cancel(run_id)
    (labels,) = pending_outbox(store, "set_labels")
    assert labels["add"] == ["harness:failed"]
    assert comment_titles(store) == ["**2027-01-15 · #7 cancelled.**"]


async def test_control_table_row_follows_the_run(store, make_runner, clock):
    upsert_issue(store, make_issue(7, labels=["agent:codex"], title="Fix the widget"))
    cfg = {REPO: RepoConfig(control_issue=5)}
    pr = {"number": 101, "url": "https://example/pull/101", "head_sha": "a1"}
    ex = Scripted(
        {
            "plan": [Done("plan_ready", {"pr": pr})],
            "rev": [Waiting(poll_after=0), Done("approve")],
            "impl": [Waiting(poll_after=0), Done("pr_ready", {"pr": pr})],
        }
    )
    runner = make_runner(ex, repo_configs=cfg)
    run_id = runner.start_run(REPO, make_issue(7, labels=["agent:codex"]), PIPELINE)

    def row():
        (r,) = pending_outbox(store, "edit_issue_body")
        assert r["key"] == f"control:{REPO}#7" and r["issue"] == 5
        return r["row"]

    assert row()["status"] == "running"
    assert row()["title"] == "Fix the widget" and row()["agent"] == "codex" and row()["run_id"] == run_id
    assert row()["pr"] is None

    await runner.tick()  # plan done -> rev pending
    clock.t += 1
    await runner.tick()  # rev waiting
    assert row()["status"] == "review" and row()["pr"] == "#101"
    runner.pause(run_id)
    assert row()["status"] == "blocked"
    runner.resume(run_id)
    await drive(runner, run_id, clock)
    assert row()["status"] == "done" and row()["pr"] == "#101"


async def test_cancelled_run_shows_failed_in_the_control_table(store, make_runner):
    runner = make_runner(Scripted({}), repo_configs={REPO: RepoConfig(control_issue=5)})
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.cancel(run_id)
    (r,) = pending_outbox(store, "edit_issue_body")
    assert r["row"]["status"] == "failed"


async def test_runner_without_an_outbox_still_runs(store, adapters, clock):
    executors = ExecutorRegistry()
    executors.register("check", Scripted({"a": [Done("pass")], "b": [Done("pass")]}))
    runner = Runner(store, None, executors, adapters, clock=clock)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)
    assert get_run(store, run_id).status == "done"
    assert outbox_rows(store) == []


async def test_oversized_snapshot_degrades_instead_of_failing(store, make_runner, clock):
    rng = random.Random(0)
    # 30 steps of incompressible 7 KB outputs: too big for one comment even after compression.
    big = lambda: {"blob": "".join(rng.choices("0123456789abcdef", k=7000))}  # noqa: E731
    pb = make_playbook({"a": check(), "b": check()}, {"a.pass": "b", "b.pass": "a", "*.end": "@done"})
    ex = Scripted(
        {
            "a": [Done("pass", big()) for _ in range(15)] + [Done("end")],
            "b": [Done("pass", big()) for _ in range(15)],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock, limit=60)

    assert get_run(store, run_id).status == "done"
    (ledger,) = pending_outbox(store, "upsert_ledger")
    assert len(ledger["body"]) < 65_000
    snapshot = parse_ledger(ledger["body"])
    assert len(snapshot["steps"]) == 31
    assert snapshot["steps"][0]["output"]["truncated"] is True
