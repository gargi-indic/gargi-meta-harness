"""The decompose executor (M4.3): proposes once per plan sha, stores the proposal GitHub first, optionally creates."""

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from harness.adapters.fake import FakeAdapter
from harness.config import AgentConfig, RepoConfig
from harness.engine import Done, ExecContext, ExecutorRegistry, SimulatedCrash
from harness.engine.executors import default_registry
from harness.engine.executors.decompose import DecomposeExecutor, MaterializeFn
from harness.llm.backend import BackendError, FakeBackend
from harness.planner.materialize import MaterializeContext, MaterializeError, MaterializeResult, materialize
from harness.planner.proposal import ProposalIssue, ProposedPlan
from harness.planner.proposal_store import read_proposals
from harness.playbook.models import DecomposeNode, Playbook
from harness.playbook.templating import Context, IssueCtx, RunCtx, render
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow, get_proposal_row
from tests.engine.helpers import Env, drive, make_issue, make_playbook
from tests.planner.world import CONTROL, PLAN_PATH, PLAN_TEXT, REPO, World

NODE = DecomposeNode(plan=PLAN_PATH)


@pytest.fixture
def world(respx_mock) -> World:
    return World(respx_mock).install()


def good_issue(key: str, *deps: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Do {key}",
        "body": "## Context\nc\n## Scope\ns\n## Acceptance criteria\n- a\n## Out of scope\n- o",
        "acceptance": ["it works"],
        "blocked_by": list(deps),
        "agent": "jules",
        "playbook": "plan-build-review",
    }


def good(*keys: str) -> dict[str, Any]:
    return {
        "epic_title": "Epic",
        "epic_body": "Body",
        "issues": [good_issue(k, *keys[:n][-1:]) for n, k in enumerate(keys)],
        "notes": "",
    }


def make_ctx(
    env: Env, *, inputs: dict | None = None, control: int | None = CONTROL, **config: Any
) -> ExecContext:
    if "jules" not in env.adapters.names():
        env.adapters.register(FakeAdapter("jules"))
    run = RunRow(
        id=f"{REPO}#7@1",
        repo=REPO,
        issue=7,
        playbook_id="campaign",
        playbook_sha=None,
        status="running",
        context={"inputs": inputs or {}},
        ledger_comment_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    pb = Playbook.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "campaign",
            "nodes": {"d": {"kind": "decompose"}},
            "edges": {"*.proposed": "@done", "*.invalid": "@failed", "*.failed": "@failed"},
        }
    )
    return ExecContext(
        store=env.store,
        outbox=env.outbox,
        gh=env.gh,
        adapters=env.adapters,
        repo_config=RepoConfig(control_issue=control, **config),
        playbook=pb,
        run=run,
        template=Context(
            issue=IssueCtx(number=7, title="t", agent="jules", labels=[]),
            inputs=inputs or {},
            run=RunCtx(id=run.id),
        ),
        clock=env.clock,
    )


def step() -> StepRow:
    return StepRow(
        id=f"{REPO}#7@1/d/1",
        run_id=f"{REPO}#7@1",
        node_id="d",
        visit=1,
        status="pending",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )


def proposal_comments(world: World) -> list[dict[str, Any]]:
    return [c for c in world.comments.get(CONTROL, []) if "harness:proposal" in c["body"].split("\n")[0]]


async def advance(executor: DecomposeExecutor, ctx: ExecContext, node: DecomposeNode = NODE) -> Done:
    result = await executor.advance(step(), node, ctx)
    assert isinstance(result, Done)
    return result


# ---- proposing ---------------------------------------------------------------------------------------------


async def test_a_proposal_is_stored_on_github_and_in_the_step_output(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([good("a", "b")])
    result = await advance(DecomposeExecutor(backend), make_ctx(env))
    assert result.outcome == "proposed"
    out = result.output
    assert out["proposal"] == ProposedPlan.model_validate(good("a", "b")).model_dump(mode="json")
    assert out["plan_sha"] == world.files[PLAN_PATH].sha and out["plan"] == PLAN_PATH
    assert out["issue_count"] == 2 and out["reused"] is False and out["status"] == "proposed"
    assert len(backend.calls) == 1
    stored = read_proposals(world.comments[CONTROL])
    assert len(stored) == 1 and stored[0].run_id == f"{REPO}#7@1" and stored[0].plan_sha == out["plan_sha"]
    assert world.ledger()["proposal"]["issues"] == 2  # type: ignore[index]
    assert get_proposal_row(env.store, f"{REPO}#7@1", out["plan_sha"]) is not None
    assert "Intro." in backend.calls[0]["prompt"]  # the plan text reached the model


async def test_the_prompt_lists_the_enabled_agents_and_the_repo_playbooks(env: Env, world: World) -> None:
    world.put_file()
    world.put_file(
        ".harness/playbooks/custom.yaml",
        "apiVersion: harness/v1\nid: custom\ndescription: Only for tests.\nnodes:\n  n: {kind: escalate}\n"
        "edges:\n  n.resolved: '@done'\n",
    )
    world.raw_contents[".harness/playbooks"] = [  # type: ignore[assignment]
        {"type": "file", "path": ".harness/playbooks/custom.yaml"},
        {"type": "file", "path": ".harness/playbooks/README.md"},
        {"type": "dir", "path": ".harness/playbooks/sub"},
    ]
    env.adapters.register(FakeAdapter("codex"))
    backend = FakeBackend([good("a")])
    ctx = make_ctx(env, agents={"jules": AgentConfig(), "codex": AgentConfig(enabled=False)})
    assert (await advance(DecomposeExecutor(backend), ctx)).outcome == "proposed"
    prompt = backend.calls[0]["prompt"]
    assert "`jules`" in prompt and "`codex`" not in prompt
    assert "`custom`: Only for tests." in prompt and "`plan-build-review`" in prompt


async def test_an_existing_proposal_is_reused_without_asking_the_model(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([good("a")])
    executor, ctx = DecomposeExecutor(backend), make_ctx(env)
    first = await advance(executor, ctx)
    again = await advance(executor, ctx)
    assert again.outcome == "proposed" and again.output["reused"] is True
    assert again.output["proposal"] == first.output["proposal"]
    assert len(backend.calls) == 1 and len(proposal_comments(world)) == 1


async def test_with_the_local_db_lost_the_proposal_is_found_on_github(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([good("a")])
    executor = DecomposeExecutor(backend)
    first = await advance(executor, make_ctx(env))
    fresh = Store(":memory:", clock=lambda: datetime.fromtimestamp(env.clock.t, UTC))
    fresh.migrate()
    ctx = make_ctx(env)
    ctx.store = fresh
    again = await advance(executor, ctx)
    assert again.outcome == "proposed" and again.output["reused"] is True
    assert again.output["proposal"] == first.output["proposal"]
    assert len(backend.calls) == 1 and len(proposal_comments(world)) == 1
    assert get_proposal_row(fresh, f"{REPO}#7@1", first.output["plan_sha"]) is not None  # refilled
    fresh.close()


async def test_a_changed_plan_is_decomposed_again(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([good("a"), good("a", "b")])
    executor, ctx = DecomposeExecutor(backend), make_ctx(env)
    first = await advance(executor, ctx)
    world.put_file(text=PLAN_TEXT + "\n## More\n", version=2)
    second = await advance(executor, ctx)
    assert second.output["plan_sha"] != first.output["plan_sha"] and second.output["issue_count"] == 2
    assert len(backend.calls) == 2 and len(proposal_comments(world)) == 2


async def test_the_plan_can_be_an_issue_or_come_from_the_run_inputs(env: Env, world: World) -> None:
    world.put_issue(12, "Campaign", "# Campaign\n\nDo it.\n")
    world.put_file()
    backend = FakeBackend([good("a"), good("a"), good("a"), good("a")])
    executor = DecomposeExecutor(backend)
    by_number = await advance(executor, make_ctx(env), DecomposeNode(plan="#12"))
    assert by_number.output["plan"] == "#12"
    by_input_issue = await advance(executor, make_ctx(env, inputs={"plan_issue": 12}), DecomposeNode())
    assert (
        by_input_issue.output["plan_sha"] == by_number.output["plan_sha"] and by_input_issue.output["reused"]
    )
    templated = await advance(
        executor, make_ctx(env, inputs={"plan_path": PLAN_PATH}), DecomposeNode(plan="{{ inputs.plan_path }}")
    )
    assert templated.output["plan"] == PLAN_PATH
    by_input_path = await advance(executor, make_ctx(env, inputs={"plan_path": PLAN_PATH}), DecomposeNode())
    assert by_input_path.output["reused"] is True and len(backend.calls) == 2


async def test_guidance_is_rendered_into_the_prompt_and_max_issues_is_passed(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([good("a")])
    ctx = make_ctx(env, inputs={"focus": "storage first"})
    node = DecomposeNode(plan=PLAN_PATH, guidance="Start with {{ inputs.focus }}.", max_issues=7)
    assert (await advance(DecomposeExecutor(backend), ctx, node)).outcome == "proposed"
    assert "Start with storage first." in backend.calls[0]["prompt"]
    assert "At most 7 issues" in backend.calls[0]["prompt"]


# ---- invalid and failed ---------------------------------------------------------------------------------------


async def test_a_proposal_that_never_validates_is_invalid_and_keeps_the_error_codes(
    env: Env, world: World
) -> None:
    world.put_file()
    bad = good("a")
    bad["issues"][0]["agent"] = "nobody"
    bad["issues"][0]["playbook"] = "nothing"
    backend = FakeBackend([bad, bad, bad])
    result = await advance(DecomposeExecutor(backend), make_ctx(env))
    assert result.outcome == "invalid" and len(backend.calls) == 3
    codes = {(e["code"], e["key"]) for e in result.output["errors"]}
    assert codes == {("unknown_agent", "a"), ("unknown_playbook", "a")}
    assert "could not produce a valid proposal" in result.output["error"]
    assert result.output["plan_sha"] == world.files[PLAN_PATH].sha
    assert proposal_comments(world) == [] and env.store.query("SELECT * FROM proposals") == []


async def test_a_backend_that_is_down_fails_the_step(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([BackendError("down")] * 3)
    result = await advance(DecomposeExecutor(backend), make_ctx(env))
    assert result.outcome == "failed" and {e["code"] for e in result.output["errors"]} == {"backend_error"}
    assert proposal_comments(world) == []


@pytest.mark.parametrize(
    ("node", "kwargs", "message"),
    [
        (DecomposeNode(plan="docs/NOPE.md"), {}, "not found"),
        (DecomposeNode(), {}, "exactly one"),
        (DecomposeNode(plan="#99"), {}, "not found"),
        (NODE, {"control": None}, "control_issue"),
        (DecomposeNode(plan="{{ inputs.nothing }}"), {}, "undefined"),
        (DecomposeNode(plan=PLAN_PATH, guidance="{{ nope.x }}"), {}, "undefined"),
    ],
)
async def test_a_bad_setup_fails_the_step_without_asking_the_model(
    env: Env, world: World, node: DecomposeNode, kwargs: dict, message: str
) -> None:
    world.put_file()
    backend = FakeBackend([good("a")])
    result = await advance(DecomposeExecutor(backend), make_ctx(env, **kwargs), node)
    assert result.outcome == "failed" and message in result.output["error"]
    assert backend.calls == []


async def test_an_outbox_that_cannot_write_fails_the_step_and_nothing_is_mirrored(
    env: Env, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.put_file()

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("write failed")

    monkeypatch.setattr(env.gh, "add_comment", refuse)
    result = await advance(DecomposeExecutor(FakeBackend([good("a")])), make_ctx(env))
    assert result.outcome == "failed" and "OutboxOpFailed" in result.output["error"]
    assert env.store.query("SELECT * FROM proposals") == []


# ---- creating --------------------------------------------------------------------------------------------------


class RecordingMaterialize:
    def __init__(self, result: MaterializeResult | Exception | None = None) -> None:
        self.result = result or MaterializeResult(
            epic_number=50,
            issues={"a": 51, "b": 52},
            created=["a", "b"],
            reused=[],
            links_created=3,
            labels_ensured=[],
        )
        self.calls: list[tuple[ProposedPlan, MaterializeContext]] = []

    async def __call__(self, plan: ProposedPlan, ctx: MaterializeContext) -> MaterializeResult:
        self.calls.append((plan, ctx))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


CREATE = DecomposeNode(plan=PLAN_PATH, create=True, max_issues=9)


async def test_create_calls_the_materialize_function_and_marks_the_proposal_created(
    env: Env, world: World
) -> None:
    world.put_file()
    fake = RecordingMaterialize()
    backend = FakeBackend([good("a", "b")])
    result = await advance(DecomposeExecutor(backend, materialize_fn=fake), make_ctx(env), CREATE)
    assert result.outcome == "created"
    assert result.output["created"]["epic_number"] == 50 and result.output["created"]["issues"] == {
        "a": 51,
        "b": 52,
    }
    assert result.output["status"] == "created" and result.output["issue_count"] == 2
    [(plan, mctx)] = fake.calls
    assert plan == ProposedPlan.model_validate(good("a", "b"))
    assert mctx.repo == REPO and mctx.control_issue == CONTROL and mctx.max_issues == 9
    assert mctx.agents == ["jules"] and "plan-build-review" in (mctx.playbooks or [])
    assert re.fullmatch(r"run-[0-9a-f]{16}", mctx.run_key)
    assert read_proposals(world.comments[CONTROL])[0].status == "created"
    row = get_proposal_row(env.store, f"{REPO}#7@1", result.output["plan_sha"])
    assert row is not None and row.status == "created"


async def test_repeating_a_create_step_reuses_everything(env: Env, world: World) -> None:
    world.put_file()
    fake = RecordingMaterialize()
    backend = FakeBackend([good("a")])
    executor, ctx = DecomposeExecutor(backend, materialize_fn=fake), make_ctx(env)
    first = await advance(executor, ctx, CREATE)
    patches = world.count("PATCH", "")
    second = await advance(executor, ctx, CREATE)
    assert second.outcome == "created" and second.output["reused"] is True
    assert len(backend.calls) == 1 and len(fake.calls) == 2
    assert fake.calls[0][1].run_key == fake.calls[1][1].run_key  # the same creation is resumed
    assert world.count("PATCH", "") == patches  # status was already "created": no write to GitHub
    assert first.output["proposal"] == second.output["proposal"]


async def test_create_uses_a_proposal_that_was_stored_and_edited_earlier(env: Env, world: World) -> None:
    world.put_file()
    backend = FakeBackend([good("a")])
    proposing = DecomposeExecutor(backend)
    await advance(proposing, make_ctx(env))
    fake = RecordingMaterialize()
    creating = DecomposeExecutor(backend, materialize_fn=fake)
    result = await advance(creating, make_ctx(env), CREATE)
    assert result.outcome == "created" and len(backend.calls) == 1 and result.output["reused"] is True


async def test_a_plan_refused_at_creation_is_invalid_and_an_op_failure_is_failed(
    env: Env, world: World
) -> None:
    world.put_file()
    problems = [ProposalIssue("unknown_playbook", "a", "playbook 'x' does not exist")]
    refused = RecordingMaterialize(MaterializeError("plan refused", problems))
    backend = FakeBackend([good("a")])
    result = await advance(DecomposeExecutor(backend, materialize_fn=refused), make_ctx(env), CREATE)
    assert result.outcome == "invalid"
    assert result.output["errors"] == [
        {"code": "unknown_playbook", "key": "a", "message": "playbook 'x' does not exist"}
    ]

    broken = RecordingMaterialize(MaterializeError("outbox op 3 failed"))
    result = await advance(DecomposeExecutor(backend, materialize_fn=broken), make_ctx(env), CREATE)
    assert result.outcome == "failed" and "outbox op 3 failed" in result.output["error"]
    assert read_proposals(world.comments[CONTROL])[0].status == "proposed"  # not created: still open


def test_the_default_materialize_function_is_the_real_one() -> None:
    assert DecomposeExecutor(FakeBackend([])).materialize_fn is materialize
    fn: MaterializeFn = DecomposeExecutor(FakeBackend([])).materialize_fn
    assert callable(fn)


def test_default_registry_wires_the_backend_into_the_decompose_executor() -> None:
    backend = FakeBackend([])
    executor = default_registry(review_backend=backend).get("decompose")
    assert isinstance(executor, DecomposeExecutor) and executor.backend is backend


# ---- inside the runner -----------------------------------------------------------------------------------------


def campaign_playbook() -> Playbook:
    return make_playbook(
        {"decompose": {"kind": "decompose", "plan": PLAN_PATH}},
        {"decompose.proposed": "@done", "decompose.invalid": "@failed", "decompose.failed": "@failed"},
        id="campaign",
    )


def registry_with(executor: DecomposeExecutor) -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register("decompose", executor)
    return registry


async def test_a_crash_after_the_model_answered_does_not_ask_it_again(env: Env, world: World) -> None:
    world.put_file()
    env.adapters.register(FakeAdapter("jules"))
    backend = FakeBackend([good("a", "b"), good("x")])
    executor = DecomposeExecutor(backend)
    configs = {REPO: RepoConfig(control_issue=CONTROL)}
    first = env.runner(executors=registry_with(executor), repo_configs=configs, crash_after="after_advance")
    run_id = first.start_run(REPO, make_issue(), campaign_playbook(), agent="jules")
    with pytest.raises(SimulatedCrash):
        await first.tick()  # the proposal is stored, the step result was not committed
    assert len(backend.calls) == 1
    assert env.steps(run_id)[0].status == "pending"

    second = env.runner(executors=registry_with(executor), repo_configs=configs)  # a restarted harness
    env.clock.advance(60)
    run = await drive(second, env, run_id)
    assert run.status == "done" and len(backend.calls) == 1
    step_row = env.steps(run_id)[0]
    assert step_row.outcome == "proposed" and step_row.output["reused"] is True  # type: ignore[index]
    assert [i["key"] for i in step_row.output["proposal"]["issues"]] == ["a", "b"]  # type: ignore[index]
    assert len(proposal_comments(world)) == 1


async def test_a_crash_with_the_local_db_lost_still_finds_the_proposal_on_github(
    env: Env, world: World
) -> None:
    world.put_file()
    env.adapters.register(FakeAdapter("jules"))
    backend = FakeBackend([good("a")])
    executor = DecomposeExecutor(backend)
    ctx = make_ctx(env)
    await advance(executor, ctx)
    env.store.execute("DELETE FROM proposals")  # the mirror is only a cache
    again = await advance(executor, ctx)
    assert again.output["reused"] is True and len(backend.calls) == 1


async def test_downstream_templates_can_use_the_proposal(env: Env, world: World) -> None:
    world.put_file()
    env.adapters.register(FakeAdapter("jules"))
    backend = FakeBackend([good("a", "b", "c")])
    runner = env.runner(
        executors=registry_with(DecomposeExecutor(backend)),
        repo_configs={REPO: RepoConfig(control_issue=CONTROL)},
    )
    run_id = runner.start_run(REPO, make_issue(), campaign_playbook(), agent="jules")
    assert (await drive(runner, env, run_id)).status == "done"
    nodes = env.run(run_id).context["nodes"]
    ctx = Context(issue=IssueCtx(number=7, title="t", agent="jules"), nodes=nodes, run=RunCtx(id=run_id))
    assert render("{{ decompose.proposal.issues | length }}", ctx) == "3"
    assert render("{{ nodes.decompose.proposal.epic_title }} {{ decompose.plan_sha[:4] }}", ctx).startswith(
        "Epic blob"
    )
    assert json.loads(json.dumps(nodes["decompose"]))["issue_count"] == 3
