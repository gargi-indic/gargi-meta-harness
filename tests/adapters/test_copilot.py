import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from harness.adapters.base import (
    AdapterConfigError,
    AdapterRegistry,
    AgentAdapter,
    AgentState,
    SessionRef,
    StartRequest,
    UnsupportedOperation,
)
from harness.adapters.copilot import (
    COPILOT_ASSIGNEE,
    FAILURE_MARKER,
    AssigneeTransport,
    CopilotAdapter,
    CopilotAgentConfig,
    TasksTransport,
    make_transport,
)
from harness.config import RepoConfig, parse_repo_config
from harness.github.client import GitHub
from harness.playbook.load import load_playbook
from harness.playbook.validate import validate

S = AgentState
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot"
REPO = "o/r"
KEY = "o/r#52@1/main"
TOKEN = "ghp_usertoken0123456789abcdef"
API = "https://api.github.com"
MARKER = f"<!-- harness:copilot key={KEY} -->"

pytestmark = pytest.mark.respx(base_url=API, assert_all_called=False, assert_all_mocked=True)


def fx(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    assert data["_synthetic"] is True
    return data


def test_every_fixture_is_marked_synthetic() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert files
    for path in files:
        assert json.loads(path.read_text())["_synthetic"] is True, path.name


def request_for(key: str = KEY, brief: str = "THE BRIEF") -> StartRequest:
    return StartRequest(
        repo=REPO,
        base_branch="develop",
        branch="harness/52-x",
        issue=52,
        title="T",
        brief=brief,
        mode="direct",
        idempotency_key=key,
    )


@pytest.fixture
async def gh() -> Any:
    client = GitHub(TOKEN, write_spacing=0.0)
    yield client
    await client.aclose()


def make_adapter(gh: GitHub, transport: str = "assignee", **kw: Any) -> CopilotAdapter:
    return CopilotAdapter(gh=gh, config=CopilotAgentConfig(transport=transport, **kw), token=TOKEN)  # type: ignore[arg-type]


def ref_with(**data: Any) -> SessionRef:
    return SessionRef(
        agent="copilot", id="52", url="https://github.com/o/r/issues/52", data={"repo": REPO, **data}
    )


def mock_basics(
    respx_mock: respx.MockRouter,
    *,
    issue: str = "issue_unassigned",
    pulls: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, respx.Route]:
    return {
        "user": respx_mock.get("/user").mock(return_value=Response(200, json=fx("user"))),
        "issue": respx_mock.get("/repos/o/r/issues/52").mock(return_value=Response(200, json=fx(issue))),
        "pulls": respx_mock.get("/repos/o/r/pulls").mock(return_value=Response(200, json=pulls or [])),
        "comments": respx_mock.get("/repos/o/r/issues/52/comments").mock(
            return_value=Response(200, json=comments or [])
        ),
        "pr_comments": respx_mock.get("/repos/o/r/issues/77/comments").mock(
            return_value=Response(200, json=[])
        ),
        "post_comment": respx_mock.post("/repos/o/r/issues/52/comments").mock(
            return_value=Response(201, json={"id": 1})
        ),
        "assign": respx_mock.post("/repos/o/r/issues/52/assignees").mock(
            return_value=Response(201, json=fx("issue_assigned"))
        ),
    }


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# -- start / find ----------------------------------------------------------------------------------------


async def test_start_posts_exact_assignee_payload_and_marker(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    routes = mock_basics(respx_mock)
    ref = await make_adapter(gh).start(request_for())
    assert routes["assign"].call_count == 1
    assert body_of(routes["assign"]) == {
        "assignees": [COPILOT_ASSIGNEE],
        "agent_assignment": {
            "target_repo": "o/r",
            "base_branch": "develop",
            "custom_instructions": "THE BRIEF",
        },
    }
    assert routes["post_comment"].call_count == 1
    assert MARKER in body_of(routes["post_comment"])["body"]
    assert (ref.agent, ref.id, ref.data["repo"], ref.data["session"]) == ("copilot", "52", REPO, "52")


async def test_start_is_idempotent_when_marker_exists(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    routes = mock_basics(respx_mock, comments=[{"id": 1, "body": MARKER}])
    ref = await make_adapter(gh).start(request_for())
    assert ref.id == "52"
    assert routes["assign"].call_count == 0 and routes["post_comment"].call_count == 0


async def test_start_adopts_existing_assignee_without_post(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    routes = mock_basics(respx_mock, issue="issue_assigned")
    ref = await make_adapter(gh).start(request_for())
    assert routes["assign"].call_count == 0
    assert routes["post_comment"].call_count == 1  # the marker is still recorded
    assert ref.id == "52"


async def test_start_adopts_existing_copilot_pr_without_post(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    routes = mock_basics(respx_mock, pulls=[fx("pull_other"), fx("pull_wip_draft")])
    await make_adapter(gh).start(request_for())
    assert routes["assign"].call_count == 0
    assert routes["post_comment"].call_count == 1


async def test_find_after_restart_uses_only_github(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, comments=[{"id": 1, "body": f"{MARKER}\n<!-- harness:copilot-session id=52 -->"}])
    adapter = make_adapter(gh)
    ref = await adapter.find(KEY)
    assert ref is not None and ref.id == "52" and ref.data["repo"] == REPO
    assert await adapter.find("o/r#52@2/main") is None
    assert await adapter.find("not-a-run-key") is None


# -- poll ------------------------------------------------------------------------------------------------


async def test_poll_queued_and_working_are_working(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, issue="issue_assigned")
    snap = await make_adapter(gh).poll(ref_with())
    assert snap.state is S.WORKING and snap.poll_after == 60.0 and snap.pr is None


async def test_poll_wip_draft_is_working(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, issue="issue_assigned", pulls=[fx("pull_wip_draft")])
    snap = await make_adapter(gh).poll(ref_with())
    assert snap.state is S.WORKING and snap.pr is None


async def test_poll_wip_title_non_draft_is_working(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, issue="issue_assigned", pulls=[fx("pull_wip_title")])
    assert (await make_adapter(gh).poll(ref_with())).state is S.WORKING


async def test_poll_wip_draft_with_finished_session_is_ready(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    mock_basics(respx_mock, issue="issue_unassigned", pulls=[fx("pull_wip_draft")])
    snap = await make_adapter(gh).poll(ref_with())
    assert snap.state is S.PR_READY and snap.pr is not None and snap.pr.number == 77


async def test_poll_ready_pr(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, issue="issue_assigned", pulls=[fx("pull_other"), fx("pull_ready")])
    snap = await make_adapter(gh).poll(ref_with())
    assert snap.state is S.PR_READY and snap.pr is not None
    assert (snap.pr.number, snap.pr.url, snap.pr.head_sha) == (77, "https://github.com/o/r/pull/77", "abc123")


async def test_poll_pr_found_after_restart_with_empty_data(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, pulls=[fx("pull_ready")])
    ref = SessionRef(agent="copilot", id="52", url="https://github.com/o/r/issues/52")
    snap = await make_adapter(gh).poll(ref)
    assert snap.state is S.PR_READY and snap.pr is not None and snap.pr.number == 77
    assert ref.data["pr"] == 77


async def test_poll_uses_cached_pr_number(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    routes = mock_basics(respx_mock)
    pull = respx_mock.get("/repos/o/r/pulls/77").mock(return_value=Response(200, json=fx("pull_ready")))
    snap = await make_adapter(gh).poll(ref_with(pr=77))
    assert snap.state is S.PR_READY and pull.call_count == 1 and routes["pulls"].call_count == 0


async def test_poll_failure_marker_comment_is_failed(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, issue="issue_assigned", comments=[fx("comment_failure")])
    snap = await make_adapter(gh).poll(ref_with())
    assert snap.state is S.FAILED and snap.error and FAILURE_MARKER in snap.error


async def test_poll_marker_text_from_a_human_is_not_a_failure(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    human = {**fx("comment_failure"), "user": {"login": "someone"}}
    mock_basics(respx_mock, issue="issue_assigned", comments=[human])
    assert (await make_adapter(gh).poll(ref_with())).state is S.WORKING


async def test_poll_closed_unmerged_pr_is_failed(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, pulls=[{**fx("pull_ready"), "state": "closed"}])
    snap = await make_adapter(gh).poll(ref_with())
    assert snap.state is S.FAILED and snap.error


async def test_poll_failed_session_from_transport(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    mock_basics(respx_mock, pulls=[fx("pull_wip_draft")])
    respx_mock.get("/agents/repos/o/r/tasks/task_abc").mock(
        return_value=Response(200, json=fx("task_failed"))
    )
    adapter = make_adapter(gh, transport="tasks")
    snap = await adapter.poll(ref_with(session="task_abc"))
    assert snap.state is S.FAILED and snap.error


# -- send / approve_plan / cancel ------------------------------------------------------------------------


async def test_send_posts_one_comment_even_when_called_twice(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    routes = mock_basics(respx_mock, pulls=[fx("pull_ready")])
    posted: list[dict[str, Any]] = []

    def comments(request: httpx.Request) -> Response:
        return Response(200, json=posted)

    def post(request: httpx.Request) -> Response:
        posted.append({"id": len(posted) + 1, "body": json.loads(request.content)["body"]})
        return Response(201, json={})

    routes["comments"].mock(side_effect=comments)
    routes["post_comment"].mock(side_effect=post)
    pr_comments = respx_mock.get("/repos/o/r/issues/77/comments").mock(side_effect=comments)
    pr_post = respx_mock.post("/repos/o/r/issues/77/comments").mock(side_effect=post)
    adapter = make_adapter(gh)
    await adapter.send(ref_with(), "please fix X")
    await adapter.send(ref_with(), "please fix X")
    assert pr_post.call_count == 1 and routes["post_comment"].call_count == 0 and pr_comments.call_count >= 2
    assert len(posted) == 1
    assert posted[0]["body"].startswith("@copilot please fix X")
    assert "<!-- harness:send ref=52 h=" in posted[0]["body"] and "n=0 -->" in posted[0]["body"]
    await adapter.send(ref_with(send_n=1), "please fix X")  # a new counter is a new message
    assert len(posted) == 2


async def test_send_goes_to_the_issue_when_there_is_no_pr(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    routes = mock_basics(respx_mock)
    await make_adapter(gh).send(ref_with(), "hello")
    assert routes["post_comment"].call_count == 1
    assert body_of(routes["post_comment"])["body"].startswith("@copilot hello")


async def test_approve_plan_raises_unsupported(gh: GitHub) -> None:
    with pytest.raises(UnsupportedOperation):
        await make_adapter(gh).approve_plan(ref_with())
    assert issubclass(UnsupportedOperation, NotImplementedError)


async def test_cancel_removes_assignee_closes_pr_and_is_idempotent(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    state = {"assigned": True, "open": True}
    comments: list[dict[str, Any]] = []
    respx_mock.get("/repos/o/r/issues/52").mock(
        side_effect=lambda r: Response(
            200, json=fx("issue_assigned" if state["assigned"] else "issue_unassigned")
        )
    )
    respx_mock.get("/repos/o/r/pulls").mock(
        side_effect=lambda r: Response(
            200, json=[{**fx("pull_wip_draft"), "state": "open" if state["open"] else "closed"}]
        )
    )
    respx_mock.get("/repos/o/r/issues/77/comments").mock(side_effect=lambda r: Response(200, json=comments))

    def unassign(request: httpx.Request) -> Response:
        state["assigned"] = False
        return Response(200, json={})

    def comment(request: httpx.Request) -> Response:
        comments.append({"body": json.loads(request.content)["body"]})
        return Response(201, json={})

    def close(request: httpx.Request) -> Response:
        state["open"] = False
        return Response(200, json={})

    delete = respx_mock.delete("/repos/o/r/issues/52/assignees").mock(side_effect=unassign)
    post = respx_mock.post("/repos/o/r/issues/77/comments").mock(side_effect=comment)
    patch = respx_mock.patch("/repos/o/r/pulls/77").mock(side_effect=close)
    adapter = make_adapter(gh)

    await adapter.cancel(ref_with())
    assert body_of(delete) == {"assignees": [COPILOT_ASSIGNEE]}
    assert post.call_count == 1 and patch.call_count == 1 and body_of(patch) == {"state": "closed"}
    await adapter.cancel(ref_with())
    assert (delete.call_count, post.call_count, patch.call_count) == (1, 1, 1)


# -- token -----------------------------------------------------------------------------------------------


def test_installation_token_is_refused_at_construction(gh: GitHub) -> None:
    with pytest.raises(AdapterConfigError, match="copilot requires a user token"):
        CopilotAdapter(gh=gh, config=CopilotAgentConfig(), token="ghs_installationtoken123456")


async def test_non_user_account_is_refused_on_health(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    respx_mock.get("/user").mock(return_value=Response(200, json=fx("user_bot")))
    with pytest.raises(AdapterConfigError, match=r"copilot requires a user token \(gh auth token\)"):
        await make_adapter(gh).health()


async def test_start_refuses_when_user_check_fails(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    routes = mock_basics(respx_mock)
    routes["user"].mock(return_value=Response(403, json={"message": "Resource not accessible"}))
    with pytest.raises(AdapterConfigError):
        await make_adapter(gh).start(request_for())
    assert routes["assign"].call_count == 0


async def test_token_never_appears_in_logs_or_errors(
    respx_mock: respx.MockRouter, gh: GitHub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    routes = mock_basics(respx_mock)
    await make_adapter(gh).start(request_for())
    routes["user"].mock(return_value=Response(401, json={"message": f"bad credentials {TOKEN}"}))
    with pytest.raises(AdapterConfigError) as err:
        await make_adapter(gh).health()
    assert TOKEN not in caplog.text and TOKEN not in str(err.value) and TOKEN not in repr(err.value.__cause__)


# -- validator / registry / config -----------------------------------------------------------------------


BUILTIN = (
    Path(__file__).resolve().parents[2] / "harness" / "playbooks" / "plan-build-review.yaml"
).read_text()
TEMPLATED = 'agent: "{{ issue.agent }}"'


async def test_validator_rejects_plan_action_for_copilot(gh: GitHub) -> None:
    caps = {"copilot": make_adapter(gh).caps.names()}
    assert caps["copilot"] == {"message", "pr"}
    pinned = load_playbook(BUILTIN.replace(TEMPLATED, "agent: copilot"))
    issues = validate(pinned, agent_caps=caps)
    assert [(i.code, i.message) for i in issues] == [
        ("capability", "Agent copilot missing capability 'plan'")
    ]


async def test_plan_auto_gate_with_copilot_is_accepted_unless_the_plan_node_pins_it(gh: GitHub) -> None:
    """Exact behaviour: the built-in playbook (``gates.plan: auto``) validates clean with copilot registered.

    Its plan node's agent is templated (``{{ issue.agent }}``) and the validator skips templated agents, so the
    capability error only appears when the plan node spells ``agent: copilot`` out (previous test); a literal
    copilot ``implement`` node needs no plan capability and is accepted too.
    """
    caps = {"copilot": make_adapter(gh).caps.names()}
    assert load_playbook(BUILTIN).gates["plan"] == "auto"
    assert validate(load_playbook(BUILTIN), agent_caps=caps) == []
    implement = BUILTIN.replace("action: implement}", "action: implement, agent: copilot}")
    assert "agent: copilot" in implement
    assert validate(load_playbook(implement), agent_caps=caps) == []


async def test_registry_exposes_copilot_caps(gh: GitHub) -> None:
    registry = AdapterRegistry()
    adapter = make_adapter(gh, max_concurrency=3)
    registry.register(adapter)
    assert isinstance(adapter, AgentAdapter) and adapter.name == "copilot"
    assert adapter.caps.max_concurrency == 3 and not adapter.caps.plan_approval
    assert registry.caps_by_name() == {"copilot": {"message", "pr"}}


async def test_transport_selection_by_config(gh: GitHub) -> None:
    assert isinstance(make_transport(CopilotAgentConfig(), gh), AssigneeTransport)
    assert isinstance(make_transport(CopilotAgentConfig(transport="tasks"), gh), TasksTransport)
    assert isinstance(make_adapter(gh, transport="tasks")._transport, TasksTransport)
    with pytest.raises(ValueError):
        CopilotAgentConfig(transport="bogus")  # type: ignore[arg-type]


def test_repo_config_with_copilot_transport_loads() -> None:
    config = parse_repo_config("agents:\n  copilot:\n    transport: tasks\n  jules: {}\n")
    entry = config.agents["copilot"]
    assert isinstance(entry, CopilotAgentConfig) and entry.transport == "tasks"
    assert (
        RepoConfig.model_validate(config.model_dump()).agents["copilot"].model_dump()["transport"] == "tasks"
    )
    assert (
        parse_repo_config("agents:\n  copilot: {}\n").agents["copilot"].model_dump()["transport"]
        == "assignee"
    )
    with pytest.raises(ValueError):
        parse_repo_config("agents:\n  jules:\n    transport: tasks\n")
    with pytest.raises(ValueError):
        parse_repo_config("agents:\n  copilot:\n    transport: nope\n")


# -- tasks transport (PREVIEW; synthetic fixtures only) --------------------------------------------------


async def test_tasks_start_and_states(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    routes = mock_basics(respx_mock)
    created = respx_mock.post("/agents/repos/o/r/tasks").mock(
        return_value=Response(201, json=fx("task_created"))
    )
    ref = await make_adapter(gh, transport="tasks").start(request_for())
    assert created.call_count == 1 and routes["assign"].call_count == 0
    assert body_of(created)["problem_statement"] == "THE BRIEF" and body_of(created)["base_ref"] == "develop"
    assert ref.id == "52" and ref.data["session"] == "task_abc"
    assert "id=task_abc" in body_of(routes["post_comment"])["body"]

    transport = TasksTransport(gh)
    for name, expected in (
        ("task_working", "working"),
        ("task_completed", "done"),
        ("task_failed", "failed"),
    ):
        respx_mock.get("/agents/repos/o/r/tasks/task_abc").mock(return_value=Response(200, json=fx(name)))
        assert await transport.agent_state(repo=REPO, issue=52, session="task_abc") == expected
    assert await transport.existing(repo=REPO, issue=52) is None


async def test_tasks_find_recovers_the_task_id_from_the_marker(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    mock_basics(
        respx_mock, comments=[{"id": 1, "body": f"{MARKER}\n<!-- harness:copilot-session id=task_abc -->"}]
    )
    ref = await make_adapter(gh, transport="tasks").find(KEY)
    assert ref is not None and ref.data["session"] == "task_abc"


async def test_tasks_parser_is_tolerant_and_rejects_missing_id(
    respx_mock: respx.MockRouter, gh: GitHub
) -> None:
    transport = TasksTransport(gh)
    assert transport._task_id({"task_id": 7}) == "7"
    assert transport._task_id({"id": "x"}) == "x"
    assert transport._task_id({"unknown": 1}) is None and transport._task_id([]) is None
    respx_mock.post("/agents/repos/o/r/tasks").mock(return_value=Response(201, json={"unknown": 1}))
    with pytest.raises(Exception, match="no task id"):
        await transport.start(repo=REPO, issue=52, base="main", instructions="x")
    respx_mock.get("/agents/repos/o/r/tasks/t").mock(return_value=Response(200, json={"state": "weird"}))
    assert await transport.agent_state(repo=REPO, issue=52, session="t") == "working"


# -- conformance against a scripted fake transport -------------------------------------------------------


class ScriptedTransport:
    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.calls: list[str] = []
        self.assigned = False

    async def existing(self, *, repo: str, issue: int) -> str | None:
        return str(issue) if self.assigned else None

    async def start(self, *, repo: str, issue: int, base: str, instructions: str) -> str:
        self.calls.append("start")
        self.assigned = True
        return "scripted-1"

    async def agent_state(self, *, repo: str, issue: int, session: str) -> Any:
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def stop(self, *, repo: str, issue: int, session: str) -> None:
        self.calls.append("stop")


async def test_conformance_with_scripted_transport(respx_mock: respx.MockRouter, gh: GitHub) -> None:
    posted: list[str] = []
    mock_basics(respx_mock)
    respx_mock.get("/repos/o/r/issues/52/comments").mock(
        side_effect=lambda r: Response(200, json=[{"id": i, "body": b} for i, b in enumerate(posted)])
    )
    respx_mock.post("/repos/o/r/issues/52/comments").mock(
        side_effect=lambda r: (posted.append(json.loads(r.content)["body"]), Response(201, json={}))[1]
    )
    pulls = respx_mock.get("/repos/o/r/pulls").mock(return_value=Response(200, json=[]))
    respx_mock.get("/repos/o/r/pulls/77").mock(return_value=Response(200, json=fx("pull_ready")))
    respx_mock.post("/repos/o/r/issues/77/comments").mock(return_value=Response(201, json={}))
    closed = respx_mock.patch("/repos/o/r/pulls/77").mock(return_value=Response(200, json={}))
    transport = ScriptedTransport(["queued", "working", "done"])
    adapter = CopilotAdapter(gh=gh, config=CopilotAgentConfig(), transport=transport, token=TOKEN)  # type: ignore[arg-type]
    assert isinstance(adapter, AgentAdapter) and adapter.caps.names() == {"message", "pr"}

    ref = await adapter.start(request_for())
    again = await adapter.start(request_for())  # idempotent: the marker exists now
    assert again == ref and transport.calls == ["start"]
    assert await adapter.find(KEY) == ref
    respx_mock.get("/repos/o/r/issues/99/comments").mock(return_value=Response(200, json=[]))
    assert await adapter.find("o/r#99@1/main") is None

    assert (await adapter.poll(ref)).state is S.WORKING
    assert (await adapter.poll(ref)).state is S.WORKING
    pulls.mock(return_value=Response(200, json=[fx("pull_ready")]))
    final = await adapter.poll(ref)
    assert final.state is S.PR_READY and final.pr is not None and final.pr.head_sha == "abc123"

    with pytest.raises(UnsupportedOperation):
        await adapter.approve_plan(ref)
    await adapter.cancel(ref)
    assert transport.calls[-1] == "stop" and closed.call_count == 1


async def test_app_registers_copilot_only_on_an_enabled_repo_config_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.app import build_app
    from harness.config import GlobalConfig, RepoEntry
    from harness.store.repo import upsert_repo

    for name in ("JULES_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name="o/r")])

    first = build_app(cfg, gh=GitHub("t"))
    assert "copilot" not in first.adapters.names()
    stored = RepoConfig(agents={"copilot": CopilotAgentConfig(transport="tasks", max_concurrency=2)})
    upsert_repo(first.store, "o/r", "main", stored.model_dump())
    await first.aclose()

    app = build_app(cfg, gh=GitHub("t"))
    try:
        adapter = app.adapters.get("copilot")
        assert isinstance(adapter, CopilotAdapter) and adapter.caps.max_concurrency == 2
        assert isinstance(adapter._transport, TasksTransport)
    finally:
        await app.aclose()

    disabled = RepoConfig(agents={"copilot": CopilotAgentConfig(enabled=False)})
    off = build_app(cfg, gh=GitHub("t"))
    upsert_repo(off.store, "o/r", "main", disabled.model_dump())
    await off.aclose()
    again = build_app(cfg, gh=GitHub("t"))
    try:
        assert "copilot" not in again.adapters.names()
    finally:
        await again.aclose()
