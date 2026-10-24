"""The Copilot adapter: GitHub's coding agent (``copilot-swe-agent[bot]``) behind :class:`AgentAdapter`.

GitHub is the only state store (there is no local table). Everything the adapter needs after a restart is
read back from GitHub:

* the start marker comment ``<!-- harness:copilot key=<idempotency_key> -->`` that :meth:`CopilotAdapter.start`
  posts on the issue (it also carries the transport's session token), which :meth:`CopilotAdapter.find` reads;
* the assignee on the issue, and the pull request whose head branch starts with ``copilot/`` and whose body
  references ``#<issue>``.

``SessionRef.id`` is the issue number; the repo is kept in ``ref.data["repo"]`` (and in ``ref.url``, so a ref
with empty ``data`` still resolves), the transport's session token in ``ref.data["session"]``.

Copilot has no plan step (``caps.plan_approval`` is false, :meth:`approve_plan` raises
:class:`UnsupportedOperation`) and needs a USER token: an installation token (``ghs_``) is refused at
construction and ``GET /user`` must answer with a ``User`` account before the first start.

Two transports start the agent, chosen by ``agents.copilot.transport``:

* ``assignee`` (default): ``POST /repos/{o}/{r}/issues/{n}/assignees`` with ``agent_assignment``.
* ``tasks``: ``POST /agents/repos/{o}/{r}/tasks``. PREVIEW API: its request and response shape is NOT verified
  against the real service; it is tested against synthetic fixtures only, with a tolerant parser.
"""

import hashlib
import logging
import re
from typing import Any, Literal, Protocol

from harness.adapters.base import (
    AdapterConfigError,
    AgentSnapshot,
    AgentState,
    Capabilities,
    PrRef,
    SessionRef,
    StartRequest,
    UnsupportedOperation,
)
from harness.config import AgentConfig
from harness.github.client import GitHub, GitHubError
from harness.secrets import redact

log = logging.getLogger(__name__)

COPILOT_ASSIGNEE = "copilot-swe-agent[bot]"
# The login the API reports for the agent on an issue (it differs from the assignable name).
COPILOT_LOGINS = frozenset({COPILOT_ASSIGNEE, "copilot"})
COPILOT_BRANCH_PREFIX = "copilot/"
# A comment from Copilot that contains this text means the agent session failed.
FAILURE_MARKER = "Copilot has encountered an error"
USER_TOKEN_ERROR = "copilot requires a user token (gh auth token)"
POLL_AFTER = 60.0

_WIP_TITLE = re.compile(r"^\s*(\[wip\]|wip:)", re.IGNORECASE)
_KEY_PARTS = re.compile(r"^(?P<repo>[^#\s]+/[^#\s]+)#(?P<issue>\d+)@")
_ISSUE_URL = re.compile(r"github\.com/(?P<repo>[^/]+/[^/]+)/issues/\d+")
_SESSION_MARKER = re.compile(r"<!-- harness:copilot-session id=(\S+) -->")

AgentSessionState = Literal["queued", "working", "done", "failed"]


class CopilotAgentConfig(AgentConfig):
    transport: Literal["assignee", "tasks"] = "assignee"


class CopilotTransport(Protocol):
    """How the agent is started and how its session is read. Pull requests are found by the adapter."""

    async def existing(self, *, repo: str, issue: int) -> str | None:
        """The session token of an agent assignment that already exists for the issue, else None."""
        ...

    async def start(self, *, repo: str, issue: int, base: str, instructions: str) -> str:
        """Start the agent and return its session token (the issue number or a task id)."""
        ...

    async def agent_state(self, *, repo: str, issue: int, session: str) -> AgentSessionState: ...

    async def stop(self, *, repo: str, issue: int, session: str) -> None:
        """Stop the agent; idempotent."""
        ...


def _has_copilot(issue: dict[str, Any]) -> bool:
    assignees = issue.get("assignees") or []
    return any(str(a.get("login", "")).lower() in COPILOT_LOGINS for a in assignees)


class AssigneeTransport:
    """Assign ``copilot-swe-agent[bot]`` to the issue with ``agent_assignment`` (the default transport)."""

    def __init__(self, gh: GitHub) -> None:
        self._gh = gh

    async def existing(self, *, repo: str, issue: int) -> str | None:
        found = await self._gh.issue(repo, issue)
        return str(issue) if _has_copilot(found) else None

    async def start(self, *, repo: str, issue: int, base: str, instructions: str) -> str:
        await self._gh.post(
            f"repos/{repo}/issues/{issue}/assignees",
            assignees=[COPILOT_ASSIGNEE],
            agent_assignment={
                "target_repo": repo,
                "base_branch": base,
                "custom_instructions": instructions,
            },
        )
        return str(issue)

    async def agent_state(self, *, repo: str, issue: int, session: str) -> AgentSessionState:
        # The assignee route reports nothing finer: assigned means working, unassigned means finished.
        return "working" if await self.existing(repo=repo, issue=issue) else "done"

    async def stop(self, *, repo: str, issue: int, session: str) -> None:
        if await self.existing(repo=repo, issue=issue) is None:
            return
        await self._gh.request(
            "DELETE", f"repos/{repo}/issues/{issue}/assignees", json={"assignees": [COPILOT_ASSIGNEE]}
        )


_TASK_STATES: dict[str, AgentSessionState] = {
    "queued": "queued",
    "pending": "queued",
    "in_progress": "working",
    "running": "working",
    "working": "working",
    "completed": "done",
    "complete": "done",
    "done": "done",
    "succeeded": "done",
    "failed": "failed",
    "error": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}


class TasksTransport:
    """PREVIEW: ``POST /agents/repos/{o}/{r}/tasks``. The real request/response shape is unverified.

    The request body and the response parsing are best guesses and are covered by synthetic fixtures only. The
    parser accepts the task id as ``id``, ``task_id`` or ``task.id`` and the state as ``state`` or ``status``;
    an unknown state counts as working. The preview API has no verified stop endpoint: :meth:`stop` removes
    the Copilot assignee, which is a no-op when there is none.
    """

    def __init__(self, gh: GitHub) -> None:
        self._gh = gh
        self._assignee = AssigneeTransport(gh)

    async def existing(self, *, repo: str, issue: int) -> str | None:
        return None  # a task id cannot be recovered from the issue: the start marker comment carries it

    async def start(self, *, repo: str, issue: int, base: str, instructions: str) -> str:
        body = await self._gh.post(
            f"agents/repos/{repo}/tasks",
            problem_statement=instructions,
            base_ref=base,
            issue_number=issue,
        )
        task_id = self._task_id(body)
        if task_id is None:
            raise GitHubError(200, "POST", f"agents/repos/{repo}/tasks", "response has no task id")
        return task_id

    @staticmethod
    def _task_id(body: Any) -> str | None:
        if not isinstance(body, dict):
            return None
        task = body.get("task")
        for candidate in (
            body.get("id"),
            body.get("task_id"),
            task.get("id") if isinstance(task, dict) else None,
        ):
            if candidate not in (None, ""):
                return str(candidate)
        return None

    async def agent_state(self, *, repo: str, issue: int, session: str) -> AgentSessionState:
        body = await self._gh.get(f"agents/repos/{repo}/tasks/{session}")
        raw = body.get("state") or body.get("status") if isinstance(body, dict) else None
        return _TASK_STATES.get(str(raw or "").lower(), "working")

    async def stop(self, *, repo: str, issue: int, session: str) -> None:
        await self._assignee.stop(repo=repo, issue=issue, session=session)


def make_transport(config: CopilotAgentConfig, gh: GitHub) -> CopilotTransport:
    return TasksTransport(gh) if config.transport == "tasks" else AssigneeTransport(gh)


def _wip(pull: dict[str, Any]) -> bool:
    return bool(pull.get("draft")) or bool(_WIP_TITLE.match(str(pull.get("title") or "")))


def _references(body: str | None, issue: int) -> bool:
    text = body or ""
    return bool(re.search(rf"(?<![\w/])#{issue}\b", text) or re.search(rf"/issues/{issue}\b", text))


class CopilotAdapter:
    name = "copilot"

    def __init__(
        self,
        *,
        gh: GitHub,
        config: CopilotAgentConfig,
        transport: CopilotTransport | None = None,
        token: str | None = None,
    ) -> None:
        # Cheap, offline check: ``ghs_`` is an installation (app) token, never a user token.
        if token is not None and token.startswith("ghs_"):
            raise AdapterConfigError(USER_TOKEN_ERROR)
        self._gh = gh
        self._config = config
        self._token = token
        self._transport = transport if transport is not None else make_transport(config, gh)
        self._verified = False
        self.caps = Capabilities(
            plan_approval=False,
            messaging=True,
            opens_pr=True,
            local_workspace=False,
            max_concurrency=config.max_concurrency,
        )

    # -- helpers ---------------------------------------------------------------------------------------

    def _safe(self, text: str) -> str:
        return redact(text, [self._token] if self._token else None)

    async def health(self) -> None:
        """Raise :class:`AdapterConfigError` unless the token belongs to a user account (``GET /user``)."""
        if self._verified:
            return
        try:
            user = await self._gh.get("user")
        except GitHubError as e:
            log.warning("copilot token check failed: %s", self._safe(str(e)))
            raise AdapterConfigError(USER_TOKEN_ERROR) from None
        if not isinstance(user, dict) or not user.get("login") or user.get("type") != "User":
            raise AdapterConfigError(USER_TOKEN_ERROR)
        self._verified = True

    def _make_ref(self, repo: str, issue: int, session: str) -> SessionRef:
        return SessionRef(
            agent=self.name,
            id=str(issue),
            url=f"https://github.com/{repo}/issues/{issue}",
            data={"repo": repo, "session": session},
        )

    @staticmethod
    def _where(ref: SessionRef) -> tuple[str, int]:
        repo = ref.data.get("repo")
        if not repo and ref.url:
            match = _ISSUE_URL.search(ref.url)
            repo = match.group("repo") if match else None
        if not repo:
            raise ValueError(f"copilot session {ref.id!r} has no repo in its data or url")
        return str(repo), int(ref.id)

    @staticmethod
    def _marker(key: str) -> str:
        return f"<!-- harness:copilot key={key} -->"

    async def _marker_comment(self, repo: str, issue: int, key: str) -> dict[str, Any] | None:
        marker = self._marker(key)
        for comment in await self._gh.comments(repo, issue):
            if marker in str(comment.get("body") or ""):
                return comment
        return None

    async def _find_pull(self, repo: str, issue: int, ref: SessionRef | None = None) -> dict[str, Any] | None:
        """The Copilot pull request for the issue: the cached number, else a newest-first search."""
        cached = ref.data.get("pr") if ref is not None else None
        if isinstance(cached, int):
            return await self._gh.pull(repo, cached)
        async for pull in self._gh.paginate(
            f"repos/{repo}/pulls", state="all", sort="created", direction="desc"
        ):
            head = str((pull.get("head") or {}).get("ref") or "")
            if head.startswith(COPILOT_BRANCH_PREFIX) and _references(pull.get("body"), issue):
                if ref is not None:
                    ref.data["pr"] = pull["number"]
                return pull
        return None

    # -- AgentAdapter ----------------------------------------------------------------------------------

    async def find(self, idempotency_key: str) -> SessionRef | None:
        parts = _KEY_PARTS.match(idempotency_key)
        if parts is None:
            return None
        repo, issue = parts.group("repo"), int(parts.group("issue"))
        comment = await self._marker_comment(repo, issue, idempotency_key)
        if comment is None:
            return None
        session = _SESSION_MARKER.search(str(comment.get("body") or ""))
        return self._make_ref(repo, issue, session.group(1) if session else str(issue))

    async def start(self, req: StartRequest) -> SessionRef:
        existing = await self.find(req.idempotency_key)
        if existing is not None:
            return existing
        await self.health()
        session = await self._transport.existing(repo=req.repo, issue=req.issue)
        if session is None and await self._find_pull(req.repo, req.issue) is not None:
            session = str(req.issue)
        if session is None:
            log.info(
                "copilot start repo=%s issue=%s transport=%s", req.repo, req.issue, self._config.transport
            )
            session = await self._transport.start(
                repo=req.repo, issue=req.issue, base=req.base_branch, instructions=req.brief
            )
        body = f"{self._marker(req.idempotency_key)}\n<!-- harness:copilot-session id={session} -->"
        await self._gh.add_comment(req.repo, req.issue, body)
        return self._make_ref(req.repo, req.issue, session)

    async def poll(self, ref: SessionRef) -> AgentSnapshot:
        repo, issue = self._where(ref)
        session = str(ref.data.get("session") or issue)
        pull = await self._find_pull(repo, issue, ref)
        if pull is not None and pull.get("state") == "closed" and not pull.get("merged"):
            return AgentSnapshot(
                state=AgentState.FAILED, error="pull request was closed", poll_after=POLL_AFTER
            )
        if pull is not None and not _wip(pull):
            return self._ready(pull)
        state = await self._transport.agent_state(repo=repo, issue=issue, session=session)
        if state == "failed":
            return AgentSnapshot(
                state=AgentState.FAILED, error="copilot session failed", poll_after=POLL_AFTER
            )
        target = int(pull["number"]) if pull is not None else issue
        for comment in await self._gh.comments(repo, target):
            author = str((comment.get("user") or {}).get("login", "")).lower()
            if author in COPILOT_LOGINS and FAILURE_MARKER in str(comment.get("body") or ""):
                return AgentSnapshot(
                    state=AgentState.FAILED,
                    error=f"copilot reported an error: {FAILURE_MARKER}",
                    poll_after=POLL_AFTER,
                )
        if pull is not None and state == "done":
            return self._ready(pull)  # the session finished and left a draft: nothing more is coming
        return AgentSnapshot(state=AgentState.WORKING, poll_after=POLL_AFTER)

    @staticmethod
    def _ready(pull: dict[str, Any]) -> AgentSnapshot:
        pr = PrRef(number=pull["number"], url=str(pull["html_url"]), head_sha=str(pull["head"]["sha"]))
        return AgentSnapshot(state=AgentState.PR_READY, pr=pr, poll_after=POLL_AFTER)

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        raise UnsupportedOperation("copilot has no plan step")

    async def send(self, ref: SessionRef, text: str) -> None:
        repo, issue = self._where(ref)
        digest = hashlib.sha256(f"{ref.id}\n{text}".encode()).hexdigest()[:12]
        counter = int(ref.data.get("send_n", 0))
        marker = f"<!-- harness:send ref={ref.id} h={digest} n={counter} -->"
        pull = await self._find_pull(repo, issue, ref)
        targets = [issue] if pull is None else [int(pull["number"]), issue]
        for number in targets:
            if any(marker in str(c.get("body") or "") for c in await self._gh.comments(repo, number)):
                return
        await self._gh.add_comment(repo, targets[0], f"@copilot {text}\n\n{marker}")

    async def cancel(self, ref: SessionRef) -> None:
        repo, issue = self._where(ref)
        session = str(ref.data.get("session") or issue)
        await self._transport.stop(repo=repo, issue=issue, session=session)
        pull = await self._find_pull(repo, issue, ref)
        if pull is None or pull.get("state") != "open":
            return
        number = int(pull["number"])
        marker = f"<!-- harness:copilot-cancel ref={ref.id} -->"
        if not any(marker in str(c.get("body") or "") for c in await self._gh.comments(repo, number)):
            await self._gh.add_comment(repo, number, f"Cancelled by the harness.\n\n{marker}")
        await self._gh.patch(f"repos/{repo}/pulls/{number}", state="closed")
