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
    SessionRef,
    StartRequest,
)
from harness.adapters.guard import DEFAULT_PROTECTED
from harness.config import AgentConfig
from harness.github.client import GitHub
from harness.publish import ProtectedPathViolation, PublishError, publish_worktree
from harness.secrets import redact
from harness.workspaces import WorktreeManager

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


@dataclass
class _TaskState:
    process: RunningProcess
    task: asyncio.Task[None]
    mode: str
    wt_path: Path
    issue_number: int
    issue_title: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    async def find(self, idempotency_key: str) -> SessionRef | None:
        return None

    def _env(self) -> dict[str, str]:
        return os.environ.copy()

    async def start(self, req: StartRequest) -> SessionRef:
        session_id = f"codex-{req.idempotency_key}"
        if req.workdir is None:
            raise ValueError("Codex requires a local workdir")

        ref = await self.find(req.idempotency_key)
        if not ref:
            ref = SessionRef(
                agent=self.name,
                id=session_id,
                url=None,
                data={
                    "idempotency_key": req.idempotency_key,
                    "mode": req.mode,
                    "state": AgentState.STARTING,
                    "repo": req.repo,
                    "issue": req.issue,
                    "title": req.title,
                    "brief": req.brief,
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

        ref.data["mode"] = req.mode
        ref.data["workdir"] = str(req.workdir)

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
        env = self._env()

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
            plan_markdown = data.get("plan")
            if plan_markdown:
                prompt = data["brief"] + f"\n\nApproved Plan:\n{plan_markdown}"
                if data.get("plan_approved_note") and data.get("plan_approved_note") != "approved":
                    prompt += f"\n\nNote: {data['plan_approved_note']}"
                argv.append(prompt)
            else:
                argv.append(data["brief"])

        process = await self.runner.spawn(argv, cwd=workdir, env=env)
        task = asyncio.create_task(self._process_worker(ref, process, mode, workdir))
        self._running[ref.id] = _TaskState(
            process=process,
            task=task,
            mode=mode,
            wt_path=workdir,
            issue_number=data["issue"],
            issue_title=data["title"],
        )
        data["state"] = AgentState.WORKING if mode == "direct" else AgentState.PLANNING

    async def _process_worker(
        self, ref: SessionRef, process: RunningProcess, mode: str, workdir: Path
    ) -> None:
        data = ref.data
        api_key = self._env().get("CODEX_API_KEY")

        try:
            async for line in process.stdout_lines():
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    data["activity"].append(
                        {
                            "ts": _now(),
                            "kind": "output",
                            "text": redact(line, extra=[api_key] if api_key else None),
                        }
                    )
                    continue

                if not data.get("codex_session_id"):
                    codex_id = extract_session_id(event)
                    if codex_id:
                        data["codex_session_id"] = codex_id

                event_type = event.get("event") or event.get("type", "unknown")
                text = event.get("text") or event.get("message", "")

                if event_type == "error":
                    data["activity"].append(
                        {
                            "ts": _now(),
                            "kind": "error",
                            "text": redact(text, extra=[api_key] if api_key else None),
                        }
                    )
                    data["error"] = redact(text, extra=[api_key] if api_key else None)
                elif event_type == "patch":
                    data["activity"].append(
                        {
                            "ts": _now(),
                            "kind": "patch",
                            "text": redact(text, extra=[api_key] if api_key else None),
                        }
                    )
                elif event_type == "command":
                    data["activity"].append(
                        {
                            "ts": _now(),
                            "kind": "command",
                            "text": redact(text, extra=[api_key] if api_key else None),
                        }
                    )
                elif event_type == "message":
                    data["activity"].append(
                        {
                            "ts": _now(),
                            "kind": "message",
                            "text": redact(text, extra=[api_key] if api_key else None),
                        }
                    )
                    if mode == "plan_first":
                        try:
                            plan_json = json.loads(text)
                            if "summary" in plan_json and "steps" in plan_json:
                                data["plan_json"] = plan_json
                        except json.JSONDecodeError:
                            pass
                else:
                    data["activity"].append(
                        {
                            "ts": _now(),
                            "kind": event_type,
                            "text": redact(json.dumps(event), extra=[api_key] if api_key else None),
                        }
                    )

            timeout = self.config.plan_timeout if mode == "plan_first" else self.config.implement_timeout
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                process.terminate()
                data["state"] = AgentState.FAILED
                data["error"] = "timeout"
                return

            if exit_code != 0:
                if mode == "plan_first" and not data.get("plan_json"):
                    if data.get("invalid_json_retries", 0) < 1:
                        data["invalid_json_retries"] = data.get("invalid_json_retries", 0) + 1
                        data["state"] = AgentState.PLANNING
                        await self._spawn_process(ref, workdir, retry_prompt=INVALID_JSON_RETRY_NOTE)
                        return
                    else:
                        data["state"] = AgentState.FAILED
                        data["error"] = redact(
                            data.get("error") or "process failed and output was not valid JSON",
                            extra=[api_key] if api_key else None,
                        )
                else:
                    data["state"] = AgentState.FAILED
                    data["error"] = redact(
                        data.get("error") or f"process exited with code {exit_code}",
                        extra=[api_key] if api_key else None,
                    )
                return

            if mode == "plan_first":
                plan_json = data.get("plan_json")
                if plan_json:
                    md = [f"**Summary:** {plan_json.get('summary', '')}\n"]
                    for step in plan_json.get("steps", []):
                        md.append(f"### {step.get('title', '')}")
                        md.append(f"Files: {', '.join(step.get('files', []))}")
                        md.append(f"{step.get('notes', '')}\n")

                    data["plan"] = "\n".join(md)
                    data["state"] = AgentState.PLAN_READY
                else:
                    if data.get("invalid_json_retries", 0) < 1:
                        data["invalid_json_retries"] = data.get("invalid_json_retries", 0) + 1
                        data["state"] = AgentState.PLANNING
                        await self._spawn_process(ref, workdir, retry_prompt=INVALID_JSON_RETRY_NOTE)
                        return
                    else:
                        data["state"] = AgentState.FAILED
                        data["error"] = "Failed to produce a valid plan"
            else:
                wt = await self.workspaces.get(data["repo"], data["issue"])
                if not wt:
                    from harness.workspaces import Worktree

                    wt = Worktree(
                        path=workdir,
                        repo=data["repo"],
                        issue=data["issue"],
                        branch=f"harness/{data['issue']}-slug",
                        base="main",
                    )

                try:
                    pub_result = await publish_worktree(
                        wt=wt,
                        gh=self.gh,
                        repo=data["repo"],
                        issue_number=data["issue"],
                        issue_title=data["title"],
                        protected_paths=self.protected_paths,
                    )

                    if pub_result is None:
                        data["state"] = AgentState.FAILED
                        data["error"] = "no_changes"
                    else:
                        data["pr"] = {
                            "number": pub_result.pr_number,
                            "url": pub_result.pr_url,
                            "head_sha": pub_result.head_sha,
                        }
                        data["state"] = AgentState.PR_READY
                except ProtectedPathViolation as e:
                    data["state"] = AgentState.NEEDS_INPUT
                    data["question"] = f"Protected path(s) changed: {', '.join(e.paths)}"
                except PublishError as e:
                    data["state"] = AgentState.FAILED
                    data["error"] = redact(str(e), extra=[api_key] if api_key else None)
                except Exception as e:
                    data["state"] = AgentState.FAILED
                    data["error"] = redact(str(e), extra=[api_key] if api_key else None)

        except asyncio.CancelledError:
            raise
        finally:
            if ref.id in self._running and self._running[ref.id].process is process:
                self._running.pop(ref.id, None)

    async def poll(self, ref: SessionRef) -> AgentSnapshot:
        data = ref.data
        pr_ref = None
        if data.get("pr"):
            from harness.adapters.base import PrRef

            pr_ref = PrRef(**data["pr"])

        activities = []
        idx = data.get("last_activity_idx", 0)
        new_activities = data["activity"][idx:]
        data["last_activity_idx"] = len(data["activity"])

        for a in new_activities:
            activities.append(ActivityItem(ts=a["ts"], kind=a["kind"], text=a["text"]))

        return AgentSnapshot(
            state=AgentState(data["state"]),
            plan=data.get("plan"),
            question=data.get("question"),
            pr=pr_ref,
            activity=activities,
            error=data.get("error"),
            poll_after=1.0 if ref.id in self._running else 60.0,
        )

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        ref.data["plan_approved_note"] = note or "approved"

    async def send(self, ref: SessionRef, text: str) -> None:
        if ref.id in self._running:
            raise ValueError("session is already running")
        workdir = Path(ref.data["workdir"])
        await self._spawn_process(ref, workdir, resume_text=text)

    async def cancel(self, ref: SessionRef) -> None:
        if ref.id not in self._running:
            return
        task_state = self._running.pop(ref.id)
        proc = task_state.process
        proc.terminate()

        async def kill_later():
            await asyncio.sleep(self.config.cancel_grace)
            proc.kill()

        asyncio.create_task(kill_later())

        ref.data["state"] = AgentState.FAILED
        ref.data["error"] = "cancelled"

        if not task_state.task.done():
            task_state.task.cancel()
