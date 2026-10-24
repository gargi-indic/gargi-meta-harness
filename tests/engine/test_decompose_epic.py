"""The decompose-epic playbook (M4.7) end to end: proposal, plan gate, issue creation, then the dispatcher.

The built-in ``decompose-epic.yaml`` runs on the real executors (decompose, plan gate, notify, escalate) with a
``FakeBackend`` as decomposer and a stateful GitHub stand-in behind respx: the planner's ``PlannerWorld`` (issues,
labels, sub-issues, ``blocked_by`` with the same 422s as GitHub, ``crash_after``) plus the few routes that the plan
gate and the outbox add (events, commits, comment edits, issue labels, the plan file's blob sha). The outbox is
drained between ticks.
"""

import hashlib
import json
from typing import Any

import httpx
import pytest

from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine.dispatcher import Dispatcher
from harness.engine.executors import default_registry, gate
from harness.engine.model import TERMINAL_STATUSES
from harness.llm.backend import FakeBackend
from harness.planner.materialize import MaterializeContext, materialize, plan_run_key
from harness.planner.plan_doc import load_plan_doc
from harness.planner.plan_gate import PlanApproval, PlanApprovalStatus, record_plan_approval
from harness.planner.proposal import ProposedPlan
from harness.planner.proposal_store import read_proposals, save_proposal
from harness.playbook.load import builtin_playbooks
from harness.store.repo import IssueRow, upsert_issue
from tests.engine.helpers import Env, make_issue
from tests.planner.gh_fake import BASE, CONTROL, REPO, Crash, PlannerWorld, Write

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)

PLAN_PATH = "docs/PLAN.md"
PLAN_TEXT = "# Campaign\n\nBuild the thing.\n\n## Steps\n\n- schema\n- api\n"
RUN_ISSUE = 2  # the epic issue that `harness run` is started on
RUN_ID = f"{REPO}#{RUN_ISSUE}@1"
OWNER = REPO.split("/")[0]
REJECTION = "Split the schema work into smaller issues"


class EpicWorld(PlannerWorld):
    """``PlannerWorld`` plus what the plan gate, the proposal store and the runner's ledger also call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.issue_labels: dict[int, set[str]] = {}

    def put_plan(self, text: str = PLAN_TEXT) -> None:
        self.files[PLAN_PATH] = text

    def blob_sha(self, path: str = PLAN_PATH) -> str:
        return hashlib.sha1(self.files[path].encode()).hexdigest()

    def install(self) -> "EpicWorld":
        super().install()

        def route(method: str, pattern: str, handler: Any) -> None:
            def wrapped(request: httpx.Request, **groups: str) -> httpx.Response:
                path = request.url.path
                if method == "GET":
                    self.reads.append((method, path))
                else:
                    body = json.loads(request.content) if request.content else None
                    self.writes.append(Write(method, path, body, self.now()))
                return handler(request, **groups)

            self.router.route(method=method, path__regex=pattern).mock(side_effect=wrapped)

        route("PATCH", BASE + r"/issues/comments/(?P<comment_id>\d+)$", self._edit_comment)
        route(
            "GET",
            BASE + r"/issues/(?P<number>\d+)/events$",
            lambda request, number: httpx.Response(200, json=[]),
        )
        route("GET", BASE + r"/commits$", lambda request: httpx.Response(200, json=[]))
        route("POST", BASE + r"/issues/(?P<number>\d+)/labels$", self._add_issue_labels)
        route("DELETE", BASE + r"/issues/(?P<number>\d+)/labels/(?P<name>.+)$", self._remove_issue_label)
        return self

    def _get_contents(self, request: httpx.Request, path: str) -> httpx.Response:
        if path not in self.files:
            return httpx.Response(404, json={"message": "Not Found"})
        text = self.files[path]
        if "raw" in request.headers.get("accept", ""):
            return httpx.Response(200, text=text)
        import base64

        body = {
            "type": "file",
            "path": path,
            "sha": self.blob_sha(path),
            "size": len(text.encode()),
            "encoding": "base64",
            "content": base64.encodebytes(text.encode()).decode(),
        }
        return httpx.Response(200, json=body)

    def _edit_comment(self, request: httpx.Request, comment_id: str) -> httpx.Response:
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == int(comment_id):
                    comment["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})

    def _add_issue_labels(self, request: httpx.Request, number: str) -> httpx.Response:
        names = json.loads(request.content)["labels"]
        self.issue_labels.setdefault(int(number), set()).update(names)
        return httpx.Response(200, json=[{"name": n} for n in sorted(self.issue_labels[int(number)])])

    def _remove_issue_label(self, request: httpx.Request, number: str, name: str) -> httpx.Response:
        self.issue_labels.setdefault(int(number), set()).discard(name.replace("%3A", ":"))
        return httpx.Response(200, json=[])

    def proposal_comments(self) -> list[dict[str, Any]]:
        return [c for c in self.comments.get(CONTROL, []) if "harness:proposal" in c["body"].split("\n")[0]]

    def children(self) -> list[dict[str, Any]]:
        """The sub-issues made by the playbook (not the control issue, the run issue or the epic)."""
        epic = {i["id"] for ids in self.sub_issues.values() for i in [{"id": x} for x in ids]}
        return [i for i in self.created() if i["id"] in epic]

    def label_names(self, issue: dict[str, Any]) -> set[str]:
        return {lab["name"] for lab in issue["labels"]}


def plan_issue(key: str, *deps: str, agent: str = "jules", paths: str = "") -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Do {key}",
        "body": "## Context\nc\n## Scope\ns\n## Acceptance criteria\n- a\n## Out of scope\n- o",
        "acceptance": [f"{key} works"],
        "blocked_by": list(deps),
        "agent": agent,
        "playbook": "plan-build-review",
        "paths": [paths or f"src/{key}/"],
    }


def proposal(*, title: str = "Epic: build the thing") -> dict[str, Any]:
    """a and b can start at once, c needs a, d needs b and c, e is independent (two jules, one codex issue)."""
    return {
        "epic_title": title,
        "epic_body": "The campaign.",
        "issues": [
            plan_issue("a"),
            plan_issue("b", agent="codex"),
            plan_issue("c", "a"),
            plan_issue("d", "b", "c", agent="codex"),
            plan_issue("e"),
        ],
        "notes": "",
    }


class Campaign:
    """One run of decompose-epic: the world, the decomposer, and helpers to tick, approve and look."""

    def __init__(self, env: Env, respx_mock: Any, responses: list[Any]) -> None:
        self.env = env
        self.world = EpicWorld(respx_mock).install()
        self.world.put_plan()
        self.world._add_issue("Epic request", "Please decompose.", [])  # RUN_ISSUE
        self.backend = FakeBackend(list(responses))
        for name in ("jules", "codex"):
            env.adapters.register(FakeAdapter(name))
        self.configs = {REPO: RepoConfig(control_issue=CONTROL, command_users=[OWNER])}
        self.playbook = builtin_playbooks()["decompose-epic"]

    def runner(self, **kwargs: Any):
        return self.env.runner(
            executors=default_registry(review_backend=self.backend), repo_configs=self.configs, **kwargs
        )

    def start(self, runner=None, **kwargs: Any):
        runner = runner or self.runner(**kwargs)
        runner.start_run(
            REPO,
            make_issue(RUN_ISSUE),
            self.playbook,
            agent="jules",
            inputs={"plan_path": PLAN_PATH},
        )
        return runner

    async def advance(self, runner, *, until: str | None = None, limit: int = 60):
        """Tick and drain the outbox until the run is terminal (or in ``until``)."""
        for _ in range(limit):
            await runner.tick()
            await self.env.outbox.drain_once()
            run = self.env.run(RUN_ID)
            if run.status in TERMINAL_STATUSES or run.status == until:
                return run
            self.env.clock.advance(20)
        raise AssertionError(f"run not settled after {limit} ticks: {self.env.run(RUN_ID).status}")

    async def approve(self) -> None:
        plan = await load_plan_doc(self.env.gh, REPO, path=PLAN_PATH)
        record_plan_approval(
            self.env.store,
            self.env.outbox,
            repo=REPO,
            control_issue=CONTROL,
            plan=plan,
            by=OWNER,
            via="cli",
        )
        await self.env.outbox.drain_once()

    def steps(self, node: str):
        return [s for s in self.env.steps(RUN_ID) if s.node_id == node]

    def outcomes(self) -> list[tuple[str, int, str | None]]:
        return [
            (s.node_id, s.visit, s.outcome)
            for s in sorted(self.env.steps(RUN_ID), key=lambda s: (s.started_at or "", s.id))
            if s.status == "done"
        ]


@pytest.fixture
def campaign(env: Env, respx_mock) -> Campaign:
    return Campaign(env, respx_mock, [proposal()])


def issue_rows(world: EpicWorld, gone: set[int] = frozenset()) -> list[IssueRow]:
    """The created sub-issues as the dispatcher sees them (``blocked_by`` as issue numbers)."""
    by_id = {i["id"]: i["number"] for i in world.issues.values()}
    (epic_number,) = world.sub_issues
    rows = []
    for issue in world.children():
        rows.append(
            IssueRow(
                repo=REPO,
                number=issue["number"],
                id=issue["id"],
                title=issue["title"],
                body=issue["body"],
                state="closed" if issue["number"] in gone else "open",
                labels=sorted(world.label_names(issue)),
                parent=epic_number,
                blocked_by=[by_id[b] for b in world.blocked_by.get(issue["number"], [])],
                updated_at="2026-01-01T00:00:00Z",
            )
        )
    return rows


# ---- the playbook ------------------------------------------------------------------------------------------


def test_the_playbook_is_builtin_and_wired_as_designed() -> None:
    pb = builtin_playbooks()["decompose-epic"]
    assert pb.gates == {"plan": "human"} and pb.inputs == {"plan_path": "docs/PLAN.md"}
    assert pb.nodes["plan_gate"].scope == "campaign"  # type: ignore[union-attr]
    assert (pb.nodes["decompose"].create, pb.nodes["create"].create) == (False, True)  # type: ignore[union-attr]
    assert pb.nodes["decompose"].max_visits == 2
    assert pb.edges["plan_gate.rejected"] == "decompose" and pb.edges["plan_gate.approved"] == "create"
    assert pb.edges["create.created"] == "notify" and pb.edges["*.exhausted"] == "escalate"


# ---- the whole loop ----------------------------------------------------------------------------------------


async def test_the_gate_blocks_until_approval_then_issues_are_created_and_the_dispatcher_starts_the_unblocked(
    campaign: Campaign, env: Env
) -> None:
    world, backend = campaign.world, campaign.backend
    runner = campaign.start()

    # 1. A proposal is made and stored on GitHub; the campaign gate waits while the plan is pending.
    run = await campaign.advance(runner, until="needs-you")
    assert run.status == "needs-you"
    assert [s.outcome for s in campaign.steps("decompose")] == ["proposed"]
    gate_step = campaign.steps("plan_gate")[0]
    assert gate_step.status == "waiting" and gate_step.output["plan_status"] == "pending"  # type: ignore[index]
    assert "needs approval" in gate_step.output["reason"]  # type: ignore[index]
    stored = read_proposals(world.comments[CONTROL])
    assert [(s.run_id, s.status) for s in stored] == [(RUN_ID, "proposed")]
    assert len(backend.calls) == 1

    # Waiting longer changes nothing: no issue is created, the model is not asked again.
    for _ in range(4):
        env.clock.advance(30)
        await runner.tick()
        await env.outbox.drain_once()
    assert env.run(RUN_ID).status == "needs-you"
    assert set(world.issues) == {CONTROL, RUN_ISSUE} and world.writes_to("POST", "/issues") == []
    assert len(backend.calls) == 1 and campaign.steps("create") == []
    assert "harness:needs-you" in world.issue_labels[CONTROL]
    assert sum("plan approval requested" in b for b in map(lambda c: c["body"], world.comments[CONTROL])) == 1

    # 2. The plan is approved (the ledger record that the label, a merged PR or `harness plan approve` write).
    await campaign.approve()
    run = await campaign.advance(runner)
    assert run.status == "done"
    assert campaign.outcomes() == [
        ("decompose", 1, "proposed"),
        ("plan_gate", 1, "approved"),
        ("create", 1, "created"),
        ("notify", 1, "sent"),
    ]
    assert len(backend.calls) == 1  # `create` reused the stored proposal
    create = campaign.steps("create")[0]
    assert create.output["reused"] is True and create.output["status"] == "created"  # type: ignore[index]
    assert [(s.run_id, s.status) for s in read_proposals(world.comments[CONTROL])] == [(RUN_ID, "created")]

    # 3. The epic, five sub-issues, their links and labels exist.
    result = create.output["created"]  # type: ignore[index]
    epic = world.issues[result["epic_number"]]
    assert epic["title"] == "Epic: build the thing"
    children = {i["title"]: i for i in world.children()}
    assert sorted(children) == [f"Do {k}" for k in "abcde"]
    assert sorted(world.sub_issues[epic["number"]]) == sorted(i["id"] for i in children.values())
    number = {k: result["issues"][k] for k in "abcde"}
    assert world.blocked_by == {
        number["c"]: [world.issues[number["a"]]["id"]],
        number["d"]: [world.issues[number["b"]]["id"], world.issues[number["c"]]["id"]],
    }
    for key in "abe":
        assert {"harness:planned", "harness:ready"} <= world.label_names(children[f"Do {key}"])
    for key in "cd":
        names = world.label_names(children[f"Do {key}"])
        assert {"harness:planned", "harness:blocked"} <= names and "harness:ready" not in names
    assert {"agent:jules", "playbook:plan-build-review"} <= world.label_names(children["Do a"])
    assert "agent:codex" in world.label_names(children["Do b"])
    note = campaign.steps("notify")[0].output["message"]  # type: ignore[index]
    assert note == f"Created 5 issues under epic #{epic['number']} for #{RUN_ISSUE}"
    assert any(note in c["body"] for c in world.comments[RUN_ISSUE])

    # 4. The dispatcher picks exactly the unblocked issues, per agent cap.
    rows = issue_rows(world)
    dispatcher = Dispatcher(store=env.store, caps={"jules": 5, "codex": 5})
    reasons = {d.issue: d.reason for d in dispatcher.plan(rows, [])}
    assert reasons == {
        number["a"]: "started",
        number["b"]: "started",
        number["c"]: "blocked",
        number["d"]: "blocked",
        number["e"]: "started",
    }
    capped = Dispatcher(store=env.store, caps={"jules": 1, "codex": 1}).plan(rows, [])
    assert {d.issue: d.reason for d in capped} == {
        number["a"]: "started",
        number["b"]: "started",
        number["c"]: "blocked",
        number["d"]: "blocked",
        number["e"]: "cap",
    }
    assert {d.issue: d.agent for d in capped}[number["b"]] == "codex"
    # When `a` is closed (the issue cache knows it), `c` is unblocked; `d` still waits for `b` and `c`.
    all_rows = issue_rows(world, gone={number["a"]})
    for row in all_rows:
        upsert_issue(env.store, row)
    still_open = [r for r in all_rows if r.state == "open"]
    after_a = Dispatcher(store=env.store, caps={"jules": 5, "codex": 5}).plan(still_open, [])
    assert {d.issue: d.reason for d in after_a} == {
        number["b"]: "started",
        number["c"]: "started",
        number["d"]: "blocked",
        number["e"]: "started",
    }


# ---- a rejected plan ---------------------------------------------------------------------------------------


@pytest.fixture
def reject(monkeypatch: pytest.MonkeyPatch):
    """Make the campaign gate answer REJECTED ``n`` times, then the real check. No signal produces REJECTED yet
    (the label, the merged PR and the CLI only approve), so the outcome is injected at the approval check."""
    state = {"left": 0}
    real = gate.check_plan_approval

    async def check(*args: Any, **kwargs: Any) -> PlanApproval:
        if state["left"] > 0:
            state["left"] -= 1
            return PlanApproval(PlanApprovalStatus.REJECTED, note=REJECTION)
        return await real(*args, **kwargs)

    monkeypatch.setattr(gate, "check_plan_approval", check)

    def times(n: int) -> None:
        state["left"] = n

    return times


async def test_a_rejected_plan_goes_back_to_decompose_with_the_message_as_guidance(
    env: Env, respx_mock, reject
) -> None:
    campaign = Campaign(
        env, respx_mock, [proposal(title="Epic: first try"), proposal(title="Epic: second try")]
    )
    reject(1)
    runner = campaign.start()

    run = await campaign.advance(runner, until="needs-you")
    assert run.status == "needs-you"
    assert campaign.outcomes() == [
        ("decompose", 1, "proposed"),
        ("plan_gate", 1, "rejected"),
        ("decompose", 2, "proposed"),
    ]
    # The rejection message reached the decomposer; the first call had no guidance.
    first, second = campaign.backend.calls
    assert REJECTION not in first["prompt"]
    assert f"The previous proposal was rejected: {REJECTION}" in second["prompt"]
    assert campaign.steps("plan_gate")[0].output["correction"] == REJECTION  # type: ignore[index]
    # The new proposal replaced the stored one (same comments, edited in place), and it is what gets created.
    assert campaign.steps("decompose")[1].output["proposal"]["epic_title"] == "Epic: second try"  # type: ignore[index]
    assert campaign.steps("decompose")[1].output["reused"] is False  # type: ignore[index]
    assert len(campaign.world.proposal_comments()) == 1

    await campaign.approve()
    run = await campaign.advance(runner)
    assert run.status == "done" and len(campaign.backend.calls) == 2
    (epic_number,) = campaign.world.sub_issues
    assert campaign.world.issues[epic_number]["title"] == "Epic: second try"
    assert len(campaign.world.children()) == 5


async def test_repeated_rejections_stop_at_max_visits_and_escalate(env: Env, respx_mock, reject) -> None:
    campaign = Campaign(env, respx_mock, [proposal(title="Epic: one"), proposal(title="Epic: two")])
    reject(99)
    runner = campaign.start()

    run = await campaign.advance(runner, until="needs-you")
    assert run.status == "needs-you"
    assert campaign.outcomes() == [
        ("decompose", 1, "proposed"),
        ("plan_gate", 1, "rejected"),
        ("decompose", 2, "proposed"),
        ("plan_gate", 2, "rejected"),
    ]
    assert len(campaign.steps("decompose")) == 2  # a third visit is not made
    escalate = campaign.steps("escalate")[0]
    assert escalate.status == "waiting" and "plan_gate" in escalate.output["reason"]  # type: ignore[index]
    assert REJECTION in escalate.output["reason"]  # type: ignore[index]
    assert [e["data"]["node"] for e in env.events("exhausted")] == ["decompose"]
    assert len(campaign.backend.calls) == 2
    assert set(campaign.world.issues) == {CONTROL, RUN_ISSUE} and campaign.steps("create") == []


# ---- a crash while the issues are created ------------------------------------------------------------------


async def test_a_crash_between_two_created_issues_resumes_without_duplicates(
    campaign: Campaign, env: Env
) -> None:
    world = campaign.world
    runner = campaign.start()
    await campaign.advance(runner, until="needs-you")
    await campaign.approve()

    world.crash_after(
        "POST", "/issues", 3
    )  # the epic and the first issue exist, the second one is made but unseen
    with pytest.raises(Crash):
        await campaign.advance(runner)
    assert len(world.sub_issues) == 0 and len(world.issues) == 5  # control, run issue, epic, two issues
    world.crash_at = None

    env.clock.advance(120)
    restarted = campaign.runner()  # a new process on the same database
    run = await campaign.advance(restarted)
    assert run.status == "done"

    titles = [i["title"] for i in world.created() if i["number"] != RUN_ISSUE]
    assert sorted(titles) == [
        "Do a",
        "Do b",
        "Do c",
        "Do d",
        "Do e",
        "Epic: build the thing",
    ]  # no duplicates
    assert len(world.writes_to("POST", "/issues")) == 6  # the unseen issue was found again, not created twice
    assert len(world.writes_to("POST", "/sub_issues")) == 5
    assert len(world.writes_to("POST", "/dependencies/blocked_by")) == 3
    assert len(world.blocked_by) == 2 and sum(len(v) for v in world.blocked_by.values()) == 3
    assert len(backend_calls := campaign.backend.calls) == 1, backend_calls  # the model was asked once
    assert len(world.proposal_comments()) == 1
    assert [(n, o) for n, _, o in campaign.outcomes()] == [
        ("decompose", "proposed"),
        ("plan_gate", "approved"),
        ("create", "created"),
        ("notify", "sent"),
    ]


# ---- the Planner's edited draft comes first ----------------------------------------------------------------

DRAFT_RUN = f"{REPO}#planner-abc123"


def edited(title: str = "Epic: edited by hand") -> ProposedPlan:
    return ProposedPlan.model_validate(
        {
            "epic_title": title,
            "epic_body": "Edited in the Planner.",
            "issues": [plan_issue("x"), plan_issue("y", "x"), plan_issue("z", "x", agent="codex")],
        }
    )


async def store_draft(
    campaign: Campaign,
    plan: ProposedPlan,
    *,
    status: str = "ready",
    sha: str | None = None,
    run: str = DRAFT_RUN,
) -> None:
    await save_proposal(
        campaign.env.store,
        campaign.env.outbox,
        repo=REPO,
        control_issue=CONTROL,
        run_id=run,
        plan_sha=sha or campaign.world.blob_sha(),
        proposal=plan,
        status=status,
        rev=4,
        meta={"source": PLAN_PATH},
    )


async def test_an_edited_planner_draft_for_the_same_plan_is_used_and_the_model_is_never_called(
    env: Env, respx_mock
) -> None:
    campaign = Campaign(env, respx_mock, [])  # the backend has nothing to say: any call would fail the run
    await store_draft(campaign, edited())
    runner = campaign.start()

    run = await campaign.advance(runner, until="needs-you")
    assert run.status == "needs-you"
    proposed = campaign.steps("decompose")[0]
    assert proposed.outcome == "proposed" and proposed.output["draft"] == DRAFT_RUN  # type: ignore[index]
    assert proposed.output["reused"] is True and proposed.output["issue_count"] == 3  # type: ignore[index]
    assert proposed.output["proposal"]["epic_title"] == "Epic: edited by hand"  # type: ignore[index]
    # The draft is copied to this run, so the run has a proposal of its own and the draft stays as it was.
    stored = {s.run_id: s for s in read_proposals(campaign.world.comments[CONTROL])}
    assert set(stored) == {DRAFT_RUN, RUN_ID} and stored[RUN_ID].proposal == stored[DRAFT_RUN].proposal
    assert stored[DRAFT_RUN].status == "ready" and stored[DRAFT_RUN].rev == 4

    await campaign.approve()
    run = await campaign.advance(runner)
    assert run.status == "done" and campaign.backend.calls == []
    (epic_number,) = campaign.world.sub_issues
    assert campaign.world.issues[epic_number]["title"] == "Epic: edited by hand"
    assert sorted(i["title"] for i in campaign.world.children()) == ["Do x", "Do y", "Do z"]
    # Created under the Planner's own key (the plan's content), like its Approve button would have done.
    key = plan_run_key(edited())
    assert (
        campaign.world.issues[epic_number]["body"]
        .rstrip()
        .endswith(f"<!-- harness:planned run={key} key=@epic -->")
    )
    assert read_proposals(campaign.world.comments[CONTROL])[-1].status in ("created", "ready")


async def test_a_draft_that_was_edited_again_before_the_issues_are_created_wins(env: Env, respx_mock) -> None:
    campaign = Campaign(env, respx_mock, [])
    await store_draft(campaign, edited("Epic: draft one"))
    runner = campaign.start()
    await campaign.advance(runner, until="needs-you")

    await save_proposal(  # the user keeps editing in the Planner while the gate waits
        env.store,
        env.outbox,
        repo=REPO,
        control_issue=CONTROL,
        run_id=DRAFT_RUN,
        plan_sha=campaign.world.blob_sha(),
        proposal=edited("Epic: draft two"),
        status="ready",
        rev=5,
    )
    await campaign.approve()
    assert (await campaign.advance(runner)).status == "done"
    (epic_number,) = campaign.world.sub_issues
    assert campaign.world.issues[epic_number]["title"] == "Epic: draft two"
    assert campaign.backend.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "drafting"},
        {"status": "invalid"},
        {"sha": "f" * 40},  # a draft of an older plan
        {"run": "o/r#7@3"},  # another run's proposal, not a Planner draft
        {"run": "other/repo#planner-abc"},
    ],
    ids=["drafting", "invalid", "other-plan-sha", "not-a-planner-run", "other-repo"],
)
async def test_drafts_that_cannot_be_used_are_ignored_and_the_model_is_asked(
    env: Env, respx_mock, kwargs: dict[str, str]
) -> None:
    campaign = Campaign(env, respx_mock, [proposal()])
    await store_draft(campaign, edited(), **kwargs)
    runner = campaign.start()
    await campaign.advance(runner, until="needs-you")
    step = campaign.steps("decompose")[0]
    assert len(campaign.backend.calls) == 1
    assert "draft" not in step.output and step.output["reused"] is False  # type: ignore[operator, index]
    assert step.output["proposal"]["epic_title"] == "Epic: build the thing"  # type: ignore[index]


async def test_a_draft_that_the_planner_already_created_is_not_created_twice(env: Env, respx_mock) -> None:
    campaign = Campaign(env, respx_mock, [])
    plan = edited()
    await store_draft(campaign, plan, status="created")
    first = await materialize(  # what the Planner's Approve did
        plan,
        MaterializeContext(
            repo=REPO,
            gh=env.gh,
            outbox=env.outbox,
            store=env.store,
            control_issue=CONTROL,
            run_key=plan_run_key(plan),
            agents=["jules", "codex"],
            playbooks=["plan-build-review"],
        ),
    )
    posts = len(campaign.world.writes_to("POST", "/issues"))
    assert posts == 4 and first.created == ["x", "y", "z"]  # the epic and three issues

    runner = campaign.start()
    await campaign.advance(runner, until="needs-you")
    await campaign.approve()
    assert (await campaign.advance(runner)).status == "done"
    assert len(campaign.world.writes_to("POST", "/issues")) == posts  # nothing was made again
    assert len(campaign.world.writes_to("POST", "/sub_issues")) == 3
    created = campaign.steps("create")[0].output["created"]  # type: ignore[index]
    assert created["created"] == [] and created["reused"] == ["x", "y", "z"]
    assert created["epic_number"] == first.epic_number
