import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness.adapters.base import AgentAdapter, AgentState, SessionRef, StartRequest
from harness.adapters.claude import (
    ALLOWED_TOOLS,
    ClaudeAdapter,
    ClaudeAgentConfig,
    ClaudeOptions,
    build_sdk_options,
    default_factory,
)
from harness.app import build_app
from harness.config import GlobalConfig, RepoEntry
from harness.github.client import GitHub
from harness.store.db import Store
from harness.store.etag_cache import SqliteEtagCache
from harness.store.repo import upsert_repo
from harness.workspaces import Worktree, WorktreeManager

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


# Message stand-ins named like the SDK classes: the adapter dispatches on the class name.
@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "tu1"


@dataclass
class AssistantMessage:
    content: list[Any]
    session_id: str | None = None


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    result: str | None = None
    session_id: str | None = None
    errors: list[str] = field(default_factory=list)


Step = Any  # a message, or a callable run in place (sync or async)


class ScriptedSession:
    """Plays one scripted turn per ``query``; records every call it receives."""

    def __init__(self, options: ClaudeOptions, turns: list[list[Step]]) -> None:
        self.options = options
        self.turns = turns
        self.queries: list[str] = []
        self.modes: list[str] = []
        self.connects = 0
        self.interrupts = 0
        self.disconnects = 0

    async def connect(self) -> None:
        self.connects += 1

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive_messages(self) -> AsyncIterator[Any]:
        steps = self.turns.pop(0) if self.turns else [ResultMessage()]
        for step in steps:
            if callable(step):
                outcome = step()
                if isinstance(outcome, Awaitable):
                    await outcome
            else:
                yield step

    async def set_permission_mode(self, mode: str) -> None:
        self.modes.append(mode)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnects += 1


class Factory:
    def __init__(self, turns: list[list[Step]]) -> None:
        self.turns = turns
        self.sessions: list[ScriptedSession] = []

    def __call__(self, options: ClaudeOptions) -> ScriptedSession:
        session = ScriptedSession(options, self.turns)
        self.sessions.append(session)
        return session


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env={**GIT_ENV, "PATH": "/usr/bin:/bin"})


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "JULES_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def git_env(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git("init", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git("clone", str(remote), str(seed))
    (seed / "file.txt").write_text("initial")
    git("add", "file.txt", cwd=seed)
    git("commit", "-m", "init", cwd=seed)
    git("push", "origin", "HEAD:main", cwd=seed)
    git("push", "origin", "HEAD:develop", cwd=seed)

    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    wt = await wtm.create(repo="owner/repo", clone_url=str(remote), issue=1, slug="slug", base="develop")
    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    gh = GitHub(token="test_token", cache=SqliteEtagCache(store))
    return remote, wtm, wt, gh


def make_adapter(git_env, factory, **kwargs) -> ClaudeAdapter:
    _, wtm, _, gh = git_env
    return ClaudeAdapter(
        workspaces=wtm,
        gh=gh,
        config=kwargs.pop("config", ClaudeAgentConfig()),
        session_factory=factory,
        **kwargs,
    )


def make_req(wt: Worktree, key: str, mode: str = "direct") -> StartRequest:
    return StartRequest(
        repo="owner/repo",
        base_branch="develop",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode=mode,
        idempotency_key=key,
        workdir=wt.path,
    )


def write_file(wt: Worktree, rel: str, text: str) -> Callable[[], None]:
    def step() -> None:
        target = wt.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    return step


def pulls_route(respx_mock):
    return respx_mock.get("https://api.github.com/repos/owner/repo/pulls")


def create_route(respx_mock):
    return respx_mock.post("https://api.github.com/repos/owner/repo/pulls").respond(
        json={"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
    )


async def run_to_idle(adapter: ClaudeAdapter, ref: SessionRef):
    await adapter.wait_idle(ref)
    return await adapter.poll(ref)


def test_adapter_satisfies_protocol_and_caps(git_env):
    adapter = make_adapter(git_env, Factory([]), config=ClaudeAgentConfig(max_concurrency=3))
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "claude"
    assert adapter.caps.plan_approval and adapter.caps.messaging and adapter.caps.local_workspace
    assert not adapter.caps.opens_pr and adapter.caps.max_concurrency == 3


async def test_plan_path_captures_exit_plan_mode_and_approve_switches_mode(git_env, respx_mock):
    _, _, wt, _ = git_env
    pulls_route(respx_mock).respond(json=[])
    create_route(respx_mock)
    factory = Factory(
        [
            [
                AssistantMessage([TextBlock("thinking"), ToolUseBlock("ExitPlanMode", {"plan": "1. do it"})]),
                ResultMessage(),
            ],
            [write_file(wt, "file.txt", "changed"), ResultMessage()],
        ]
    )
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "plan", "plan_first"))
    snapshot = await run_to_idle(adapter, ref)

    assert snapshot.state == AgentState.PLAN_READY
    assert snapshot.plan == "1. do it"
    session = factory.sessions[0]
    assert session.options.permission_mode == "plan"
    assert session.queries == ["Fix things"]
    assert [a.kind for a in snapshot.activity] == ["text", "tool"]

    await adapter.approve_plan(ref, None)
    assert session.modes == ["acceptEdits"]
    assert (await adapter.poll(ref)).state == AgentState.WORKING
    snapshot = await run_to_idle(adapter, ref)
    assert session.queries[-1] == "Plan approved. Implement it."
    assert snapshot.state == AgentState.PR_READY, snapshot.error
    assert len(factory.sessions) == 1


async def test_exit_plan_mode_is_denied_by_the_hook_without_counting(git_env):
    _, _, wt, _ = git_env
    factory = Factory([[ResultMessage()]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "planhook", "plan_first"))
    await adapter.wait_idle(ref)
    hook = factory.sessions[0].options.hooks["PreToolUse"][0]
    for _ in range(4):
        out = await hook({"tool_name": "ExitPlanMode", "tool_input": {"plan": "x"}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ref.data["denials"] == 0


async def test_implement_path_creates_pr_and_second_poll_is_idempotent(git_env, respx_mock):
    _, _, wt, _ = git_env
    pulls_route(respx_mock).respond(json=[])
    create = create_route(respx_mock)
    factory = Factory([[write_file(wt, "file.txt", "change"), ResultMessage(session_id=None)]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "impl"))
    assert ref.data["state"] == AgentState.WORKING
    first = await run_to_idle(adapter, ref)

    assert first.state == AgentState.PR_READY, first.error
    assert first.pr.number == 42
    second = await adapter.poll(ref)
    assert second.pr == first.pr and second.state == AgentState.PR_READY
    assert second.activity == []
    assert create.call_count == 1
    body = json.loads(create.calls.last.request.content)
    assert body["head"] == "harness/1-slug" and body["base"] == "develop"
    assert factory.sessions[0].options.permission_mode == "acceptEdits"
    assert factory.sessions[0].options.cwd == wt.path
    assert factory.sessions[0].options.allowed_tools == ALLOWED_TOOLS


async def test_veto_denies_protected_write_and_three_denials_need_input(git_env):
    _, _, wt, _ = git_env
    gate = asyncio.Event()
    factory = Factory([[gate.wait, ResultMessage()]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "veto"))
    while not factory.sessions:
        await asyncio.sleep(0)
    hook = factory.sessions[0].options.hooks["PreToolUse"][0]
    write = {"tool_name": "Write", "tool_input": {"file_path": ".harness/x.yaml", "content": "x"}}

    out = await hook(write, "id1", None)
    decision = out["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "deny"
    assert ".harness/x.yaml" in decision["permissionDecisionReason"]
    assert not (wt.path / ".harness" / "x.yaml").exists()

    await hook(write, "id2", None)
    assert (await hook({"tool_name": "Read", "tool_input": {"file_path": "file.txt"}}, None, None)) == {}
    assert ref.data["denials"] == 0
    for _ in range(3):
        await hook(write, "id3", None)
    snapshot = await adapter.poll(ref)
    assert snapshot.state == AgentState.NEEDS_INPUT
    assert ".harness/x.yaml" in snapshot.question

    gate.set()
    await adapter.wait_idle(ref)
    assert (await adapter.poll(ref)).state == AgentState.NEEDS_INPUT
    assert factory.sessions[0].interrupts == 1


async def test_resume_with_a_fresh_adapter_uses_the_persisted_session_id(git_env, respx_mock):
    _, wtm, wt, _ = git_env
    pulls_route(respx_mock).respond(json=[])
    create = create_route(respx_mock)
    first_factory = Factory([[write_file(wt, "file.txt", "change"), ResultMessage()]])
    first = make_adapter(git_env, first_factory)
    ref = await first.start(make_req(wt, "resume"))
    assert (await run_to_idle(first, ref)).state == AgentState.PR_READY
    sid = ref.data["claude_session_id"]
    assert ref.id == sid and first_factory.sessions[0].options.session_id == sid
    assert first_factory.sessions[0].options.resume is None

    restored = SessionRef.model_validate(json.loads(ref.model_dump_json()))
    before = await wtm.list()
    second_factory = Factory([[ResultMessage()]])
    second = make_adapter(git_env, second_factory)
    await second.send(restored, "carry on")
    await second.wait_idle(restored)

    assert second_factory.sessions[0].options.resume == sid
    assert second_factory.sessions[0].options.session_id is None
    assert await wtm.list() == before
    assert create.call_count == 1
    assert (await second.poll(restored)).pr.number == 42


async def test_poll_after_restart_resumes_an_interrupted_turn(git_env):
    _, _, wt, _ = git_env
    first = make_adapter(git_env, Factory([]))
    ref = await first.start(make_req(wt, "orphan"))
    await first.wait_idle(ref)
    restored = SessionRef.model_validate(json.loads(ref.model_dump_json()))
    restored.data["state"] = AgentState.WORKING
    factory = Factory([[ResultMessage(is_error=True, subtype="error_max_turns", result="too many")]])
    second = make_adapter(git_env, factory)
    await second.poll(restored)
    snapshot = await run_to_idle(second, restored)
    assert factory.sessions[0].options.resume == restored.data["claude_session_id"]
    assert snapshot.state == AgentState.FAILED


async def test_revise_after_pr_ready_pushes_a_follow_up_commit(git_env, respx_mock):
    remote, _, wt, _ = git_env
    pulls_route(respx_mock).respond(json=[])
    create_route(respx_mock)
    factory = Factory(
        [
            [write_file(wt, "file.txt", "change"), ResultMessage()],
            [write_file(wt, "file.txt", "revised"), ResultMessage()],
        ]
    )
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "revise"))
    first = await run_to_idle(adapter, ref)
    assert first.state == AgentState.PR_READY

    await adapter.send(ref, "Please also handle the edge case\nmore detail")
    second = await run_to_idle(adapter, ref)
    assert second.state == AgentState.PR_READY, second.error
    assert second.pr.number == 42
    assert second.pr.head_sha != first.pr.head_sha
    remote_head = subprocess.run(
        ["git", "rev-parse", "harness/1-slug"], cwd=remote, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert remote_head == second.pr.head_sha


async def test_revise_without_changes_stays_pr_ready(git_env, respx_mock):
    _, _, wt, _ = git_env
    pulls_route(respx_mock).respond(json=[])
    create_route(respx_mock)
    factory = Factory([[write_file(wt, "file.txt", "change"), ResultMessage()], [ResultMessage()]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "norevise"))
    first = await run_to_idle(adapter, ref)
    await adapter.send(ref, "just checking")
    second = await run_to_idle(adapter, ref)
    assert second.state == AgentState.PR_READY and second.pr == first.pr


async def test_protected_path_edits_need_input_and_create_no_pr(git_env, respx_mock):
    _, _, wt, _ = git_env
    create = create_route(respx_mock)
    factory = Factory([[write_file(wt, ".harness/secret.yml", "x"), ResultMessage()]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "protected"))
    snapshot = await run_to_idle(adapter, ref)
    assert snapshot.state == AgentState.NEEDS_INPUT
    assert ".harness/secret.yml" in snapshot.question
    assert create.call_count == 0


async def test_no_changes_fails(git_env):
    _, _, wt, _ = git_env
    adapter = make_adapter(git_env, Factory([[ResultMessage()]]))
    ref = await adapter.start(make_req(wt, "nochange"))
    snapshot = await run_to_idle(adapter, ref)
    assert snapshot.state == AgentState.FAILED and snapshot.error == "no_changes"


@pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_budget_usd"])
async def test_budget_and_max_turn_errors_fail_with_redacted_message(git_env, monkeypatch, subtype):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-1234567890")
    _, _, wt, _ = git_env
    result = ResultMessage(subtype=subtype, is_error=True, result="stopped sk-ant-secret-1234567890")
    adapter = make_adapter(git_env, Factory([[result]]))
    ref = await adapter.start(make_req(wt, "err-" + subtype))
    snapshot = await run_to_idle(adapter, ref)
    assert snapshot.state == AgentState.FAILED
    assert subtype in snapshot.error and "sk-ant-secret-1234567890" not in snapshot.error


async def test_unexpected_sdk_exception_fails(git_env):
    _, _, wt, _ = git_env

    def boom() -> None:
        raise RuntimeError("connection lost")

    adapter = make_adapter(git_env, Factory([[boom]]))
    ref = await adapter.start(make_req(wt, "boom"))
    snapshot = await run_to_idle(adapter, ref)
    assert snapshot.state == AgentState.FAILED and "connection lost" in snapshot.error


async def test_timeout_fails_and_interrupts(git_env):
    _, _, wt, _ = git_env
    factory = Factory([[asyncio.Event().wait]])
    adapter = make_adapter(git_env, factory, config=ClaudeAgentConfig(implement_timeout=0.05))
    ref = await adapter.start(make_req(wt, "timeout"))
    snapshot = await run_to_idle(adapter, ref)
    assert snapshot.state == AgentState.FAILED and snapshot.error == "timeout"
    assert factory.sessions[0].interrupts == 1


async def test_cancel_is_idempotent(git_env):
    _, _, wt, _ = git_env
    factory = Factory([[asyncio.Event().wait]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "cancel"))
    while not factory.sessions or not factory.sessions[0].queries:
        await asyncio.sleep(0)
    await adapter.cancel(ref)
    await adapter.cancel(ref)
    session = factory.sessions[0]
    assert session.interrupts == 1 and session.disconnects == 1
    snapshot = await adapter.poll(ref)
    assert snapshot.state == AgentState.FAILED and snapshot.error == "cancelled"
    assert snapshot.poll_after == 60.0


async def test_same_idempotency_key_starts_one_session(git_env):
    _, _, wt, _ = git_env
    factory = Factory([[ResultMessage()]])
    adapter = make_adapter(git_env, factory)
    req = make_req(wt, "same")
    first = await adapter.start(req)
    assert await adapter.find("same") is first
    assert await adapter.start(req) is first
    await adapter.wait_idle(first)
    assert len(factory.sessions) == 1
    assert await adapter.find("other") is None


async def test_start_requires_a_workdir(git_env):
    _, _, wt, _ = git_env
    adapter = make_adapter(git_env, Factory([]))
    with pytest.raises(ValueError):
        await adapter.start(make_req(wt, "x").model_copy(update={"workdir": None}))


async def test_env_is_minimal_and_activity_and_data_are_redacted(git_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-1234567890")
    monkeypatch.setenv("GH_TOKEN", "gh-token-1234567890")
    monkeypatch.setenv("JULES_API_KEY", "jules-key-1234567890")
    _, _, wt, _ = git_env
    factory = Factory([[AssistantMessage([TextBlock("key is sk-ant-secret-1234567890")]), ResultMessage()]])
    adapter = make_adapter(git_env, factory)
    ref = await adapter.start(make_req(wt, "env"))
    snapshot = await run_to_idle(adapter, ref)
    env = factory.sessions[0].options.env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-secret-1234567890"
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "ANTHROPIC_API_KEY"}
    assert snapshot.activity and "sk-ant-secret-1234567890" not in snapshot.activity[0].text
    assert "sk-ant-secret-1234567890" not in json.dumps(ref.data, default=str)


def test_default_factory_builds_sdk_options_without_connecting(tmp_path):
    pytest.importorskip("claude_agent_sdk")

    async def hook(input_data, tool_use_id, context):
        return {}

    options = ClaudeOptions(
        cwd=tmp_path,
        permission_mode="plan",
        allowed_tools=ALLOWED_TOOLS,
        max_turns=7,
        max_budget_usd=1.5,
        env={"ANTHROPIC_API_KEY": "k"},
        resume="abc",
        hooks={"PreToolUse": [hook]},
    )
    sdk_options = build_sdk_options(options)
    assert sdk_options.permission_mode == "plan"
    assert sdk_options.cwd == str(tmp_path)
    assert sdk_options.allowed_tools == list(ALLOWED_TOOLS)
    assert sdk_options.max_turns == 7 and sdk_options.max_budget_usd == 1.5
    assert sdk_options.resume == "abc"
    assert sdk_options.hooks["PreToolUse"][0].hooks == [hook]

    session = default_factory(options)
    assert session.client.options.permission_mode == "plan"
    fresh = build_sdk_options(
        ClaudeOptions(
            cwd=tmp_path, permission_mode="acceptEdits", allowed_tools=(), max_turns=1, session_id="s"
        )
    )
    assert fresh.session_id == "s" and not fresh.resume


def _app_cfg(tmp_path: Path) -> GlobalConfig:
    return GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[])


async def test_claude_is_registered_only_with_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token-0123456789")
    app = build_app(_app_cfg(tmp_path / "a"))
    try:
        assert "claude" not in app.adapters.names()
    finally:
        await app.aclose()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789")
    app = build_app(_app_cfg(tmp_path / "b"))
    try:
        adapter = app.adapters.get("claude")
        assert isinstance(adapter, ClaudeAdapter)
        assert adapter.workspaces.root == app.cfg.worktrees_dir
        assert adapter.gh is app.gh
        assert "sk-ant-0123456789" not in repr(app)
    finally:
        await app.aclose()


async def test_claude_honours_repo_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token-0123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789")
    for sub, agents, registered in (
        ("off", {"claude": {"enabled": False}}, False),
        ("on", {"claude": {"max_concurrency": 4, "model": "m1"}}, True),
    ):
        cfg = GlobalConfig(data_dir=tmp_path / sub, env_files=[], repos=[RepoEntry(name="o/r")])
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(cfg.db_path)
        store.migrate()
        upsert_repo(store, "o/r", "main", {"agents": agents})
        store.close()
        app = build_app(cfg)
        try:
            assert ("claude" in app.adapters.names()) is registered
            if registered:
                adapter = app.adapters.get("claude")
                assert adapter.caps.max_concurrency == 4 and adapter.config.model == "m1"
        finally:
            await app.aclose()
