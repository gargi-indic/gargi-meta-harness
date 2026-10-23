"""Demo mode for ``harness serve --demo``: seeded in-memory data, an in-memory GitHub and a scripted run.

Nothing here touches the network or a token file. The real engine Runner drives the real agent, gate and escalate
executors with a :class:`FakeAdapter`; only the executors that would call GitHub or an LLM (``review``, ``check``,
``merge``) are replaced by deterministic stand-ins. The routes defined here exist only on an app built by
:func:`create_demo_app`.
"""

import asyncio
import contextlib
import os
import time
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Request

from harness.adapters.base import (
    ActivityItem,
    AdapterRegistry,
    AgentSnapshot,
    AgentState,
    PrRef,
)
from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine.executors.agent import AgentExecutor
from harness.engine.executors.base import Done, ExecContext, ExecutorRegistry, Waiting
from harness.engine.executors.escalate import EscalateExecutor
from harness.engine.executors.gate import GateExecutor
from harness.engine.runner import Runner
from harness.github.client import GitHub, GitHubError
from harness.playbook.load import builtin_playbooks
from harness.playbook.models import Node
from harness.server.app import create_app
from harness.server.auth import generate_token
from harness.server.demo_seed import REPO, Seed, seed_config, seed_store
from harness.server.static import default_web_dist
from harness.store.db import Store
from harness.store.repo import IssueRow, StepRow, get_issue

DEMO_TOKEN_ENV = "HARNESS_DEMO_TOKEN"
DEMO_ISSUE = 7  # the seeded issue that has no run (label agent:fake)
STEP_INTERVAL_S = 0.5
# The runner's clock advances this much per tick, so poll_after delays (gates wait 15 s) never slow the demo.
VIRTUAL_TICK_S = 60.0
DEMO_PR = PrRef(number=101, url="https://example.invalid/pr/101", head_sha="demo0001")


class DemoGitHub:
    """The few GitHub calls the server makes (repo, contents get/put, refs, create PR), answered from memory.

    Deliberately not a :class:`GitHub` subclass: anything it does not implement fails loudly instead of
    reaching the network.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.refs: dict[str, str] = {"main": "0" * 40}
        self.created_prs: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    async def repo(self, repo: str) -> dict[str, Any]:
        return {"full_name": repo, "default_branch": "main"}

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str | None:
        return self.files.get(f"{ref or 'main'}:{path}")

    async def get(self, path: str, **params: Any) -> Any:
        if path.endswith("/pulls"):
            return []
        if "/git/ref/heads/" in path:
            branch = path.split("/git/ref/heads/", 1)[1]
            if branch in self.refs:
                return {"ref": f"refs/heads/{branch}", "object": {"sha": self.refs[branch]}}
        raise GitHubError(404, "GET", path, "not found (demo)")

    async def post(self, path: str, **json: Any) -> Any:
        if path.endswith("/git/refs"):
            self.refs[str(json["ref"]).removeprefix("refs/heads/")] = str(json["sha"])
            return {"ref": json["ref"]}
        raise GitHubError(404, "POST", path, "not found (demo)")

    async def put(self, path: str, **json: Any) -> Any:
        if "/contents/" in path:
            file_path = path.split("/contents/", 1)[1]
            self.files[f"{json.get('branch', 'main')}:{file_path}"] = str(json.get("content", ""))
            return {"content": {"path": file_path}}
        raise GitHubError(404, "PUT", path, "not found (demo)")

    async def create_pull(self, repo: str, title: str, head: str, base: str, **kwargs: Any) -> dict[str, Any]:
        number = len(self.created_prs) + 1
        pr = {
            "number": number,
            "html_url": f"https://example.invalid/pr/{number}",
            "title": title,
            "head": head,
            "base": base,
            "repo": repo,
        }
        self.created_prs.append(pr)
        return pr


# ---- executors that would need GitHub or an LLM --------------------------------------------------------


class _DemoReview:
    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Done:
        return Done(
            "approve",
            {"verdict": "approve", "findings": [], "correction": "", "head_sha": DEMO_PR.head_sha},
        )


class _DemoCheck:
    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Done:
        return Done("pass", {"head_sha": DEMO_PR.head_sha})


class _DemoMerge:
    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Done | Waiting:
        return Done("merged", {"note": "merged in demo mode", "head_sha": DEMO_PR.head_sha})


def _note(text: str, kind: str = "progress") -> ActivityItem:
    return ActivityItem(ts="2026-01-01T00:00:00Z", kind=kind, text=text)


def demo_script() -> list[list[AgentSnapshot]]:
    """Phase 0 plans, ``approve_plan`` moves to phase 1 which builds and opens the PR."""
    planning = [
        AgentSnapshot(state=AgentState.PLANNING, poll_after=0, activity=[_note(text)])
        for text in ("Reading the issue", "Reading the widget module", "Drafting the plan")
    ]
    working = [
        AgentSnapshot(state=AgentState.WORKING, poll_after=0, activity=[_note(text, kind)])
        for text, kind in (
            ("Editing docs/widgets.md", "message"),
            ("pytest -q", "command"),
            ("Committing the change", "progress"),
        )
    ]
    return [
        [
            *planning,
            AgentSnapshot(state=AgentState.PLAN_READY, plan="1. Document the widget API.", poll_after=0),
        ],
        [*working, AgentSnapshot(state=AgentState.PR_READY, pr=DEMO_PR, poll_after=0)],
    ]


class _DemoRunner(Runner):
    """Advances only the scripted demo run; the seeded runs stay exactly as seeded."""

    only_run: str | None = None

    def _due_steps(self, now_iso: str) -> list[StepRow]:
        return [s for s in super()._due_steps(now_iso) if s.run_id == self.only_run]


class DemoDriver:
    """Starts one scripted run and ticks the real Runner on a timer (one step per ``interval_s``)."""

    def __init__(self, store: Store, *, interval_s: float = STEP_INTERVAL_S) -> None:
        self.store = store
        self.interval_s = interval_s
        self.adapter = FakeAdapter(name="fake", script={"*": demo_script()})
        adapters = AdapterRegistry()
        adapters.register(self.adapter)
        executors = ExecutorRegistry()
        executors.register("agent", AgentExecutor())
        executors.register("gate", GateExecutor())
        executors.register("escalate", EscalateExecutor())
        executors.register("review", _DemoReview())
        executors.register("check", _DemoCheck())
        executors.register("merge", _DemoMerge())
        self._virtual_offset = 0.0
        self.runner = _DemoRunner(
            store,
            None,
            executors,
            adapters,
            repo_configs={REPO: RepoConfig(default_agent="fake")},
            clock=lambda: time.time() + self._virtual_offset,
        )
        self.run_id: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def step(self) -> int:
        """One tick of the Runner; the virtual clock moves on so waiting steps are due again."""
        self._virtual_offset += VIRTUAL_TICK_S
        return await self.runner.tick()

    async def _loop(self) -> None:
        while True:
            await self.step()
            await asyncio.sleep(self.interval_s)

    def start_run(self, *, background: bool = True) -> str:
        """Start the run (once) and, unless ``background`` is false, the timer that advances it."""
        if self.run_id is None:
            issue = get_issue(self.store, REPO, DEMO_ISSUE)
            assert isinstance(issue, IssueRow)
            playbook = builtin_playbooks()["plan-build-review"]
            self.run_id = self.runner.start_run(REPO, issue, playbook, agent="fake")
            self.runner.only_run = self.run_id
        if background and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())
        return self.run_id

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


def _demo_router() -> APIRouter:
    router = APIRouter(prefix="/api/demo", tags=["demo"])

    @router.post("/start-run", summary="Start the scripted demo run (demo mode only)")
    async def start_run(request: Request) -> dict[str, str]:
        driver: DemoDriver = request.app.state.driver
        return {"run_id": driver.start_run()}

    return router


def create_demo_app(
    *, token: str | None = None, port: int = 8799, interval_s: float = STEP_INTERVAL_S
) -> tuple[FastAPI, str]:
    """The app, seeded in memory, and its token (``token``, else ``HARNESS_DEMO_TOKEN``, else generated)."""
    token = token or os.environ.get(DEMO_TOKEN_ENV) or generate_token()
    store = Store(":memory:")
    store.migrate()
    seed: Seed = seed_store(store)
    gh = DemoGitHub()
    app = create_app(
        store=store,
        config=seed_config(),
        token=token,
        web_dist=default_web_dist(),
        port=port,
        gh=cast(GitHub, gh),
        demo=True,
        extra_routers=[_demo_router()],
    )
    app.state.driver = DemoDriver(store, interval_s=interval_s)
    app.state.demo_gh = gh
    app.state.seed = seed
    return app, token
