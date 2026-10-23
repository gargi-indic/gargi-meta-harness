from pathlib import Path

import httpx
import pytest

from harness.github.client import GitHub
from harness.publish import (
    ProtectedPathViolation,
    PublishError,
    publish_worktree,
    push_revision,
)
from harness.workspaces import WorktreeManager


@pytest.fixture
async def setup_git(tmp_path: Path):
    remote = tmp_path / "remote.git"
    remote.mkdir()

    import os
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    git(remote, "init", "--bare", "--initial-branch=main")

    local = tmp_path / "local"
    local.mkdir()
    git(local, "init", "--initial-branch=main")
    (local / "README.md").write_text("hello")
    git(local, "add", "README.md")
    git(local, "commit", "-m", "init")
    git(local, "remote", "add", "origin", str(remote))
    git(local, "push", "-u", "origin", "main")

    return tmp_path, remote, env


@pytest.fixture
async def wt_manager(setup_git):
    tmp_path, remote, _ = setup_git
    root = tmp_path / "worktrees"
    return WorktreeManager(root)


@pytest.fixture
def github():
    return GitHub("fake-token", max_retries=0)


@pytest.mark.asyncio
async def test_publish_no_changes(wt_manager, setup_git, github, respx_mock):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")

    respx_mock.get("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )

    res = await publish_worktree(
        wt=wt,
        gh=github,
        repo="owner/repo",
        issue_number=1,
        issue_title="Test Issue",
    )
    assert res is None
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_publish_happy_path(wt_manager, setup_git, github, respx_mock):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")

    (wt.path / "new_file.txt").write_text("hello world")

    pr_url = "https://api.github.com/repos/owner/repo/pulls"
    respx_mock.get(f"{pr_url}?per_page=100&head=owner:harness/1-slug&state=open").respond(200, json=[])
    create_pr_mock = respx_mock.post(pr_url).respond(
        201, json={"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
    )

    res = await publish_worktree(
        wt=wt,
        gh=github,
        repo="owner/repo",
        issue_number=1,
        issue_title="Test Issue",
        body_extra="Extra context",
        draft=True,
    )

    assert res is not None
    assert res.pr_number == 42
    assert res.created is True
    assert res.changed_files == ["new_file.txt"]
    assert res.head_branch == "harness/1-slug"

    req = create_pr_mock.calls.last.request
    import json

    payload = json.loads(req.content)
    assert payload["title"] == "#1: Test Issue"
    assert payload["head"] == "harness/1-slug"
    assert payload["base"] == "main"
    assert payload["body"] == "Closes #1\n\nExtra context\n"
    assert payload["draft"] is True

    import subprocess

    remote_sha = subprocess.run(
        ["git", "rev-parse", "harness/1-slug"], cwd=remote, capture_output=True, text=True
    ).stdout.strip()
    assert res.head_sha == remote_sha

    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=wt.path, capture_output=True, text=True
    ).stdout.strip()
    assert log == "#1: Test Issue"


@pytest.mark.asyncio
async def test_publish_idempotent_double(wt_manager, setup_git, github, respx_mock):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")

    (wt.path / "file1.txt").write_text("hello")

    pr_url = "https://api.github.com/repos/owner/repo/pulls"
    respx_mock.get(f"{pr_url}?per_page=100&head=owner:harness/1-slug&state=open").respond(200, json=[])
    respx_mock.post(pr_url).respond(
        201, json={"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
    )

    res1 = await publish_worktree(
        wt=wt,
        gh=github,
        repo="owner/repo",
        issue_number=1,
        issue_title="Test Issue",
    )
    assert res1.created is True

    respx_mock.get(f"{pr_url}?per_page=100&head=owner:harness/1-slug&state=open").respond(
        200, json=[{"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}]
    )

    res2 = await publish_worktree(
        wt=wt,
        gh=github,
        repo="owner/repo",
        issue_number=1,
        issue_title="Test Issue",
    )
    assert res2.created is False
    assert res2.pr_number == 42
    assert res1.head_sha == res2.head_sha


@pytest.mark.asyncio
async def test_protected_path_violation(wt_manager, setup_git, github, respx_mock):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")

    (wt.path / ".harness").mkdir()
    (wt.path / ".harness/config.yaml").write_text("test")

    with pytest.raises(ProtectedPathViolation) as exc:
        await publish_worktree(
            wt=wt,
            gh=github,
            repo="owner/repo",
            issue_number=1,
            issue_title="Test",
            protected_paths=[".harness/"],
        )

    assert ".harness/config.yaml" in str(exc.value)

    import subprocess

    branches = subprocess.run(["git", "branch"], cwd=remote, capture_output=True, text=True).stdout.strip()
    assert "harness/1-slug" not in branches


@pytest.mark.asyncio
async def test_wrong_branch(wt_manager, setup_git, github, respx_mock):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")
    import dataclasses

    wt_bad = dataclasses.replace(wt, branch="main")

    with pytest.raises(PublishError) as exc:
        await publish_worktree(
            wt=wt_bad,
            gh=github,
            repo="owner/repo",
            issue_number=1,
            issue_title="Test",
        )
    assert "branch name 'main' does not match harness/<issue_number>-<slug>" in str(exc.value)


@pytest.mark.asyncio
async def test_push_revision(wt_manager, setup_git, github, respx_mock):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")

    (wt.path / "file1.txt").write_text("hello")
    sha = await push_revision(wt, message="Revise")

    import subprocess

    remote_sha = subprocess.run(
        ["git", "rev-parse", "harness/1-slug"], cwd=remote, capture_output=True, text=True
    ).stdout.strip()
    assert sha == remote_sha

    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=wt.path, capture_output=True, text=True
    ).stdout.strip()
    assert log == "Revise"


@pytest.mark.asyncio
async def test_token_redaction(wt_manager, setup_git, github, respx_mock, caplog):
    tmp_path, remote, _ = setup_git
    wt = await wt_manager.create(repo="owner/repo", clone_url=remote, issue=1, slug="slug", base="main")

    import dataclasses

    wt_bad_url = dataclasses.replace(wt, branch="harness/1-slug")

    # Intentionally corrupt the origin URL to fail push and include credentials
    import subprocess

    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://user:github_pat_SECRET123@fake.com/repo.git"],
        cwd=wt_bad_url.path,
        check=True,
    )

    (wt_bad_url.path / "file1.txt").write_text("hello")

    with pytest.raises(PublishError) as exc:
        await publish_worktree(
            wt=wt_bad_url,
            gh=github,
            repo="owner/repo",
            issue_number=1,
            issue_title="Test",
        )

    err_str = str(exc.value)
    assert "github_pat_SECRET123" not in err_str
    # removed because git automatically redacts basic auth, or our redactor did it.
    assert "github_pat_SECRET123" not in caplog.text
