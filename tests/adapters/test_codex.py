import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest

from harness.adapters.base import AgentSnapshot, AgentState, SessionRef, StartRequest
from harness.adapters.codex import (
    INVALID_JSON_RETRY_NOTE,
    CodexAdapter,
    CodexAgentConfig,
    RunningProcess,
    extract_session_id,
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
PLAN_JSON = '{"summary": "Plan summary", "steps": [], "risks": [], "questions": []}'


class FakeRunningProcess:
    def __init__(self, transcript_file: Path):
        self.transcript_file = transcript_file
        self.terminated = False
        self.killed = False

    async def stdout_lines(self) -> AsyncIterator[str]:
        if not self.transcript_file.exists():
            return
        with open(self.transcript_file) as f:
            for line in f:
                yield line
                await asyncio.sleep(0.01)

    async def wait(self) -> int:
        if self.terminated or self.killed:
            return 1
        if "failed" in self.transcript_file.name:
            return 1
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class HangingProcess:
    """Never produces output and never exits until killed (or terminated when ``responsive``)."""

    def __init__(self, responsive: bool = False):
        self.responsive = responsive
        self.exited = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    async def stdout_lines(self) -> AsyncIterator[str]:
        await self.exited.wait()
        return
        yield ""

    async def wait(self) -> int:
        await self.exited.wait()
        return 1

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.responsive:
            self.exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.exited.set()


class FakeProcessRunner:
    def __init__(self, fixtures_dir: Path):
        self.fixtures_dir = fixtures_dir
        self.queue: list[Path] = []
        self.factory: Callable[[], RunningProcess] | None = None
        self.argvs: list[list[str]] = []
        self.last_argv: list[str] = []
        self.last_env: dict[str, str] = {}
        self.processes: list = []

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> RunningProcess:
        self.last_argv = argv
        self.last_env = dict(env)
        self.argvs.append(argv)
        if self.factory:
            proc = self.factory()
        elif self.queue:
            proc = FakeRunningProcess(self.queue.pop(0))
        else:
            if "--sandbox" in argv:
                mode = "plan"
            elif "resume" in argv:
                mode = "resume"
            else:
                mode = "implement"
            proc = FakeRunningProcess(self.fixtures_dir / f"{mode}.jsonl")
        self.processes.append(proc)
        return proc


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("CODEX_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "JULES_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent.parent / "fixtures" / "codex"


@pytest.fixture
def runner(fixtures_dir):
    return FakeProcessRunner(fixtures_dir)


@pytest.fixture
def config():
    return CodexAgentConfig()


def make_req(tmp_path: Path, key: str, mode: str = "plan_first", **overrides) -> StartRequest:
    fields = dict(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode=mode,
        idempotency_key=key,
        workdir=tmp_path,
    )
    fields.update(overrides)
    return StartRequest(**fields)


def make_adapter(runner, config, tmp_path, gh=None, **kwargs) -> CodexAdapter:
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    return CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner, **kwargs)


async def settle(adapter: CodexAdapter, ref: SessionRef) -> AgentSnapshot:
    for _ in range(100):
        if ref.id not in adapter._running:
            break
        await asyncio.sleep(0.02)
    return await adapter.poll(ref)


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env={**GIT_ENV, "PATH": "/usr/bin:/bin"})


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


def pulls_route(respx_mock):
    return respx_mock.get("https://api.github.com/repos/owner/repo/pulls")


def create_route(respx_mock):
    return respx_mock.post("https://api.github.com/repos/owner/repo/pulls").respond(
        json={"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
    )


def direct_req(wt: Worktree, key: str) -> StartRequest:
    return make_req(wt.path, key, "direct", repo="owner/repo", base_branch="develop", workdir=wt.path)


async def test_codex_plan(runner, config, tmp_path):
    adapter = make_adapter(runner, config, tmp_path)
    ref = await adapter.start(make_req(tmp_path, "key1"))
    assert ref.data["state"] == AgentState.PLANNING
    schema = Path(runner.last_argv[6])
    assert schema.name == "codex_plan.schema.json" and schema.is_file()
    assert runner.last_argv == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema),
        "Fix things",
    ]

    snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.PLAN_READY
    assert "Plan summary" in snapshot.plan
    assert "start" in [a.kind for a in snapshot.activity]
    assert "message" in [a.kind for a in snapshot.activity]
    assert ref.data["codex_session_id"] == "codex_123"


async def test_codex_implement_argv_and_pr(runner, config, tmp_path):
    with patch("harness.adapters.codex.publish_worktree") as mock_pub:
        mock_result = Mock(pr_number=42, pr_url="http://pr", head_sha="abc")
        mock_pub.return_value = mock_result
        adapter = make_adapter(runner, config, tmp_path, gh=Mock())
        ref = await adapter.start(make_req(tmp_path, "key2", "direct"))
        assert ref.data["state"] == AgentState.WORKING
        assert runner.last_argv == ["codex", "exec", "--json", "--full-auto", "Fix things"]

        snapshot = await settle(adapter, ref)
        assert snapshot.state == AgentState.PR_READY, snapshot.error
        assert snapshot.pr.number == 42


async def test_redact_api_key(runner, config, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "super_secret_key_12345")
    transcript = tmp_path / "plan.jsonl"
    transcript.write_text('{"event": "message", "text": "using key super_secret_key_12345"}\n')
    runner.fixtures_dir = tmp_path
    adapter = make_adapter(runner, config, tmp_path)
    ref = await adapter.start(make_req(tmp_path, "key3"))
    snapshot = await settle(adapter, ref)
    assert snapshot.activity
    for act in snapshot.activity:
        assert "super_secret_key_12345" not in act.text
        assert "[redacted]" in act.text
    assert "super_secret_key_12345" not in json.dumps(ref.data, default=str)


async def test_env_is_minimal_and_has_only_the_codex_key(runner, config, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "codex-key-1234567890")
    monkeypatch.setenv("GH_TOKEN", "gh-token-1234567890")
    monkeypatch.setenv("JULES_API_KEY", "jules-key-1234567890")
    monkeypatch.setenv("PATH", "/usr/bin")
    adapter = make_adapter(runner, config, tmp_path)
    await adapter.start(make_req(tmp_path, "env"))
    assert runner.last_env["CODEX_API_KEY"] == "codex-key-1234567890"
    assert runner.last_env["PATH"] == "/usr/bin"
    assert "GH_TOKEN" not in runner.last_env and "JULES_API_KEY" not in runner.last_env
    assert set(runner.last_env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "CODEX_API_KEY"}


async def test_codex_cancel_terminates_once_then_kills_after_grace(runner, tmp_path):
    adapter = make_adapter(runner, CodexAgentConfig(cancel_grace=0.05), tmp_path)
    hanging = HangingProcess()
    runner.factory = lambda: hanging
    ref = await adapter.start(make_req(tmp_path, "cancel_key"))
    await asyncio.sleep(0.01)
    await adapter.cancel(ref)
    await adapter.cancel(ref)
    assert hanging.terminate_calls == 1
    assert hanging.kill_calls == 0
    await asyncio.sleep(0.15)
    assert hanging.terminate_calls == 1
    assert hanging.kill_calls == 1
    snapshot = await adapter.poll(ref)
    assert snapshot.state == AgentState.FAILED
    assert snapshot.error == "cancelled"
    assert not adapter._kill_tasks


async def test_codex_cancel_does_not_kill_after_clean_exit(runner, tmp_path):
    adapter = make_adapter(runner, CodexAgentConfig(cancel_grace=0.05), tmp_path)
    hanging = HangingProcess(responsive=True)
    runner.factory = lambda: hanging
    ref = await adapter.start(make_req(tmp_path, "cancel_clean"))
    await asyncio.sleep(0.01)
    await adapter.cancel(ref)
    await asyncio.sleep(0.15)
    assert hanging.terminate_calls == 1
    assert hanging.kill_calls == 0
    assert not adapter._kill_tasks


async def test_codex_send_resume(runner, config, tmp_path):
    adapter = make_adapter(runner, config, tmp_path)
    ref = await adapter.start(make_req(tmp_path, "resume_key"))
    await settle(adapter, ref)
    await adapter.send(ref, "Please continue")
    assert runner.last_argv == ["codex", "exec", "resume", "codex_123", "Please continue"]


async def test_send_works_on_a_fresh_adapter_given_only_the_ref(runner, config, tmp_path):
    first = make_adapter(runner, config, tmp_path)
    ref = await first.start(make_req(tmp_path, "restart"))
    await settle(first, ref)
    restored = SessionRef.model_validate(json.loads(ref.model_dump_json()))
    assert restored.data["codex_session_id"] == "codex_123"

    fresh = make_adapter(runner, config, tmp_path)
    assert restored.id not in fresh._running
    await fresh.send(restored, "carry on")
    assert runner.last_argv == ["codex", "exec", "resume", "codex_123", "carry on"]


async def test_same_idempotency_key_spawns_once(runner, config, tmp_path):
    adapter = make_adapter(runner, config, tmp_path)
    req = make_req(tmp_path, "same")
    first = await adapter.start(req)
    assert await adapter.find("same") is first
    second = await adapter.start(req)
    assert second is first
    assert len(runner.processes) == 1
    assert await adapter.find("other") is None


async def test_plan_invalid_json_then_valid_is_plan_ready(runner, config, fixtures_dir, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"event": "message", "text": "invalid json"}\n')
    runner.queue = [bad, fixtures_dir / "plan.jsonl"]
    adapter = make_adapter(runner, config, tmp_path)
    ref = await adapter.start(make_req(tmp_path, "retry_ok"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(runner.processes) == 2 and ref.id not in adapter._running:
            break
    snapshot = await adapter.poll(ref)
    assert snapshot.state == AgentState.PLAN_READY
    assert len(runner.processes) == 2
    assert runner.argvs[0][-1] == "Fix things"
    assert INVALID_JSON_RETRY_NOTE in runner.argvs[1][-1]


async def test_plan_two_invalid_replies_fail_after_two_spawns(runner, config, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"event": "message", "text": "invalid json"}\n')
    runner.queue = [bad, bad]
    adapter = make_adapter(runner, config, tmp_path)
    ref = await adapter.start(make_req(tmp_path, "retry_fail"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len(runner.processes) == 2 and ref.id not in adapter._running:
            break
    snapshot = await adapter.poll(ref)
    assert snapshot.state == AgentState.FAILED
    assert len(runner.processes) == 2


async def test_failed_run_reports_error_event(runner, config, tmp_path):
    runner.factory = lambda: FakeRunningProcess(runner.fixtures_dir / "failed.jsonl")
    adapter = make_adapter(runner, config, tmp_path)
    ref = await adapter.start(make_req(tmp_path, "failed", "direct"))
    snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.FAILED
    assert snapshot.error == "Something broke"


async def test_plan_timeout_fires_while_streaming(runner, tmp_path):
    adapter = make_adapter(runner, CodexAgentConfig(plan_timeout=0.05), tmp_path)
    hanging = HangingProcess()
    runner.factory = lambda: hanging
    ref = await adapter.start(make_req(tmp_path, "timeout"))
    snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.FAILED
    assert snapshot.error == "timeout"
    assert hanging.terminate_calls == 1


async def test_implement_timeout_uses_implement_timeout(runner, tmp_path):
    adapter = make_adapter(runner, CodexAgentConfig(implement_timeout=0.05), tmp_path)
    runner.factory = HangingProcess
    ref = await adapter.start(make_req(tmp_path, "timeout2", "direct"))
    snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.FAILED
    assert snapshot.error == "timeout"


async def test_unexpected_publish_error_fails_with_redacted_message(runner, config, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "super_secret_key_12345")
    with patch(
        "harness.adapters.codex.publish_worktree", side_effect=RuntimeError("boom super_secret_key_12345")
    ):
        adapter = make_adapter(runner, config, tmp_path, gh=Mock())
        ref = await adapter.start(make_req(tmp_path, "boom", "direct"))
        snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.FAILED
    assert "super_secret_key_12345" not in snapshot.error
    assert "boom" in snapshot.error


def test_extract_session_id():
    assert extract_session_id({"session_id": "sid"}) == "sid"
    assert extract_session_id({"thread_id": "tid"}) == "tid"
    assert extract_session_id({"msg": {"session_id": "sid2"}}) == "sid2"
    assert extract_session_id({"item": {"thread_id": "tid2"}}) == "tid2"
    assert extract_session_id({}) is None


async def test_approved_plan_is_in_the_implement_prompt(runner, config, tmp_path):
    adapter = make_adapter(runner, config, tmp_path, gh=Mock())
    ref = await adapter.start(make_req(tmp_path, "approve"))
    await settle(adapter, ref)
    await adapter.approve_plan(ref, "go ahead")
    ref.data["mode"] = "direct"
    with patch("harness.adapters.codex.publish_worktree", return_value=None):
        await adapter._spawn_process(ref, tmp_path)
        await settle(adapter, ref)
    assert "Plan summary" in runner.last_argv[-1]
    assert "go ahead" in runner.last_argv[-1]


async def test_implement_publishes_with_request_branch_and_base(runner, config, git_env, respx_mock):
    remote, wtm, wt, gh = git_env
    (wt.path / "file.txt").write_text("change")
    pulls_route(respx_mock).respond(json=[])
    create = create_route(respx_mock)

    adapter = CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner)
    ref = await adapter.start(direct_req(wt, "impl"))
    snapshot = await settle(adapter, ref)

    assert snapshot.state == AgentState.PR_READY, snapshot.error
    assert snapshot.pr.number == 42
    body = json.loads(create.calls.last.request.content)
    assert body["head"] == "harness/1-slug"
    assert body["base"] == "develop"
    assert ref.data["branch"] == "harness/1-slug" and ref.data["base_branch"] == "develop"


async def test_rerun_without_new_changes_does_not_create_a_second_pr(runner, config, git_env, respx_mock):
    remote, wtm, wt, gh = git_env
    (wt.path / "file.txt").write_text("change")
    pull = {"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
    pulls_route(respx_mock).mock(side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[pull])])
    create = create_route(respx_mock)

    adapter = CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner)
    ref = await adapter.start(direct_req(wt, "rerun"))
    assert (await settle(adapter, ref)).state == AgentState.PR_READY

    await adapter.send(ref, "anything else?")
    snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.PR_READY, snapshot.error
    assert snapshot.pr.number == 42
    assert create.call_count == 1


async def test_protected_path_edits_need_input_and_push_nothing(runner, config, git_env, respx_mock):
    remote, wtm, wt, gh = git_env
    (wt.path / ".harness").mkdir()
    (wt.path / ".harness" / "secret.yml").write_text("x")
    create = create_route(respx_mock)

    adapter = CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner)
    ref = await adapter.start(direct_req(wt, "protected"))
    snapshot = await settle(adapter, ref)

    assert snapshot.state == AgentState.NEEDS_INPUT
    assert ".harness/secret.yml" in snapshot.question
    assert create.call_count == 0
    heads = subprocess.run(
        ["git", "ls-remote", "--heads", str(remote), "harness/1-slug"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert heads == ""


async def test_no_changes_fails(runner, config, git_env, respx_mock):
    remote, wtm, wt, gh = git_env
    adapter = CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner)
    ref = await adapter.start(direct_req(wt, "nochange"))
    snapshot = await settle(adapter, ref)
    assert snapshot.state == AgentState.FAILED
    assert snapshot.error == "no_changes"


def _app_cfg(tmp_path: Path) -> GlobalConfig:
    return GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[])


async def test_codex_is_registered_only_with_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token-0123456789")
    app = build_app(_app_cfg(tmp_path / "a"))
    try:
        assert "codex" not in app.adapters.names()
    finally:
        await app.aclose()

    monkeypatch.setenv("CODEX_API_KEY", "codex-key-0123456789")
    app = build_app(_app_cfg(tmp_path / "b"))
    try:
        adapter = app.adapters.get("codex")
        assert isinstance(adapter, CodexAdapter)
        assert adapter.workspaces.root == app.cfg.worktrees_dir
        assert adapter.gh is app.gh
        assert "codex-key-0123456789" not in repr(app)
    finally:
        await app.aclose()


async def test_codex_is_not_registered_when_disabled_in_repo_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token-0123456789")
    monkeypatch.setenv("CODEX_API_KEY", "codex-key-0123456789")
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name="o/r")])
    store = Store(cfg.db_path.parent.mkdir(parents=True, exist_ok=True) or cfg.db_path)
    store.migrate()
    upsert_repo(store, "o/r", "main", {"agents": {"codex": {"enabled": False}}})
    store.close()
    app = build_app(cfg)
    try:
        assert "codex" not in app.adapters.names()
    finally:
        await app.aclose()
