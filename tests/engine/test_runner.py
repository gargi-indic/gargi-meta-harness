"""Routing, visits, parallel/join, run controls, tick selection and executor failures."""

import asyncio
import json

import pytest

from harness.adapters.fake import FakeAdapter
from harness.engine import Done, ExecutorRegistry, RunExists, Waiting
from harness.engine.runner import JoinExecutor
from harness.playbook.templating import render
from tests.engine.helpers import (
    CHECK,
    GATE,
    NOTIFY,
    REPO,
    CountingAdapter,
    Scripted,
    StubAgentExecutor,
    agent_playbook,
    drive,
    make_issue,
    make_playbook,
    registry_for,
)

# --- routing ----------------------------------------------------------------------------------------


async def test_explicit_edge_beats_wildcard(env):
    pb = make_playbook(
        {"a": CHECK, "b": NOTIFY, "c": NOTIFY},
        {"a.pass": "b", "a.fail": "c", "*.fail": "@failed", "b.sent": "@done", "c.sent": "@done"},
    )
    ex = Scripted({"a": [Done("fail")], "b": [Done("sent")], "c": [Done("sent")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    run = await drive(runner, env, run_id)
    assert run.status == "done"
    assert env.step_ids(run_id) == ["a/1", "c/1"]


async def test_wildcard_edge_used_when_no_explicit_edge(env):
    pb = make_playbook({"a": CHECK, "b": NOTIFY}, {"a.pass": "b", "*.fail": "@failed", "b.sent": "@done"})
    ex = Scripted({"a": [Done("fail", {"error": "nope"})], "b": [Done("sent")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    run = await drive(runner, env, run_id)
    assert run.status == "failed"
    assert env.step_ids(run_id) == ["a/1"]


async def test_terminal_done_and_failed(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done", "a.fail": "@failed"})
    ex = Scripted({"a": [Done("pass")]})
    runner = env.runner(ex)
    ok = runner.start_run(REPO, make_issue(1), pb)
    bad = runner.start_run(REPO, make_issue(2), pb)
    ex.script["a"] = [lambda step, ctx: Done("pass" if ctx.run.issue == 1 else "fail")]
    await runner.tick()
    assert env.run(ok).status == "done"
    assert env.run(bad).status == "failed"
    assert env.events("run_failed")[0]["data"]["reason"] == "a.fail"


async def test_no_route_fails_the_run_with_event_and_comment(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    runner = env.runner(Scripted({"a": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "failed"
    (event,) = env.events("no_route")
    assert event["data"] == {"node": "a", "outcome": "fail"}
    assert "no route" in env.outbox.comment_titles()
    assert env.outbox.comment_titles()[-1] == "failed"


async def test_prev_is_the_output_of_the_routing_step(env):
    pb = make_playbook({"a": CHECK, "b": NOTIFY}, {"a.fail": "b", "b.sent": "@done"})
    first = {"correction": "fix the tests", "n": 1}
    seen = {}

    def capture(step, ctx):
        seen["prev"] = ctx.template.prev
        seen["text"] = render("Please: {{ prev.correction }}", ctx.template)
        return Done("sent")

    ex = Scripted({"a": [Done("fail", first)], "b": [capture]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    run = await drive(runner, env, run_id)
    assert run.status == "done"
    assert seen["prev"] == first
    assert seen["text"] == "Please: fix the tests"
    assert run.context["prev_for"][f"{run_id}/b/1"] == first
    assert run.context["nodes"]["a"] == first


# --- visits and exhaustion ----------------------------------------------------------------------------


async def test_exhausted_port_when_max_visits_is_hit(env):
    pb = make_playbook(
        {"work": {**CHECK, "max_visits": 2}, "giveup": NOTIFY},
        {"work.fail": "work", "work.exhausted": "giveup", "giveup.sent": "@done"},
    )
    ex = Scripted({"work": [Done("fail")], "giveup": [Done("sent")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    run = await drive(runner, env, run_id)
    assert run.status == "done"
    assert env.step_ids(run_id) == ["giveup/1", "work/1", "work/2"]
    assert run.context["visits"] == {"work": 2, "giveup": 1}
    assert env.events("exhausted")[0]["data"]["node"] == "work"


async def test_exhausted_without_edge_is_no_route(env):
    pb = make_playbook({"work": {**CHECK, "max_visits": 1}}, {"work.fail": "work"})
    runner = env.runner(Scripted({"work": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, env, run_id)
    assert env.run(run_id).status == "failed"
    assert env.events("no_route")[0]["data"] == {"node": "work", "outcome": "exhausted"}


async def test_routing_loop_is_capped(env):
    pb = make_playbook({"x": {**CHECK, "max_visits": 1}}, {"x.fail": "x", "x.exhausted": "x"})
    runner = env.runner(Scripted({"x": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "failed"
    assert len(env.events("routing_loop")) == 1
    assert env.step_ids(run_id) == ["x/1"]


# --- parallel and join --------------------------------------------------------------------------------


def fork_playbook(mode: str = "all", extra_edges: dict | None = None):
    return make_playbook(
        {
            "fork": {"kind": "parallel", "branches": ["a", "b"]},
            "a": NOTIFY,
            "b": NOTIFY,
            "join": {"kind": "join", "mode": mode},
            "after": NOTIFY,
        },
        {
            "a.sent": "join",
            "b.sent": "join",
            "join.done": "after",
            "join.failed": "@failed",
            "after.sent": "@done",
            **(extra_edges or {}),
        },
    )


async def test_parallel_then_join_all(env):
    ex = Scripted(
        {
            "a": [Done("sent", {"v": "a"})],
            "b": [Waiting(poll_after=0), Done("sent", {"v": "b"})],
            "after": [Done("sent")],
        }
    )
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), fork_playbook("all"), inputs={"x": 1})
    ex.script["fork"] = []  # the runner handles parallel itself; must never be called
    run = await drive(runner, env, run_id)
    assert run.status == "done"
    assert env.step_ids(run_id) == ["a/1", "after/1", "b/1", "fork/1", "join/1"]
    join = next(s for s in env.steps(run_id) if s.node_id == "join")
    assert join.output == {"inputs": {"a": {"v": "a"}, "b": {"v": "b"}}}
    assert ex.step_calls("fork") == 0


async def test_join_all_waits_for_every_input(env):
    ex = Scripted(
        {"a": [Done("sent")], "b": [Waiting(poll_after=0)] * 3 + [Done("sent")], "after": [Done("sent")]}
    )
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), fork_playbook("all"))
    await runner.tick()  # fork
    await runner.tick()  # a done, b waiting
    await runner.tick()
    assert [s.status for s in env.steps(run_id) if s.node_id == "join"] == ["waiting"]
    run = await drive(runner, env, run_id)
    assert run.status == "done"
    assert "join/2" not in env.step_ids(run_id)


async def test_join_all_fails_when_an_input_failed(env):
    pb = fork_playbook("all", {"a.failed": "join"})
    ex = Scripted({"a": [Done("failed", {"error": "x"})], "b": [Done("sent")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    run = await drive(runner, env, run_id)
    assert run.status == "failed"
    join = next(s for s in env.steps(run_id) if s.node_id == "join")
    assert join.outcome == "failed"
    assert join.output["failed"] == ["a"]


async def test_join_any_fires_on_first_input_and_ignores_the_late_one(env):
    ex = Scripted(
        {
            "a": [Done("sent", {"v": "a"})],
            "b": [Waiting(poll_after=0), Waiting(poll_after=0), Done("sent", {"v": "b"})],
            "after": [Waiting(poll_after=0)] * 4 + [Done("sent")],
        }
    )
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), fork_playbook("any"))
    run = await drive(runner, env, run_id)
    assert run.status == "done"
    join = next(s for s in env.steps(run_id) if s.node_id == "join")
    assert join.output == {"inputs": {"a": {"v": "a"}}}
    # b finished after the join fired: its arrival must not fire the join (or `after`) a second time.
    b = next(s for s in env.steps(run_id) if s.node_id == "b")
    assert b.status == "done"
    assert env.step_ids(run_id).count("join/1") == 1 and "join/2" not in env.step_ids(run_id)
    assert ex.step_calls("after") == 5


async def test_join_select_is_not_implemented_and_fails_the_step(env):
    pb = make_playbook({"j": {"kind": "join", "mode": "select", "inputs": ["j"]}}, {"j.failed": "@failed"})
    runner = env.runner(Scripted({}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "failed"
    assert "NotImplementedError" in env.events("executor_error")[0]["data"]["error"]


async def test_join_executor_is_registered_by_the_runner_unless_provided(env):
    registry = registry_for(Scripted({}))
    env.runner(executors=registry)
    assert "join" in registry.kinds()
    custom = ExecutorRegistry()
    marker = JoinExecutor()
    custom.register("join", marker)
    env.runner(executors=custom)
    assert custom.get("join") is marker


# --- executor failures --------------------------------------------------------------------------------


async def test_executor_exception_becomes_failed_with_redacted_message(env, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_supersecret_token")
    pb = make_playbook({"a": CHECK}, {"*.failed": "@failed", "a.pass": "@done"})
    runner = env.runner(Scripted({"a": [RuntimeError("boom ghp_supersecret_token")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    run = env.run(run_id)
    assert run.status == "failed"
    (step,) = env.steps(run_id)
    assert step.status == "done" and step.outcome == "failed"
    assert step.output["error"] == "RuntimeError: boom [redacted]"
    assert step.output["correction"].startswith("Executor error:")
    (event,) = env.events("executor_error")
    assert event["data"]["error"] == "RuntimeError: boom [redacted]"
    everything = json.dumps(env.store.query("SELECT payload_json FROM outbox")) + json.dumps(env.events())
    assert "ghp_supersecret_token" not in everything
    assert "executor error" in env.outbox.comment_titles()


async def test_executor_exception_does_not_abort_the_tick(env):
    pb = make_playbook({"a": CHECK}, {"*.failed": "@failed", "a.pass": "@done"})

    def maybe_boom(step, ctx):
        if ctx.run.issue == 1:
            raise ValueError("first one explodes")
        return Done("pass")

    runner = env.runner(Scripted({"a": [maybe_boom]}))
    r1 = runner.start_run(REPO, make_issue(1), pb)
    r2 = runner.start_run(REPO, make_issue(2), pb)
    assert await runner.tick() == 2
    assert env.run(r1).status == "failed"
    assert env.run(r2).status == "done"


async def test_missing_executor_and_bad_return_value_fail_the_step(env):
    pb = make_playbook({"a": {"kind": "merge"}, "b": NOTIFY}, {"*.failed": "@failed"})
    runner = env.runner(executors=registry_for(Scripted({}), kinds=("notify",)))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "failed"
    assert "no executor registered for kind 'merge'" in env.events("executor_error")[0]["data"]["error"]

    runner2 = env.runner(Scripted({"a": [None]}), executors=None)
    run2 = runner2.start_run(REPO, make_issue(2), make_playbook({"a": CHECK}, {"*.failed": "@failed"}))
    await runner2.tick()
    assert env.run(run2).status == "failed"
    assert "TypeError" in env.events("executor_error")[-1]["data"]["error"]


# --- tick selection -----------------------------------------------------------------------------------


async def test_waiting_step_is_polled_only_after_poll_after(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    ex = Scripted({"a": [Waiting(ref={"n": 1}, poll_after=60), Done("pass")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    assert await runner.tick() == 1
    step = env.steps(run_id)[0]
    assert step.status == "waiting" and step.ref == {"n": 1}
    assert step.poll_after == "2027-01-15T08:01:00Z"  # fake clock + 60s, formatted like Store.now()
    assert await runner.tick() == 0
    env.clock.advance(59)
    assert await runner.tick() == 0
    env.clock.advance(1)
    assert await runner.tick() == 1
    assert env.run(run_id).status == "done"


async def test_ref_is_kept_when_a_later_waiting_has_none(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    ex = Scripted({"a": [Waiting(ref={"id": "s1"}, poll_after=0), Waiting(poll_after=0), Done("pass")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    await runner.tick()
    assert env.steps(run_id)[0].ref == {"id": "s1"}


async def test_template_context_agent_resolution_and_issue_fields(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    ex = Scripted({"a": [Done("pass")]})
    runner = env.runner(ex)

    r_explicit = runner.start_run(REPO, make_issue(1, ("agent:claude",)), pb, agent="codex")
    r_label = runner.start_run(REPO, make_issue(2, ("bug", "agent:claude")), pb)
    r_default = runner.start_run(REPO, make_issue(3), pb)
    await runner.tick()
    agents = {rid: ex.contexts[f"{rid}/a/1"].template.issue for rid in (r_explicit, r_label, r_default)}
    assert agents[r_explicit].agent == "codex"
    assert agents[r_label].agent == "claude"
    assert agents[r_default].agent == "jules"
    assert agents[r_label].playbook == "mini" and agents[r_label].title == "Fix it"


async def test_runner_works_without_a_cached_issue_and_pr_comes_from_context(env):
    pb = make_playbook({"a": CHECK, "b": NOTIFY}, {"a.pass": "b", "b.sent": "@done"})
    pr = {"number": 5, "url": "https://x/pull/5", "head_sha": "abc", "ignored": True}
    ex = Scripted({"a": [Done("pass", {"pr": pr})], "b": [Done("sent")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    env.store.execute("DELETE FROM issues")
    await runner.tick()
    await runner.tick()
    ctx_a = ex.contexts[f"{run_id}/a/1"].template
    ctx_b = ex.contexts[f"{run_id}/b/1"].template
    assert ctx_a.issue.title == "" and ctx_a.issue.agent == "jules" and ctx_a.pr is None
    assert ctx_b.pr is not None and ctx_b.pr.number == 5 and ctx_b.pr.url == "https://x/pull/5"
    assert env.run(run_id).context["pr"]["number"] == 5


# --- run ids and RunExists ----------------------------------------------------------------------------


async def test_run_exists_until_terminal_then_next_run_number(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    runner = env.runner(Scripted({"a": [Done("pass")]}))
    first = runner.start_run(REPO, make_issue(), pb)
    assert first == "o/r#7@1"
    with pytest.raises(RunExists) as exc:
        runner.start_run(REPO, make_issue(), pb)
    assert exc.value.run_id == first
    assert runner.start_run(REPO, make_issue(8), pb) == "o/r#8@1"
    await runner.tick()
    assert env.run(first).status == "done"
    assert runner.start_run(REPO, make_issue(), pb) == "o/r#7@2"
    assert env.step_ids("o/r#7@2") == ["a/1"]


async def test_start_run_records_first_step_and_context(env):
    pb = make_playbook({"first": CHECK, "second": NOTIFY}, {"first.pass": "second"})
    runner = env.runner()
    run_id = runner.start_run(
        REPO, make_issue(), pb, playbook_sha="deadbeef", inputs={"k": "v"}, agent="fake"
    )
    run = env.run(run_id)
    assert run.status == "running" and run.playbook_id == "mini" and run.playbook_sha == "deadbeef"
    assert {k: run.context[k] for k in ("inputs", "agent", "visits", "sessions", "nodes")} == {
        "inputs": {"k": "v"},
        "agent": "fake",
        "visits": {"first": 1},
        "sessions": {},
        "nodes": {},
    }
    (step,) = env.steps(run_id)
    assert (step.id, step.status, step.visit) == (f"{run_id}/first/1", "pending", 1)
    assert env.events("run_started")[0]["run_id"] == run_id


# --- run controls -------------------------------------------------------------------------------------


async def test_pause_blocks_ticks_and_resume_continues(env):
    pb = make_playbook({"a": CHECK, "b": NOTIFY}, {"a.pass": "b", "b.sent": "@done"})
    ex = Scripted({"a": [Done("pass")], "b": [Done("sent")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    runner.pause(run_id)
    assert env.run(run_id).status == "paused"
    assert await runner.tick() == 0 and ex.calls == []
    assert env.outbox.of("set_labels")[-1]["add"] == ["harness:blocked"]
    runner.pause(run_id)  # idempotent
    runner.resume(run_id)
    assert env.run(run_id).status == "running"
    assert (await drive(runner, env, run_id)).status == "done"
    assert [e["kind"] for e in env.events() if e["kind"] in ("paused", "resumed")] == ["paused", "resumed"]


async def test_resume_to_resolves_needs_you_steps_and_overrides_max_visits(env):
    pb = make_playbook(
        {"fix": {**NOTIFY, "max_visits": 1}, "esc": {"kind": "escalate"}},
        {"fix.sent": "esc", "esc.resolved": "@done"},
    )
    attention = {"needs_you": True, "reason": "stuck", "links": ["https://x/1"]}
    ex = Scripted({"fix": [Done("sent", {"n": 1})], "esc": [Waiting(output=attention, poll_after=60)]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    await runner.tick()
    assert env.run(run_id).status == "needs-you"
    with pytest.raises(ValueError, match="unknown node"):
        runner.resume(run_id, to="nope")
    ex.script["fix"] = [Done("sent")]
    ex.script["esc"] = [Done("resolved")]
    runner.resume(run_id, to="fix")
    run = env.run(run_id)
    assert run.status == "running"
    esc, fix1, fix2 = env.steps(run_id)
    assert (esc.status, esc.outcome) == ("done", "resolved")
    assert fix2.id == f"{run_id}/fix/2" and fix2.status == "pending"  # max_visits=1 overridden by the human
    assert run.context["prev_for"][fix2.id] == attention
    assert run.context["visits"]["fix"] == 2
    assert env.events("resumed")[-1]["data"]["to"] == "fix"
    # The resolved step was not routed (esc.resolved -> @done would have ended the run).
    assert (await drive(runner, env, run_id)).status == "done"
    assert env.step_ids(run_id) == ["esc/1", "esc/2", "fix/1", "fix/2"]


async def test_cancel_cancels_waiting_agent_sessions_via_the_adapter(env):
    adapter = CountingAdapter.happy_path()
    env.adapters.register(adapter)
    runner = env.runner(StubAgentExecutor())
    run_id = runner.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    await runner.tick()
    (step,) = env.steps(run_id)
    assert step.status == "waiting" and step.ref["agent"] == "fake"
    await runner.cancel(run_id)
    assert adapter.count("cancel") == 1
    assert adapter.calls[-1] == ("cancel", {"session": step.ref["id"]})
    assert env.run(run_id).status == "cancelled"
    (step,) = env.steps(run_id)
    assert (step.status, step.outcome) == ("failed", "cancelled")
    assert await runner.tick() == 0
    assert env.outbox.of("set_labels")[-1]["add"] == ["harness:failed"]
    assert env.outbox.comment_titles()[-1] == "cancelled"
    await runner.cancel(run_id)  # no-op on a terminal run
    assert adapter.count("cancel") == 1


async def test_cancel_logs_adapter_errors_and_still_cancels(env):
    class BadCancel(FakeAdapter):
        async def cancel(self, ref):
            raise RuntimeError("remote said no")

    env.adapters.register(BadCancel.happy_path())
    runner = env.runner(StubAgentExecutor())
    run_id = runner.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    await runner.tick()
    await runner.cancel(run_id)
    assert env.run(run_id).status == "cancelled"
    (event,) = env.events("cancel_error")
    assert "RuntimeError: remote said no" in event["data"]["error"]


async def test_retry_node_inserts_a_new_visit_and_obeys_max_visits(env):
    pb = make_playbook({"a": {**CHECK, "max_visits": 2}}, {"a.pass": "@done", "a.fail": "@failed"})
    ex = Scripted({"a": [Done("fail", {"correction": "again"}), Done("fail"), Done("pass")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "failed"
    runner.retry_node(run_id, "a")
    run = env.run(run_id)
    assert run.status == "running"
    assert env.step_ids(run_id) == ["a/1", "a/2"]
    assert env.steps(run_id)[1].status == "pending" and run.context["visits"]["a"] == 2
    await runner.tick()
    assert env.run(run_id).status == "failed"
    with pytest.raises(ValueError, match="max_visits"):
        runner.retry_node(run_id, "a")
    assert env.step_ids(run_id) == ["a/1", "a/2"]
    with pytest.raises(ValueError, match="unknown node"):
        runner.retry_node(run_id, "zzz")


async def test_retry_node_refuses_an_active_node_and_a_finished_run(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    runner = env.runner(Scripted({"a": [Done("pass")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    with pytest.raises(ValueError, match="active"):
        runner.retry_node(run_id, "a")
    await runner.tick()
    with pytest.raises(ValueError, match="done"):
        runner.retry_node(run_id, "a")


# --- needs-you ----------------------------------------------------------------------------------------


async def test_needs_you_status_comment_once_and_revert(env):
    pb = make_playbook({"g": GATE}, {"g.approved": "@done"})
    attention = {"needs_you": True, "reason": "approve the plan", "links": ["https://x/plan"]}
    ex = Scripted(
        {
            "g": [
                Waiting(output=attention, poll_after=60),
                Waiting(output=attention, poll_after=60),
                Waiting(output=None, poll_after=60),
                Done("approved"),
            ]
        }
    )
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "needs-you"
    body = next(p["body"] for p in env.outbox.of("add_comment") if "needs you" in p["body"])
    assert "- approve the plan" in body and "- https://x/plan" in body
    env.clock.advance(60)
    await runner.tick()  # same needs-you step polled again: no new comment
    assert env.outbox.comment_titles().count("needs you") == 1
    assert env.run(run_id).status == "needs-you"
    env.clock.advance(60)
    await runner.tick()  # no longer requests attention
    assert env.run(run_id).status == "running"
    env.clock.advance(60)
    await runner.tick()
    assert env.run(run_id).status == "done"
    adds = [p["add"] for p in env.outbox.of("set_labels")]
    assert adds == [["harness:running"], ["harness:needs-you"], ["harness:running"], ["harness:done"]]


async def test_needs_you_run_is_still_ticked(env):
    pb = make_playbook({"g": GATE}, {"g.approved": "@done"})
    ex = Scripted({"g": [Waiting(output={"needs_you": True}, poll_after=0), Done("approved")]})
    runner = env.runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "needs-you"
    env.clock.advance(1)
    await runner.tick()
    assert env.run(run_id).status == "done"


# --- run_forever --------------------------------------------------------------------------------------


async def test_run_forever_ticks_until_stopped_and_survives_tick_errors(env):
    pb = make_playbook({"a": CHECK}, {"a.pass": "@done"})
    stop = asyncio.Event()

    def finish(step, ctx):
        stop.set()
        return Done("pass")

    runner = env.runner(Scripted({"a": [finish]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    original = runner.tick
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return await original()

    runner.tick = flaky
    await asyncio.wait_for(runner.run_forever(stop, idle_sleep=0.0), timeout=5)
    assert env.run(run_id).status == "done"
    assert env.events("tick_error")[0]["data"]["error"] == "RuntimeError: db hiccup"


def test_unknown_crash_point_is_rejected(env):
    with pytest.raises(ValueError, match="unknown crash point"):
        env.runner(crash_after="nowhere")
