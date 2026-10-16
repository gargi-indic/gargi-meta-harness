"""Mirroring to GitHub: everything goes through the outbox, coalesced where it should be."""

import random

from harness.config import RepoConfig
from harness.engine import Done, Runner, Waiting
from harness.github.ledger import is_ledger, parse_ledger
from tests.engine.helpers import (
    CHECK,
    GATE,
    REPO,
    CountingAdapter,
    Scripted,
    StubAgentExecutor,
    agent_playbook,
    drive,
    make_issue,
    make_playbook,
)


async def test_start_run_enqueues_comment_ledger_and_running_label(env):
    runner = env.runner(Scripted({}))
    run_id = runner.start_run(REPO, make_issue(), make_playbook({"a": CHECK}, {}), agent="fake")
    assert [op for op, _, _ in env.outbox.enqueued] == ["add_comment", "upsert_ledger", "set_labels"]
    assert env.outbox.comment_titles() == ["run started"]
    (labels,) = env.outbox.of("set_labels")
    assert labels["add"] == ["harness:running"]
    assert "harness:running" not in labels["remove"]
    assert {"harness:done", "harness:failed", "harness:needs-you", "harness:blocked"} <= set(labels["remove"])
    (ledger,) = env.outbox_rows("upsert_ledger")
    assert ledger["coalesce_key"] == f"ledger:{REPO}#7" and ledger["status"] == "pending"
    body = ledger["payload"]["body"]
    assert is_ledger(body) and run_id in body and "| Status | running |" in body
    snap = parse_ledger(body)
    assert snap["v"] == 1 and snap["run"]["id"] == run_id and snap["cursor"] == [f"{run_id}/a/1"]
    # Nothing is sent directly: the outbox was never drained.
    assert env.store.query("SELECT COUNT(*) AS n FROM outbox WHERE status != 'pending'")[0]["n"] == 0


async def test_happy_path_mirrors_only_meaningful_transitions(env):
    env.adapters.register(CountingAdapter.happy_path())
    cfg = {REPO: RepoConfig(control_issue=3, default_agent="fake")}
    runner = env.runner(StubAgentExecutor(), repo_configs=cfg)
    run_id = runner.start_run(REPO, make_issue(), agent_playbook())
    run = await drive(runner, env, run_id)
    assert run.status == "done"

    # Event comments: never on plain polls or waiting transitions.
    assert env.outbox.comment_titles() == ["run started", "plan ready", "PR ready", "done"]
    # Labels only when the run status changed.
    assert [p["add"] for p in env.outbox.of("set_labels")] == [["harness:running"], ["harness:done"]]
    # The ledger is rewritten per transition that changed something, never per poll, and stays one row.
    assert len(env.outbox.of("upsert_ledger")) == 5
    assert len(env.outbox_rows("upsert_ledger")) == 1
    final = parse_ledger(env.outbox_rows("upsert_ledger")[0]["payload"]["body"])
    assert final["run"]["status"] == "done" and final["pr"]["number"] == 101
    assert final["sessions"]["main"]["agent"] == "fake"
    assert sorted(s["id"].removeprefix(run_id + "/") for s in final["steps"]) == ["implement/1", "plan/1"]

    # Control table row: one coalesced edit to the control issue.
    rows = env.outbox_rows("edit_issue_body")
    assert len(rows) == 1 and rows[0]["coalesce_key"] == f"control:{REPO}#7"
    assert rows[0]["payload"]["issue"] == 3
    assert rows[0]["payload"]["row"] == {
        "issue": 7,
        "title": "Fix it",
        "status": "done",
        "agent": "fake",
        "run_id": run_id,
        "pr": "#101",
        "note": None,
    }
    # The PR is in the run context and the ledger summary.
    assert env.run(run_id).context["pr"] == {
        "number": 101,
        "url": "https://github.com/example/repo/pull/101",
        "head_sha": "a1",
    }
    assert "| PR | #101 |" in env.outbox_rows("upsert_ledger")[0]["payload"]["body"]


async def test_no_control_row_without_control_issue_and_no_outbox_at_all(env):
    runner = env.runner(Scripted({"a": [Done("pass")]}))
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.outbox.of("edit_issue_body") == []

    bare = Runner(
        env.store, None, env.runner(Scripted({"a": [Done("pass")]})).executors, env.adapters, clock=env.clock
    )
    run_id = bare.start_run(REPO, make_issue(9), pb)
    await bare.tick()
    assert env.run(run_id).status == "done"


async def test_control_status_shows_review_and_blocked(env):
    pb = make_playbook(
        {"rev": {"kind": "review", "target": "pr", "checklist": ["x"]}}, {"rev.approve": "@done"}
    )
    ex = Scripted({"rev": [Waiting(poll_after=60), Done("approve", {"summary": "lgtm"})]})
    runner = env.runner(ex, repo_configs={REPO: RepoConfig(control_issue=3)})
    run_id = runner.start_run(REPO, make_issue(), pb)
    assert env.outbox.of("edit_issue_body")[-1]["row"].status == "review"
    await runner.tick()
    assert env.outbox.of("edit_issue_body")[-1]["row"].status == "review"
    runner.pause(run_id)
    assert env.outbox.of("edit_issue_body")[-1]["row"].status == "blocked"
    runner.resume(run_id)
    env.clock.advance(61)
    await runner.tick()
    assert env.outbox.of("edit_issue_body")[-1]["row"].status == "done"
    assert env.outbox.comment_titles() == ["run started", "review approve", "done"]
    review_comment = env.outbox.of("add_comment")[1]["body"]
    assert "- lgtm" in review_comment


async def test_failed_run_comment_carries_the_reason(env):
    pb = make_playbook({"a": CHECK}, {"a.fail": "@failed"})
    runner = env.runner(Scripted({"a": [Done("fail", {"error": "tests red"})]}))
    runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    body = env.outbox.of("add_comment")[-1]["body"]
    assert " failed.**" in body and "- a.fail: tests red" in body
    assert env.outbox.of("set_labels")[-1]["add"] == ["harness:failed"]


async def test_waiting_polls_do_not_touch_the_outbox(env):
    pb = make_playbook({"g": GATE}, {"g.approved": "@done"})
    ex = Scripted({"g": [Waiting(ref={"id": 1}, poll_after=0)] * 4 + [Done("approved")]})
    runner = env.runner(ex)
    runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    before = len(env.outbox.enqueued)
    for _ in range(3):
        env.clock.advance(1)
        await runner.tick()
    assert len(env.outbox.enqueued) == before


async def test_oversized_ledger_falls_back_to_smaller_output_caps(env):
    rng = random.Random(1)
    blobs = {i: "".join(rng.choice("0123456789abcdef") for _ in range(7000)) for i in range(1, 31)}
    pb = make_playbook({"work": {**CHECK, "max_visits": 40}}, {"work.fail": "work", "work.pass": "@done"})

    def work(step, ctx):
        return Done("pass" if step.visit == 30 else "fail", {"log": blobs[step.visit]})

    runner = env.runner(Scripted({"work": [work]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    for _ in range(30):
        await runner.tick()
    assert env.run(run_id).status == "done"
    body = env.outbox_rows("upsert_ledger")[0]["payload"]["body"]
    assert len(body) <= 65_000
    snap = parse_ledger(body)
    assert len(snap["steps"]) == 30
    assert all(s["output"] == {"truncated": True, "keys": ["log"]} for s in snap["steps"])
    assert env.events("ledger_too_large") == []
    # The full output stays in the local store.
    assert env.steps(run_id)[0].output["log"] == blobs[1]
