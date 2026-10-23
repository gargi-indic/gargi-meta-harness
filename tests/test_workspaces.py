"""Git worktree manager (M3.1). Real git in tmp_path with a local bare repo as the remote; no network."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from harness import workspaces
from harness.config import GlobalConfig
from harness.workspaces import WorktreeBusy, WorktreeError, WorktreeManager, branch_name, sanitize_slug

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    import os

    r = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
    )
    return r.stdout.strip()


def push_commit(tmp: Path, remote: Path, name: str, content: str, msg: str) -> None:
    clone = tmp / "pusher"
    if not clone.exists():
        git(tmp, "clone", "-q", str(remote), str(clone))
    (clone / name).write_text(content)
    git(clone, "add", name)
    git(clone, "commit", "-qm", msg)
    git(clone, "push", "-q", "origin", "HEAD:main")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    push_commit(tmp_path, bare, "a.txt", "one\n", "init")
    return bare


@pytest.fixture
def mgr(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(tmp_path / "wt")


async def make(mgr: WorktreeManager, remote: Path, issue: int = 7, slug: str = "Add Thing"):
    return await mgr.create(repo="o/r", clone_url=remote, issue=issue, slug=slug, base="main")


def test_config_default_dir():
    assert GlobalConfig().worktrees_dir == GlobalConfig().data_path / "worktrees"


def test_slug_sanitised():
    assert sanitize_slug("../x") == "x"
    assert sanitize_slug("Hello, World!!") == "hello-world"
    assert len(sanitize_slug("a" * 100)) == 40
    assert branch_name(5, "///") == "harness/5-task"


async def test_create_and_idempotent(mgr, remote):
    wt = await make(mgr, remote)
    assert wt.branch == "harness/7-add-thing" and wt.base == "main"
    assert wt.path == (mgr.root / "o/r/7").resolve()
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == wt.branch
    assert (wt.path / "a.txt").read_text() == "one\n"
    again = await make(mgr, remote)
    assert again == wt
    assert await mgr.get("o/r", 7) == wt
    assert await mgr.get("o/r", 8) is None


async def test_independent_worktrees(mgr, remote):
    a, b = await make(mgr, remote, 1, "a"), await make(mgr, remote, 2, "b")
    assert a.path != b.path
    (a.path / "only-a.txt").write_text("x")
    assert not (b.path / "only-a.txt").exists()
    assert [w.issue for w in await mgr.list()] == [1, 2]
    assert [w.issue for w in await mgr.list("o/r")] == [1, 2]


async def test_create_errors(mgr, remote):
    with pytest.raises(WorktreeError, match="base branch"):
        await mgr.create(repo="o/r", clone_url=remote, issue=1, slug="s", base="nope")
    wt = await make(mgr, remote, 3, "one")
    with pytest.raises(WorktreeError, match="expected"):
        await make(mgr, remote, 3, "other")
    stray = mgr.root / "o/r/9"
    stray.mkdir()
    with pytest.raises(WorktreeError, match="not a valid worktree"):
        await make(mgr, remote, 9, "s")
    assert wt.path.exists()


async def test_invalid_inputs_rejected_before_git(tmp_path, remote):
    calls: list[tuple] = []

    class Spy(WorktreeManager):
        async def _run(self, *a, **k):
            calls.append(a)
            return await super()._run(*a, **k)

    m = Spy(tmp_path / "wt")
    for kwargs in (
        {"repo": "../evil", "base": "main", "issue": 1},
        {"repo": "o/r/x", "base": "main", "issue": 1},
        {"repo": "o/r", "base": "--upload-pack=x", "issue": 1},
        {"repo": "o/r", "base": "a..b", "issue": 1},
        {"repo": "o/r", "base": "main", "issue": 0},
        {"repo": "o/r", "base": "main", "issue": -4},
    ):
        with pytest.raises(WorktreeError):
            await m.create(clone_url=remote, slug="s", **kwargs)
    with pytest.raises(WorktreeError):
        await m.create(repo="o/r", clone_url="--bad", issue=1, slug="s", base="main")
    assert calls == [] or all(c[0] != "worktree" for c in calls)


async def test_slug_path_escape_is_sanitised(mgr, remote):
    wt = await make(mgr, remote, 4, "../../x")
    assert wt.branch == "harness/4-x"
    assert mgr.root.resolve() in wt.path.parents


async def test_symlink_escape_rejected(mgr, remote, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (mgr.root / "o").mkdir(parents=True)
    (mgr.root / "o/r").symlink_to(outside)
    with pytest.raises(WorktreeError, match="escapes"):
        await make(mgr, remote)
    assert list(outside.iterdir()) == []


async def test_symlinked_issue_dir_rejected(mgr, remote, tmp_path):
    wt = await make(mgr, remote, 5, "s")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = mgr.root / "o/r/6"
    link.symlink_to(outside)
    with pytest.raises(WorktreeError, match="escapes"):
        await make(mgr, remote, 6, "s")
    with pytest.raises(WorktreeError):
        await mgr.clean(workspaces.Worktree(outside, "o/r", 5, wt.branch, "main"))


async def test_lock_exclusive_and_released(mgr, remote):
    wt = await make(mgr, remote)
    async with mgr.lock(wt, "a") as held:
        assert held == wt
        with pytest.raises(WorktreeBusy):
            async with mgr.lock(wt, "b", timeout=0.1):
                pytest.fail("should not acquire")
    async with mgr.lock(wt, "b", timeout=0.1):
        pass
    with pytest.raises(RuntimeError):
        async with mgr.lock(wt, "a"):
            raise RuntimeError("boom")
    async with mgr.lock(wt, "c", timeout=0.1):
        pass


async def test_lock_waits_for_release(mgr, remote):
    wt = await make(mgr, remote)
    order: list[str] = []

    async def first():
        async with mgr.lock(wt, "a"):
            order.append("a-in")
            await asyncio.sleep(0.2)
            order.append("a-out")

    async def second():
        await asyncio.sleep(0.05)
        async with mgr.lock(wt, "b", timeout=5):
            order.append("b-in")

    await asyncio.gather(first(), second())
    assert order == ["a-in", "a-out", "b-in"]


async def test_lock_across_processes(mgr, remote):
    wt = await make(mgr, remote)
    lock_file = mgr.root / "o/r/.cache.git/gargi-locks/7.lock"
    code = (
        "import fcntl,sys,time;f=open(sys.argv[1],'r+');fcntl.flock(f,fcntl.LOCK_EX);print('ok',flush=True);"
        "time.sleep(30)"
    )
    async with mgr.lock(wt, "x"):
        pass
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", code, str(lock_file), stdout=asyncio.subprocess.PIPE
    )
    try:
        assert (await proc.stdout.readline()).strip() == b"ok"
        with pytest.raises(WorktreeBusy):
            async with mgr.lock(wt, "y", timeout=0.1):
                pass
    finally:
        proc.kill()
        await proc.wait()


async def test_clean_idempotent(mgr, remote):
    wt = await make(mgr, remote)
    await mgr.clean(wt)
    assert not wt.path.exists()
    assert await mgr.get("o/r", 7) is None
    assert await mgr.list() == []
    await mgr.clean(wt)
    cache = mgr.root / "o/r/.cache.git"
    assert "harness/7-add-thing" in git(cache, "branch", "--list", "harness/*")
    again = await make(mgr, remote)  # branch survived, worktree re-attaches to it
    assert again.branch == wt.branch
    await mgr.clean(wt, delete_branch=True)
    await mgr.clean(wt, delete_branch=True)
    assert git(cache, "branch", "--list", "harness/*") == ""


async def test_clean_removes_dirty_worktree(mgr, remote):
    wt = await make(mgr, remote)
    (wt.path / "dirty.txt").write_text("x")
    await mgr.clean(wt)
    assert not wt.path.exists()


async def test_gc_respects_keep(mgr, remote):
    for i in (1, 2, 3):
        await make(mgr, remote, i, "s")
    assert await mgr.gc("o/r", keep={2}) == [1, 3]
    assert [w.issue for w in await mgr.list("o/r")] == [2]
    assert await mgr.gc("o/r", keep={2}) == []
    assert await mgr.gc("o/r", keep=set()) == [2]
    assert await mgr.list() == []


async def test_rebase_clean(mgr, remote, tmp_path):
    wt = await make(mgr, remote)
    (wt.path / "mine.txt").write_text("mine\n")
    git(wt.path, "add", "mine.txt")
    git(wt.path, "commit", "-qm", "mine")
    push_commit(tmp_path, remote, "other.txt", "other\n", "advance")
    result = await mgr.rebase(wt, "main")
    assert result.ok and result.conflicts == []
    assert (wt.path / "other.txt").exists() and (wt.path / "mine.txt").exists()
    assert git(wt.path, "status", "--porcelain") == ""


async def test_rebase_conflict_aborts(mgr, remote, tmp_path):
    wt = await make(mgr, remote)
    (wt.path / "a.txt").write_text("mine\n")
    git(wt.path, "commit", "-qam", "mine")
    head = git(wt.path, "rev-parse", "HEAD")
    push_commit(tmp_path, remote, "a.txt", "theirs\n", "theirs")
    result = await mgr.rebase(wt, "main")
    assert not result.ok and result.conflicts == ["a.txt"]
    assert git(wt.path, "status", "--porcelain") == ""
    assert git(wt.path, "rev-parse", "HEAD") == head
    assert not (wt.path / ".git").is_dir() and git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == wt.branch
    with pytest.raises(WorktreeError):
        await mgr.rebase(wt, "--abort")
    with pytest.raises(WorktreeError, match="not found"):
        await mgr.rebase(wt, "no-such-branch")


async def test_crash_after_worktree_add_recovers(mgr, remote, monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt("simulated crash")

    monkeypatch.setattr(WorktreeManager, "_write_meta", boom)
    with pytest.raises(KeyboardInterrupt):
        await make(mgr, remote)
    monkeypatch.undo()
    assert (mgr.root / "o/r/7").exists()
    wt = await make(mgr, remote)
    assert wt.branch == "harness/7-add-thing" and wt.base == "main"
    assert await mgr.get("o/r", 7) == wt


async def test_cache_fetched_on_every_create(mgr, remote, tmp_path):
    await make(mgr, remote, 1, "a")
    push_commit(tmp_path, remote, "new.txt", "n\n", "later")
    wt = await make(mgr, remote, 2, "b")
    assert (wt.path / "new.txt").exists()
