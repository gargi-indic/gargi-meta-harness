import asyncio
import importlib.resources
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from harness.adapters.base import (
    ActivityItem,
    AgentSnapshot,
    AgentState,
    Capabilities,
    PrRef,
    SessionRef,
    StartRequest,
)
from harness.adapters.guard import DEFAULT_PROTECTED
from harness.config import AgentConfig
from harness.github.client import GitHub
from harness.publish import ProtectedPathViolation, PublishError, publish_worktree
from harness.secrets import redact
from harness.workspaces import Worktree, WorktreeManager

INVALID_JSON_RETRY_NOTE = "Your previous reply was not valid JSON. Reply with only JSON matching the schema."


class CodexAgentConfig(AgentConfig):
    implement_flags: list[str] = Field(default_factory=lambda: ["--full-auto"])
    plan_timeout: float = 600.0
    implement_timeout: float = 3600.0
    cancel_grace: float = 5.0


class RunningProcess(Protocol):
    def stdout_lines(self) -> AsyncIterator[str]: ...
    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class ProcessRunner(Protocol):
    async def spawn(self, argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> RunningProcess: ...


class SubprocessRunningProcess:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def stdout_lines(self) -> AsyncIterator[str]:
        if not self._proc.stdout:
            return
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace")

    async def wait(self) -> int:
        return await self._proc.wait()

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass


class SubprocessRunner:
    async def spawn(self, argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> RunningProcess:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return SubprocessRunningProcess(proc)


def extract_session_id(event: dict[str, Any]) -> str | None:
    for key in ("thread_id", "session_id"):
        if key in event and isinstance(event[key], str):
            return event[key]
    if "msg" in event and isinstance(event["msg"], dict):
        for key in ("thread_id", "session_id"):
            if key in event["msg"] and isinstance(event["msg"][key], str):
                return event["msg"][key]
    if "item" in event and isinstance(event["item"], dict):
        for key in ("thread_id", "session_id"):
            if key in event["item"] and isinstance(event["item"][key], str):
                return event["item"][key]
    return None


_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")


@dataclass
class _TaskState:
    process: RunningProcess
    task: asyncio.Task[None]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _api_key() -> str | None:
    return os.environ.get("CODEX_API_KEY") or None


def _activity(data: dict[str, Any], kind: str, text: str, api_key: str | None) -> str:
    safe = redact(text, extra=[api_key] if api_key else None)
    data.setdefault("activity", []).append({"ts": _now(), "kind": kind, "text": safe})
    return safe


def _render_plan(plan_json: dict[str, Any]) -> str:
    md = [f"**Summary:** {plan_json.get('summary', '')}\n"]
    for step in plan_json.get("steps", []):
        md.append(f"### {step.get('title', '')}")
        md.append(f"Files: {', '.join(step.get('files', []))}")
        md.append(f"{step.get('notes', '')}\n")
    return "\n".join(md)


class CodexAdapter:
    name = "codex"

    def __init__(
        self,
        *,
        workspaces: WorktreeManager,
        gh: GitHub,
        config: CodexAgentConfig,
        runner: ProcessRunner | None = None,
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
        self.runner = runner or SubprocessRunner()
        self.store = store
        self.protected_paths = protected_paths
        self._running: dict[str, _TaskState] = {}
        self._by_key: dict[str, SessionRef] = {}
        self._kill_tasks: set[asyncio.Task[None]] = set()

    async def find(self, idempotency_key: str) -> SessionRef | None:
        return self._by_key.get(idempotency_key)

    def _env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
        key = _api_key()
        if key:
            env["CODEX_API_KEY"] = key
        return env

    async def start(self, req: StartRequest) -> SessionRef:
        if req.workdir is None:
            raise ValueError("Codex requires a local workdir")

        existing = await self.find(req.idempotency_key)
        if existing:
            return existing

        ref = SessionRef(
            agent=self.name,
            id=f"codex-{req.idempotency_key}",
            url=None,
            data={
                "idempotency_key": req.idempotency_key,
                "mode": req.mode,
                "state": AgentState.STARTING,
                "repo": req.repo,
                "issue": req.issue,
                "title": req.title,
                "brief": req.brief,
                "branch": req.branch,
                "base_branch": req.base_branch,
                "workdir": str(req.workdir),
                "activity": [],
                "codex_session_id": None,
                "plan": None,
                "error": None,
                "pr": None,
                "question": None,
                "invalid_json_retries": 0,
            },
        )
        self._by_key[req.idempotency_key] = ref
        await self._spawn_process(ref, req.workdir)
        return ref

    async def _spawn_process(
        self,
        ref: SessionRef,
        workdir: Path,
        *,
        resume_text: str | None = None,
        retry_prompt: str | None = None,
    ) -> None:
        data = ref.data
        mode = data["mode"]

        if resume_text:
            codex_id = data.get("codex_session_id")
            if not codex_id:
                raise ValueError("cannot resume without a codex session id")
            argv = ["codex", "exec", "resume", codex_id, resume_text]
        elif mode == "plan_first":
            schema_path = str(
                importlib.resources.files("harness.adapters").joinpath("codex_plan.schema.json")
            )
            prompt = data["brief"]
            if retry_prompt:
                prompt += "\n\n" + retry_prompt
            argv = [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--output-schema",
                schema_path,
                prompt,
            ]
        else:
            argv = ["codex", "exec", "--json"] + self.config.implement_flags
            prompt = data["brief"]
            plan_markdown = data.get("plan")
            if plan_markdown:
                prompt += f"\n\nApproved Plan:\n{plan_markdown}"
                note = data.get("plan_approved_note")
                if note and note != "approved":
                    prompt += f"\n\nNote: {note}"
            argv.append(prompt)

        process = await self.runner.spawn(argv, cwd=workdir, env=self._env())
        task = asyncio.create_task(self._process_worker(ref, process, mode, workdir))
        self._running[ref.id] = _TaskState(process=process, task=task)
        data["state"] = AgentState.WORKING if mode == "direct" else AgentState.PLANNING

    def _handle_line(self, data: dict[str, Any], line: str, mode: str, api_key: str | None) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            _activity(data, "output", line, api_key)
            return
        if not isinstance(event, dict):
            _activity(data, "output", line, api_key)
            return

        if not data.get("codex_session_id"):
            codex_id = extract_session_id(event)
            if codex_id:
                data["codex_session_id"] = codex_id

        event_type = event.get("event") or event.get("type", "unknown")
        text = event.get("text") or event.get("message", "")

        if event_type == "error":
            data["error"] = _activity(data, "error", text, api_key)
        elif event_type in ("patch", "command", "message"):
            _activity(data, event_type, text, api_key)
            if event_type == "message" and mode == "plan_first":
                try:
                    plan_json = json.loads(text)
                except json.JSONDecodeError:
                    return
                if isinstance(plan_json, dict) and "summary" in plan_json and "steps" in plan_json:
                    data["plan_json"] = plan_json
        else:
            _activity(data, event_type, json.dumps(event), api_key)

    async def _retry_plan(self, ref: SessionRef, workdir: Path) -> bool:
        data = ref.data
        if data.get("invalid_json_retries", 0) >= 1:
            return False
        data["invalid_json_retries"] = data.get("invalid_json_retries", 0) + 1
        data["state"] = AgentState.PLANNING
        await self._spawn_process(ref, workdir, retry_prompt=INVALID_JSON_RETRY_NOTE)
        return True

    async def _process_worker(
        self, ref: SessionRef, process: RunningProcess, mode: str, workdir: Path
    ) -> None:
        data = ref.data
        api_key = _api_key()
        timeout = self.config.plan_timeout if mode == "plan_first" else self.config.implement_timeout

        try:
            try:
                async with asyncio.timeout(timeout):
                    async for line in process.stdout_lines():
                        if line.strip():
                            self._handle_line(data, line, mode, api_key)
                    exit_code = await process.wait()
            except TimeoutError:
                process.terminate()
                data["state"] = AgentState.FAILED
                data["error"] = "timeout"
                return

            if exit_code != 0:
                if (
                    mode == "plan_first"
                    and not data.get("plan_json")
                    and await self._retry_plan(ref, workdir)
                ):
                    return
                data["state"] = AgentState.FAILED
                data["error"] = redact(
                    data.get("error") or f"process exited with code {exit_code}",
                    extra=[api_key] if api_key else None,
                )
                return

            if mode == "plan_first":
                plan_json = data.get("plan_json")
                if plan_json:
                    data["plan"] = redact(_render_plan(plan_json), extra=[api_key] if api_key else None)
                    data["state"] = AgentState.PLAN_READY
                elif not await self._retry_plan(ref, workdir):
                    data["state"] = AgentState.FAILED
                    data["error"] = "Failed to produce a valid plan"
                return

            await self._publish(ref, workdir, api_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            data["state"] = AgentState.FAILED
            data["error"] = redact(str(e), extra=[api_key] if api_key else None)
        finally:
            state = self._running.get(ref.id)
            if state is not None and state.process is process:
                self._running.pop(ref.id, None)

    async def _publish(self, ref: SessionRef, workdir: Path, api_key: str | None) -> None:
        data = ref.data
        wt = Worktree(
            path=workdir,
            repo=data["repo"],
            issue=data["issue"],
            branch=data["branch"],
            base=data["base_branch"],
        )
        try:
            result = await publish_worktree(
                wt=wt,
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

    async def poll(self, ref: SessionRef) -> AgentSnapshot:
        data = ref.data
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
            poll_after=1.0 if ref.id in self._running else 60.0,
        )

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        ref.data["plan_approved_note"] = note or "approved"

    async def send(self, ref: SessionRef, text: str) -> None:
        if ref.id in self._running:
            raise ValueError("session is already running")
        await self._spawn_process(ref, Path(ref.data["workdir"]), resume_text=text)

    async def _kill_after_grace(self, proc: RunningProcess) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.config.cancel_grace)
        except TimeoutError:
            proc.kill()

    async def cancel(self, ref: SessionRef) -> None:
        state = self._running.pop(ref.id, None)
        if state is None:
            return
        state.process.terminate()

        kill_task = asyncio.create_task(self._kill_after_grace(state.process))
        self._kill_tasks.add(kill_task)
        kill_task.add_done_callback(self._kill_tasks.discard)

        ref.data["state"] = AgentState.FAILED
        ref.data["error"] = "cancelled"

        if not state.task.done():
            state.task.cancel()
