"""Campaign-scoped plan gate (M4.1): waits while pending, approves through the ledger record, reopens on stale."""

import pytest

from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig
from harness.engine import Done, ExecContext, Waiting
from harness.engine.executors.gate import GateExecutor
from harness.playbook.models import GateNode, Playbook
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.server.derive import inbox_kind
from harness.store.repo import RunRow, StepRow
from tests.engine.helpers import Env
from tests.planner.world import CONTROL, PLAN_PATH, PLAN_TEXT, REPO, T_LATER, T_LATEST, World

NODE = GateNode(gate="plan", scope="campaign")


@pytest.fixture
def world(respx_mock) -> World:
    return World(respx_mock).install()


def playbook(mode="human", inputs=None) -> Playbook:
    return Playbook.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "campaign",
            "gates": {"plan": mode},
            "inputs": inputs or {},
            "nodes": {"plan": {"kind": "gate", "gate": "plan", "scope": "campaign"}},
            "edges": {"plan.approved": "@done", "plan.rejected": "@failed"},
        }
    )


def make_ctx(env: Env, *, mode="human", inputs=None, control=CONTROL, allowlist=()) -> ExecContext:
    run = RunRow(
        id="run1",
        repo=REPO,
        issue=7,
        playbook_id="campaign",
        playbook_sha=None,
        status="running",
        context={"inputs": {"plan_path": PLAN_PATH} if inputs is None else inputs},
        ledger_comment_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    return ExecContext(
        store=env.store,
        outbox=env.outbox,
        gh=env.gh,
        adapters=AdapterRegistry(),
        repo_config=RepoConfig(control_issue=control, command_users=list(allowlist)),
        playbook=playbook(mode),
        run=run,
        template=Context(
            issue=IssueCtx(number=7, title="t", agent="jules", labels=[]), run=RunCtx(id="run1")
        ),
        clock=env.clock,
    )


def step(visit: int = 1) -> StepRow:
    return StepRow(
        id=f"run1/plan/{visit}",
        run_id="run1",
        node_id="plan",
        visit=visit,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )


async def test_waits_while_pending_and_asks_once(env: Env, world: World) -> None:
    world.put_file()
    executor, ctx = GateExecutor(), make_ctx(env)
    first = await executor.advance(step(), NODE, ctx)
    second = await executor.advance(step(), NODE, ctx)
    assert isinstance(first, Waiting) and first == second
    out = first.output or {}
    assert out["needs_you"] is True and out["plan_status"] == "pending"
    assert PLAN_PATH in out["reason"] and "harness:plan-approved" in out["reason"]
    assert out["links"] == [f"https://github.com/{REPO}/blob/HEAD/{PLAN_PATH}"]
    assert len(env.outbox.of("add_comment")) == 1  # one request comment for this sha
    assert [p["add"] for p in env.outbox.of("set_labels")] == [["harness:needs-you"]]
    # No per-issue gate row: campaign gates do not use the gates table.
    assert env.store.query("SELECT * FROM gates") == []


async def test_the_waiting_step_is_a_plan_approval_inbox_item(env: Env, world: World) -> None:
    world.put_file()
    result = await GateExecutor().advance(step(), NODE, make_ctx(env))
    assert isinstance(result, Waiting)
    waiting = step().model_copy(update={"status": "waiting", "output": result.output})
    pb = playbook().model_dump(mode="json")
    assert inbox_kind(waiting, [waiting], pb) == "plan_approval"


async def test_approval_goes_to_the_ledger_first_then_the_gate_resolves(env: Env, world: World) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    executor, ctx = GateExecutor(), make_ctx(env)
    queued = await executor.advance(step(), NODE, ctx)
    # The signal is queued as a ledger record; the gate does not resolve before GitHub has it.
    assert isinstance(queued, Waiting) and not (queued.output or {}).get("needs_you")
    assert world.ledger() is None
    await env.outbox.drain_once()
    assert world.ledger()["plan"]["approved_by"] == "o"  # type: ignore[index]

    done = await executor.advance(step(), NODE, ctx)
    assert isinstance(done, Done) and done.outcome == "approved"
    assert done.output["decided_by"] == "o" and done.output["via"] == "label"
    assert done.output["plan_sha"] == world.files[PLAN_PATH].sha


async def test_a_label_by_a_stranger_keeps_waiting_with_the_reason(env: Env, world: World) -> None:
    world.put_file()
    world.label_event("mallory", at=T_LATER)
    result = await GateExecutor().advance(step(), NODE, make_ctx(env))
    assert isinstance(result, Waiting)
    assert "@mallory" in (result.output or {})["reason"]
    assert env.outbox.of("merge_ledger") == []


async def test_the_gate_reopens_when_the_plan_changes_after_approval(env: Env, world: World) -> None:
    world.put_file()
    world.label_event("o", at=T_LATER)
    executor, ctx = GateExecutor(), make_ctx(env)
    await executor.advance(step(), NODE, ctx)
    await env.outbox.drain_once()
    assert isinstance(await executor.advance(step(), NODE, ctx), Done)

    world.put_file(text=PLAN_TEXT + "\n## Late change\n", version=2, date=T_LATEST)
    again = await executor.advance(step(2), NODE, ctx)
    assert isinstance(again, Waiting)
    out = again.output or {}
    assert out["needs_you"] is True and out["plan_status"] == "stale" and "changed after" in out["reason"]
    comments = env.outbox.of("add_comment")  # the first version was approved at once; the new sha is asked
    assert len(comments) == 1 and world.files[PLAN_PATH].sha[:12] in comments[0]["body"]


async def test_a_merged_plan_pr_approves_through_the_same_record(env: Env, world: World) -> None:
    world.put_file()
    world.merge_pr(5)
    executor, ctx = GateExecutor(), make_ctx(env)
    await executor.advance(step(), NODE, ctx)
    await env.outbox.drain_once()
    done = await executor.advance(step(), NODE, ctx)
    assert isinstance(done, Done) and done.output["via"] == "pr"
    assert world.ledger()["plan"]["via"] == "pr"  # type: ignore[index]


async def test_an_issue_plan_is_read_from_the_run_inputs(env: Env, world: World) -> None:
    world.put_issue(12, "Campaign plan", "# Plan\n")
    world.label_event("o", at=T_LATER)
    executor, ctx = GateExecutor(), make_ctx(env, inputs={"plan_issue": 12})
    await executor.advance(step(), NODE, ctx)
    await env.outbox.drain_once()
    done = await executor.advance(step(), NODE, ctx)
    assert isinstance(done, Done) and done.outcome == "approved"
    assert world.ledger()["plan"]["issue"] == 12  # type: ignore[index]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"inputs": {}}, "exactly one"),
        ({"inputs": {"plan_path": "docs/NOPE.md"}}, "not found"),
        ({"control": None}, "control_issue"),
    ],
)
async def test_a_bad_setup_fails_the_step(env: Env, world: World, kwargs: dict, message: str) -> None:
    result = await GateExecutor().advance(step(), NODE, make_ctx(env, **kwargs))
    assert isinstance(result, Done) and result.outcome == "failed" and message in result.output["error"]


async def test_an_auto_plan_gate_still_approves_without_looking_at_github(env: Env) -> None:
    result = await GateExecutor().advance(step(), NODE, make_ctx(env, mode="auto"))
    assert result == Done("approved", {"note": None, "decided_by": "auto"})
