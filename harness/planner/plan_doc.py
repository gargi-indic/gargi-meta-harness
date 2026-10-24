"""Load the plan document of a campaign: a file in the repo, or a GitHub issue.

``PlanDoc.sha`` identifies the exact text that a human approves:

* file plan: the git blob sha that the contents API reports for the file at ``ref``;
* issue plan: the hex ``sha256(title + "\\n" + body)`` of the issue as GitHub returns it.

Both change whenever the text changes, so an approval bound to a ``sha`` is void as soon as the plan is edited.
Reads go through the GitHub client, so they use its ETag cache (an unchanged plan answers 304 and costs no rate
limit).
"""

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from harness.github.client import GitHub, GitHubError

MAX_PLAN_BYTES = 500 * 1024

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


class PlanDocError(ValueError):
    """The plan document cannot be used: missing, not a file, too large or not text."""


@dataclass(frozen=True)
class PlanDoc:
    kind: Literal["file", "issue"]
    repo: str
    path: str | None
    issue: int | None
    ref: str | None
    sha: str
    text: str
    title: str
    headings: list[str]

    @property
    def source(self) -> str:
        """A short human description such as ``docs/PLAN.md`` or ``#12``."""
        return self.path if self.kind == "file" and self.path else f"#{self.issue}"

    @property
    def url(self) -> str:
        if self.kind == "issue":
            return f"https://github.com/{self.repo}/issues/{self.issue}"
        return f"https://github.com/{self.repo}/blob/{self.ref or 'HEAD'}/{self.path}"


def parse_headings(text: str) -> list[tuple[int, str]]:
    """ATX headings outside fenced code blocks, as ``(level, text)`` in document order."""
    found: list[tuple[int, str]] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            fence = None if fence == marker.group(1) else (fence or marker.group(1))
            continue
        if fence is not None:
            continue
        heading = _HEADING.match(line)
        if heading:
            found.append((len(heading.group(1)), heading.group(2).strip()))
    return found


def _title_and_headings(text: str, fallback: str) -> tuple[str, list[str]]:
    parsed = parse_headings(text)
    top = next((h for level, h in parsed if level == 1), None)
    title = top or (parsed[0][1] if parsed else fallback)
    return title, [h for _, h in parsed]


def issue_plan_sha(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


async def _load_file(gh: GitHub, repo: str, path: str, ref: str | None) -> PlanDoc:
    params = {"ref": ref} if ref else None
    try:
        resp = await gh.request("GET", f"repos/{repo}/contents/{quote(path, safe='/')}", params=params)
    except GitHubError as exc:
        if exc.status == 404:
            raise PlanDocError(
                f"plan file {path!r} not found in {repo}" + (f" at {ref}" if ref else "")
            ) from exc
        raise
    data: Any = resp.body
    if not isinstance(data, dict) or data.get("type") != "file":
        raise PlanDocError(f"{path!r} in {repo} is not a file")
    size = data.get("size")
    if isinstance(size, int) and size > MAX_PLAN_BYTES:
        raise PlanDocError(f"plan file {path!r} is {size} bytes, the limit is {MAX_PLAN_BYTES}")
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        raise PlanDocError(f"plan file {path!r} came back without base64 content")
    try:
        raw = base64.b64decode(data["content"])
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise PlanDocError(f"plan file {path!r} is not valid UTF-8 text") from exc
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanDocError(f"plan file {path!r} is {len(raw)} bytes, the limit is {MAX_PLAN_BYTES}")
    sha = data.get("sha")
    if not isinstance(sha, str) or not sha:
        raise PlanDocError(f"plan file {path!r} came back without a blob sha")
    title, headings = _title_and_headings(text, path)
    return PlanDoc("file", repo, path, None, ref, sha, text, title, headings)


async def _load_issue(gh: GitHub, repo: str, number: int) -> PlanDoc:
    try:
        data = await gh.issue(repo, number)
    except GitHubError as exc:
        if exc.status == 404:
            raise PlanDocError(f"plan issue #{number} not found in {repo}") from exc
        raise
    if not isinstance(data, dict) or "pull_request" in data:
        raise PlanDocError(f"#{number} in {repo} is not an issue")
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    if len(body.encode("utf-8")) > MAX_PLAN_BYTES:
        raise PlanDocError(f"plan issue #{number} is larger than {MAX_PLAN_BYTES} bytes")
    _, headings = _title_and_headings(body, title)
    return PlanDoc("issue", repo, None, number, None, issue_plan_sha(title, body), body, title, headings)


async def load_plan_doc(
    gh: GitHub,
    repo: str,
    path: str | None = None,
    issue: int | None = None,
    ref: str | None = None,
) -> PlanDoc:
    """Load the plan from ``path`` (a repo file at ``ref``, default branch when None) or from ``issue``."""
    if (path is None) == (issue is None):
        raise PlanDocError("give exactly one of path or issue as the plan source")
    if path is not None:
        return await _load_file(gh, repo, path, ref)
    assert issue is not None
    return await _load_issue(gh, repo, issue)
