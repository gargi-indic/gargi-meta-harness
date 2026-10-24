"""Claude Code adapter: a ``claude-agent-sdk`` session running in a local git worktree.

``claude_agent_sdk`` lives in the optional ``claude`` extra, so it is imported inside :func:`build_sdk_options`
and :func:`default_factory` only. Everything else talks to the thin :class:`ClaudeSession` seam, which tests
replace with a scripted fake. The turn runs as a background task that pumps SDK messages into the session ref's
activity buffer; plan mode captures the ``ExitPlanMode`` input as the plan, and a PreToolUse hook asks
:class:`~harness.adapters.guard.PathGuard` before every tool use.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from harness.adapters.base import (
    ActivityItem,
    AgentSnapshot,
    AgentState,
    Capabilities,
    PrRef,
    SessionRef,
    StartRequest,
)
from harness.adapters.guard import DEFAULT_PROTECTED, PathGuard
from harness.config import AgentConfig
from harness.github.client import GitHub
from harness.publish import ProtectedPathViolation, PublishError, publish_worktree, push_revision
from harness.secrets import redact
from harness.workspaces import Worktree, WorktreeManager

SDKMessage = Any
HookCallback = Callable[[Mapping[str, Any], str | None, Any], Awaitable[dict[str, Any]]]

ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "Edit", "Write", "Bash", "TodoWrite")
DEFAULT_APPROVAL_NOTE = "Plan approved. Implement it."
MAX_CONSECUTIVE_DENIALS = 3
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
_BLANKED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "JULES_API_KEY", "CODEX_API_KEY")
_PLAN_DENIAL = "The plan was recorded for approval; stop now and wait for the operator."


class ClaudeAgentConfig(AgentConfig):
    max_turns: int = 40
    max_budget_usd: float | None = None
    plan_timeout: float = 600.0
    implement_timeout: float = 3600.0
    cancel_grace: float = 5.0


class ClaudeTurnError(Exception):
    """The SDK ended a turn with an error result (budget, max turns, API failure)."""


@dataclass(frozen=True)
class ClaudeOptions:
    """What the adapter asks of a session; :func:`build_sdk_options` turns it into SDK options."""

    cwd: Path
    permission_mode: str
    allowed_tools: tuple[str, ...]
    max_turns: int
    max_budget_usd: float | None = None
    model: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    resume: str | None = None
    session_id: str | None = None
    hooks: Mapping[str, Sequence[HookCallback]] = field(default_factory=dict)


class ClaudeSession(Protocol):
    """Thin seam over ``ClaudeSDKClient`` so tests never import the real SDK client."""

    async def connect(self) -> None: ...
    async def query(self, text: str) -> None: ...
    def receive_messages(self) -> AsyncIterator[SDKMessage]: ...
    async def set_permission_mode(self, mode: str) -> None: ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None: ...


ClaudeSessionFactory = Callable[[ClaudeOptions], ClaudeSession]


def build_sdk_options(options: ClaudeOptions) -> Any:
    import claude_agent_sdk as sdk

    kwargs: dict[str, Any] = {
        "cwd": str(options.cwd),
        "permission_mode": options.permission_mode,
        "allowed_tools": list(options.allowed_tools),
        "max_turns": options.max_turns,
        "max_budget_usd": options.max_budget_usd,
        "model": options.model,
        "env": dict(options.env),
        "hooks": {
            event: [sdk.HookMatcher(matcher=None, hooks=list(callbacks))]
            for event, callbacks in options.hooks.items()
        },
    }
    if options.resume:
        kwargs["resume"] = options.resume
    elif options.session_id:
        kwargs["session_id"] = options.session_id
    return sdk.ClaudeAgentOptions(**kwargs)


class _SdkSession:
    """``ClaudeSDKClient`` with best-effort teardown: a dead CLI process must not break ``cancel``."""

    def __init__(self, client: Any, sdk_error: type[Exception]) -> None:
        self.client = client
        self._sdk_error = sdk_error

    async def connect(self) -> None:
        await self.client.connect()

    async def query(self, text: str) -> None:
        await self.client.query(text)

    def receive_messages(self) -> AsyncIterator[SDKMessage]:
        return self.client.receive_messages()

    async def set_permission_mode(self, mode: str) -> None:
        await self.client.set_permission_mode(mode)

    async def interrupt(self) -> None:
        try:
            await self.client.interrupt()
        except self._sdk_error:
            pass

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except self._sdk_error:
            pass


def default_factory(options: ClaudeOptions) -> ClaudeSession:
    import claude_agent_sdk as sdk

    return _SdkSession(sdk.ClaudeSDKClient(build_sdk_options(options)), sdk.ClaudeSDKError)


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD")) or upper.endswith("_KEY")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _activity(data: dict[str, Any], kind: str, text: str, api_key: str | None) -> str:
    safe = redact(text, extra=[api_key] if api_key else None)
    data.setdefault("activity", []).append({"ts": _now(), "kind": kind, "text": safe})
    return safe


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class ClaudeAdapter:
    name = "claude"

    def __init__(
        self,
        *,
        workspaces: WorktreeManager,
        gh: GitHub,
        config: ClaudeAgentConfig,
        session_factory: ClaudeSessionFactory = default_factory,
        store: Any = None,
        protected_paths: Sequence[str] = DEFAULT_PROTECTED,
    ) -> None:
        self.caps = Capabilities(
            plan_approval=True,
            messaging=True,
            opens_pr=False,
            local_workspace=True,
            max_concurrency=config.max_concurrency,
        )
        self.workspaces = workspaces
        self.gh = gh
        self.config = config
        self.session_factory = session_factory
        self.store = store
        self.protected_paths = protected_paths
        self._sessions: dict[str, ClaudeSession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._by_key: dict[str, SessionRef] = {}
        self._seen: set[str] = set()

    # -- lookup / start ------------------------------------------------------------------------------
    async def find(self, idempotency_key: str) -> SessionRef | None:
        return self._by_key.get(idempotency_key)

    def _env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
        # The SDK merges options.env over os.environ, so other secrets are blanked rather than omitted.
        for name in (*_BLANKED_ENV, *os.environ):
            if (
                name not in env
                and name != "ANTHROPIC_API_KEY"
                and (name in _BLANKED_ENV or _looks_secret(name))
            ):
                env[name] = ""
        key = _api_key()
        if key:
            env["ANTHROPIC_API_KEY"] = key
        return env

    async def start(self, req: StartRequest) -> SessionRef:
        if req.workdir is None:
            raise ValueError("Claude requires a local workdir")
        existing = await self.find(req.idempotency_key)
        if existing:
            return existing

        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude:{req.idempotency_key}"))
        ref = SessionRef(
            agent=self.name,
            id=session_id,
            url=None,
            data={
                "idempotency_key": req.idempotency_key,
                "mode": req.mode,
                "phase": "plan" if req.mode == "plan_first" else "implement",
                "state": AgentState.STARTING,
                "repo": req.repo,
                "issue": req.issue,
                "title": req.title,
                "brief": req.brief,
                "branch": req.branch,
                "base_branch": req.base_branch,
                "workdir": str(req.workdir),
                "activity": [],
                "claude_session_id": session_id,
                "turns": 0,
                "plan": None,
                "error": None,
                "pr": None,
                "question": None,
                "denials": 0,
            },
        )
        self._by_key[req.idempotency_key] = ref
        self._begin_turn(ref, req.brief)
        return ref

    # -- turns ---------------------------------------------------------------------------------------
    def _begin_turn(self, ref: SessionRef, text: str) -> None:
        data = ref.data
        data["state"] = AgentState.PLANNING if data["phase"] == "plan" else AgentState.WORKING
        data["error"] = None
        data["question"] = None
        data["denials"] = 0
        self._seen.add(ref.id)
        self._tasks[ref.id] = asyncio.create_task(self._worker(ref, text))

    def _options(self, ref: SessionRef) -> ClaudeOptions:
        data = ref.data
        resume = data["claude_session_id"] if data.get("turns", 0) > 0 else None
        return ClaudeOptions(
            cwd=Path(data["workdir"]),
            permission_mode="plan" if data["phase"] == "plan" else "acceptEdits",
            allowed_tools=ALLOWED_TOOLS,
            max_turns=self.config.max_turns,
            max_budget_usd=self.config.max_budget_usd,
            model=self.config.model,
            env=self._env(),
            resume=resume,
            session_id=None if resume else data["claude_session_id"],
            hooks={"PreToolUse": [self._make_hook(ref)]},
        )

    async def _session(self, ref: SessionRef) -> ClaudeSession:
        session = self._sessions.get(ref.id)
        if session is None:
            session = self.session_factory(self._options(ref))
            await session.connect()
            self._sessions[ref.id] = session
        return session

    def _make_hook(self, ref: SessionRef) -> HookCallback:
        data = ref.data
        guard = PathGuard(Path(data["workdir"]), self.protected_paths)
        api_key = _api_key()

        async def pre_tool_use(
            input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            tool_name = str(input_data.get("tool_name", ""))
            tool_input = input_data.get("tool_input") or {}
            if tool_name == "ExitPlanMode":
                return _deny(_PLAN_DENIAL)
            decision = guard.check_tool(tool_name, tool_input)
            if decision.allow:
                data["denials"] = 0
                return {}
            reason = _activity(data, "denied", f"{tool_name}: {decision.reason}", api_key)
            data["denials"] = data.get("denials", 0) + 1
            if data["denials"] >= MAX_CONSECUTIVE_DENIALS:
                data["state"] = AgentState.NEEDS_INPUT
                data["question"] = redact(
                    f"{data['denials']} tool uses in a row were denied; last: {reason}",
                    extra=[api_key] if api_key else None,
                )
            return _deny(decision.reason or "denied")

        return pre_tool_use

    def _handle_message(self, ref: SessionRef, msg: SDKMessage, api_key: str | None) -> bool:
        """Record one SDK message; return True when it ends the turn."""
        data = ref.data
        kind = type(msg).__name__
        seen_id = getattr(msg, "session_id", None)
        if kind == "SystemMessage" and isinstance(getattr(msg, "data", None), dict):
            seen_id = msg.data.get("session_id")
        if isinstance(seen_id, str) and seen_id and seen_id != data.get("claude_session_id"):
            data["claude_session_id"] = seen_id

        if kind == "AssistantMessage":
            for block in getattr(msg, "content", []):
                block_kind = type(block).__name__
                if block_kind == "TextBlock":
                    _activity(data, "text", block.text, api_key)
                elif block_kind == "ToolUseBlock":
                    _activity(data, "tool", f"{block.name} {json.dumps(block.input, default=str)}", api_key)
                    if block.name == "ExitPlanMode" and isinstance(block.input.get("plan"), str):
                        data["plan"] = redact(block.input["plan"], extra=[api_key] if api_key else None)
                        data["state"] = AgentState.PLAN_READY
        elif kind == "ResultMessage":
            subtype = str(getattr(msg, "subtype", "") or "")
            if getattr(msg, "is_error", False) or subtype.startswith("error"):
                detail = getattr(msg, "result", None) or "; ".join(getattr(msg, "errors", None) or [])
                raise ClaudeTurnError(f"{subtype or 'error'}: {detail}".rstrip(": "))
            return True
        return False

    async def _pump(self, ref: SessionRef, session: ClaudeSession, api_key: str | None) -> None:
        async for msg in session.receive_messages():
            done = self._handle_message(ref, msg, api_key)
            if ref.data["state"] == AgentState.NEEDS_INPUT:
                await session.interrupt()
                return
            if done:
                return

    async def _worker(self, ref: SessionRef, text: str) -> None:
        data = ref.data
        api_key = _api_key()
        timeout = self.config.plan_timeout if data["phase"] == "plan" else self.config.implement_timeout
        try:
            try:
                async with asyncio.timeout(timeout):
                    session = await self._session(ref)
                    data["turns"] = data.get("turns", 0) + 1
                    await session.query(text)
                    await self._pump(ref, session, api_key)
            except TimeoutError:
                data["state"] = AgentState.FAILED
                data["error"] = "timeout"
                stuck = self._sessions.get(ref.id)
                if stuck is not None:
                    await stuck.interrupt()
                return

            state = data["state"]
            if state == AgentState.PLANNING:
                data["state"] = AgentState.FAILED
                data["error"] = "Failed to produce a plan"
            elif state == AgentState.WORKING:
                if data.get("pr"):
                    await self._revise(ref, text, api_key)
                else:
                    await self._publish(ref, api_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the one top-level handler: a worker failure becomes FAILED, never a crash
            data["state"] = AgentState.FAILED
            data["error"] = redact(str(e) or type(e).__name__, extra=[api_key] if api_key else None)
        finally:
            if self._tasks.get(ref.id) is asyncio.current_task():
                self._tasks.pop(ref.id, None)
            if data["state"] == AgentState.FAILED:
                failed = self._sessions.pop(ref.id, None)
                if failed is not None:
                    await failed.disconnect()

    def _worktree(self, data: dict[str, Any]) -> Worktree:
        return Worktree(
            path=Path(data["workdir"]),
            repo=data["repo"],
            issue=data["issue"],
            branch=data["branch"],
            base=data["base_branch"],
        )

    async def _publish(self, ref: SessionRef, api_key: str | None) -> None:
        data = ref.data
        try:
            result = await publish_worktree(
                wt=self._worktree(data),
                gh=self.gh,
                repo=data["repo"],
                issue_number=data["issue"],
                issue_title=data["title"],
                protected_paths=self.protected_paths,
            )
        except ProtectedPathViolation as e:
            data["state"] = AgentState.NEEDS_INPUT
            data["question"] = f"Protected path(s) changed: {', '.join(e.paths)}"
            return
        except (PublishError, OSError) as e:
            data["state"] = AgentState.FAILED
            data["error"] = redact(str(e), extra=[api_key] if api_key else None)
            return
        if result is None:
            data["state"] = AgentState.FAILED
            data["error"] = "no_changes"
            return
        data["pr"] = {"number": result.pr_number, "url": result.pr_url, "head_sha": result.head_sha}
        data["state"] = AgentState.PR_READY

    async def _revise(self, ref: SessionRef, text: str, api_key: str | None) -> None:
        data = ref.data
        try:
            head_sha = await push_revision(
                self._worktree(data),
                message=text.strip().splitlines()[0][:72] if text.strip() else "Address feedback",
                protected_paths=self.protected_paths,
            )
        except ProtectedPathViolation as e:
            data["state"] = AgentState.NEEDS_INPUT
            data["question"] = f"Protected path(s) changed: {', '.join(e.paths)}"
            return
        except PublishError as e:
            if str(e) == "nothing to commit":
                data["state"] = AgentState.PR_READY
                return
            data["state"] = AgentState.FAILED
            data["error"] = redact(str(e), extra=[api_key] if api_key else None)
            return
        data["pr"] = {**data["pr"], "head_sha": head_sha}
        data["state"] = AgentState.PR_READY

    # -- protocol ------------------------------------------------------------------------------------
    async def wait_idle(self, ref: SessionRef) -> None:
        """Wait until the ref's background turn (if any) has finished."""
        task = self._tasks.get(ref.id)
        if task is not None:
            await asyncio.wait({task})

    async def poll(self, ref: SessionRef) -> AgentSnapshot:
        data = ref.data
        orphaned = (
            ref.id not in self._seen
            and ref.id not in self._tasks
            and data["state"] in (AgentState.PLANNING, AgentState.WORKING)
        )
        if orphaned:  # the daemon restarted mid-turn: resume the persisted session
            self._begin_turn(ref, "Continue where you left off.")

        pr_ref = PrRef(**data["pr"]) if data.get("pr") else None
        idx = data.get("last_activity_idx", 0)
        new_activities = data["activity"][idx:]
        data["last_activity_idx"] = len(data["activity"])
        return AgentSnapshot(
            state=AgentState(data["state"]),
            plan=data.get("plan"),
            question=data.get("question"),
            pr=pr_ref,
            activity=[ActivityItem(ts=a["ts"], kind=a["kind"], text=a["text"]) for a in new_activities],
            error=data.get("error"),
            poll_after=1.0 if ref.id in self._tasks else 60.0,
        )

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        if ref.id in self._tasks:
            raise ValueError("session is already running")
        ref.data["phase"] = "implement"
        session = await self._session(ref)
        await session.set_permission_mode("acceptEdits")
        self._begin_turn(ref, note or DEFAULT_APPROVAL_NOTE)

    async def send(self, ref: SessionRef, text: str) -> None:
        if ref.id in self._tasks:
            raise ValueError("session is already running")
        if ref.data["phase"] == "plan":
            ref.data["plan"] = None
        self._begin_turn(ref, text)

    async def cancel(self, ref: SessionRef) -> None:
        task = self._tasks.pop(ref.id, None)
        session = self._sessions.pop(ref.id, None)
        if task is None and session is None:
            return
        ref.data["state"] = AgentState.FAILED
        ref.data["error"] = "cancelled"
        if task is not None and not task.done():
            task.cancel()
        if session is not None:
            await session.interrupt()
            await session.disconnect()
