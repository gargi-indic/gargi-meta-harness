import asyncio

import pytest

from harness.adapters.base import StartRequest
from harness.config import RepoConfig
from harness.engine.executors.base import Done, ExecutorRegistry, Waiting
from harness.engine.runner import JoinExecutor, RunExists, Runner
from harness.playbook.models import JoinNode
from harness.store.repo import get_run, upsert_issue
from tests.engine.support import (
    REPO,
    Scripted,
    check,
    drive,
    events,
    flush_outbox,
    make_issue,
    make_playbook,
    outbox_rows,
    pending_outbox,
    step_map,
)

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

LINEAR = make_playbook(
    {"a": check(), "b": check(), "c": check()},
    {"a.pass": "b", "b.pass": "c", "c.pass": "@done", "*.fail": "@failed"},
)


def statuses(store, run_id):
    return {sid: (s.status, s.outcome) for sid, s in step_map(store, run_id).items()}


async def test_start_run_creates_run_and_entry_step(store, make_runner):
    runner = make_runner(Scripted({}))
    run_id = runner.start_run(REPO, make_issue(7), LINEAR, playbook_sha="abc", inputs={"x": 1}, agent="codex")

    assert run_id == f"{REPO}#7@1"
    run = get_run(store, run_id)
    assert run.status == "running"
    assert run.playbook_id == "mini" and run.playbook_sha == "abc"
    assert run.context["inputs"] == {"x": 1} and run.context["agent"] == "codex"
    assert run.context["sessions"] == {} and run.context["nodes"] == {}
    assert run.context["visits"] == {"a": 1}
    steps = step_map(store, run_id)
    assert list(steps) == [f"{run_id}/a/1"]
    assert steps[f"{run_id}/a/1"].status == "pending"
    assert [e["kind"] for e in events(store)] == ["run_started"]


async def test_start_run_refuses_second_active_run_then_numbers_next_run(store, make_runner, clock):
    runner = make_runner(Scripted({"a": [Done("pass")], "b": [Done("pass")], "c": [Done("pass")]}))
    first = runner.start_run(REPO, make_issue(7), LINEAR)
    with pytest.raises(RunExists):
        runner.start_run(REPO, make_issue(7), LINEAR)
    # Other issues are independent.
    assert runner.start_run(REPO, make_issue(8), LINEAR) == f"{REPO}#8@1"

    await drive(runner, first, clock)
    assert get_run(store, first).status == "done"
    assert runner.start_run(REPO, make_issue(7), LINEAR) == f"{REPO}#7@2"


async def test_explicit_route_beats_wildcard_and_terminal_done(store, make_runner, clock):
    pb = make_playbook(
        {"a": check(), "b": check(), "c": check()},
        {"a.fail": "b", "*.fail": "@failed", "b.pass": "c", "c.pass": "@done"},
    )
    ex = Scripted({"a": [Done("fail")], "b": [Done("pass")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "done"
    assert ex.calls == [f"{run_id}/a/1", f"{run_id}/b/1", f"{run_id}/c/1"]


async def test_wildcard_route_to_failed_terminal(store, make_runner, clock):
    ex = Scripted({"a": [Done("pass")], "b": [Done("fail")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)

    run = get_run(store, run_id)
    assert run.status == "failed"
    assert f"{run_id}/c/1" not in step_map(store, run_id)
    assert [e["kind"] for e in events(store, "run_failed")] == ["run_failed"]


async def test_no_route_fails_the_run_with_event_and_comment(store, make_runner, clock):
    ex = Scripted({"a": [Done("pass")], "b": [Done("weird")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "failed"
    (event,) = events(store, "no_route")
    assert event["data"] == {"node": "b", "outcome": "weird"}
    comments = [r["body"] for r in outbox_rows(store, "add_comment")]
    assert any("no route" in c for c in comments)
    # The failure is explained once, not twice.
    assert sum("failed" in c.splitlines()[0] for c in comments) == 0


async def test_exhausted_port_when_max_visits_is_reached(store, make_runner, clock):
    pb = make_playbook(
        {"work": check(), "fix": check(max_visits=2)},
        {"work.pass": "@done", "work.fail": "fix", "fix.fail": "work", "fix.exhausted": "@failed"},
    )
    ex = Scripted({"work": [Done("fail")], "fix": [Done("fail")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    run = get_run(store, run_id)
    assert run.status == "failed"
    steps = step_map(store, run_id)
    assert f"{run_id}/fix/2" in steps and f"{run_id}/fix/3" not in steps
    assert run.context["visits"]["fix"] == 2
    assert events(store, "exhausted")[0]["data"]["node"] == "fix"


async def test_exhausted_can_route_to_another_node(store, make_runner, clock):
    pb = make_playbook(
        {"work": check(), "fix": check(max_visits=1), "human": check()},
        {
            "work.pass": "@done",
            "work.fail": "fix",
            "fix.fail": "work",
            "fix.exhausted": "human",
            "human.pass": "@done",
        },
    )
    ex = Scripted({"work": [Done("fail")], "fix": [Done("fail")], "human": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "done"
    assert f"{run_id}/human/1" in step_map(store, run_id)


async def test_exhausted_without_edge_is_no_route(store, make_runner, clock):
    pb = make_playbook(
        {"work": check(), "fix": check(max_visits=1)},
        {"work.fail": "fix", "fix.fail": "work"},
    )
    runner = make_runner(Scripted({"work": [Done("fail")], "fix": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "failed"
    assert events(store, "no_route")[0]["data"] == {"node": "fix", "outcome": "exhausted"}


async def test_exhausted_ring_stops_with_routing_loop(store, make_runner, clock):
    pb = make_playbook(
        {"s": check(), "a": check(max_visits=1), "b": check(max_visits=1)},
        {
            "s.pass": "a",
            "a.pass": "b",
            "b.pass": "a",
            "a.exhausted": "b",
            "b.exhausted": "a",
        },
    )
    ex = Scripted({"s": [Done("pass")], "a": [Done("pass")], "b": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "failed"
    assert len(events(store, "routing_loop")) == 1


async def test_executor_exception_becomes_failed_outcome_with_redacted_error(
    store, make_runner, clock, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue1234")
    pb = make_playbook({"a": check(), "b": check()}, {"a.failed": "b", "b.pass": "@done"})
    ex = Scripted(
        {"a": [RuntimeError("boom ghp_supersecretvalue1234")], "b": [Done("pass")]},
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    steps = step_map(store, run_id)
    a = steps[f"{run_id}/a/1"]
    assert a.status == "done" and a.outcome == "failed"
    assert a.output["error"] == "RuntimeError: boom [redacted]"
    assert "correction" in a.output
    (event,) = events(store, "executor_error")
    assert "ghp_" not in str(event)
    # The failed outcome is routed like any other, and b saw the error as `prev`.
    assert get_run(store, run_id).status == "done"
    assert ex.templates[f"{run_id}/b/1"].prev["error"] == a.output["error"]


async def test_unregistered_node_kind_fails_the_step_not_the_tick(store, make_runner, clock):
    runner = make_runner(None)  # no executors at all
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    other = runner.start_run(REPO, make_issue(9), LINEAR)
    assert await runner.tick() == 2
    assert get_run(store, run_id).status == "failed"
    assert get_run(store, other).status == "failed"
    assert "no executor registered" in events(store, "executor_error")[0]["data"]["error"]


async def test_prev_is_the_output_of_the_step_that_routed_in(store, make_runner, clock):
    ex = Scripted(
        {
            "a": [Done("pass", {"correction": "fix the thing", "n": 1})],
            "b": [Done("pass", {"m": 2})],
            "c": [Done("pass")],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR, inputs={"k": "v"})
    await drive(runner, run_id, clock)

    t = ex.templates
    assert t[f"{run_id}/a/1"].prev is None
    assert t[f"{run_id}/b/1"].prev == {"correction": "fix the thing", "n": 1}
    assert t[f"{run_id}/c/1"].prev == {"m": 2}
    assert t[f"{run_id}/c/1"].nodes["a"] == {"correction": "fix the thing", "n": 1}
    assert t[f"{run_id}/c/1"].inputs == {"k": "v"}
    assert t[f"{run_id}/c/1"].run.visits == {"a": 1, "b": 1, "c": 1}
    # Consumed `prev` entries are dropped from the context.
    assert get_run(store, run_id).context["prev_for"] == {}


async def test_exhausted_hop_keeps_prev_of_the_routing_step(store, make_runner, clock):
    pb = make_playbook(
        {"work": check(), "fix": check(max_visits=1), "human": check()},
        {"work.fail": "fix", "fix.fail": "work", "fix.exhausted": "human", "human.pass": "@done"},
    )
    ex = Scripted(
        {"work": [Done("fail", {"why": "w"})], "fix": [Done("fail", {"why": "f"})], "human": [Done("pass")]}
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)
    assert ex.templates[f"{run_id}/human/1"].prev == {"why": "w"}


async def test_agent_resolution_and_template_context_without_cached_issue(store, make_runner, clock):
    seen = {}

    class Probe:
        async def advance(self, step, node, ctx):
            seen[step.node_id] = ctx.template
            return Done("pass")

    pb = make_playbook({"a": check(), "b": check()}, {"a.pass": "b", "b.pass": "@done"})

    # Explicit agent beats the label and the repo default.
    runner = make_runner(Probe(), repo_configs={REPO: RepoConfig(default_agent="claude")})
    run_id = runner.start_run(REPO, make_issue(1, labels=["agent:codex"]), pb, agent="gemini")
    await drive(runner, run_id, clock)
    assert seen["a"].issue.agent == "gemini"

    # Issue not cached: default agent, empty title.
    run2 = runner.start_run(REPO, make_issue(2), pb)
    await drive(runner, run2, clock)
    assert seen["a"].issue.agent == "claude" and seen["a"].issue.title == "" and seen["a"].issue.number == 2
    assert seen["a"].issue.playbook == "mini"

    # Cached issue: agent label wins over the default.
    upsert_issue(store, make_issue(3, labels=["bug", "agent:codex"], title="Cached"))
    run3 = runner.start_run(REPO, make_issue(3, labels=["bug", "agent:codex"]), pb)
    await drive(runner, run3, clock)
    assert seen["a"].issue.agent == "codex" and seen["a"].issue.title == "Cached"

    # No config for the repo at all: RepoConfig() defaults.
    bare = make_runner(Probe())
    run4 = bare.start_run(REPO, make_issue(4), pb)
    await drive(bare, run4, clock)
    assert seen["a"].issue.agent == "jules"


async def test_pr_output_lands_in_run_context_and_template(store, make_runner, clock):
    pr = {"number": 101, "url": "https://example/pull/101", "head_sha": "a1", "extra": "ignored"}
    ex = Scripted({"a": [Done("pass", {"pr": pr})], "b": [Done("pass")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).context["pr"]["number"] == 101
    assert ex.templates[f"{run_id}/a/1"].pr is None
    assert ex.templates[f"{run_id}/b/1"].pr.number == 101
    assert ex.templates[f"{run_id}/b/1"].pr.head_sha == "a1"


async def test_tick_counts_advanced_steps_and_respects_poll_after(store, make_runner, clock):
    ex = Scripted({"a": [Waiting(poll_after=120), Done("pass")], "b": [Done("pass")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)

    assert await runner.tick() == 1  # a: pending -> waiting
    step = step_map(store, run_id)[f"{run_id}/a/1"]
    assert step.status == "waiting" and step.poll_after == "2027-01-15T08:02:00Z"
    assert await runner.tick() == 0  # not due yet
    clock.t += 119
    assert await runner.tick() == 0
    clock.t += 1
    assert await runner.tick() == 1  # due: a done, b created
    assert step_map(store, run_id)[f"{run_id}/b/1"].status == "pending"


async def test_waiting_keeps_ref_and_output_when_none_is_given(store, make_runner, clock):
    ex = Scripted(
        {
            "a": [
                Waiting(ref={"id": "s1"}, poll_after=0, output={"note": "x"}),
                Waiting(poll_after=0),
                Done("pass"),
            ],
            "b": [Done("pass")],
            "c": [Done("pass")],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.tick()
    clock.t += 1
    await runner.tick()
    step = step_map(store, run_id)[f"{run_id}/a/1"]
    assert step.ref == {"id": "s1"} and step.output == {"note": "x"}


# ---------------------------------------------------------------- parallel and join

FORK = make_playbook(
    {
        "fork": {"kind": "parallel", "branches": ["a", "b"]},
        "a": check(),
        "b": check(),
        "join": {"kind": "join", "mode": "all", "inputs": ["a", "b"]},
    },
    {"a.pass": "join", "b.pass": "join", "join.done": "@done", "join.failed": "@failed"},
)


async def test_parallel_creates_one_step_per_branch_and_join_all_waits_for_both(store, make_runner, clock):
    ex = Scripted(
        {
            "a": [Done("pass", {"from": "a"})],
            "b": [Waiting(poll_after=0), Done("pass", {"from": "b"})],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), FORK)
    assert await runner.tick() == 1  # the parallel step is handled by the runner
    steps = step_map(store, run_id)
    assert steps[f"{run_id}/fork/1"].status == "done"
    assert steps[f"{run_id}/a/1"].status == "pending" and steps[f"{run_id}/b/1"].status == "pending"

    await drive(runner, run_id, clock)
    steps = step_map(store, run_id)
    assert get_run(store, run_id).status == "done"
    joins = [s for s in steps.values() if s.node_id == "join"]
    assert len(joins) == 1 and joins[0].outcome == "done"
    assert joins[0].output == {"inputs": {"a": {"from": "a"}, "b": {"from": "b"}}}


async def test_join_all_fails_when_an_input_failed(store, make_runner, clock):
    pb = make_playbook(
        {
            "fork": {"kind": "parallel", "branches": ["a", "b"]},
            "a": check(),
            "b": check(),
            "join": {"kind": "join", "mode": "all"},
        },
        {"a.pass": "join", "b.failed": "join", "join.done": "@done", "join.failed": "@failed"},
    )
    ex = Scripted({"a": [Done("pass")], "b": [Done("failed", {"error": "x"})]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    assert get_run(store, run_id).status == "failed"
    join = step_map(store, run_id)[f"{run_id}/join/1"]
    assert join.outcome == "failed" and join.output["failed"] == ["b"]


async def test_join_any_completes_on_first_input_and_closes_the_rest(store, make_runner, clock):
    pb = make_playbook(
        {
            "fork": {"kind": "parallel", "branches": ["a", "b"]},
            "a": check(),
            "b": check(),
            "join": {"kind": "join", "mode": "any", "inputs": ["a", "b"]},
        },
        {"a.pass": "join", "b.pass": "join", "join.done": "@done", "join.failed": "@failed"},
    )
    ex = Scripted({"a": [Waiting(poll_after=0)], "b": [Done("pass", {"from": "b"})]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    steps = step_map(store, run_id)
    assert get_run(store, run_id).status == "done"
    assert steps[f"{run_id}/join/1"].output == {"inputs": {"b": {"from": "b"}}}
    assert steps[f"{run_id}/a/1"].status == "cancelled"


async def test_join_select_is_not_implemented(store, make_runner, clock):
    pb = make_playbook(
        {
            "fork": {"kind": "parallel", "branches": ["a"]},
            "a": check(),
            "join": {"kind": "join", "mode": "select", "inputs": ["a"]},
        },
        {"a.pass": "join", "join.done": "@done", "*.failed": "@failed"},
    )
    runner = make_runner(Scripted({"a": [Done("pass")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)

    # The error is not swallowed: the join step fails and an event records it.
    assert get_run(store, run_id).status == "failed"
    assert "NotImplementedError" in events(store, "executor_error")[0]["data"]["error"]

    step = step_map(store, run_id)[f"{run_id}/join/1"]
    with pytest.raises(NotImplementedError):
        await JoinExecutor().advance(step, pb.nodes["join"], None)  # type: ignore[arg-type]
    assert isinstance(pb.nodes["join"], JoinNode)


async def test_join_registered_by_default_but_not_overriding_a_given_one(make_runner, store, adapters):
    assert "join" in make_runner(Scripted({})).executors.kinds()

    registry = ExecutorRegistry()
    custom = Scripted({})
    registry.register("join", custom)
    Runner(store, None, registry, adapters)
    assert registry.get("join") is custom


# ---------------------------------------------------------------- run controls


async def test_pause_stops_ticks_and_resume_continues(store, make_runner, clock):
    ex = Scripted({"a": [Done("pass")], "b": [Done("pass")], "c": [Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    runner.pause(run_id)
    assert get_run(store, run_id).status == "paused"
    assert await runner.tick() == 0 and ex.calls == []
    runner.pause(run_id)  # idempotent

    runner.resume(run_id)
    assert get_run(store, run_id).status == "running"
    await drive(runner, run_id, clock)
    assert get_run(store, run_id).status == "done"
    assert [e["kind"] for e in events(store) if e["kind"] in ("paused", "resumed")] == ["paused", "resumed"]


async def test_resume_to_resolves_needs_you_steps_and_ignores_max_visits(store, make_runner, clock):
    pb = make_playbook(
        {"a": check(max_visits=1), "esc": {"kind": "escalate"}, "b": check(max_visits=1)},
        {"a.fail": "esc", "esc.resolved": "b", "b.pass": "@done"},
    )
    ex = Scripted(
        {
            "a": [Done("fail", {"why": "a failed"})],
            "esc": [Waiting(poll_after=0, output={"needs_you": True, "reason": "stuck"})],
            "b": [Done("pass")],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    clock.t += 1
    await runner.tick()
    assert get_run(store, run_id).status == "needs-you"

    # Send the run back to `a`, which is already at max_visits: a human override.
    runner.resume(run_id, to="a")
    run = get_run(store, run_id)
    assert run.status == "running" and run.context["visits"]["a"] == 2
    steps = step_map(store, run_id)
    esc = steps[f"{run_id}/esc/1"]
    assert esc.status == "done" and esc.outcome == "resolved"
    assert steps[f"{run_id}/a/2"].status == "pending"
    assert run.context["prev_for"][f"{run_id}/a/2"] == {"needs_you": True, "reason": "stuck"}
    assert events(store, "resumed")[-1]["data"] == {"to": "a"}

    with pytest.raises(ValueError, match="unknown node"):
        runner.resume(run_id, to="nope")


async def test_resume_from_paused_returns_to_needs_you_if_a_step_still_asks(store, make_runner, clock):
    ex = Scripted({"a": [Waiting(poll_after=0, output={"needs_you": True})]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.tick()
    assert get_run(store, run_id).status == "needs-you"
    runner.pause(run_id)
    runner.resume(run_id)
    assert get_run(store, run_id).status == "needs-you"


async def test_cannot_pause_or_resume_a_finished_run(store, make_runner, clock):
    runner = make_runner(Scripted({"a": [Done("pass")], "b": [Done("pass")], "c": [Done("pass")]}))
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await drive(runner, run_id, clock)
    with pytest.raises(ValueError):
        runner.pause(run_id)
    with pytest.raises(ValueError):
        runner.resume(run_id)
    with pytest.raises(KeyError):
        runner.pause("nope")


async def test_cancel_calls_adapter_cancel_for_waiting_agent_steps(store, make_runner, adapter, clock):
    ref = await adapter.start(
        StartRequest(
            repo=REPO,
            base_branch="main",
            branch="b",
            issue=7,
            title="t",
            brief="b",
            mode="plan_first",
            idempotency_key="k",
        )
    )
    ex = Scripted({"a": [Waiting(ref={**ref.model_dump(), "approved": True}, poll_after=0)]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.tick()

    await runner.cancel(run_id)
    assert ("cancel", {"session": ref.id}) in adapter.calls
    assert get_run(store, run_id).status == "cancelled"
    assert step_map(store, run_id)[f"{run_id}/a/1"].status == "cancelled"
    assert await runner.tick() == 0
    await runner.cancel(run_id)  # idempotent
    assert adapter.calls.count(("cancel", {"session": ref.id})) == 1


async def test_cancel_logs_adapter_errors_and_still_cancels(store, make_runner, clock):
    ex = Scripted({"a": [Waiting(ref={"agent": "fake", "id": "no-such-session"}, poll_after=0)]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await runner.tick()
    await runner.cancel(run_id)

    assert get_run(store, run_id).status == "cancelled"
    assert "KeyError" in events(store, "cancel_error")[0]["data"]["error"]


async def test_retry_node_inserts_a_new_visit_and_revives_a_failed_run(store, make_runner, clock):
    pb = make_playbook(
        {"a": check(), "b": check(max_visits=2)},
        {"a.pass": "b", "b.pass": "@done", "b.fail": "@failed"},
    )
    ex = Scripted({"a": [Done("pass")], "b": [Done("fail"), Done("pass")]})
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)
    assert get_run(store, run_id).status == "failed"

    new_id = runner.retry_node(run_id, "b")
    assert new_id == f"{run_id}/b/2"
    assert get_run(store, run_id).status == "running"
    await drive(runner, run_id, clock)
    assert get_run(store, run_id).status == "done"

    with pytest.raises(ValueError):  # done runs cannot be retried
        runner.retry_node(run_id, "a")


async def test_retry_node_obeys_max_visits_and_validates_node(store, make_runner, clock):
    pb = make_playbook({"a": check(max_visits=1)}, {"a.pass": "@done", "a.fail": "@failed"})
    runner = make_runner(Scripted({"a": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await drive(runner, run_id, clock)
    with pytest.raises(ValueError, match="visits"):
        runner.retry_node(run_id, "a")
    with pytest.raises(ValueError, match="unknown node"):
        runner.retry_node(run_id, "zzz")
    assert get_run(store, run_id).status == "failed"


# ---------------------------------------------------------------- needs-you and run_forever


async def test_needs_you_status_comment_once_and_revert(store, make_runner, clock):
    needs = {"needs_you": True, "reason": "approve the plan", "links": ["https://example/plan"]}
    ex = Scripted(
        {
            "a": [
                Waiting(poll_after=0, output=needs),
                Waiting(poll_after=0, output=needs),
                Waiting(poll_after=0, output={"needs_you": False}),
                Done("pass"),
            ],
            "b": [Done("pass")],
            "c": [Done("pass")],
        }
    )
    runner = make_runner(ex)
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    flush_outbox(store)

    await runner.tick()
    assert get_run(store, run_id).status == "needs-you"
    comments = [r["body"] for r in pending_outbox(store, "add_comment")]
    assert len(comments) == 1
    assert "#7 needs you." in comments[0]
    assert "- approve the plan" in comments[0] and "- https://example/plan" in comments[0]
    assert pending_outbox(store, "set_labels")[0]["add"] == ["harness:needs-you"]
    flush_outbox(store)

    clock.t += 1
    await runner.tick()  # same output again: nothing to say
    assert pending_outbox(store) == []

    clock.t += 1
    await runner.tick()  # no longer asks
    assert get_run(store, run_id).status == "running"
    assert pending_outbox(store, "set_labels")[0]["add"] == ["harness:running"]
    assert pending_outbox(store, "add_comment") == []


async def test_run_forever_ticks_until_stopped(store, make_runner, clock):
    stop = asyncio.Event()

    class StopAtEnd:
        async def advance(self, step, node, ctx):
            if step.node_id == "c":
                stop.set()
            return Done("pass")

    runner = make_runner(StopAtEnd())
    run_id = runner.start_run(REPO, make_issue(), LINEAR)
    await asyncio.wait_for(runner.run_forever(stop, interval=0), timeout=5)
    assert get_run(store, run_id).status == "done"
