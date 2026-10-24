"""Playbook listing, reading, validation and save-as-PR for the editor API (M2.8)."""

import asyncio
import base64
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field, ValidationError

from harness.github.client import GitHub, GitHubError
from harness.playbook.models import Playbook
from harness.playbook.validate import validate
from harness.secrets import redact

log = logging.getLogger(__name__)

PLAYBOOK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
MAX_ID_LEN = 64
MAX_YAML_CHARS = 256_000
MAX_YAML_DEPTH = 64
REPO_DIR = ".harness/playbooks"
_YAML_SUFFIXES = (".yaml", ".yml")
_BRANCH_SUFFIX_RE = re.compile(r"\d{14}")


def valid_playbook_id(playbook_id: str) -> bool:
    return len(playbook_id) <= MAX_ID_LEN and PLAYBOOK_ID_RE.fullmatch(playbook_id) is not None


# ---- validation -------------------------------------------------------------------------------------------


class Issue(BaseModel):
    severity: Literal["error", "warning"] = Field(default="error", description="Only errors block a save")
    code: str = Field(description="Stable machine-readable issue code")
    message: str = Field(description="Human-readable explanation")
    node: str | None = Field(default=None, description="Node the issue is about, if any")
    port: str | None = Field(default=None, description="Port of that node, for edge issues")
    path: str | None = Field(
        default=None, description="Dotted location, or 'line L, column C' for YAML syntax"
    )


class ValidationResult(BaseModel):
    ok: bool
    issues: list[Issue]
    # Set whenever the YAML parsed into a Playbook, even if the validator then reported issues.
    playbook: Playbook | None = None


def _failed(*issues: Issue, playbook: Playbook | None = None) -> ValidationResult:
    return ValidationResult(ok=False, issues=list(issues), playbook=playbook)


def _yaml_syntax_issue(exc: Exception) -> Issue:
    mark = getattr(exc, "problem_mark", None)
    path = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else None
    problem = getattr(exc, "problem", None) or str(exc)
    return Issue(code="yaml_syntax", message=str(problem), path=path)


def _schema_issues(exc: ValidationError) -> list[Issue]:
    issues = []
    for err in exc.errors():
        loc = err["loc"]
        node = str(loc[1]) if len(loc) > 1 and loc[0] == "nodes" else None
        # `msg` is pydantic's own wording; it carries no Python type names.
        issues.append(Issue(code="schema", message=err["msg"], node=node, path=".".join(str(p) for p in loc)))
    return issues


def _validator_issue(path: str, code: str, message: str, severity: str = "error") -> Issue:
    node: str | None = None
    port: str | None = None
    parts = path.split(".")
    if parts[0] == "nodes" and len(parts) > 1:
        node = parts[1]
    elif parts[0] == "edges" and len(parts) == 3:
        node = None if parts[1] == "*" else parts[1]
        port = parts[2]
    return Issue(code=code, message=message, node=node, port=port, path=path, severity=severity)  # type: ignore[arg-type]


_OPENERS = (
    yaml.FlowSequenceStartToken,
    yaml.FlowMappingStartToken,
    yaml.BlockSequenceStartToken,
    yaml.BlockMappingStartToken,
)
_CLOSERS = (yaml.FlowSequenceEndToken, yaml.FlowMappingEndToken, yaml.BlockEndToken)


def _scan_guard(text: str) -> Issue | None:
    """Reject anchors/aliases (alias bomb) and absurd nesting before ``safe_load`` builds anything.

    The scan is lazy, so it stops at the first offending token; a scanner error is left to the load that follows.
    """
    depth = 0
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.AliasToken, yaml.AnchorToken)):
                return Issue(code="yaml_aliases", message="anchors and aliases are not supported")
            if isinstance(token, _OPENERS):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    return Issue(
                        code="yaml_too_deep", message=f"YAML is nested more than {MAX_YAML_DEPTH} levels deep"
                    )
            elif isinstance(token, _CLOSERS):
                depth -= 1
    except yaml.YAMLError:
        return None
    return None


def validate_yaml(text: str, agent_caps: dict[str, set[str]] | None = None) -> ValidationResult:
    """Parse and validate playbook YAML. Never raises for any input text."""
    try:
        return _validate_yaml(text, agent_caps)
    except Exception:
        log.exception("playbook validation crashed")
        return _failed(Issue(code="internal", message="internal error while validating the playbook"))


def _validate_yaml(text: str, agent_caps: dict[str, set[str]] | None) -> ValidationResult:
    if len(text) > MAX_YAML_CHARS:
        return _failed(
            Issue(code="too_large", message=f"playbook is longer than {MAX_YAML_CHARS} characters")
        )
    if (guard := _scan_guard(text)) is not None:
        return _failed(guard)
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as exc:
        # ValueError: constructors such as int (over 4300 digits), timestamps (9999-99-99, bad offsets).
        return _failed(_yaml_syntax_issue(exc))
    except RecursionError:
        return _failed(Issue(code="yaml_syntax", message="YAML is nested too deeply"))
    if not isinstance(data, dict):
        return _failed(
            Issue(code="not_mapping", message="a playbook must be a YAML mapping at the top level")
        )
    try:
        playbook = Playbook.model_validate(data)
    except ValidationError as exc:
        return _failed(*_schema_issues(exc))
    issues = [
        _validator_issue(i.path, i.code, i.message, i.severity)
        for i in validate(playbook, agent_caps=agent_caps)
    ]
    return ValidationResult(
        ok=not any(i.severity == "error" for i in issues), issues=issues, playbook=playbook
    )


# ---- listing and reading ----------------------------------------------------------------------------------


class PlaybookSummary(BaseModel):
    id: str = Field(description="Playbook id (the file stem when the file does not parse)")
    source: Literal["builtin", "repo"] = Field(description="Where the playbook lives")
    path: str = Field(description="Path of the file: package-relative for built-ins, repo path otherwise")
    sha: str | None = Field(description="Git blob sha for repo playbooks, null for built-ins")
    valid: bool = Field(description="True when the file parses and the validator reports no issues")


class PlaybookDetail(BaseModel):
    id: str = Field(description="Playbook id")
    source: Literal["builtin", "repo"] = Field(description="Where the playbook lives")
    path: str = Field(description="Path of the file")
    sha: str | None = Field(description="Git blob sha for repo playbooks, null for built-ins")
    yaml: str = Field(description="The raw YAML text")
    parsed: dict[str, Any] | None = Field(description="The parsed playbook, or null if it does not parse")


def _id_of(result: ValidationResult, path: str) -> str:
    return result.playbook.id if result.playbook is not None else Path(path).stem


def _builtin_files(builtin_dir: Path) -> list[tuple[str, str]]:
    """(file name, text) of every YAML file in the built-in directory, sorted by name."""
    if not builtin_dir.is_dir():
        return []
    files = []
    for entry in sorted(builtin_dir.iterdir()):
        if entry.is_file() and entry.suffix in _YAML_SUFFIXES:
            files.append((entry.name, entry.read_text(encoding="utf-8")))
    return files


def _list_builtin_sync(builtin_dir: Path) -> list[PlaybookSummary]:
    summaries = []
    for name, text in _builtin_files(builtin_dir):
        result = validate_yaml(text, None)
        summaries.append(
            PlaybookSummary(
                id=_id_of(result, name), source="builtin", path=f"playbooks/{name}", sha=None, valid=result.ok
            )
        )
    return sorted(summaries, key=lambda s: s.id)


async def list_builtin(builtin_dir: Path) -> list[PlaybookSummary]:
    # Parsing is CPU-bound; keep it off the event loop.
    return await asyncio.to_thread(_list_builtin_sync, builtin_dir)


async def _repo_entries(gh: GitHub, repo: str) -> list[dict[str, Any]]:
    """The YAML file entries of the repo's ``.harness/playbooks`` directory; a missing directory is empty."""
    try:
        body = await gh.get(f"repos/{repo}/contents/{REPO_DIR}")
    except GitHubError as exc:
        if exc.status == 404:
            return []
        raise
    if not isinstance(body, list):
        return []
    return [
        e
        for e in body
        if isinstance(e, dict) and e.get("type") == "file" and str(e.get("path", "")).endswith(_YAML_SUFFIXES)
    ]


async def list_repo(gh: GitHub, repo: str) -> list[PlaybookSummary]:
    summaries = []
    for entry in await _repo_entries(gh, repo):
        text = await gh.file_text(repo, entry["path"])
        if text is None:
            continue
        result = await asyncio.to_thread(validate_yaml, text, None)
        summaries.append(
            PlaybookSummary(
                id=_id_of(result, entry["path"]),
                source="repo",
                path=entry["path"],
                sha=entry.get("sha"),
                valid=result.ok,
            )
        )
    return sorted(summaries, key=lambda s: s.id)


async def _detail(
    playbook_id: str, source: Literal["builtin", "repo"], path: str, sha: str | None, text: str
) -> PlaybookDetail:
    result = await asyncio.to_thread(validate_yaml, text, None)
    parsed = result.playbook.model_dump(mode="json") if result.playbook is not None else None
    return PlaybookDetail(id=playbook_id, source=source, path=path, sha=sha, yaml=text, parsed=parsed)


def _find_builtin(builtin_dir: Path, playbook_id: str) -> tuple[str, str] | None:
    for name, text in _builtin_files(builtin_dir):
        if _id_of(validate_yaml(text, None), name) == playbook_id:
            return name, text
    return None


async def get_playbook(
    playbook_id: str, *, builtin_dir: Path, gh: GitHub | None = None, repo: str | None = None
) -> PlaybookDetail | None:
    """A repo playbook (``.yaml`` before ``.yml``) if ``repo`` is given and has it, else the built-in."""
    if repo is not None:
        assert gh is not None
        entries = {Path(e["path"]).name: e for e in await _repo_entries(gh, repo)}
        for name in (f"{playbook_id}.yaml", f"{playbook_id}.yml"):
            entry = entries.get(name)
            if entry is None:
                continue
            text = await gh.file_text(repo, entry["path"])
            if text is not None:
                return await _detail(playbook_id, "repo", entry["path"], entry.get("sha"), text)
    found = await asyncio.to_thread(_find_builtin, builtin_dir, playbook_id)
    if found is not None:
        name, text = found
        return await _detail(playbook_id, "builtin", f"playbooks/{name}", None, text)
    return None


# ---- save as a PR -----------------------------------------------------------------------------------------


class SavedPr(BaseModel):
    number: int = Field(description="Pull request number")
    url: str = Field(description="Pull request web URL")


class SaveResult(BaseModel):
    pr: SavedPr = Field(description="The pull request carrying the playbook")
    branch: str = Field(description="Head branch of that pull request")
    existing: bool = Field(description="True when an identical open PR was found and reused")


class SaveConflict(Exception):
    """The timestamped branch name is already taken: another save of the same playbook is in flight."""


def github_detail(exc: GitHubError, *, with_message: bool, secrets: list[str] | None = None) -> str:
    detail = f"github: {exc.status}"
    if with_message:
        # `redact` knows the env tokens; a token that came from `gh auth token` has to be passed in.
        detail = f"{detail} {redact(exc.message, secrets)}"
    return detail


def _pr_body(warnings: list[Issue]) -> str:
    lines = ["Saved from the harness playbook editor.", "", "## Validation", ""]
    if warnings:
        lines.extend(f"- {w.code}: {w.message}" for w in warnings)
    else:
        lines.append("no issues")
    return "\n".join(lines)


async def _existing_pr(
    gh: GitHub, repo: str, playbook_id: str, text: str, base: str
) -> dict[str, Any] | None:
    """An open PR from a ``harness/playbook-<id>-<timestamp>`` branch that already carries exactly ``text``."""
    prefix = f"harness/playbook-{playbook_id}-"
    path = f"{REPO_DIR}/{playbook_id}.yaml"
    pulls = await gh.get(f"repos/{repo}/pulls", state="open", per_page=100)
    for pr in pulls if isinstance(pulls, list) else []:
        head = (pr.get("head") or {}).get("ref") or ""
        if not head.startswith(prefix) or not _BRANCH_SUFFIX_RE.fullmatch(head[len(prefix) :]):
            continue
        pr_base = (pr.get("base") or {}).get("ref")
        if pr_base is not None and pr_base != base:
            continue
        if await gh.file_text(repo, path, ref=head) == text:
            return pr
    return None


async def save_playbook(
    gh: GitHub,
    repo: str,
    playbook_id: str,
    text: str,
    message: str | None,
    base: str | None,
    now: Callable[[], datetime],
    warnings: list[Issue] | None = None,
) -> SaveResult:
    """Open a PR adding ``.harness/playbooks/<id>.yaml``; never writes to the base branch itself."""
    if base is None:
        base = (await gh.repo(repo))["default_branch"]
    existing = await _existing_pr(gh, repo, playbook_id, text, base)
    if existing is not None:
        return SaveResult(
            pr=SavedPr(number=existing["number"], url=existing["html_url"]),
            branch=existing["head"]["ref"],
            existing=True,
        )
    sha = (await gh.get(f"repos/{repo}/git/ref/heads/{quote(base, safe='/')}"))["object"]["sha"]
    branch = f"harness/playbook-{playbook_id}-{now():%Y%m%d%H%M%S}"
    assert branch != base
    try:
        await gh.post(f"repos/{repo}/git/refs", ref=f"refs/heads/{branch}", sha=sha)
    except GitHubError as exc:
        if exc.status == 422 and "already exists" in exc.message.lower():
            raise SaveConflict(branch) from exc
        raise
    path = f"{REPO_DIR}/{playbook_id}.yaml"
    payload: dict[str, Any] = {}
    try:
        current = await gh.get(f"repos/{repo}/contents/{path}", ref=base)
        if isinstance(current, dict) and current.get("sha"):
            payload["sha"] = current["sha"]
    except GitHubError as exc:
        if exc.status != 404:
            raise
    await gh.put(
        f"repos/{repo}/contents/{path}",
        message=message or f"Playbook: {playbook_id}",
        content=base64.b64encode(text.encode("utf-8")).decode("ascii"),
        branch=branch,
        **payload,
    )
    pull = await gh.create_pull(
        repo, title=f"Playbook: {playbook_id}", head=branch, base=base, body=_pr_body(warnings or [])
    )
    return SaveResult(pr=SavedPr(number=pull["number"], url=pull["html_url"]), branch=branch, existing=False)
