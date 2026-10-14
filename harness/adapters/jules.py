"""The Jules adapter: an async httpx port of the working reference client in ``.orchestrator/jules.py``.

Jules (https://jules.googleapis.com/v1alpha) is the first real :class:`~harness.adapters.base.AgentAdapter`.
Endpoints used: ``POST sessions``, ``GET sessions``, ``GET sessions/{id}``, ``GET sessions/{id}/activities``,
``POST sessions/{id}:approvePlan``, ``:sendMessage`` and ``:archive``.

Three Jules quirks (``docs/LESSONS.md`` 6, 10 and 11) are handled inside :meth:`JulesAdapter.poll`. All of
the state they need lives in ``ref.data`` (which the engine persists), so they survive a restart:

* ``approved_at``: the ``clock()`` reading when :meth:`approve_plan` ran.
* ``auto_replied``: whether the "does this plan look good" re-ask has already been answered.
* ``fix_base_head``: the PR head sha when a follow-up message was sent to a session that already has a PR.
* ``last_activity_id``: the newest activity already returned to the engine.
"""

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from harness.adapters.base import (
    ActivityItem,
    AgentSnapshot,
    AgentState,
    Capabilities,
    PrRef,
    SessionRef,
    StartRequest,
)
from harness.secrets import redact

DEFAULT_BASE_URL = "https://jules.googleapis.com/v1alpha"

# Seconds after approve_plan during which a still-reported AWAITING_PLAN_APPROVAL is stale (lesson 11).
STALE_APPROVAL_SECONDS = 120.0
# Seconds after approve_plan during which a "does this plan look good" question is a re-ask (lesson 10).
REASK_WINDOW_SECONDS = 600.0
REASK_PATTERN = re.compile(r"(?i)(look good|approve|proceed\?|shall i proceed)")
APPROVED_REPLY = "Approved, proceed."

# Jules states that mean the agent is still busy on its own (the reference client's ``busy`` set).
_BUSY = frozenset({"QUEUED", "PLANNING", "IN_PROGRESS"})
_ATTENTION = frozenset(
    {AgentState.PLAN_READY, AgentState.NEEDS_INPUT, AgentState.PR_READY, AgentState.FAILED}
)
_PR_NUMBER = re.compile(r"/pull/(\d+)")
_SOURCE_PREFIX = "sources/github/"


class JulesError(RuntimeError):
    """A failed Jules API call. The message is redacted: it never contains the API key."""

    def __init__(self, status: int, method: str, path: str, message: str) -> None:
        super().__init__(f"[{status}] {method} {path}: {message}")
        self.status = status
        self.method = method
        self.path = path
        self.message = message


def _error_message(resp: httpx.Response) -> str:
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        message = str(error.get("message") or "") if isinstance(error, dict) else str(error or "")
    if not message:
        message = resp.text.strip() or resp.reason_phrase or f"HTTP {resp.status_code}"
    return message


def _session_path(ref: SessionRef) -> str:
    return f"sessions/{ref.id}"


def _repo_of(session: dict[str, Any]) -> str | None:
    source = (session.get("sourceContext") or {}).get("source")
    if isinstance(source, str) and source.startswith(_SOURCE_PREFIX):
        return source.removeprefix(_SOURCE_PREFIX)
    return None


def _pull_request(session: dict[str, Any]) -> dict[str, Any] | None:
    for output in session.get("outputs") or []:
        pr = output.get("pullRequest")
        if pr:
            return pr
    return None


def _plan_text(activity: dict[str, Any]) -> str:
    steps = (activity.get("planGenerated") or {}).get("plan", {}).get("steps") or []
    lines = []
    for step in steps:
        title = step.get("title") or ""
        description = step.get("description") or ""
        lines.append(f"- {title}: {description}" if description else f"- {title}")
    return "\n".join(lines)


def _activity_item(activity: dict[str, Any]) -> ActivityItem | None:
    ts = str(activity.get("createTime") or "")
    if "progressUpdated" in activity:
        update = activity["progressUpdated"] or {}
        text = ": ".join(part for part in (update.get("title"), update.get("description")) if part)
        return ActivityItem(ts=ts, kind="progress", text=text)
    if "agentMessaged" in activity:
        return ActivityItem(
            ts=ts, kind="message", text=str(activity["agentMessaged"].get("agentMessage") or "")
        )
    if "planGenerated" in activity:
        return ActivityItem(ts=ts, kind="plan", text=_plan_text(activity))
    if "sessionCompleted" in activity:
        return ActivityItem(ts=ts, kind="end", text="session completed")
    if "sessionFailed" in activity:
        reason = (activity["sessionFailed"] or {}).get("reason")
        return ActivityItem(
            ts=ts, kind="end", text=f"session failed: {reason}" if reason else "session failed"
        )
    return None


def _latest(activities: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for activity in reversed(activities):
        if key in activity:
            return activity
    return None


class JulesAdapter:
    """Drives Jules sessions through the Jules REST API.

    ``clock`` is injectable so the time-based quirks can be tested without sleeping. ``pr_head(repo, n)``
    returns the current head sha of PR ``n`` (normally backed by the GitHub client); without it, PR head
    shas are reported as ``""`` and the PR-fix quirk is disabled.
    """

    name = "jules"

    def __init__(
        self,
        api_key: str,
        *,
        max_concurrency: int = 1,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        pr_head: Callable[[str, int], Awaitable[str]] | None = None,
    ) -> None:
        self.caps = Capabilities(
            plan_approval=True,
            messaging=True,
            opens_pr=True,
            local_workspace=False,
            max_concurrency=max_concurrency,
        )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._clock = clock
        self._pr_head = pr_head
        # The key lives only in this header. Full URLs are built per request, so base_url never merges.
        self._client = httpx.AsyncClient(
            headers={"X-Goog-Api-Key": api_key, "Content-Type": "application/json"},
            transport=transport,
            timeout=60.0,
        )

    def __repr__(self) -> str:
        return f"JulesAdapter(base_url={self._base_url!r}, max_concurrency={self.caps.max_concurrency})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "JulesAdapter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # HTTP

    def _redact(self, text: str) -> str:
        return redact(text, [self._api_key])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._client.request(method, f"{self._base_url}/{path}", params=params, json=body)
        except httpx.HTTPError as exc:
            raise JulesError(0, method, path, self._redact(f"{type(exc).__name__}: {exc}")) from None
        if resp.status_code >= 400:
            raise JulesError(resp.status_code, method, path, self._redact(_error_message(resp)))
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            raise JulesError(resp.status_code, method, path, "response is not valid JSON") from None
        return data if isinstance(data, dict) else {}

    async def _all_pages(self, path: str, items_key: str) -> list[dict[str, Any]]:
        """Fetch every page of a list endpoint, following ``nextPageToken``."""
        items: list[dict[str, Any]] = []
        token = ""
        while True:
            params: dict[str, Any] = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            page = await self._request("GET", path, params=params)
            items.extend(page.get(items_key) or [])
            token = page.get("nextPageToken") or ""
            if not token:
                return items

    # AgentAdapter

    async def start(self, req: StartRequest) -> SessionRef:
        session = await self._request(
            "POST",
            "sessions",
            body={
                "title": f"#{req.issue}: {req.title} [h:{req.idempotency_key}]",
                "prompt": req.brief,
                "sourceContext": {
                    "source": f"{_SOURCE_PREFIX}{req.repo}",
                    "githubRepoContext": {"startingBranch": req.base_branch},
                },
                "automationMode": "AUTO_CREATE_PR",
                "requirePlanApproval": req.mode == "plan_first",
            },
        )
        return self._ref(session, req.repo)

    async def find(self, idempotency_key: str) -> SessionRef | None:
        tag = f"[h:{idempotency_key}]"
        for session in await self._all_pages("sessions", "sessions"):
            if str(session.get("title") or "").endswith(tag):
                return self._ref(session, _repo_of(session))
        return None

    @staticmethod
    def _ref(session: dict[str, Any], repo: str | None) -> SessionRef:
        session_id = session.get("id") or str(session.get("name", "")).split("/")[-1]
        return SessionRef(
            agent="jules",
            id=str(session_id),
            url=session.get("url"),
            data={"repo": repo, "last_activity_id": None},
        )

    async def poll(self, ref: SessionRef) -> AgentSnapshot:
        data = ref.data
        session = await self._request("GET", _session_path(ref))
        activities = await self._all_pages(f"{_session_path(ref)}/activities", "activities")
        if not data.get("repo"):
            data["repo"] = _repo_of(session)

        snapshot = await self._snapshot(ref, session, activities)
        snapshot.activity = self._new_activity(data, activities)
        snapshot.poll_after = 0.0 if snapshot.state in _ATTENTION else 60.0
        return snapshot

    async def _snapshot(
        self, ref: SessionRef, session: dict[str, Any], activities: list[dict[str, Any]]
    ) -> AgentSnapshot:
        data = ref.data
        state = str(session.get("state") or "")
        since_approval = self._since_approval(data)
        message = _latest(activities, "agentMessaged")
        question = str(message["agentMessaged"].get("agentMessage") or "") if message else None

        match state:
            case "QUEUED":
                result = AgentSnapshot(state=AgentState.STARTING)
            case "PLANNING":
                result = AgentSnapshot(state=AgentState.PLANNING)
            case "AWAITING_PLAN_APPROVAL":
                if since_approval is not None and since_approval < STALE_APPROVAL_SECONDS:
                    # Lesson 11: right after approval the API still reports the old state.
                    result = AgentSnapshot(state=AgentState.WORKING)
                else:
                    plan = _latest(activities, "planGenerated")
                    result = AgentSnapshot(
                        state=AgentState.PLAN_READY, plan=_plan_text(plan) if plan else None
                    )
            case "AWAITING_USER_FEEDBACK":
                if (
                    since_approval is not None
                    and since_approval <= REASK_WINDOW_SECONDS
                    and not data.get("auto_replied")
                    and question is not None
                    and REASK_PATTERN.search(question)
                ):
                    # Lesson 10: Jules asked "does this plan look good?" after an approval. Answer once.
                    await self.send(ref, APPROVED_REPLY)
                    data["auto_replied"] = True
                    result = AgentSnapshot(state=AgentState.WORKING)
                else:
                    result = AgentSnapshot(state=AgentState.NEEDS_INPUT, question=question)
            case "PAUSED":
                result = AgentSnapshot(state=AgentState.NEEDS_INPUT, question=question)
            case "COMPLETED":
                result = await self._completed(ref, session)
            case "FAILED":
                failed = _latest(activities, "sessionFailed")
                reason = (failed["sessionFailed"] or {}).get("reason") if failed else None
                result = AgentSnapshot(state=AgentState.FAILED, error=str(reason or "session failed"))
            case _:
                # IN_PROGRESS, plus any state this adapter does not know about.
                result = AgentSnapshot(state=AgentState.WORKING)

        if (
            data.get("fix_base_head") is not None
            and self._pr_head is not None
            and state in _BUSY | {"COMPLETED"}
        ):
            result = self._apply_pr_fix(ref, state, result)
        return result

    async def _completed(self, ref: SessionRef, session: dict[str, Any]) -> AgentSnapshot:
        pr = _pull_request(session)
        if pr is None:
            return AgentSnapshot(state=AgentState.FAILED, error="completed without PR")
        ref_pr = await self._pr_ref(ref, pr)
        return AgentSnapshot(state=AgentState.PR_READY, pr=ref_pr)

    async def _pr_ref(self, ref: SessionRef, pr: dict[str, Any]) -> PrRef:
        url = str(pr.get("url") or "")
        found = _PR_NUMBER.search(url)
        number = int(found.group(1)) if found else 0
        head = ""
        if self._pr_head is not None and number:
            head = await self._pr_head(str(ref.data.get("repo") or ""), number)
        return PrRef(number=number, url=url, head_sha=head)

    @staticmethod
    def _apply_pr_fix(ref: SessionRef, state: str, result: AgentSnapshot) -> AgentSnapshot:
        """Lesson 6: after a PR-fix message, PR_READY needs a new PR head sha and an idle session."""
        pr = result.pr
        if pr is None:
            # Busy, or completed without a PR: no head to compare, so just report that work continues.
            return AgentSnapshot(state=AgentState.WORKING) if state in _BUSY else result
        if pr.head_sha != ref.data["fix_base_head"] and state not in _BUSY:
            ref.data["fix_base_head"] = None
            return result
        return AgentSnapshot(state=AgentState.WORKING)

    def _since_approval(self, data: dict[str, Any]) -> float | None:
        approved_at = data.get("approved_at")
        if approved_at is None:
            return None
        elapsed = self._clock() - float(approved_at)
        # A negative age means the clock restarted (a monotonic clock resets on reboot): treat as expired.
        return elapsed if elapsed >= 0 else None

    @staticmethod
    def _new_activity(data: dict[str, Any], activities: list[dict[str, Any]]) -> list[ActivityItem]:
        if not activities:
            return []
        ids = [activity.get("id") for activity in activities]
        last = data.get("last_activity_id")
        start = ids.index(last) + 1 if last in ids else 0
        data["last_activity_id"] = ids[-1]
        items = (_activity_item(activity) for activity in activities[start:])
        return [item for item in items if item is not None]

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        await self._request("POST", f"{_session_path(ref)}:approvePlan", body={})
        ref.data["approved_at"] = self._clock()
        # A fresh approval can trigger a fresh "does this plan look good" re-ask.
        ref.data["auto_replied"] = False
        if note:
            await self.send(ref, note)

    async def send(self, ref: SessionRef, text: str) -> None:
        # Lesson 6: if the session already has a PR, remember its head so poll can tell when Jules pushes.
        base_head: str | None = None
        if self._pr_head is not None:
            pr = _pull_request(await self._request("GET", _session_path(ref)))
            if pr is not None:
                base_head = (await self._pr_ref(ref, pr)).head_sha
        await self._request("POST", f"{_session_path(ref)}:sendMessage", body={"prompt": text})
        if base_head is not None:
            ref.data["fix_base_head"] = base_head

    async def cancel(self, ref: SessionRef) -> None:
        """Best effort: archive the session, which also frees its concurrency slot.

        The Jules API has no working cancel endpoint (it returns 404), but ``:archive`` works. A 404 on
        the archive itself (the session is already gone) is treated as success.
        """
        try:
            await self._request("POST", f"{_session_path(ref)}:archive", body={})
        except JulesError as exc:
            if exc.status != 404:
                raise
