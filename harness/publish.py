import asyncio
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from subprocess import DEVNULL

from harness.github.client import GitHub
from harness.scope import match_protected
from harness.workspaces import Worktree, validate_ref


@dataclass(frozen=True)
class PublishResult:
    pr_number: int
    pr_url: str
    head_branch: str
    head_sha: str
    created: bool
    changed_files: list[str]


class PublishError(Exception):
    pass


class ProtectedPathViolation(PublishError):
    def __init__(self, paths: list[str]) -> None:
        super().__init__(f"Protected path(s) changed: {', '.join(paths)}")
        self.paths = paths


async def _run_git(cwd: os.PathLike[str], *args: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdin=DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _redact_token(text: str) -> str:
    text = re.sub(r"://[^@]+@", "://***@", text)
    text = re.sub(r"(Bearer|ghp_|github_pat_)[^\s'\"\\:]+", "***", text)
    return text


async def _changed_paths(wt: Worktree, git: str, base_ref: str | None = None) -> list[str]:
    cmds = []
    if base_ref:
        cmds.append((git, "diff", "--name-only", "--no-renames", "-z", f"{base_ref}...HEAD"))
    cmds.extend(
        [
            (git, "diff", "--name-only", "--no-renames", "-z"),
            (git, "diff", "--name-only", "--no-renames", "--cached", "-z"),
            (git, "ls-files", "--others", "--exclude-standard", "-z"),
        ]
    )

    all_changed = set()
    for cmd in cmds:
        code, out, err = await _run_git(wt.path, *cmd)
        if code != 0:
            raise PublishError(f"{' '.join(cmd)} failed: {_redact_token(err)}")
        if out:
            all_changed.update(f for f in out.split("\0") if f)

    return sorted(list(all_changed))


async def _commit(wt: Worktree, git: str, author: tuple[str, str] | None, message: str) -> None:
    code, _, err = await _run_git(wt.path, git, "add", "-A")
    if code != 0:
        raise PublishError(f"git add failed: {_redact_token(err)}")

    commit_cmd = [git]
    if author:
        commit_cmd.extend(["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"])
    else:
        commit_cmd.extend(["-c", "user.name=gargi-harness", "-c", "user.email=harness@localhost"])
    commit_cmd.extend(["commit", "-m", message])

    code, _, err = await _run_git(wt.path, *commit_cmd)
    if code != 0:
        raise PublishError(f"git commit failed: {_redact_token(err)}")


async def publish_worktree(
    *,
    wt: Worktree,
    gh: GitHub,
    repo: str,
    issue_number: int,
    issue_title: str,
    body_extra: str = "",
    draft: bool = False,
    protected_paths: Sequence[str] = (),
    git: str = "git",
    author: tuple[str, str] | None = None,
) -> PublishResult | None:
    branch_re = re.compile(rf"^harness/{issue_number}-[a-z0-9-]+$")
    if not branch_re.match(wt.branch):
        raise PublishError(f"branch name {wt.branch!r} does not match harness/<issue_number>-<slug>")
    validate_ref(wt.branch)
    if wt.branch == wt.base:
        raise PublishError(f"branch name equals base {wt.base!r}")

    # Check dirty state and commits ahead of base
    code, status_out, err = await _run_git(wt.path, git, "status", "--porcelain")
    if code != 0:
        raise PublishError(f"git status failed: {_redact_token(err)}")

    base_ref = f"refs/remotes/origin/{wt.base}"
    code, _, _ = await _run_git(wt.path, git, "rev-parse", "--verify", "--quiet", base_ref)
    if code != 0:
        base_ref = wt.base
        validate_ref(base_ref)

    code, rev_count, err = await _run_git(wt.path, git, "rev-list", "--count", f"{base_ref}..HEAD")
    if code != 0:
        raise PublishError(f"git rev-list failed: {_redact_token(err)}")

    if not status_out.strip() and rev_count.strip() == "0":
        return None

    all_changed_list = await _changed_paths(wt, git, base_ref)
    violations = [p for p in all_changed_list if match_protected(p, protected_paths)]
    if violations:
        raise ProtectedPathViolation(violations)

    if status_out.strip():
        await _commit(wt, git, author, f"#{issue_number}: {issue_title}")

    code, _, err = await _run_git(
        wt.path, git, "push", "--force-with-lease", "origin", f"{wt.branch}:{wt.branch}"
    )
    if code != 0:
        raise PublishError(f"git push failed: {_redact_token(err)}")

    code, head_sha_out, err = await _run_git(wt.path, git, "rev-parse", "HEAD")
    if code != 0:
        raise PublishError(f"git rev-parse failed: {_redact_token(err)}")
    head_sha = head_sha_out.strip()

    pulls = await gh.pulls_for_head(repo, wt.branch, state="open")

    code, final_diff, err = await _run_git(
        wt.path, git, "diff", "--name-only", "--no-renames", "-z", f"{base_ref}...HEAD"
    )
    if code != 0:
        raise PublishError(f"git diff failed: {_redact_token(err)}")
    changed_files = sorted(list(set(f for f in final_diff.split("\0") if f)))

    if pulls:
        pr = pulls[0]
        return PublishResult(
            pr_number=pr["number"],
            pr_url=pr.get("html_url", ""),
            head_branch=wt.branch,
            head_sha=head_sha,
            created=False,
            changed_files=changed_files,
        )

    body = (
        f"Closes #{issue_number}\n\n{body_extra}".rstrip() + "\n" if body_extra else f"Closes #{issue_number}"
    )

    pr = await gh.create_pull(
        repo,
        f"#{issue_number}: {issue_title}",
        wt.branch,
        wt.base,
        body=body,
        draft=draft,
    )
    return PublishResult(
        pr_number=pr["number"],
        pr_url=pr.get("html_url", ""),
        head_branch=wt.branch,
        head_sha=head_sha,
        created=True,
        changed_files=changed_files,
    )


async def push_revision(
    wt: Worktree,
    *,
    message: str,
    git: str = "git",
    author: tuple[str, str] | None = None,
    protected_paths: Sequence[str] = (),
) -> str:
    issue_number = wt.issue
    branch_re = re.compile(rf"^harness/{issue_number}-[a-z0-9-]+$")
    if not branch_re.match(wt.branch):
        raise PublishError(f"branch name {wt.branch!r} does not match harness/<issue_number>-<slug>")
    validate_ref(wt.branch)
    if wt.branch == wt.base:
        raise PublishError(f"branch name equals base {wt.base!r}")

    code, status_out, err = await _run_git(wt.path, git, "status", "--porcelain")
    if code != 0:
        raise PublishError(f"git status failed: {_redact_token(err)}")
    if not status_out.strip():
        raise PublishError("nothing to commit")

    all_changed_list = await _changed_paths(wt, git)
    violations = [p for p in all_changed_list if match_protected(p, protected_paths)]
    if violations:
        raise ProtectedPathViolation(violations)

    await _commit(wt, git, author, message)

    code, _, err = await _run_git(
        wt.path, git, "push", "--force-with-lease", "origin", f"{wt.branch}:{wt.branch}"
    )
    if code != 0:
        raise PublishError(f"git push failed: {_redact_token(err)}")

    code, head_sha_out, err = await _run_git(wt.path, git, "rev-parse", "HEAD")
    if code != 0:
        raise PublishError(f"git rev-parse failed: {_redact_token(err)}")

    return head_sha_out.strip()
