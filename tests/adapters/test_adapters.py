import pytest
from pydantic import ValidationError

from harness.adapters.base import (
    ActivityItem,
    AdapterRegistry,
    AgentAdapter,
    AgentSnapshot,
    AgentState,
    Capabilities,
    SessionRef,
    StartRequest,
)
from harness.adapters.fake import FakeAdapter

S = AgentState


def make_request(key: str = "k1", issue: int = 13) -> StartRequest:
    return StartRequest(
        repo="o/r",
        base_branch="main",
        branch=f"harness/{issue}-slug",
        issue=issue,
        title="A title",
        brief="Do the work.",
        mode="plan_first",
        idempotency_key=key,
    )


async def states(adapter: FakeAdapter, ref: SessionRef, n: int) -> list[AgentState]:
    return [(await adapter.poll(ref)).state for _ in range(n)]


def test_state_values_are_exact() -> None:
    assert [s.value for s in AgentState] == [
        "STARTING",
        "PLANNING",
        "PLAN_READY",
        "WORKING",
        "NEEDS_INPUT",
        "PR_READY",
        "FAILED",
    ]


def test_capabilities_names_and_defaults() -> None:
    caps = Capabilities(plan_approval=True, messaging=False, opens_pr=True, local_workspace=False)
    assert caps.max_concurrency == 1
    assert caps.names() == {"plan", "pr"}
    all_caps = Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=True)
    assert all_caps.names() == {"plan", "message", "pr", "local"}
    none = Capabilities(plan_approval=False, messaging=False, opens_pr=False, local_workspace=False)
    assert none.names() == set()


def test_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=True, bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AgentSnapshot(state=S.WORKING, bogus=1)  # type: ignore[call-arg]


def test_snapshot_and_ref_defaults_are_not_shared() -> None:
    a, b = AgentSnapshot(state=S.WORKING), AgentSnapshot(state=S.WORKING)
    assert a.poll_after == 60.0
    assert a.activity is not b.activity
    r1, r2 = SessionRef(agent="x", id="1"), SessionRef(agent="x", id="2")
    assert r1.data is not r2.data and r1.url is None


def test_fake_adapter_satisfies_protocol() -> None:
    adapter = FakeAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "fake"
    assert adapter.caps.max_concurrency == 10
    assert adapter.caps.names() == {"plan", "message", "pr", "local"}
    assert not isinstance(object(), AgentAdapter)


async def test_happy_path() -> None:
    adapter = FakeAdapter.happy_path()
    ref = await adapter.start(make_request())
    assert await states(adapter, ref, 2) == [S.PLANNING, S.PLAN_READY]
    snap = await adapter.poll(ref)
    assert snap.state is S.PLAN_READY and snap.plan  # exhausted phase repeats its last snapshot
    await adapter.approve_plan(ref, note="lgtm")
    assert await states(adapter, ref, 1) == [S.WORKING]
    final = await adapter.poll(ref)
    assert final.state is S.PR_READY
    assert final.pr is not None and final.pr.number == 101


async def test_plan_rejected_once() -> None:
    adapter = FakeAdapter.plan_rejected_once()
    ref = await adapter.start(make_request())
    first = await adapter.poll(ref)
    assert first.state is S.PLAN_READY and first.plan == "plan v1"
    await adapter.send(ref, "revise it")
    second = await adapter.poll(ref)
    assert second.state is S.PLAN_READY and second.plan == "plan v2"
    await adapter.approve_plan(ref)
    final = await adapter.poll(ref)
    assert final.state is S.PR_READY and final.pr is not None


async def test_pr_fix_round() -> None:
    adapter = FakeAdapter.pr_fix_round()
    ref = await adapter.start(make_request())
    assert (await adapter.poll(ref)).state is S.PLAN_READY
    await adapter.approve_plan(ref)
    first = await adapter.poll(ref)
    assert first.state is S.PR_READY and first.pr is not None and first.pr.head_sha == "a1"
    await adapter.send(ref, "fix the findings")
    assert (await adapter.poll(ref)).state is S.WORKING
    second = await adapter.poll(ref)
    assert second.state is S.PR_READY and second.pr is not None and second.pr.head_sha == "b2"


async def test_fails() -> None:
    adapter = FakeAdapter.fails()
    ref = await adapter.start(make_request())
    assert (await adapter.poll(ref)).state is S.PLANNING
    snap = await adapter.poll(ref)
    assert snap.state is S.FAILED and snap.error
    assert (await adapter.poll(ref)).state is S.FAILED


async def test_asks_question() -> None:
    adapter = FakeAdapter.asks_question()
    ref = await adapter.start(make_request())
    assert (await adapter.poll(ref)).state is S.PLANNING
    snap = await adapter.poll(ref)
    assert snap.state is S.NEEDS_INPUT and snap.question
    await adapter.send(ref, "use approach B")
    assert (await adapter.poll(ref)).state is S.PLAN_READY
    await adapter.approve_plan(ref)
    final = await adapter.poll(ref)
    assert final.state is S.PR_READY and final.pr is not None


async def test_calls_are_recorded() -> None:
    adapter = FakeAdapter.happy_path()
    ref = await adapter.start(make_request())
    await adapter.poll(ref)
    await adapter.approve_plan(ref, note="go")
    await adapter.send(ref, "hello")
    await adapter.cancel(ref)
    assert ("send", {"session": "fake-1", "text": "hello"}) in adapter.calls
    assert ("approve_plan", {"session": "fake-1", "note": "go"}) in adapter.calls
    assert [name for name, _ in adapter.calls] == ["start", "poll", "approve_plan", "send", "cancel"]


async def test_idempotent_start() -> None:
    adapter = FakeAdapter.happy_path()
    first = await adapter.start(make_request("same"))
    again = await adapter.start(make_request("same"))
    other = await adapter.start(make_request("different"))
    assert again == first and first.id == "fake-1"
    assert other.id == "fake-2"
    assert [name for name, _ in adapter.calls].count("start") == 2
    assert await adapter.find("same") == first
    assert await adapter.find("missing") is None


async def test_sessions_progress_independently() -> None:
    adapter = FakeAdapter.happy_path()
    a = await adapter.start(make_request("a"))
    b = await adapter.start(make_request("b"))
    await adapter.poll(a)
    await adapter.poll(a)
    await adapter.approve_plan(a)
    assert (await adapter.poll(b)).state is S.PLANNING
    assert (await adapter.poll(a)).state is S.WORKING


async def test_script_per_key_with_default_fallback() -> None:
    script = {
        "special": [[AgentSnapshot(state=S.WORKING)]],
        "*": [[AgentSnapshot(state=S.PLANNING)]],
    }
    adapter = FakeAdapter(script=script)
    special = await adapter.start(make_request("special"))
    other = await adapter.start(make_request("other"))
    assert (await adapter.poll(special)).state is S.WORKING
    assert (await adapter.poll(other)).state is S.PLANNING


async def test_no_script_stays_starting() -> None:
    adapter = FakeAdapter()
    ref = await adapter.start(make_request())
    assert await states(adapter, ref, 3) == [S.STARTING] * 3


async def test_exhausted_phase_repeats_last_and_advance_at_end_stays() -> None:
    adapter = FakeAdapter(
        script={
            "*": [
                [AgentSnapshot(state=S.PLANNING), AgentSnapshot(state=S.WORKING)],
                [AgentSnapshot(state=S.PR_READY)],
            ]
        }
    )
    ref = await adapter.start(make_request())
    assert await states(adapter, ref, 4) == [S.PLANNING, S.WORKING, S.WORKING, S.WORKING]
    await adapter.send(ref, "next")
    assert await states(adapter, ref, 2) == [S.PR_READY, S.PR_READY]
    await adapter.send(ref, "again")  # no further phase: stays on the last one
    assert await states(adapter, ref, 1) == [S.PR_READY]


async def test_poll_returns_copies() -> None:
    adapter = FakeAdapter.happy_path()
    ref = await adapter.start(make_request())
    snap = await adapter.poll(ref)
    snap.plan = "mutated"
    snap.activity.append(ActivityItem(ts="t", kind="k", text="x"))
    again = await adapter.poll(ref)
    assert again.plan != "mutated" and again.activity == []


async def test_cancel_sets_failed() -> None:
    adapter = FakeAdapter.happy_path()
    ref = await adapter.start(make_request())
    await adapter.cancel(ref)
    for _ in range(2):
        snap = await adapter.poll(ref)
        assert snap.state is S.FAILED and snap.error == "cancelled"


async def test_unknown_session_raises() -> None:
    adapter = FakeAdapter.happy_path()
    with pytest.raises(KeyError):
        await adapter.poll(SessionRef(agent="fake", id="fake-99"))


def test_invalid_script_rejected() -> None:
    with pytest.raises(ValueError):
        FakeAdapter(script={"*": []})
    with pytest.raises(ValueError):
        FakeAdapter(script={"*": [[]]})


def test_registry() -> None:
    registry = AdapterRegistry()
    limited = Capabilities(plan_approval=False, messaging=True, opens_pr=True, local_workspace=False)
    fake = FakeAdapter()
    other = FakeAdapter(name="copilot", caps=limited)
    registry.register(fake)
    registry.register(other)
    assert registry.get("fake") is fake
    assert registry.names() == ["copilot", "fake"]
    assert registry.caps_by_name() == {
        "fake": {"plan", "message", "pr", "local"},
        "copilot": {"message", "pr"},
    }
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeAdapter())
    with pytest.raises(KeyError, match="copilot, fake"):
        registry.get("nope")


def test_empty_registry_unknown_name() -> None:
    with pytest.raises(KeyError, match="nope"):
        AdapterRegistry().get("nope")
