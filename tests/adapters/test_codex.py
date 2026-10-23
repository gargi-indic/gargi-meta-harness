import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness.adapters.base import AgentState, StartRequest
from harness.adapters.codex import (
    INVALID_JSON_RETRY_NOTE,
    CodexAdapter,
    CodexAgentConfig,
    RunningProcess,
)
from harness.config import GlobalConfig
from harness.workspaces import WorktreeManager


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


class FakeProcessRunner:
    def __init__(self, fixtures_dir: Path):
        self.fixtures_dir = fixtures_dir
        self.last_argv = []
        self.last_env = {}
        self.processes = []

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> RunningProcess:
        self.last_argv = argv
        self.last_env = env
        mode = "plan"
        if "--sandbox" in argv:
            mode = "plan"
        elif "resume" in argv:
            mode = "resume"
        elif "failed" in argv:
            mode = "failed"
        else:
            mode = "implement"
        proc = FakeRunningProcess(self.fixtures_dir / f"{mode}.jsonl")
        self.processes.append(proc)
        return proc


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent.parent / "fixtures" / "codex"


@pytest.fixture
def runner(fixtures_dir):
    return FakeProcessRunner(fixtures_dir)


@pytest.fixture
def config():
    return CodexAgentConfig()


@pytest.mark.asyncio
async def test_codex_plan(runner, config, tmp_path):
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    adapter = CodexAdapter(workspaces=wtm, gh=None, config=config, runner=runner)
    req = StartRequest(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="plan_first",
        idempotency_key="key1",
        workdir=tmp_path,
    )
    ref = await adapter.start(req)
    assert ref.data["state"] == AgentState.PLANNING
    assert runner.last_argv[:4] == ["codex", "exec", "--json", "--sandbox"]
    assert runner.last_argv[4:6] == ["read-only", "--output-schema"]
    assert runner.last_argv[-1] == "Fix things"

    for _ in range(50):
        snapshot = await adapter.poll(ref)
        if snapshot.state not in (AgentState.WORKING, AgentState.PLANNING):
            break
        await asyncio.sleep(0.1)

    assert snapshot.state == AgentState.PLAN_READY
    assert "Plan summary" in snapshot.plan
    assert "start" in [a.kind for a in snapshot.activity]
    assert "message" in [a.kind for a in snapshot.activity]
    assert ref.data["codex_session_id"] == "codex_123"


@pytest.mark.asyncio
async def test_codex_implement(runner, config, tmp_path):
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    gh = Mock()
    with patch("harness.adapters.codex.publish_worktree") as mock_pub:
        mock_result = Mock()
        mock_result.pr_number = 42
        mock_result.pr_url = "http://pr"
        mock_result.head_sha = "abc"
        mock_pub.return_value = mock_result

        adapter = CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner, store=None)
        req = StartRequest(
            repo="test/repo",
            base_branch="main",
            branch="harness/1-slug",
            issue=1,
            title="Test Issue",
            brief="Fix things",
            mode="direct",
            idempotency_key="key2",
            workdir=tmp_path,
        )
        ref = await adapter.start(req)
        assert ref.data["state"] == AgentState.WORKING
        assert runner.last_argv == ["codex", "exec", "--json", "--full-auto", "Fix things"]

        for _ in range(50):
            snapshot = await adapter.poll(ref)
            if snapshot.state not in (AgentState.WORKING, AgentState.PLANNING):
                break
            await asyncio.sleep(0.1)

        assert snapshot.state == AgentState.PR_READY
        assert snapshot.pr.number == 42


@pytest.mark.asyncio
async def test_redact_api_key(runner, config, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "super_secret_key_12345")
    transcript = tmp_path / "plan.jsonl"
    transcript.write_text('{"event": "message", "text": "using key super_secret_key_12345"}\n')
    runner.fixtures_dir = tmp_path
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    adapter = CodexAdapter(workspaces=wtm, gh=None, config=config, runner=runner)
    req = StartRequest(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="plan_first",
        idempotency_key="key3",
        workdir=tmp_path,
    )
    ref = await adapter.start(req)
    for _ in range(50):
        snapshot = await adapter.poll(ref)
        if snapshot.state not in (AgentState.WORKING, AgentState.PLANNING):
            break
        await asyncio.sleep(0.1)
    for act in snapshot.activity:
        assert "super_secret_key_12345" not in act.text
        assert "[redacted]" in act.text


@pytest.mark.asyncio
async def test_codex_cancel(runner, config, tmp_path):
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    adapter = CodexAdapter(workspaces=wtm, gh=None, config=config, runner=runner)
    req = StartRequest(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="plan_first",
        idempotency_key="cancel_key",
        workdir=tmp_path,
    )
    ref = await adapter.start(req)
    await asyncio.sleep(0.01)
    await adapter.cancel(ref)
    await adapter.cancel(ref)  # Idempotent
    snapshot = await adapter.poll(ref)
    assert snapshot.state == AgentState.FAILED
    assert snapshot.error == "cancelled"
    assert runner.processes[0].terminated


@pytest.mark.asyncio
async def test_codex_send_resume(runner, config, tmp_path):
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    adapter = CodexAdapter(workspaces=wtm, gh=None, config=config, runner=runner)
    req = StartRequest(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="plan_first",
        idempotency_key="resume_key",
        workdir=tmp_path,
    )
    ref = await adapter.start(req)
    for _ in range(50):
        snapshot = await adapter.poll(ref)
        if snapshot.state not in (AgentState.WORKING, AgentState.PLANNING):
            break
        await asyncio.sleep(0.1)

    await adapter.send(ref, "Please continue")
    assert runner.last_argv == ["codex", "exec", "resume", "codex_123", "Please continue"]


@pytest.mark.asyncio
async def test_codex_plan_retry_invalid_json(runner, config, tmp_path):
    transcript = tmp_path / "plan.jsonl"
    transcript.write_text('{"event": "message", "text": "invalid json"}\n')
    runner.fixtures_dir = tmp_path
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    adapter = CodexAdapter(workspaces=wtm, gh=None, config=config, runner=runner)
    req = StartRequest(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="plan_first",
        idempotency_key="retry_key",
        workdir=tmp_path,
    )
    ref = await adapter.start(req)
    for _ in range(50):
        snapshot = await adapter.poll(ref)
        if snapshot.state not in (AgentState.WORKING, AgentState.PLANNING):
            break
        await asyncio.sleep(0.1)

    assert len(runner.processes) == 2
    assert runner.last_argv[-1].startswith("Fix things")
    assert INVALID_JSON_RETRY_NOTE in runner.last_argv[-1]


def test_extract_session_id():
    from harness.adapters.codex import extract_session_id

    assert extract_session_id({"session_id": "sid"}) == "sid"
    assert extract_session_id({"thread_id": "tid"}) == "tid"
    assert extract_session_id({"msg": {"session_id": "sid2"}}) == "sid2"
    assert extract_session_id({"item": {"thread_id": "tid2"}}) == "tid2"
    assert extract_session_id({}) is None


@pytest.mark.asyncio
async def test_codex_implement_with_worktree_and_respx(runner, config, tmp_path, respx_mock):
    import subprocess

    remote = tmp_path / "remote"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    repo_tmp = tmp_path / "tmp_repo"
    subprocess.run(["git", "clone", str(remote), str(repo_tmp)], check=True)
    (repo_tmp / "file.txt").write_text("initial")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "file.txt"], cwd=repo_tmp, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_tmp, env=env, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=repo_tmp, check=True)

    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    wt = await wtm.create(repo="owner/repo", clone_url=str(remote), issue=1, slug="slug", base="main")
    transcript = tmp_path / "implement.jsonl"
    transcript.write_text(
        '{"session_id": "codex_456", "event": "start"}\n{"event": "message", "text": "Done."}\n'
    )
    runner.fixtures_dir = tmp_path
    (wt.path / "file.txt").write_text("change")

    from harness.github.client import GitHub
    from harness.store.db import Store
    from harness.store.etag_cache import SqliteEtagCache

    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    gh = GitHub(token="test_token", cache=SqliteEtagCache(store))

    respx_mock.get(
        "https://api.github.com/repos/owner/repo/pulls?per_page=100&head=owner:harness/1-slug&state=open"
    ).respond(json=[])
    respx_mock.post("https://api.github.com/repos/owner/repo/pulls").respond(
        json={"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
    )

    adapter = CodexAdapter(workspaces=wtm, gh=gh, config=config, runner=runner, store=store)
    req = StartRequest(
        repo="owner/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="direct",
        idempotency_key="key2",
        workdir=wt.path,
    )
    ref = await adapter.start(req)
    ref.data["plan"] = "My Plan Markdown"
    ref.data["plan_approved_note"] = "approved"
    ref.data["state"] = AgentState.PLAN_READY
    await adapter.cancel(ref)
    await adapter._spawn_process(ref, wt.path)

    assert runner.last_argv[:-1] == ["codex", "exec", "--json", "--full-auto"]
    assert "My Plan Markdown" in runner.last_argv[-1]
    assert "Fix things" in runner.last_argv[-1]

    for _ in range(50):
        snapshot = await adapter.poll(ref)
        if snapshot.state not in (AgentState.WORKING, AgentState.PLANNING):
            break
        await asyncio.sleep(0.1)

    assert snapshot.state == AgentState.PR_READY, snapshot.error


@pytest.mark.asyncio
async def test_codex_env_leak(runner, config, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "should_not_leak")
    monkeypatch.setenv("CODEX_API_KEY", "should_be_present")
    wtm = WorktreeManager(GlobalConfig(data_dir=tmp_path).worktrees_dir)
    adapter = CodexAdapter(workspaces=wtm, gh=None, config=config, runner=runner)
    req = StartRequest(
        repo="test/repo",
        base_branch="main",
        branch="harness/1-slug",
        issue=1,
        title="Test Issue",
        brief="Fix things",
        mode="plan_first",
        idempotency_key="env_key",
        workdir=tmp_path,
    )
    await adapter.start(req)
    assert runner.last_env.get("CODEX_API_KEY") == "should_be_present"
    assert "GH_TOKEN" not in runner.last_env
