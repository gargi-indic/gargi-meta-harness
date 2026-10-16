"""The durable reconcile loop.

A run executes one playbook against one issue. Its whole state lives in the store (a ``runs`` row plus one
``steps`` row per node visit), so a fresh ``Runner`` on the same store resumes exactly where a crashed one
stopped. ``tick()`` advances every runnable step; each result is applied in one ``store.tx()`` together with
its routing and the outbox writes that mirror it to GitHub.

Ordering rule for anything with side effects: the step row (the intent) exists first, the executor makes the
external call (idempotent, keyed by ``f"{run_id}/{session}"``), and only then is the returned ``ref``
persisted. A crash between the call and the persist makes the executor find its own work again.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from harness.adapters.base import AdapterRegistry, SessionRef
from harness.config import RepoConfig
from harness.engine.executors.base import Done, ExecContext, ExecutorRegistry, Waiting
from harness.engine.model import MAX_OUTPUT_BYTES, to_snapshot
from harness.github.client import GitHub
from harness.github.control_table import ControlRow
from harness.github.labels import HARNESS_LABELS
from harness.github.ledger import LedgerTooLarge, render_event, render_ledger
from harness.github.outbox import Outbox
from harness.playbook.models import JoinNode, Node, ParallelNode, Playbook
from harness.playbook.templating import Context, IssueCtx, PrCtx, RunCtx
from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import (
    IssueRow,
    RunRow,
    StepRow,
    append_event,
    get_issue,
    get_run,
    insert_run,
    insert_step,
    list_runs,
    steps_for_run,
    update_run,
    update_step,
)

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
RUNNABLE_STATUSES = frozenset({"running", "needs-you"})
CRASH_POINTS = ("before_advance", "after_advance", "after_step_update", "after_route", "after_mirror")
MAX_EXHAUSTED_HOPS = 10

_STATUS_LABELS = {
    "running": "harness:running",
    "needs-you": "harness:needs-you",
    "paused": "harness:blocked",
    "done": "harness:done",
    "failed": "harness:failed",
    "cancelled": "harness:failed",
}
_CONTROL_STATUS = {
    "running": "running",
    "needs-you": "needs-you",
    "paused": "blocked",
    "done": "done",
    "failed": "failed",
    "cancelled": "failed",
}
_AGENT_COMMENTS = {"plan_ready": "plan ready", "pr_ready": "PR opened"}


class SimulatedCrash(BaseException):
    """Raised by the ``crash_after`` test hook. A BaseException so no ``except Exception`` can swallow it."""


class RunExists(Exception):
    """A non-terminal run already exists for the issue."""


def _iso(ts: float) -> str:
    """Format epoch seconds like ``Store.now()``."""
    return datetime.fromtimestamp(ts, UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class JoinExecutor:
    """Built-in executor for ``join`` nodes. Re-evaluated on every tick until its condition holds."""

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, JoinNode)
        if node.mode == "select":
            raise NotImplementedError("join mode 'select' arrives in M3")
        inputs = list(node.inputs) if node.inputs else self._inputs_from_edges(step.node_id, ctx.playbook)
        latest: dict[str, StepRow] = {}
        for s in steps_for_run(ctx.store, ctx.run.id):
            if s.node_id in inputs and (s.node_id not in latest or s.visit > latest[s.node_id].visit):
                latest[s.node_id] = s
        finished = {n: s for n, s in latest.items() if s.status == "done"}
        failed = sorted(n for n, s in finished.items() if s.outcome == "failed")
        ok = {n: s.output or {} for n, s in finished.items() if s.outcome != "failed"}
        if node.mode == "all":
            if failed:
                return Done("failed", {"inputs": ok, "failed": failed})
            if inputs and len(finished) == len(inputs):
                return Done("done", {"inputs": ok})
        else:
            if ok:
                return Done("done", {"inputs": ok})
            if inputs and len(finished) == len(inputs):
                return Done("failed", {"inputs": {}, "failed": failed})
        return Waiting(poll_after=0)

    @staticmethod
    def _inputs_from_edges(join_id: str, playbook: Playbook) -> list[str]:
        sources = {
            key.split(".", 1)[0]
            for key, target in playbook.edges.items()
            if target == join_id and not key.startswith("*.")
        }
        return sorted(sources)


class Runner:
    def __init__(
        self,
        store: Store,
        outbox: Outbox | None,
        executors: ExecutorRegistry,
        adapters: AdapterRegistry,
        *,
        gh: GitHub | None = None,
        repo_configs: dict[str, RepoConfig] | None = None,
        playbooks: dict[str, Playbook] | None = None,
        clock: Callable[[], float] = time.time,
        crash_after: str | None = None,
    ) -> None:
        self.store = store
        self.outbox = outbox
        self.executors = executors
        self.adapters = adapters
        self.gh = gh
        self.repo_configs = repo_configs or {}
        # Fallback for runs whose playbook is not embedded in the run context (e.g. rehydrated runs).
        self.playbooks = dict(playbooks or {})
        self.clock = clock
        self.crash_after = crash_after
        self._crash_hits = 0
        self._crash_nth = 1
        if crash_after is not None:
            point, _, nth = crash_after.partition("#")
            if point not in CRASH_POINTS:
                raise ValueError(f"unknown crash point {crash_after!r}; known: {', '.join(CRASH_POINTS)}")
            self.crash_after = point
            self._crash_nth = int(nth) if nth else 1
        if "join" not in self.executors.kinds():
            self.executors.register("join", JoinExecutor())
        self._playbook_cache: dict[str, Playbook] = {}
        # Set when the current transition already posted a comment explaining a failure.
        self._failure_commented = False
        self._reported_missing: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def _crash(self, point: str) -> None:
        """Test hook: raise ``SimulatedCrash`` at the named point (once per Runner).

        ``crash_after="point#3"`` crashes on the third time that point is reached.
        """
        if self.crash_after != point:
            return
        self._crash_hits += 1
        if self._crash_hits >= self._crash_nth:
            self.crash_after = None
            raise SimulatedCrash(point)

    def register_playbook(self, playbook: Playbook) -> None:
        self.playbooks[playbook.id] = playbook

    def _playbook(self, run: RunRow) -> Playbook:
        cached = self._playbook_cache.get(run.id)
        if cached is not None:
            return cached
        raw = run.context.get("playbook")
        if raw is not None:
            playbook = Playbook.model_validate(raw)
        elif run.playbook_id in self.playbooks:
            playbook = self.playbooks[run.playbook_id]
        else:
            raise KeyError(f"playbook {run.playbook_id!r} for run {run.id} is not available")
        self._playbook_cache[run.id] = playbook
        return playbook

    def _repo_config(self, repo: str) -> RepoConfig:
        return self.repo_configs.get(repo) or RepoConfig()

    def _agent_for(self, run: RunRow, issue_row: IssueRow | None) -> str:
        agent = run.context.get("agent")
        if agent:
            return agent
        if issue_row is not None:
            for label in issue_row.labels:
                if label.startswith("agent:") and len(label) > len("agent:"):
                    return label[len("agent:") :]
        return self._repo_config(run.repo).default_agent

    def _step(self, step_id: str, run_id: str) -> StepRow | None:
        for s in steps_for_run(self.store, run_id):
            if s.id == step_id:
                return s
        return None

    def _template_context(self, run: RunRow, step: StepRow) -> Context:
        ctx = run.context
        issue_row = get_issue(self.store, run.repo, run.issue)
        issue = IssueCtx(
            number=run.issue,
            title=issue_row.title if issue_row else "",
            body=(issue_row.body or "") if issue_row else "",
            labels=list(issue_row.labels) if issue_row else [],
            agent=self._agent_for(run, issue_row),
            playbook=run.playbook_id,
        )
        pr = None
        if ctx.get("pr"):
            pr = PrCtx(**{k: v for k, v in ctx["pr"].items() if k in PrCtx.model_fields})
        return Context(
            issue=issue,
            pr=pr,
            inputs=ctx.get("inputs") or {},
            nodes=ctx.get("nodes") or {},
            prev=(ctx.get("prev_for") or {}).get(step.id),
            run=RunCtx(id=run.id, visits=dict(ctx.get("visits") or {})),
        )

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%d")

    def _emit(
        self,
        run: RunRow,
        kind: str,
        data: dict[str, Any],
        comment: tuple[str, list[str]] | None = None,
    ) -> None:
        """Append an event and, for meaningful transitions, enqueue one (uncoalesced) event comment."""
        if kind in ("no_route", "routing_loop", "executor_error"):
            self._failure_commented = True
        append_event(self.store, kind=kind, data=data, repo=run.repo, issue=run.issue, run_id=run.id)
        if comment is not None and self.outbox is not None:
            text, bullets = comment
            self.outbox.enqueue(
                "add_comment",
                {
                    "repo": run.repo,
                    "issue": run.issue,
                    "body": render_event(self._today(), run.issue, text, bullets),
                },
            )

    def _derive_status(self, run_id: str, current: str) -> str:
        if current in TERMINAL_STATUSES or current == "paused":
            return current
        needs_you = any(
            s.status == "waiting" and (s.output or {}).get("needs_you")
            for s in steps_for_run(self.store, run_id)
        )
        return "needs-you" if needs_you else "running"

    def _create_step(self, run_id: str, ctxd: dict[str, Any], node_id: str, visit: int, prev: Any) -> str:
        step_id = f"{run_id}/{node_id}/{visit}"
        ctxd.setdefault("visits", {})[node_id] = visit
        insert_step(
            self.store,
            StepRow(
                id=step_id,
                run_id=run_id,
                node_id=node_id,
                visit=visit,
                status="pending",
                outcome=None,
                ref=None,
                output=None,
                poll_after=None,
                started_at=self.store.now(),
                ended_at=None,
            ),
        )
        if prev is not None:
            ctxd.setdefault("prev_for", {})[step_id] = prev
        return step_id

    def _close_open_steps(self, run_id: str) -> None:
        now = self.store.now()
        for s in steps_for_run(self.store, run_id):
            if s.status in ("pending", "waiting", "running"):
                update_step(self.store, s.id, status="cancelled", outcome="cancelled", ended_at=now)

    # ------------------------------------------------------------------ routing

    @staticmethod
    def _lookup(playbook: Playbook, node_id: str, port: str) -> str | None:
        explicit = playbook.edges.get(f"{node_id}.{port}")
        if explicit is not None:
            return explicit
        return playbook.edges.get(f"*.{port}")

    def _route(
        self, run: RunRow, ctxd: dict[str, Any], playbook: Playbook, node_id: str, outcome: str, prev: Any
    ) -> str | None:
        """Route ``outcome`` of ``node_id``. Returns a terminal run status, or None if the run goes on."""
        target = self._lookup(playbook, node_id, outcome)
        if target is None:
            self._emit(
                run,
                "no_route",
                {"node": node_id, "outcome": outcome},
                (
                    "no route",
                    [f"node `{node_id}` finished with `{outcome}` and the playbook has no edge for it"],
                ),
            )
            return "failed"
        return self._enter(run, ctxd, playbook, target, prev, 0)

    def _enter(
        self, run: RunRow, ctxd: dict[str, Any], playbook: Playbook, target: str, prev: Any, hops: int
    ) -> str | None:
        if target == "@done":
            return "done"
        if target == "@failed":
            return "failed"
        node = playbook.nodes.get(target)
        if node is None:
            self._emit(
                run,
                "no_route",
                {"node": target, "outcome": "unknown_node"},
                ("no route", [f"unknown node `{target}`"]),
            )
            return "failed"
        visits: dict[str, int] = ctxd.setdefault("visits", {})
        next_visit = visits.get(target, 0) + 1
        if node.max_visits is not None and next_visit > node.max_visits:
            if hops >= MAX_EXHAUSTED_HOPS:
                self._emit(
                    run,
                    "routing_loop",
                    {"node": target, "hops": hops},
                    ("routing loop", [f"gave up after {hops} `exhausted` hops at `{target}`"]),
                )
                return "failed"
            exhausted = self._lookup(playbook, target, "exhausted")
            if exhausted is None:
                self._emit(
                    run,
                    "no_route",
                    {"node": target, "outcome": "exhausted"},
                    ("no route", [f"node `{target}` is out of visits and has no `exhausted` edge"]),
                )
                return "failed"
            self._emit(run, "exhausted", {"node": target, "max_visits": node.max_visits})
            return self._enter(run, ctxd, playbook, exhausted, prev, hops + 1)
        if isinstance(node, JoinNode):
            # Every branch routes into the same join; they share one open step.
            for s in steps_for_run(self.store, run.id):
                if s.node_id == target and s.status in ("pending", "waiting"):
                    if s.status == "waiting":
                        update_step(self.store, s.id, poll_after=_iso(self.clock()))
                    return None
        self._create_step(run.id, ctxd, target, next_visit, prev)
        return None

    # ------------------------------------------------------------------ run lifecycle

    def start_run(
        self,
        repo: str,
        issue: IssueRow,
        playbook: Playbook,
        *,
        playbook_sha: str | None = None,
        inputs: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> str:
        with self.store.tx():
            existing = [r for r in list_runs(self.store) if r.repo == repo and r.issue == issue.number]
            if any(r.status not in TERMINAL_STATUSES for r in existing):
                raise RunExists(f"{repo}#{issue.number} already has a run in progress")
            run_id = f"{repo}#{issue.number}@{len(existing) + 1}"
            entry = next(iter(playbook.nodes))
            now = self.store.now()
            ctxd: dict[str, Any] = {
                "inputs": inputs or {},
                "agent": agent,
                "visits": {},
                "sessions": {},
                "nodes": {},
                "prev_for": {},
                # Embedded so that a fresh Runner on the same store can resume without being handed the playbook.
                "playbook": playbook.model_dump(mode="json"),
            }
            insert_run(
                self.store,
                RunRow(
                    id=run_id,
                    repo=repo,
                    issue=issue.number,
                    playbook_id=playbook.id,
                    playbook_sha=playbook_sha,
                    status="running",
                    context=ctxd,
                    ledger_comment_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            )
            self._create_step(run_id, ctxd, entry, 1, None)
            update_run(self.store, run_id, context=ctxd)
            run = get_run(self.store, run_id)
            assert run is not None
            self._playbook_cache[run_id] = playbook
            self._emit(
                run,
                "run_started",
                {"playbook": playbook.id, "entry": entry},
                ("run started", [f"playbook `{playbook.id}`", f"run `{run_id}`"]),
            )
            self._mirror(run_id, None)
        return run_id

    async def tick(self) -> int:
        """Advance every runnable step once and return how many advanced."""
        now = _iso(self.clock())
        rows = self.store.query(
            """
            SELECT s.id AS id, s.run_id AS run_id
            FROM steps s JOIN runs r ON r.id = s.run_id
            WHERE r.status IN ('running', 'needs-you')
              AND (s.status = 'pending'
                   OR (s.status = 'waiting' AND (s.poll_after IS NULL OR s.poll_after <= ?)))
            ORDER BY s.id
            """,
            (now,),
        )
        advanced = 0
        for row in rows:
            if await self._advance(row["id"], row["run_id"], now):
                advanced += 1
        return advanced

    async def run_forever(self, stop: asyncio.Event, interval: float = 1.0) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _advance(self, step_id: str, run_id: str, now: str) -> bool:
        run = get_run(self.store, run_id)
        if run is None or run.status not in RUNNABLE_STATUSES:
            return False
        step = self._step(step_id, run_id)
        if step is None or step.status not in ("pending", "waiting"):
            return False
        if step.status == "waiting" and step.poll_after is not None and step.poll_after > now:
            return False
        try:
            playbook = self._playbook(run)
        except KeyError as exc:
            if run.id not in self._reported_missing:
                self._reported_missing.add(run.id)
                append_event(
                    self.store,
                    kind="playbook_missing",
                    data={"error": str(exc)},
                    repo=run.repo,
                    issue=run.issue,
                    run_id=run.id,
                )
            return False
        node = playbook.nodes[step.node_id]

        if isinstance(node, ParallelNode):
            self._finish(step, "forked", {"branches": list(node.branches)}, fork=node.branches)
            return True

        self._crash("before_advance")
        ctx = ExecContext(
            store=self.store,
            outbox=self.outbox,
            gh=self.gh,
            adapters=self.adapters,
            repo_config=self._repo_config(run.repo),
            playbook=playbook,
            run=run,
            template=self._template_context(run, step),
            clock=self.clock,
            crash=self._crash,
        )
        error: str | None = None
        try:
            result = await self.executors.get(node.kind).advance(step, node, ctx)
            if not isinstance(result, Waiting | Done):
                raise TypeError(f"executor for {node.kind!r} returned {type(result).__name__}")
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}")
            result = Done("failed", {"error": error, "correction": f"The step failed with an error: {error}"})
        self._crash("after_advance")

        if isinstance(result, Waiting):
            self._wait(step, result)
        else:
            self._finish(step, result.outcome, result.output, error=error)
        return True

    # ------------------------------------------------------------------ applying results

    def _wait(self, step: StepRow, waiting: Waiting) -> None:
        with self.store.tx():
            run = get_run(self.store, step.run_id)
            fresh = self._step(step.id, step.run_id)
            if run is None or fresh is None or run.status in TERMINAL_STATUSES:
                return
            if fresh.status not in ("pending", "waiting"):
                return
            old_status = run.status
            ref = waiting.ref if waiting.ref is not None else fresh.ref
            output = waiting.output if waiting.output is not None else fresh.output
            changed = fresh.status != "waiting" or ref != fresh.ref or output != fresh.output
            update_step(
                self.store,
                fresh.id,
                status="waiting",
                ref=ref,
                output=output,
                poll_after=_iso(self.clock() + waiting.poll_after),
            )
            if not changed:
                return
            self._crash("after_step_update")
            if fresh.status != "waiting":
                self._emit(run, "step_waiting", {"step": fresh.id, "node": fresh.node_id})
            was_needs_you = fresh.status == "waiting" and bool((fresh.output or {}).get("needs_you"))
            if (output or {}).get("needs_you") and not was_needs_you:
                bullets = [str(output["reason"])] if output.get("reason") else []
                bullets.extend(str(link) for link in output.get("links", []))
                self._emit(
                    run,
                    "needs_you",
                    {"step": fresh.id, "node": fresh.node_id, "reason": output.get("reason")},
                    ("needs you", bullets),
                )
            new_status = self._derive_status(run.id, run.status)
            if new_status != run.status:
                update_run(self.store, run.id, status=new_status)
            self._mirror(run.id, old_status)
        self._crash("after_mirror")

    def _finish(
        self,
        step: StepRow,
        outcome: str,
        output: dict[str, Any],
        *,
        error: str | None = None,
        fork: list[str] | None = None,
    ) -> None:
        with self.store.tx():
            run = get_run(self.store, step.run_id)
            fresh = self._step(step.id, step.run_id)
            if run is None or fresh is None or run.status in TERMINAL_STATUSES:
                return
            if fresh.status not in ("pending", "waiting"):
                return
            playbook = self._playbook(run)
            node = playbook.nodes[fresh.node_id]
            old_status = run.status
            self._failure_commented = False
            update_step(
                self.store,
                fresh.id,
                status="done",
                outcome=outcome,
                output=output,
                poll_after=None,
                ended_at=self.store.now(),
            )
            self._crash("after_step_update")

            ctxd = run.context
            ctxd.setdefault("nodes", {})[fresh.node_id] = output
            prev_in = (ctxd.get("prev_for") or {}).pop(fresh.id, None)
            pr = output.get("pr")
            if isinstance(pr, dict) and isinstance(pr.get("number"), int):
                ctxd["pr"] = pr

            if error is not None:
                self._emit(
                    run,
                    "executor_error",
                    {"step": fresh.id, "node": fresh.node_id, "error": error},
                    ("executor error", [f"node `{fresh.node_id}`: {error}"]),
                )
            comment = None
            if node.kind == "agent" and outcome in _AGENT_COMMENTS:
                bullets = [f"node `{fresh.node_id}`"]
                if outcome == "pr_ready" and ctxd.get("pr"):
                    bullets.append(str(ctxd["pr"].get("url") or f"#{ctxd['pr']['number']}"))
                comment = (_AGENT_COMMENTS[outcome], bullets)
            elif node.kind == "review":
                comment = (f"review: {outcome}", [f"node `{fresh.node_id}`"])
            self._emit(
                run, "step_done", {"step": fresh.id, "node": fresh.node_id, "outcome": outcome}, comment
            )

            terminal: str | None = None
            if fork is not None:
                for branch in fork:
                    terminal = self._enter(run, ctxd, playbook, branch, prev_in, 0)
                    if terminal is not None:
                        break
            else:
                terminal = self._route(run, ctxd, playbook, fresh.node_id, outcome, output)
            self._crash("after_route")

            if terminal is not None:
                self._close_open_steps(run.id)
                new_status = terminal
                self._emit(
                    run,
                    f"run_{terminal}",
                    {"status": terminal},
                    (terminal, [f"run `{run.id}`"])
                    if terminal == "done" or not self._failure_commented
                    else None,
                )
            else:
                new_status = self._derive_status(run.id, run.status)
            update_run(self.store, run.id, status=new_status, context=ctxd)
            self._mirror(run.id, old_status)
        self._crash("after_mirror")

    # ------------------------------------------------------------------ run controls

    def _controllable(self, run_id: str) -> RunRow:
        run = get_run(self.store, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        return run

    def pause(self, run_id: str) -> None:
        with self.store.tx():
            run = self._controllable(run_id)
            if run.status == "paused":
                return
            if run.status in TERMINAL_STATUSES:
                raise ValueError(f"run {run_id} is {run.status}; it cannot be paused")
            update_run(self.store, run_id, status="paused")
            self._emit(run, "paused", {})
            self._mirror(run_id, run.status)

    def resume(self, run_id: str, to: str | None = None) -> None:
        with self.store.tx():
            run = self._controllable(run_id)
            if run.status in TERMINAL_STATUSES:
                raise ValueError(f"run {run_id} is {run.status}; it cannot be resumed")
            old_status = run.status
            if to is None:
                if run.status != "paused":
                    return
                update_run(self.store, run_id, status="running")
                status = self._derive_status(run_id, "running")
                if status != "running":
                    update_run(self.store, run_id, status=status)
                self._emit(run, "resumed", {"to": None})
                self._mirror(run_id, old_status)
                return

            playbook = self._playbook(run)
            if to not in playbook.nodes:
                raise ValueError(f"unknown node {to!r}")
            ctxd = run.context
            prev: Any = None
            now = self.store.now()
            for s in steps_for_run(self.store, run_id):
                if s.status == "waiting" and (s.output or {}).get("needs_you"):
                    update_step(
                        self.store, s.id, status="done", outcome="resolved", poll_after=None, ended_at=now
                    )
                    prev = s.output
            visit = (ctxd.get("visits") or {}).get(to, 0) + 1  # human override: max_visits is ignored
            self._create_step(run_id, ctxd, to, visit, prev)
            update_run(self.store, run_id, status="running", context=ctxd)
            self._emit(run, "resumed", {"to": to})
            self._mirror(run_id, old_status)

    async def cancel(self, run_id: str) -> None:
        run = self._controllable(run_id)
        if run.status in TERMINAL_STATUSES:
            return
        for s in steps_for_run(self.store, run_id):
            ref = s.ref or {}
            if s.status != "waiting" or not ref.get("agent") or not ref.get("id"):
                continue
            try:
                session = SessionRef(
                    agent=ref["agent"], id=ref["id"], url=ref.get("url"), data=ref.get("data") or {}
                )
                await self.adapters.get(ref["agent"]).cancel(session)
            except Exception as exc:
                append_event(
                    self.store,
                    kind="cancel_error",
                    data={"step": s.id, "error": redact(f"{type(exc).__name__}: {exc}")},
                    repo=run.repo,
                    issue=run.issue,
                    run_id=run.id,
                )
        with self.store.tx():
            run = self._controllable(run_id)
            if run.status in TERMINAL_STATUSES:
                return
            self._close_open_steps(run_id)
            update_run(self.store, run_id, status="cancelled")
            self._emit(run, "cancelled", {}, ("cancelled", [f"run `{run_id}`"]))
            self._mirror(run_id, run.status)

    def retry_node(self, run_id: str, node_id: str) -> str:
        """Insert a new pending visit of ``node_id`` and return its step id."""
        with self.store.tx():
            run = self._controllable(run_id)
            if run.status in ("done", "cancelled"):
                raise ValueError(f"run {run_id} is {run.status}; it cannot be retried")
            playbook = self._playbook(run)
            node = playbook.nodes.get(node_id)
            if node is None:
                raise ValueError(f"unknown node {node_id!r}")
            ctxd = run.context
            visit = (ctxd.get("visits") or {}).get(node_id, 0) + 1
            if node.max_visits is not None and visit > node.max_visits:
                raise ValueError(f"node {node_id!r} has used all {node.max_visits} visits")
            step_id = self._create_step(run_id, ctxd, node_id, visit, None)
            status = "running" if run.status == "failed" else run.status
            update_run(self.store, run_id, status=status, context=ctxd)
            self._emit(run, "retried", {"node": node_id, "step": step_id})
            self._mirror(run_id, run.status)
        return step_id

    # ------------------------------------------------------------------ mirroring to GitHub (outbox only)

    def _control_status(self, run: RunRow, playbook: Playbook | None, steps: list[StepRow]) -> str:
        if run.status == "running" and playbook is not None:
            for s in steps:
                node = playbook.nodes.get(s.node_id)
                if s.status == "waiting" and node is not None and node.kind == "review":
                    return "review"
        return _CONTROL_STATUS[run.status]

    def _mirror(self, run_id: str, old_status: str | None) -> None:
        """Enqueue the ledger, label and control-table writes for the run's current state."""
        if self.outbox is None:
            return
        run = get_run(self.store, run_id)
        if run is None or not run.repo:
            return
        try:
            playbook: Playbook | None = self._playbook(run)
        except KeyError:
            playbook = None
        steps = steps_for_run(self.store, run_id)
        ctxd = run.context
        pr = ctxd.get("pr")
        pr_text = f"#{pr['number']}" if pr and pr.get("number") is not None else "-"
        current = ", ".join(s.node_id for s in steps if s.status in ("pending", "waiting")) or "-"
        visits = ", ".join(f"{n} {v}" for n, v in (ctxd.get("visits") or {}).items()) or "-"
        summary = (
            "| Run | Playbook | Status | Current | Visits | PR |\n"
            "|---|---|---|---|---|---|\n"
            f"| `{run.id}` | `{run.playbook_id}` | {run.status} | {current} | {visits} | {pr_text} |"
        )

        body: str | None = None
        for cap in (MAX_OUTPUT_BYTES, 1024, 0):
            try:
                body = render_ledger(to_snapshot(self.store, run_id, max_output_bytes=cap), summary)
                break
            except LedgerTooLarge:
                continue
        if body is None:
            append_event(
                self.store,
                kind="ledger_too_large",
                data={"run": run_id},
                repo=run.repo,
                issue=run.issue,
                run_id=run_id,
            )
        else:
            self.outbox.enqueue(
                "upsert_ledger",
                {"repo": run.repo, "issue": run.issue, "body": body},
                coalesce_key=f"ledger:{run.repo}#{run.issue}",
            )

        if run.status != old_status:
            label = _STATUS_LABELS[run.status]
            self.outbox.enqueue(
                "set_labels",
                {
                    "repo": run.repo,
                    "issue": run.issue,
                    "add": [label],
                    "remove": [name for name in HARNESS_LABELS if name != label],
                },
                coalesce_key=f"labels:{run.repo}#{run.issue}",
            )

        control_issue = self._repo_config(run.repo).control_issue
        if control_issue is not None:
            issue_row = get_issue(self.store, run.repo, run.issue)
            row = ControlRow(
                issue=run.issue,
                title=issue_row.title if issue_row else "",
                status=self._control_status(run, playbook, steps),  # type: ignore[arg-type]
                agent=self._agent_for(run, issue_row),
                run_id=run.id,
                pr=pr_text if pr_text != "-" else None,
                note=f"at {current}" if current != "-" else None,
            )
            self.outbox.enqueue(
                "edit_issue_body",
                {"repo": run.repo, "issue": control_issue, "row": row},
                coalesce_key=f"control:{run.repo}#{run.issue}",
            )
