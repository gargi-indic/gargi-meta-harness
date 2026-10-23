"""Git worktree manager (M3.1): an isolated checkout per issue, shared bare cache per repo.

Layout under ``root``: ``<repo>/.cache.git`` (bare clone, fetched on every ``create``) and
``<repo>/<issue>`` (one worktree per issue on branch ``harness/<issue>-<slug>``). ``repo`` is ``name`` or
``owner/name``. All git calls go through ``asyncio.create_subprocess_exec`` and every ref or path that
reaches git is validated first; nothing outside ``root`` is ever touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = ".cache.git"
_META_DIR = "gargi-worktrees"
_LOCK_DIR = "gargi-locks"
_REPO_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_FETCH_SPEC = "+refs/heads/*:refs/remotes/origin/*"


class WorktreeError(Exception):
    """A worktree operation failed or was refused."""


class WorktreeBusy(WorktreeError):
    """Another holder has the worktree lock and the timeout expired."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    repo: str
    issue: int
    branch: str
    base: str


@dataclass(frozen=True)
class RebaseResult:
    ok: bool
    conflicts: list[str] = field(default_factory=list)


def sanitize_slug(slug: str) -> str:
    """Lower-case, anything outside ``[a-z0-9-]`` becomes ``-``, collapsed, max 40 chars, never empty."""
    s = re.sub(r"[^a-z0-9-]+", "-", slug.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")[:40].strip("-")
    return s or "task"


def branch_name(issue: int, slug: str) -> str:
    return f"harness/{issue}-{sanitize_slug(slug)}"


def validate_ref(ref: str) -> str:
    """Reject anything that is not a plain branch-like ref (no option-like or odd strings)."""
    if (
        not isinstance(ref, str)
        or not _REF.match(ref)
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or ref.endswith(("/", ".", ".lock"))
    ):
        raise WorktreeError(f"invalid ref: {ref!r}")
    return ref


def _validate_repo(repo: str) -> str:
    parts = repo.split("/") if isinstance(repo, str) else []
    if not 1 <= len(parts) <= 2 or not all(_REPO_PART.match(p) and p not in {".", ".."} for p in parts):
        raise WorktreeError(f"invalid repo name: {repo!r}")
    return repo


def _validate_issue(issue: int) -> int:
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        raise WorktreeError(f"invalid issue number: {issue!r}")
    return issue


class WorktreeManager:
    def __init__(self, root: Path, *, git: str = "git") -> None:
        self.root = Path(root).expanduser()
        self._git = git

    # -- paths and safety ---------------------------------------------------------------------------

    def _inside(self, path: Path) -> Path:
        """``path`` resolved (symlinks followed); raises unless it is strictly inside the resolved root."""
        root = self.root.resolve()
        resolved = path.resolve()
        if resolved == root or root not in resolved.parents:
            raise WorktreeError(f"path escapes the worktree root: {path}")
        return resolved

    def _cache(self, repo: str) -> Path:
        return self._inside(self.root / _validate_repo(repo) / CACHE_DIR)

    def _wt_path(self, repo: str, issue: int) -> Path:
        _validate_issue(issue)
        self._inside(self.root / _validate_repo(repo))
        return self._inside(self.root / repo / str(issue))

    def _checked(self, wt: Worktree) -> Path:
        path = self._wt_path(wt.repo, wt.issue)
        if self._inside(wt.path) != path:
            raise WorktreeError(f"worktree path does not match its repo/issue: {wt.path}")
        return path

    # -- git ----------------------------------------------------------------------------------------

    async def _run(self, *args: str, cwd: Path | None = None, check: bool = True) -> tuple[int, str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        proc = await asyncio.create_subprocess_exec(
            self._git,
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        code = proc.returncode or 0
        o, e = out.decode(errors="replace"), err.decode(errors="replace")
        if check and code != 0:
            raise WorktreeError(f"git {' '.join(args[:2])} failed ({code}): {e.strip() or o.strip()}")
        return code, o, e

    async def _ensure_cache(self, repo: str, clone_url: str | Path) -> Path:
        cache = self._cache(repo)
        url = str(clone_url)
        if url.startswith("-"):
            raise WorktreeError(f"invalid clone url: {url!r}")
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            await self._run("clone", "--bare", "--", url, str(cache))
            await self._run("config", "remote.origin.url", url, cwd=cache)
            await self._run("config", "remote.origin.fetch", _FETCH_SPEC, cwd=cache)
        elif not (cache / "HEAD").is_file():
            raise WorktreeError(f"cache exists but is not a git repository: {cache}")
        await self._run("fetch", "--prune", "origin", cwd=cache)
        return cache

    async def _registered(self, cache: Path) -> dict[Path, str | None]:
        """Worktrees git knows about (excluding the bare cache itself): resolved path -> branch."""
        _, out, _ = await self._run("worktree", "list", "--porcelain", cwd=cache)
        found: dict[Path, str | None] = {}
        for block in out.split("\n\n"):
            path: Path | None = None
            branch: str | None = None
            bare = False
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = Path(line[len("worktree ") :])
                elif line.startswith("branch refs/heads/"):
                    branch = line[len("branch refs/heads/") :]
                elif line == "bare":
                    bare = True
            if path is not None and not bare:
                found[path.resolve()] = branch
        return found

    # -- metadata (the base a worktree was cut from) ------------------------------------------------

    def _meta_path(self, cache: Path, issue: int) -> Path:
        return cache / _META_DIR / f"{issue}.json"

    def _write_meta(self, cache: Path, issue: int, branch: str, base: str) -> None:
        p = self._meta_path(cache, issue)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"branch": branch, "base": base}), encoding="utf-8")
        tmp.replace(p)

    def _read_meta(self, cache: Path, issue: int) -> dict[str, str]:
        try:
            data = json.loads(self._meta_path(cache, issue).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- public API ---------------------------------------------------------------------------------

    async def create(self, *, repo: str, clone_url: str | Path, issue: int, slug: str, base: str) -> Worktree:
        """Create (or recover) the worktree for ``issue`` on ``harness/<issue>-<slug>`` cut from ``base``."""
        _validate_repo(repo)
        _validate_issue(issue)
        validate_ref(base)
        branch = branch_name(issue, slug)
        validate_ref(branch)
        path = self._wt_path(repo, issue)
        cache = await self._ensure_cache(repo, clone_url)
        await self._run("worktree", "prune", cwd=cache)

        if os.path.lexists(path):
            registered = await self._registered(cache)
            if path not in registered:
                raise WorktreeError(f"{path} exists but is not a valid worktree")
            if registered[path] != branch:
                raise WorktreeError(f"{path} is on branch {registered[path]!r}, expected {branch!r}")
            meta = self._read_meta(cache, issue)
            if meta.get("base") != base and not meta:
                self._write_meta(cache, issue, branch, base)  # recovered after a crash before metadata
                meta = {"base": base}
            return Worktree(path, repo, issue, branch, meta.get("base", base))

        start = f"refs/remotes/origin/{base}"
        code, _, _ = await self._run(
            "rev-parse", "--verify", "--quiet", f"{start}^{{commit}}", cwd=cache, check=False
        )
        if code != 0:
            raise WorktreeError(f"base branch not found on origin: {base!r}")
        code, _, _ = await self._run(
            "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=cache, check=False
        )
        if code == 0:
            await self._run("worktree", "add", "--", str(path), branch, cwd=cache)
        else:
            await self._run("worktree", "add", "-b", branch, "--", str(path), start, cwd=cache)
        self._write_meta(cache, issue, branch, base)
        return Worktree(path, repo, issue, branch, base)

    async def get(self, repo: str, issue: int) -> Worktree | None:
        path = self._wt_path(repo, issue)
        cache = self._cache(repo)
        if not cache.is_dir() or not os.path.lexists(path):
            return None
        branch = (await self._registered(cache)).get(path)
        if branch is None:
            return None
        return Worktree(path, repo, issue, branch, self._read_meta(cache, issue).get("base", ""))

    @asynccontextmanager
    async def lock(self, wt: Worktree, owner: str, *, timeout: float = 30.0) -> AsyncIterator[Worktree]:
        """Exclusive cross-process lock on ``wt``; raises :class:`WorktreeBusy` after ``timeout`` seconds."""
        self._checked(wt)
        lock_dir = self._cache(wt.repo) / _LOCK_DIR
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_dir / f"{wt.issue}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WorktreeBusy(f"worktree {wt.repo}#{wt.issue} is locked") from None
                    await asyncio.sleep(0.02)
            os.ftruncate(fd, 0)
            os.write(fd, owner.encode())
            try:
                yield wt
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    async def clean(self, wt: Worktree, *, delete_branch: bool = False) -> None:
        """Remove the worktree, prune, optionally delete its local branch. Safe to repeat."""
        path = self._checked(wt)
        cache = self._cache(wt.repo)
        if not cache.is_dir():
            return
        if path in await self._registered(cache):
            await self._run("worktree", "remove", "--force", "--", str(path), cwd=cache)
        if os.path.lexists(path):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        await self._run("worktree", "prune", cwd=cache)
        if delete_branch:
            branch = validate_ref(wt.branch)
            code, _, _ = await self._run(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=cache, check=False
            )
            if code == 0:
                await self._run("branch", "-D", "--", branch, cwd=cache)
        with contextlib.suppress(FileNotFoundError):
            self._meta_path(cache, wt.issue).unlink()

    async def rebase(self, wt: Worktree, onto: str) -> RebaseResult:
        """Rebase the worktree branch onto ``onto``; on conflict abort and list the conflicting paths."""
        path = self._checked(wt)
        validate_ref(onto)
        cache = self._cache(wt.repo)
        await self._run("fetch", "--prune", "origin", cwd=cache)
        target = f"refs/remotes/origin/{onto}"
        code, _, _ = await self._run(
            "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}", cwd=cache, check=False
        )
        if code != 0:
            code, _, _ = await self._run(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{onto}^{{commit}}", cwd=cache, check=False
            )
            if code != 0:
                raise WorktreeError(f"rebase target not found: {onto!r}")
            target = f"refs/heads/{onto}"
        ident = ("-c", "user.name=gargi-harness", "-c", "user.email=harness@localhost")
        code, _, err = await self._run(*ident, "rebase", target, cwd=path, check=False)
        if code == 0:
            return RebaseResult(True, [])
        _, out, _ = await self._run("diff", "--name-only", "--diff-filter=U", cwd=path, check=False)
        conflicts = sorted(line for line in out.splitlines() if line)
        await self._run("rebase", "--abort", cwd=path, check=False)
        if not conflicts:
            raise WorktreeError(f"rebase failed: {err.strip()}")
        return RebaseResult(False, conflicts)

    async def list(self, repo: str | None = None) -> list[Worktree]:
        if repo is not None:
            caches = {repo: self._cache(repo)}
        else:
            caches = {}
            if self.root.is_dir():
                for hit in sorted([*self.root.glob(f"*/{CACHE_DIR}"), *self.root.glob(f"*/*/{CACHE_DIR}")]):
                    name = hit.parent.relative_to(self.root).as_posix()
                    if _REPO_PART.match(name.split("/")[0]):
                        caches[name] = hit
        found: list[Worktree] = []
        for name, cache in caches.items():
            if not cache.is_dir():
                continue
            for path, branch in (await self._registered(cache)).items():
                try:
                    if path.parent != self._inside(self.root / name) or not path.name.isdigit() or not branch:
                        continue
                except WorktreeError:
                    continue
                issue = int(path.name)
                found.append(
                    Worktree(path, name, issue, branch, self._read_meta(cache, issue).get("base", ""))
                )
        return sorted(found, key=lambda w: (w.repo, w.issue))

    async def gc(self, repo: str, *, keep: set[int]) -> list[int]:
        """Remove worktrees of ``repo`` whose issue is not in ``keep``; returns the removed issue numbers."""
        removed: list[int] = []
        for wt in await self.list(repo):
            if wt.issue not in keep:
                await self.clean(wt)
                removed.append(wt.issue)
        return removed
