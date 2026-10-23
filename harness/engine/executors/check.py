"""Deterministic PR checks: ``ci``, ``scope``, ``pinned_versions`` (and the ``command`` stub until M3).

Every check re-reads the PR head from GitHub, so a force-push between two ``advance`` calls is seen. Every
output carries ``head_sha`` (the merge executor compares it), stays small, and every ``fail`` carries a
non-empty ``correction`` string that a revise node can show to the agent.
"""

import re
from typing import Any
from urllib.parse import quote

from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.github.client import GitHub, GitHubError
from harness.playbook.models import CheckNode
from harness.scope import match_protected as _is_protected
from harness.store.repo import StepRow

MAX_ITEMS = 20
_LINE_MAX = 200

_CI_OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_FLOATING_REFS = frozenset({"main", "master", "latest"})
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_USES_RE = re.compile(r"""^\+\s*-?\s*uses:\s*["']?([^"'\s#]+)""")
_FROM_RE = re.compile(r"^[+ ]\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_IMAGE_RE = re.compile(r"""^\+\s*-?\s*image:\s*["']?([^"'\s#]+)""")


class CheckExecutor:
    def __init__(self, *, no_ci_timeout: float = 600.0, poll: float = 60.0) -> None:
        self.no_ci_timeout = no_ci_timeout
        self.poll = poll

    async def advance(self, step: StepRow, node: CheckNode, ctx: ExecContext) -> Waiting | Done:
        if node.check == "command":
            return self._command()

        number = _pr_number(ctx)
        if number is None:
            return Done(
                "fail",
                {
                    "error": "no PR in run context",
                    "correction": "No pull request is attached to this run yet, so there is nothing to check. "
                    "Open a pull request for your branch and try again.",
                },
            )
        if ctx.gh is None:
            raise RuntimeError("check executors need a GitHub client")

        repo = ctx.run.repo
        # Never trust a cached sha: the head moves whenever the agent pushes.
        pull = await ctx.gh.pull(repo, number)
        sha = str(pull["head"]["sha"])

        if node.check == "ci":
            return await self._ci(step, ctx.gh, ctx, repo, sha)
        if node.check == "scope":
            return await self._scope(ctx.gh, ctx, repo, number, sha)
        return await self._pinned(ctx.gh, repo, number, sha)

    # command ---------------------------------------------------------------------------------------------

    def _command(self) -> Done:
        return Done(
            "fail",
            {
                "note": "command checks need a local workspace (M3)",
                "correction": "Command checks are not available yet because they need a local workspace (M3). "
                "This playbook cannot verify the change with a command; ask a maintainer to review it.",
            },
        )

    # ci --------------------------------------------------------------------------------------------------

    async def _ci(self, step: StepRow, gh: GitHub, ctx: ExecContext, repo: str, sha: str) -> Waiting | Done:
        runs = await gh.check_runs(repo, sha)
        combined = await gh.combined_status(repo, sha)
        statuses = [s for s in (combined.get("statuses") or []) if isinstance(s, dict)]

        # A new head sha resets the no-CI clock.
        since = ctx.clock()
        if step.ref and step.ref.get("sha") == sha and isinstance(step.ref.get("since"), int | float):
            since = float(step.ref["since"])
        waiting = Waiting(ref={"sha": sha, "since": since}, poll_after=self.poll)

        pending = any(r.get("status") != "completed" for r in runs) or any(
            s.get("state") == "pending" for s in statuses
        )
        if pending:
            return waiting

        if not runs and not statuses:
            if ctx.clock() - since < self.no_ci_timeout:
                return waiting
            minutes = f"{self.no_ci_timeout / 60:g}"
            return Done(
                "fail",
                {
                    "head_sha": sha,
                    "failed": [],
                    "note": "no CI reported",
                    "correction": f"No CI reported for {sha[:7]} within {minutes} minutes. Check that the "
                    "workflows trigger on pull requests for this branch, then push a fix to the same branch.",
                },
            )

        failed: list[dict[str, Any]] = []
        for run in runs:
            if run.get("conclusion") not in _CI_OK_CONCLUSIONS:
                failed.append(
                    {
                        "name": str(run.get("name") or "(unnamed check)"),
                        "conclusion": str(run.get("conclusion") or "unknown"),
                        "url": str(run.get("html_url") or ""),
                    }
                )
        for status in statuses:
            if status.get("state") != "success":
                failed.append(
                    {
                        "name": str(status.get("context") or "(unnamed status)"),
                        "conclusion": str(status.get("state") or "unknown"),
                        "url": str(status.get("target_url") or ""),
                    }
                )

        if not failed:
            return Done("pass", {"head_sha": sha, "checks": len(runs) + len(statuses)})

        shown = failed[:MAX_ITEMS]
        lines = [f"{i}. {f['name']}: {f['conclusion']} ({f['url']})" for i, f in enumerate(shown, 1)]
        if len(failed) > len(shown):
            lines.append(f"... and {len(failed) - len(shown)} more")
        correction = f"CI failed on {sha[:7]}. Fix these checks and push to the same branch:\n" + "\n".join(
            lines
        )
        return Done("fail", {"head_sha": sha, "failed": shown, "correction": correction})

    # scope -----------------------------------------------------------------------------------------------

    async def _scope(self, gh: GitHub, ctx: ExecContext, repo: str, number: int, sha: str) -> Done:
        files = await gh.pull_files(repo, number)
        protected = [p for p in ctx.repo_config.protected_paths if p]

        violations: list[str] = []
        for f in files:
            for path in (f.get("filename"), f.get("previous_filename")):
                if isinstance(path, str) and path not in violations and _is_protected(path, protected):
                    violations.append(path)

        if not violations:
            return Done("pass", {"head_sha": sha, "files": len(files)})

        shown = violations[:MAX_ITEMS]
        listing = ", ".join(shown)
        if len(violations) > len(shown):
            listing += f", ... and {len(violations) - len(shown)} more"
        return Done(
            "fail",
            {
                "head_sha": sha,
                "violations": shown,
                "correction": f"These files are protected and must not be changed: {listing}. "
                "Revert them on the same branch.",
            },
        )

    # pinned_versions -------------------------------------------------------------------------------------

    async def _pinned(self, gh: GitHub, repo: str, number: int, sha: str) -> Done:
        files = await gh.pull_files(repo, number)
        tag_cache: dict[tuple[str, str], bool] = {}
        problems: list[dict[str, str]] = []

        for f in files:
            patch = f.get("patch")
            filename = f.get("filename")
            if not isinstance(patch, str) or not isinstance(filename, str):
                continue
            if _is_workflow(filename):
                problems.extend(await _workflow_problems(gh, filename, patch, tag_cache))
            elif _is_dockerfile(filename):
                problems.extend(_dockerfile_problems(filename, patch))
            elif _is_compose(filename):
                problems.extend(_compose_problems(filename, patch))

        if not problems:
            return Done("pass", {"head_sha": sha, "problems": []})

        shown = problems[:MAX_ITEMS]
        lines = [f"{i}. {p['file']}: {p['problem']}: {p['line']}" for i, p in enumerate(shown, 1)]
        if len(problems) > len(shown):
            lines.append(f"... and {len(problems) - len(shown)} more")
        correction = (
            f"Unpinned or unknown versions in {sha[:7]}. Pin actions to an existing tag or a full commit sha "
            "and images to a specific tag or digest, then push to the same branch:\n" + "\n".join(lines)
        )
        return Done("fail", {"head_sha": sha, "problems": shown, "correction": correction})


# helpers -------------------------------------------------------------------------------------------------


def _pr_number(ctx: ExecContext) -> int | None:
    pr = ctx.run.context.get("pr")
    if not isinstance(pr, dict):
        return None
    number = pr.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return number


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_workflow(path: str) -> bool:
    prefix = ".github/workflows/"
    return path.startswith(prefix) and "/" not in path[len(prefix) :] and path.endswith((".yml", ".yaml"))


def _is_dockerfile(path: str) -> bool:
    name = _basename(path)
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".dockerfile")


def _is_compose(path: str) -> bool:
    name = _basename(path)
    return name.startswith(("docker-compose", "compose")) and name.endswith((".yml", ".yaml"))


def _problem(file: str, line: str, problem: str) -> dict[str, str]:
    text = line[1:].strip() if line[:1] in "+ " else line.strip()
    return {"file": file, "line": text[:_LINE_MAX], "problem": problem}


def _added(patch: str) -> list[str]:
    return [ln for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


async def _tag_exists(gh: GitHub, owner_repo: str, ref: str, cache: dict[tuple[str, str], bool]) -> bool:
    key = (owner_repo, ref)
    if key not in cache:
        try:
            await gh.get(f"repos/{owner_repo}/git/ref/tags/{quote(ref, safe='/')}")
            cache[key] = True
        except GitHubError as e:
            if e.status != 404:
                raise
            cache[key] = False
    return cache[key]


async def _workflow_problems(
    gh: GitHub, filename: str, patch: str, cache: dict[tuple[str, str], bool]
) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for line in _added(patch):
        match = _USES_RE.match(line)
        if not match:
            continue
        target = match.group(1)
        if target.startswith(("./", "docker://")):
            continue
        action, at, ref = target.partition("@")
        if not at or not ref or ref.lower() in _FLOATING_REFS:
            problems.append(_problem(filename, line, "unpinned ref"))
            continue
        if _SHA_RE.match(ref):
            continue
        parts = action.split("/")
        if len(parts) < 2:
            problems.append(_problem(filename, line, "unpinned ref"))
            continue
        if not await _tag_exists(gh, f"{parts[0]}/{parts[1]}", ref, cache):
            problems.append(_problem(filename, line, "tag not found"))
    return problems


def _unpinned_image(image: str) -> bool:
    if "$" in image:
        # A build arg or env substitution cannot be judged from the diff.
        return False
    if "@sha256:" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return True
    return last.rsplit(":", 1)[1].lower() == "latest"


def _dockerfile_problems(filename: str, patch: str) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    stages: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("-", "+++", "---", "@@")):
            continue
        match = _FROM_RE.match(line)
        if not match:
            continue
        image, alias = match.group(1), match.group(2)
        if line.startswith("+") and image.lower() != "scratch" and image.lower() not in stages:
            if _unpinned_image(image):
                problems.append(_problem(filename, line, "unpinned image"))
        if alias:
            stages.add(alias.lower())
    return problems


def _compose_problems(filename: str, patch: str) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for line in _added(patch):
        match = _IMAGE_RE.match(line)
        if match and _unpinned_image(match.group(1)):
            problems.append(_problem(filename, line, "unpinned image"))
    return problems
