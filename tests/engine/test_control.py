"""Control requests (M2.3): enqueue, apply_pending per kind, exactly-once, and the Runner.tick call site."""

import json

import pytest

from harness.adapters.fake import FakeAdapter
from harness.engine import Done, Waiting
from harness.engine.control import (
    CONTROL_KINDS,
    ControlRequest,
    _request_from_row,
    apply_pending,
    enqueue,
    get_request,
)
from harness.store.repo import get_run
from tests.engine.helpers import (
    CHECK,
    NOTIFY,
    REPO,
    CountingAdapter,
    Env,
    Scripted,
    StubAgentExecutor,
    agent_playbook,
    drive,
    make_issue,
    make_playbook,
)


def two_step_run(env: Env):
    pb = make_playbook({"a": CHECK, "b": NOTIFY}, {"a.pass": "b", "b.sent": "@done"})
    ex = Scripted({"a": [Done("pass")], "b": [Done("sent")]})
    runner = env.runner(ex)
    return runner, ex, runner.start_run(REPO, make_issue(), pb)


async def agent_run(env: Env, *, ticks: int = 1):
    adapter = CountingAdapter.happy_path()
    env.adapters.register(adapter)
    runner = env.runner(StubAgentExecutor())
    run_id = runner.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    for _ in range(ticks):
        await runner.tick()
    return runner, adapter, run_id


def result_of(env: Env, request_id: int) -> ControlRequest:
    req = get_request(env.store, request_id)
    assert req is not None and req.handled_at is not None
    return req


# --- enqueue / get_request -----------------------------------------------------------------------------


async def test_enqueue_inserts_a_pending_row_and_one_event(env):
    runner, _, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "retry_node", {"node": "a"}, step_id="s1")
    req = get_request(env.store, request_id)
    assert req == ControlRequest(
        id=request_id,
        run_id=run_id,
        step_id="s1",
        kind="retry_node",
        payload={"node": "a"},
        created_at=req.created_at,
        handled_at=None,
        result=None,
    )
    assert req.status == "pending"
    (event,) = env.events("control_requested")
    assert event["data"] == {"request_id": request_id, "kind": "retry_node"}
    assert (event["repo"], event["issue"], event["run_id"]) == (REPO, 7, run_id)


async def test_enqueue_rejects_unknown_kinds_and_runs(env):
    _, _, run_id = two_step_run(env)
    with pytest.raises(ValueError, match="unknown control kind"):
        enqueue(env.store, run_id, "explode", {})
    with pytest.raises(KeyError):
        enqueue(env.store, "nope#1@1", "pause", {})
    assert env.store.query("SELECT * FROM control_requests") == []
    assert env.events("control_requested") == []
    assert get_request(env.store, 999) is None
    assert set(CONTROL_KINDS) == {"pause", "resume", "cancel", "retry_node", "send_message", "reroute"}


async def test_corrupt_json_reads_as_empty_and_never_raises(env):
    _, _, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "pause", {})
    env.store.execute(
        "UPDATE control_requests SET payload_json = '{oops', result_json = '[1', handled_at = 'x' WHERE id = ?",
        (request_id,),
    )
    req = get_request(env.store, request_id)
    assert req is not None
    assert (req.payload, req.result, req.status) == ({}, {"ok": False, "error": "interrupted"}, "error")


async def test_status_property(env):
    _, _, run_id = two_step_run(env)
    base = dict(id=1, run_id=run_id, step_id=None, kind="pause", payload={}, created_at="t")
    assert ControlRequest(**base, handled_at=None, result=None).status == "pending"
    assert ControlRequest(**base, handled_at="t", result={"ok": True}).status == "done"
    assert ControlRequest(**base, handled_at="t", result={"ok": False, "error": "x"}).status == "error"
    assert ControlRequest(**base, handled_at="t", result=None).status == "error"
    row = dict(base, payload_json="{}", step_id=None, handled_at="t", result_json=None)
    interrupted = _request_from_row(row)
    assert (interrupted.status, interrupted.result) == ("error", {"ok": False, "error": "interrupted"})


# --- apply_pending per kind ----------------------------------------------------------------------------


async def test_pause_stops_tick_from_advancing_and_resume_continues(env):
    runner, ex, run_id = two_step_run(env)
    pause_id = enqueue(env.store, run_id, "pause", {})
    assert await apply_pending(runner, env.store) == 1
    assert env.run(run_id).status == "paused"
    assert result_of(env, pause_id).result == {"ok": True}
    assert await runner.tick() == 0 and ex.calls == []

    resume_id = enqueue(env.store, run_id, "resume", {})
    await apply_pending(runner, env.store)
    assert env.run(run_id).status == "running"
    assert result_of(env, resume_id).status == "done"
    assert (await drive(runner, env, run_id)).status == "done"


async def test_resume_to_a_node_and_an_unknown_node(env):
    runner, ex, run_id = two_step_run(env)
    bad = enqueue(env.store, run_id, "resume", {"to": "nope"})
    good = enqueue(env.store, run_id, "resume", {"to": "b"})
    await apply_pending(runner, env.store)
    assert result_of(env, bad).result == {"ok": False, "error": "unknown node 'nope'"}
    assert result_of(env, good).status == "done"
    assert env.step_ids(run_id) == ["a/1", "b/1"]


async def test_cancel_calls_the_adapter_once_and_cancels_the_run(env):
    runner, adapter, run_id = await agent_run(env)
    (step,) = env.steps(run_id)
    assert step.status == "waiting"
    request_id = enqueue(env.store, run_id, "cancel", {})
    await apply_pending(runner, env.store)
    await apply_pending(runner, env.store)
    assert adapter.count("cancel") == 1
    assert adapter.calls[-1] == ("cancel", {"session": step.ref["id"]})
    assert env.run(run_id).status == "cancelled"
    assert result_of(env, request_id).result == {"ok": True}


async def test_retry_node_makes_a_new_visit_and_errors_at_max_visits(env):
    pb = make_playbook({"a": {**CHECK, "max_visits": 2}}, {"a.pass": "@done", "a.fail": "@failed"})
    runner = env.runner(Scripted({"a": [Done("fail")]}))
    run_id = runner.start_run(REPO, make_issue(), pb)
    await runner.tick()
    assert env.run(run_id).status == "failed"

    first = enqueue(env.store, run_id, "retry_node", {"node": "a"})
    await apply_pending(runner, env.store)
    assert result_of(env, first).result == {"ok": True, "node": "a", "visit": 2}
    assert env.step_ids(run_id) == ["a/1", "a/2"]
    await runner.tick()
    assert env.run(run_id).status == "failed"

    second = enqueue(env.store, run_id, "retry_node", {"node": "a"})
    await apply_pending(runner, env.store)
    req = result_of(env, second)
    assert req.status == "error" and "max_visits" in req.result["error"]
    assert env.step_ids(run_id) == ["a/1", "a/2"]


async def test_retry_node_without_a_node_is_an_error_result(env):
    runner, _, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "retry_node", {})
    await apply_pending(runner, env.store)
    assert result_of(env, request_id).result == {"ok": False, "error": "missing 'node'"}


async def test_send_message_calls_send_once_on_the_waiting_session(env):
    runner, adapter, run_id = await agent_run(env)
    (step,) = env.steps(run_id)
    request_id = enqueue(env.store, run_id, "send_message", {"text": "use the v2 API"})
    await apply_pending(runner, env.store)
    await apply_pending(runner, env.store)
    sends = [c for c in adapter.calls if c[0] == "send"]
    assert sends == [("send", {"session": step.ref["id"], "text": "use the v2 API"})]
    assert result_of(env, request_id).result == {"ok": True, "step": step.id}


async def test_send_message_to_a_given_step_and_to_an_unknown_one(env):
    runner, adapter, run_id = await agent_run(env)
    (step,) = env.steps(run_id)
    given = enqueue(env.store, run_id, "send_message", {"text": "hi"}, step_id=step.id)
    wrong = enqueue(env.store, run_id, "send_message", {"text": "hi"}, step_id=f"{run_id}/zzz/1")
    await apply_pending(runner, env.store)
    assert result_of(env, given).status == "done"
    assert result_of(env, wrong).result == {"ok": False, "error": "no agent session to message"}
    assert adapter.count("send") == 1


async def test_send_message_on_a_run_without_a_session_is_an_error_result(env):
    runner, _, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "send_message", {"text": "hi"})
    await apply_pending(runner, env.store)
    assert result_of(env, request_id).result == {"ok": False, "error": "no agent session to message"}


async def test_reroute_before_a_session_changes_the_agent_the_next_step_uses(env):
    first, second = CountingAdapter.happy_path(), CountingAdapter.happy_path()
    second.name = "other"
    env.adapters.register(first)
    env.adapters.register(second)
    runner = env.runner(StubAgentExecutor())
    run_id = runner.start_run(REPO, make_issue(), agent_playbook(), agent="fake")
    request_id = enqueue(env.store, run_id, "reroute", {"agent": "other"})
    await apply_pending(runner, env.store)
    assert result_of(env, request_id).result == {"ok": True, "from": "fake", "to": "other"}
    assert env.run(run_id).context["agent"] == "other"
    (event,) = env.events("rerouted")
    assert event["data"] == {"from": "fake", "to": "other"}
    await runner.tick()
    assert second.start_invocations == 1 and first.start_invocations == 0


async def test_reroute_after_a_session_started_is_an_error_result(env):
    runner, adapter, run_id = await agent_run(env)
    other = FakeAdapter(name="other")
    env.adapters.register(other)
    request_id = enqueue(env.store, run_id, "reroute", {"agent": "other"})
    await apply_pending(runner, env.store)
    assert result_of(env, request_id).result == {"ok": False, "error": "agent session already started"}
    assert env.run(run_id).context["agent"] == "fake"
    assert env.events("rerouted") == []


async def test_reroute_to_an_unregistered_agent_and_a_finished_run(env):
    runner, _, run_id = two_step_run(env)
    unknown = enqueue(env.store, run_id, "reroute", {"agent": "ghost"})
    await apply_pending(runner, env.store)
    assert "unknown agent" in result_of(env, unknown).result["error"]
    await drive(runner, env, run_id)
    env.adapters.register(FakeAdapter(name="other"))
    late = enqueue(env.store, run_id, "reroute", {"agent": "other"})
    await apply_pending(runner, env.store)
    assert result_of(env, late).result == {"ok": False, "error": f"run {run_id} is done"}


async def test_errors_are_recorded_redacted_and_never_break_the_batch(env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue123")
    runner, _, run_id = two_step_run(env)
    missing = enqueue(env.store, run_id, "pause", {})
    env.store.execute("UPDATE control_requests SET run_id = 'ghost#1@1' WHERE id = ?", (missing,))
    leaky = enqueue(env.store, run_id, "resume", {"to": "ghp_supersecretvalue123"})
    good = enqueue(env.store, run_id, "pause", {})
    assert await apply_pending(runner, env.store) == 3
    assert result_of(env, missing).result == {"ok": False, "error": "run ghost#1@1 not found"}
    assert "ghp_supersecretvalue123" not in json.dumps(result_of(env, leaky).result)
    assert result_of(env, good).status == "done" and env.run(run_id).status == "paused"
    kinds = [(e["data"]["request_id"], e["data"]["ok"]) for e in env.events("control_applied")]
    assert kinds == [(missing, False), (leaky, False), (good, True)]


# --- exactly once --------------------------------------------------------------------------------------


async def test_each_request_is_applied_exactly_once(env):
    runner, _, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "pause", {})
    assert await apply_pending(runner, env.store) == 1
    assert await apply_pending(runner, env.store) == 0
    await runner.tick()
    await runner.tick()
    req = result_of(env, request_id)
    assert req.handled_at is not None
    assert len(env.events("control_applied")) == 1 and len(env.events("paused")) == 1


async def test_a_request_claimed_elsewhere_is_skipped(env):
    runner, _, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "pause", {})
    env.store.execute("UPDATE control_requests SET handled_at = 'claimed' WHERE id = ?", (request_id,))
    assert await apply_pending(runner, env.store) == 0
    assert env.run(run_id).status == "running" and env.events("control_applied") == []


async def test_requests_apply_in_id_order(env):
    runner, _, run_id = two_step_run(env)
    enqueue(env.store, run_id, "pause", {})
    enqueue(env.store, run_id, "resume", {})
    await apply_pending(runner, env.store)
    assert [e["kind"] for e in env.events() if e["kind"] in ("paused", "resumed")] == ["paused", "resumed"]
    assert env.run(run_id).status == "running"


# --- the Runner.tick call site --------------------------------------------------------------------------


async def test_tick_applies_a_queued_pause_before_advancing_anything(env):
    runner, ex, run_id = two_step_run(env)
    request_id = enqueue(env.store, run_id, "pause", {})
    assert await runner.tick() == 0
    assert ex.calls == []
    assert env.run(run_id).status == "paused"
    assert result_of(env, request_id).status == "done"


async def test_tick_returns_only_the_advanced_step_count(env):
    runner, ex, run_id = two_step_run(env)
    enqueue(env.store, run_id, "retry_node", {"node": "zzz"})  # an error result, not a tick failure
    assert await runner.tick() == 1
    assert get_run(env.store, run_id).status == "running"


# --- step-bound requests are applied once ---------------------------------------------------------------


def escalation_run(env: Env):
    pb = make_playbook(
        {"fix": NOTIFY, "esc": {"kind": "escalate"}},
        {"fix.sent": "esc", "esc.resolved": "@done"},
    )
    attention = {"needs_you": True, "reason": "stuck"}
    ex = Scripted({"fix": [Done("sent")], "esc": [Waiting(output=attention, poll_after=60)]})
    runner = env.runner(ex)
    return runner, runner.start_run(REPO, make_issue(), pb)


async def test_two_resumes_for_one_escalation_give_one_success_and_one_already_resolved(env):
    runner, run_id = escalation_run(env)
    await runner.tick()
    await runner.tick()
    esc = f"{run_id}/esc/1"
    first = enqueue(env.store, run_id, "resume", {"to": "fix"}, step_id=esc)
    second = enqueue(env.store, run_id, "resume", {"to": "fix"}, step_id=esc)
    assert await apply_pending(runner, env.store) == 2
    assert result_of(env, first).result == {"ok": True}
    assert result_of(env, second).result == {"ok": False, "error": "already resolved"}
    assert env.step_ids(run_id) == ["esc/1", "fix/1", "fix/2"]
    (step,) = [s for s in env.steps(run_id) if s.node_id == "esc"]
    assert (step.status, step.outcome) == ("done", "resolved")


async def test_a_reroute_for_a_step_that_is_not_waiting_is_already_resolved(env):
    runner, run_id = escalation_run(env)
    env.adapters.register(FakeAdapter(name="other"))
    request_id = enqueue(env.store, run_id, "reroute", {"agent": "other"}, step_id=f"{run_id}/esc/9")
    await apply_pending(runner, env.store)
    assert result_of(env, request_id).result == {"ok": False, "error": "already resolved"}
    assert env.events("rerouted") == []
