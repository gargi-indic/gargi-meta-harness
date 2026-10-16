"""The durable reconcile loop: starts runs, advances steps through executors, routes outcomes, mirrors to GitHub.

Every step transition happens inside a single ``store.tx()`` (step update, routing, events, outbox enqueues), so a
crash anywhere leaves either the old state or the new state. Recovery is a new ``Runner`` on the same store
calling ``tick()`` until the run is terminal; executors are idempotent and resume from ``step.ref``.

IDs are deterministic: run ``{repo}#{issue}@{n}``, step ``{run}/{node}/{visit}``, gate ``{step}/gate``.
"""

import asyncio
import copy
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from harness.adapters.base import AdapterRegistry, SessionRef
from harness.config import RepoConfig
from harness.engine.control import apply_pending
from harness.engine.executors.base import Done, ExecContext, ExecutorRegistry, Waiting
from harness.engine.model import TERMINAL_STATUSES, to_snapshot
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
    upsert_issue,
)

CRASH_POINTS = ("before_advance", "after_advance", "after_step_update", "after_route", "after_mirror")
ACTIVE_RUN_STATUSES = ("running", "needs-you")
MAX_ROUTE_HOPS = 10
# Output caps tried in turn when the rendered ledger comment would be too large.
_LEDGER_CAPS = (8 * 1024, 2 * 1024, 512, 0)

STATUS_LABEL = {
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


class SimulatedCrash(Exception):
    """Raised by the ``crash_after`` test hook at a named point."""


class RunExists(Exception):
    """A non-terminal run already exists for the issue."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"a non-terminal run already exists for this issue: {run_id}")
        self.run_id = run_id


# --------------------------------------------------------------------------------------------------------
# join


def join_inputs(playbook: Playbook, node_id: str, node: JoinNode) -> list[str]:
    """The nodes a join waits for: its explicit ``inputs``, else every node with an edge into it."""
    if node.inputs:
        return list(node.inputs)
    found: list[str] = []
    for key, target in playbook.edges.items():
        if target != node_id:
            continue
        source = key.rsplit(".", 1)[0]
        if source != "*" and source in playbook.nodes and source not in found:
            found.append(source)
    return found


def _latest_step(steps: list[StepRow], node_id: str) -> StepRow | None:
    visits = [s for s in steps if s.node_id == node_id]
    return max(visits, key=lambda s: s.visit) if visits else None


def _step_failed(step: StepRow) -> bool:
    return step.status == "failed" or step.outcome == "failed"


class JoinExecutor:
    """Built-in executor for ``join`` nodes: ``all`` and ``any`` modes (``select`` is M3)."""

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Waiting | Done:
        if not isinstance(node, JoinNode):
            raise TypeError(f"JoinExecutor cannot run a {node.kind!r} node")
        if node.mode == "select":
            raise NotImplementedError("join mode 'select' is implemented in M3")
        names = join_inputs(ctx.playbook, step.node_id, node)
        steps = steps_for_run(ctx.store, ctx.run.id)
        latest = {n: _latest_step(steps, n) for n in names}
        complete = {n: s for n, s in latest.items() if s is not None and s.status in ("done", "failed")}
        failed = [n for n, s in complete.items() if _step_failed(s)]
        good = {n: (s.output or {}) for n, s in complete.items() if n not in failed}
        if node.mode == "all":
            if failed:
                return Done(
                    "failed",
                    {"inputs": {n: (s.output or {}) for n, s in complete.items()}, "failed": failed},
                )
            if len(complete) == len(names):
                return Done("done", {"inputs": good})
            return Waiting(poll_after=0.0)
        # mode == "any"
        if good:
            return Done("done", {"inputs": good})
        if names and len(complete) == len(names):
            return Done(
                "failed", {"inputs": {n: (s.output or {}) for n, s in complete.items()}, "failed": failed}
            )
        return Waiting(poll_after=0.0)


# --------------------------------------------------------------------------------------------------------
# runner


@dataclass
class _Work:
    """Mutable state of one run during one transaction."""

    run: RunRow
    ctx: dict[str, Any]
    playbook: Playbook | None
    status: str
    reason: str | None = None
    comments: list[tuple[str, list[str]]] = field(default_factory=list)


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
        clock: Callable[[], float] = time.time,
        crash_after: str | None = None,
        playbooks: Mapping[str, Playbook] | None = None,
    ) -> None:
        if crash_after is not None and crash_after not in CRASH_POINTS:
            raise ValueError(f"unknown crash point {crash_after!r}; known: {', '.join(CRASH_POINTS)}")
        self.store = store
        self.outbox = outbox
        self.executors = executors
        self.adapters = adapters
        self.gh = gh
        self.repo_configs = repo_configs or {}
        self.clock = clock
        self.crash_after = crash_after
        # Fallback playbook lookup (by playbook id) for runs whose context carries no playbook copy,
        # for example runs rebuilt from a ledger snapshot.
        self.playbooks = playbooks or {}
        self._playbook_cache: dict[str, Playbook] = {}
        self._warned_missing: set[str] = set()
        if "join" not in self.executors.kinds():
            self.executors.register("join", JoinExecutor())

    # ---- small helpers ------------------------------------------------------------------------------

    def _crash(self, point: str) -> None:
        if self.crash_after == point:
            self.crash_after = None  # one-shot
            raise SimulatedCrash(point)

    def _iso(self, offset: float = 0.0) -> str:
        """Clock time (plus ``offset`` seconds) formatted like ``Store.now()``."""
        moment = datetime.fromtimestamp(self.clock() + offset, UTC).replace(microsecond=0)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _repo_config(self, repo: str) -> RepoConfig:
        return self.repo_configs.get(repo) or RepoConfig()

    def _event(self, run: RunRow, kind: str, data: dict[str, Any]) -> None:
        append_event(self.store, kind=kind, data=data, repo=run.repo, issue=run.issue, run_id=run.id)

    def _playbook_for(self, run: RunRow) -> Playbook | None:
        cached = self._playbook_cache.get(run.id)
        if cached is not None:
            return cached
        raw = run.context.get("playbook")
        playbook = Playbook.model_validate(raw) if raw else self.playbooks.get(run.playbook_id)
        if playbook is not None:
            self._playbook_cache[run.id] = playbook
        return playbook

    @staticmethod
    def _resolve_agent(run: RunRow, issue: IssueRow | None, cfg: RepoConfig) -> str:
        if run.context.get("agent"):
            return run.context["agent"]
        if issue is not None:
            for label in issue.labels:
                if label.startswith("agent:") and len(label) > len("agent:"):
                    return label[len("agent:") :]
        return cfg.default_agent

    @staticmethod
    def _pr_ctx(raw: Any) -> PrCtx | None:
        if not isinstance(raw, dict):
            return None
        try:
            return PrCtx(**{k: v for k, v in raw.items() if k in PrCtx.model_fields})
        except ValidationError:
            return None

    def _template_ctx(self, run: RunRow, step: StepRow) -> Context:
        issue = get_issue(self.store, run.repo, run.issue)
        agent = self._resolve_agent(run, issue, self._repo_config(run.repo))
        ctx = run.context
        prev = ctx.get("prev_for", {}).get(step.id)
        return Context(
            issue=IssueCtx(
                number=run.issue,
                title=issue.title if issue else "",
                body=(issue.body or "") if issue else "",
                labels=list(issue.labels) if issue else [],
                agent=agent,
                playbook=run.playbook_id,
            ),
            pr=self._pr_ctx(ctx.get("pr")),
            inputs=dict(ctx.get("inputs", {})),
            nodes={k: v for k, v in ctx.get("nodes", {}).items() if isinstance(v, dict)},
            prev=prev if isinstance(prev, dict) else None,
            run=RunCtx(id=run.id, visits=dict(ctx.get("visits", {}))),
        )

    def _exec_ctx(self, run: RunRow, step: StepRow, playbook: Playbook) -> ExecContext:
        return ExecContext(
            store=self.store,
            outbox=self.outbox,
            gh=self.gh,
            adapters=self.adapters,
            repo_config=self._repo_config(run.repo),
            playbook=playbook,
            run=run,
            template=self._template_ctx(run, step),
            clock=self.clock,
            crash=self._crash,
        )

    def _new_step(self, work: _Work, node_id: str, visit: int, prev: dict[str, Any] | None) -> str:
        step_id = f"{work.run.id}/{node_id}/{visit}"
        insert_step(
            self.store,
            StepRow(
                id=step_id,
                run_id=work.run.id,
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
        work.ctx.setdefault("prev_for", {})[step_id] = prev
        return step_id

    def _record_session(self, work: _Work, node: Node | None, ref: dict[str, Any] | None) -> None:
        slot = getattr(node, "session", None)
        if slot and isinstance(ref, dict) and "agent" in ref and "id" in ref:
            work.ctx.setdefault("sessions", {})[slot] = {
                k: ref[k] for k in ("agent", "id", "url", "data") if k in ref
            }

    # ---- routing ------------------------------------------------------------------------------------

    def _no_route(self, work: _Work, node: str, outcome: str) -> None:
        self._event(work.run, "no_route", {"node": node, "outcome": outcome})
        work.status = "failed"
        work.reason = f"no edge for {node}.{outcome}"
        work.comments.append(("no route", [f"node `{node}` has no edge for outcome `{outcome}`"]))

    def _join_absorbs(self, work: _Work, join_id: str, source_step_id: str | None) -> bool:
        """True if an arrival at a join must not create a new join step.

        An arrival is absorbed while a join step is still active, or when the arriving step was already
        part of the last firing of the join (a late branch of a ``mode: any`` join).
        """
        steps = [s for s in steps_for_run(self.store, work.run.id) if s.node_id == join_id]
        if any(s.status in ("pending", "running", "waiting") for s in steps):
            return True
        consumed = work.ctx.get("joins", {}).get(join_id)
        return bool(steps) and consumed is not None and source_step_id in consumed

    def _enter(
        self,
        work: _Work,
        target: str,
        prev: dict[str, Any] | None,
        *,
        via: str,
        source_step_id: str | None = None,
        override_max_visits: bool = False,
    ) -> None:
        """Enter ``target``: terminals end the run, ``max_visits`` redirects to the ``exhausted`` port."""
        assert work.playbook is not None
        edges = work.playbook.edges
        visits = work.ctx.setdefault("visits", {})
        for _ in range(MAX_ROUTE_HOPS):
            if target == "@done":
                work.status = "done"
                return
            if target == "@failed":
                work.status = "failed"
                work.reason = work.reason or f"routed to @failed from {via}"
                return
            node = work.playbook.nodes.get(target)
            if node is None:
                self._event(work.run, "unknown_node", {"node": target, "via": via})
                work.status = "failed"
                work.reason = f"edge target {target!r} is not a node of the playbook"
                work.comments.append(("no route", [work.reason]))
                return
            if node.kind == "join" and self._join_absorbs(work, target, source_step_id):
                return
            next_visit = visits.get(target, 0) + 1
            if node.max_visits is not None and next_visit > node.max_visits and not override_max_visits:
                self._event(work.run, "exhausted", {"node": target, "max_visits": node.max_visits})
                exhausted = edges.get(f"{target}.exhausted") or edges.get("*.exhausted")
                if exhausted is None:
                    self._no_route(work, target, "exhausted")
                    return
                via, target = target, exhausted
                continue
            visits[target] = next_visit
            self._new_step(work, target, next_visit, prev)
            return
        self._event(work.run, "routing_loop", {"node": via})
        work.status = "failed"
        work.reason = f"routing loop through {via} (more than {MAX_ROUTE_HOPS} hops)"
        work.comments.append(("failed", [work.reason]))

    def _route(
        self, work: _Work, node_id: str, outcome: str, output: dict[str, Any], source_step_id: str
    ) -> None:
        assert work.playbook is not None
        edges = work.playbook.edges
        target = edges.get(f"{node_id}.{outcome}") or edges.get(f"*.{outcome}")
        if target is None:
            self._no_route(work, node_id, outcome)
            return
        if target == "@failed":
            error = output.get("error") if isinstance(output, dict) else None
            work.reason = f"{node_id}.{outcome}" + (f": {error}" if error else "")
        self._enter(work, target, output, via=node_id, source_step_id=source_step_id)

    @staticmethod
    def _wants_attention(output: dict[str, Any] | None) -> bool:
        return bool(output and output.get("needs_you"))

    def _recompute_status(self, work: _Work) -> None:
        if work.status not in ACTIVE_RUN_STATUSES:
            return
        steps = steps_for_run(self.store, work.run.id)
        needs = any(s.status == "waiting" and self._wants_attention(s.output) for s in steps)
        work.status = "needs-you" if needs else "running"

    # ---- finalising a transition ---------------------------------------------------------------------

    def _finalize(self, work: _Work, prev_status: str | None) -> None:
        run = work.run
        if work.status != prev_status and work.status in TERMINAL_STATUSES:
            self._event(run, f"run_{work.status}", {"reason": work.reason})
            pr = work.ctx.get("pr")
            bullets = [f"run `{run.id}`"]
            if work.status == "done" and isinstance(pr, dict) and "number" in pr:
                bullets.append(f"PR #{pr['number']}")
            if work.status != "done" and work.reason:
                bullets.insert(0, work.reason)
            work.comments.append((work.status, bullets))
        update_run(self.store, run.id, context=work.ctx, status=work.status)
        self._mirror(work, prev_status)

    def _summary_md(self, run: RunRow, steps: list[StepRow], status: str) -> str:
        active = [f"{s.node_id} (v{s.visit})" for s in steps if s.status in ("pending", "running", "waiting")]
        visits = run.context.get("visits", {})
        pr = run.context.get("pr")
        rows = [
            ("Run", f"`{run.id}`"),
            ("Playbook", run.playbook_id),
            ("Status", status),
            ("Current", ", ".join(active) or "-"),
            ("Visits", ", ".join(f"{n} {v}" for n, v in visits.items()) or "-"),
            ("PR", f"#{pr['number']}" if isinstance(pr, dict) and "number" in pr else "-"),
        ]
        lines = ["| Field | Value |", "| --- | --- |"]
        lines.extend(f"| {k} | {str(v).replace('|', chr(92) + '|')} |" for k, v in rows)
        return "\n".join(lines)

    def _control_status(self, run: RunRow, steps: list[StepRow], playbook: Playbook | None) -> str:
        if run.status == "running" and playbook is not None:
            for s in steps:
                node = playbook.nodes.get(s.node_id)
                if node is not None and node.kind == "review" and s.status in ("pending", "waiting"):
                    return "review"
        return _CONTROL_STATUS[run.status]

    def _mirror(self, work: _Work, prev_status: str | None) -> None:
        """Enqueue the GitHub writes for this transition. Outbox only, never direct; atomic with the tx."""
        outbox = self.outbox
        if outbox is None:
            return
        run = get_run(self.store, work.run.id)
        if run is None or not run.repo:
            return
        repo, issue = run.repo, run.issue
        date = self.store.now()[:10]
        for event, bullets in work.comments:
            outbox.enqueue(
                "add_comment",
                {
                    "repo": repo,
                    "issue": issue,
                    "body": render_event(date, issue, event, [redact(b) for b in bullets]),
                },
            )
        steps = steps_for_run(self.store, run.id)
        summary = self._summary_md(run, steps, run.status)
        body: str | None = None
        for cap in _LEDGER_CAPS:
            try:
                body = render_ledger(to_snapshot(self.store, run.id, max_output_bytes=cap), summary)
                break
            except LedgerTooLarge:
                continue
        if body is None:
            self._event(run, "ledger_too_large", {})
        else:
            outbox.enqueue(
                "upsert_ledger",
                {"repo": repo, "issue": issue, "body": body},
                coalesce_key=f"ledger:{repo}#{issue}",
            )
        if run.status != prev_status:
            label = STATUS_LABEL[run.status]
            outbox.enqueue(
                "set_labels",
                {
                    "repo": repo,
                    "issue": issue,
                    "add": [label],
                    "remove": [name for name in HARNESS_LABELS if name != label],
                },
                coalesce_key=f"labels:{repo}#{issue}",
            )
        cfg = self._repo_config(repo)
        if cfg.control_issue is not None:
            cached = get_issue(self.store, repo, issue)
            pr = run.context.get("pr")
            active = [s.node_id for s in steps if s.status in ("pending", "running", "waiting")]
            row = ControlRow(
                issue=issue,
                title=cached.title if cached else "",
                status=self._control_status(run, steps, work.playbook),  # type: ignore[arg-type]
                agent=self._resolve_agent(run, cached, cfg),
                run_id=run.id,
                pr=f"#{pr['number']}" if isinstance(pr, dict) and "number" in pr else None,
                note=("at " + ", ".join(active)) if active else None,
            )
            outbox.enqueue(
                "edit_issue_body",
                {"repo": repo, "issue": cfg.control_issue, "row": row},
                coalesce_key=f"control:{repo}#{issue}",
            )

    # ---- starting a run ------------------------------------------------------------------------------

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
        entry = next(iter(playbook.nodes))
        with self.store.tx():
            existing = self.store.query(
                "SELECT id, status FROM runs WHERE repo = ? AND issue = ?", (repo, issue.number)
            )
            for row in existing:
                if row["status"] not in TERMINAL_STATUSES:
                    raise RunExists(row["id"])
            run_id = f"{repo}#{issue.number}@{len(existing) + 1}"
            now = self.store.now()
            context: dict[str, Any] = {
                "inputs": dict(inputs or {}),
                "agent": agent,
                "visits": {entry: 1},
                "sessions": {},
                "nodes": {},
                "prev_for": {},
                "playbook": playbook.model_dump(mode="json"),
            }
            upsert_issue(self.store, issue)
            run = RunRow(
                id=run_id,
                repo=repo,
                issue=issue.number,
                playbook_id=playbook.id,
                playbook_sha=playbook_sha,
                status="running",
                context=context,
                ledger_comment_id=None,
                created_at=now,
                updated_at=now,
            )
            insert_run(self.store, run)
            self._playbook_cache[run_id] = playbook
            work = _Work(run=run, ctx=context, playbook=playbook, status="running")
            self._new_step(work, entry, 1, None)
            self._event(run, "run_started", {"playbook": playbook.id, "sha": playbook_sha, "agent": agent})
            cfg = self._repo_config(repo)
            work.comments.append(
                (
                    "run started",
                    [
                        f"run `{run_id}`",
                        f"playbook `{playbook.id}`",
                        f"agent `{self._resolve_agent(run, issue, cfg)}`",
                    ],
                )
            )
            self._finalize(work, None)
        return run_id

    # ---- the reconcile loop --------------------------------------------------------------------------

    def _due_steps(self, now_iso: str) -> list[StepRow]:
        due: list[StepRow] = []
        for status in ACTIVE_RUN_STATUSES:
            for run in list_runs(self.store, status):
                for step in steps_for_run(self.store, run.id):
                    if step.status == "pending" or (
                        step.status == "waiting" and (step.poll_after is None or step.poll_after <= now_iso)
                    ):
                        due.append(step)
        return sorted(due, key=lambda s: s.id)

    async def tick(self) -> int:
        """Advance every runnable step once; returns how many steps were advanced."""
        await apply_pending(self, self.store)
        advanced = 0
        for due in self._due_steps(self._iso()):
            if await self._advance_one(due.run_id, due.id):
                advanced += 1
        return advanced

    async def _advance_one(self, run_id: str, step_id: str) -> bool:
        run = get_run(self.store, run_id)
        step = next((s for s in steps_for_run(self.store, run_id) if s.id == step_id), None)
        if run is None or step is None:
            return False
        if run.status not in ACTIVE_RUN_STATUSES or step.status not in ("pending", "waiting"):
            return False
        playbook = self._playbook_for(run)
        if playbook is None:
            if run.id not in self._warned_missing:
                self._warned_missing.add(run.id)
                self._event(run, "playbook_missing", {"playbook_id": run.playbook_id})
            return False
        node = playbook.nodes.get(step.node_id)

        self._crash("before_advance")
        result: Waiting | Done | None = None
        error: str | None = None
        if isinstance(node, ParallelNode):
            pass  # handled by the runner itself, in _commit
        else:
            try:
                if node is None:
                    raise LookupError(f"node {step.node_id!r} is not in playbook {playbook.id!r}")
                executor = self.executors.get(node.kind)
                result = await executor.advance(step, node, self._exec_ctx(run, step, playbook))
                if not isinstance(result, (Waiting, Done)):
                    raise TypeError(f"executor returned {type(result).__name__}, expected Waiting or Done")
            except SimulatedCrash:
                raise
            except Exception as exc:
                error = redact(f"{type(exc).__name__}: {exc}")
                result = Done("failed", {"error": error, "correction": f"Executor error: {error}"})
        self._crash("after_advance")
        self._commit(run_id, step_id, playbook, node, result, error)
        self._crash("after_mirror")
        return True

    def _commit(
        self,
        run_id: str,
        step_id: str,
        playbook: Playbook,
        node: Node | None,
        result: Waiting | Done | None,
        error: str | None,
    ) -> None:
        with self.store.tx():
            run = get_run(self.store, run_id)
            step = next((s for s in steps_for_run(self.store, run_id) if s.id == step_id), None)
            if run is None or step is None:
                return
            if run.status not in ACTIVE_RUN_STATUSES or step.status not in ("pending", "waiting"):
                return
            work = _Work(run=run, ctx=copy.deepcopy(run.context), playbook=playbook, status=run.status)
            prev_status = run.status
            mirror = True
            if result is None:  # parallel node: fork into one pending step per branch
                assert isinstance(node, ParallelNode)
                self._commit_parallel(work, step, node)
            elif isinstance(result, Waiting):
                mirror = self._commit_waiting(work, step, node, result)
            else:
                self._commit_done(work, step, node, result, error)
            if mirror:
                self._finalize(work, prev_status)

    def _commit_waiting(self, work: _Work, step: StepRow, node: Node | None, result: Waiting) -> bool:
        """Persist a Waiting result. Returns False for a plain poll (nothing observable changed): no mirroring."""
        run = work.run
        was_waiting = step.status == "waiting"
        was_needs = was_waiting and self._wants_attention(step.output)
        now_needs = self._wants_attention(result.output)
        ref = result.ref if result.ref is not None else step.ref
        changed = (not was_waiting) or ref != step.ref or result.output != step.output
        update_step(
            self.store,
            step.id,
            status="waiting",
            ref=ref,
            output=result.output,
            poll_after=self._iso(result.poll_after),
        )
        self._record_session(work, node, ref)
        if not was_waiting:
            self._event(run, "step_waiting", {"step": step.id, "node": step.node_id, "visit": step.visit})
        if now_needs and not was_needs:
            output = result.output or {}
            bullets = [str(output["reason"])] if output.get("reason") else []
            bullets.extend(str(link) for link in output.get("links", []))
            work.comments.append(("needs you", bullets))
            self._event(
                run, "needs_you", {"step": step.id, "node": step.node_id, "reason": output.get("reason")}
            )
        self._crash("after_step_update")
        self._recompute_status(work)
        self._crash("after_route")
        return changed or work.status != run.status

    def _commit_done(
        self, work: _Work, step: StepRow, node: Node | None, result: Done, error: str | None
    ) -> None:
        run = work.run
        output = result.output if isinstance(result.output, dict) else {}
        update_step(
            self.store,
            step.id,
            status="done",
            outcome=result.outcome,
            output=output,
            poll_after=None,
            ended_at=self.store.now(),
        )
        work.ctx.setdefault("nodes", {})[step.node_id] = output
        self._record_session(work, node, step.ref)
        pr = output.get("pr")
        if (
            isinstance(pr, dict)
            and isinstance(pr.get("number"), int)
            and not isinstance(pr.get("number"), bool)
        ):
            work.ctx["pr"] = pr
        self._event(
            run,
            "step_done",
            {"step": step.id, "node": step.node_id, "visit": step.visit, "outcome": result.outcome},
        )
        if error is not None:
            self._event(run, "executor_error", {"step": step.id, "node": step.node_id, "error": error})
            work.comments.append(("executor error", [f"node `{step.node_id}`: {error}"]))
        self._comment_for_done(work, step, node, result.outcome, output)
        if isinstance(node, JoinNode) and work.playbook is not None:
            latest = [
                _latest_step(steps_for_run(self.store, run.id), n)
                for n in join_inputs(work.playbook, step.node_id, node)
            ]
            work.ctx.setdefault("joins", {})[step.node_id] = [s.id for s in latest if s is not None]
        self._crash("after_step_update")
        self._route(work, step.node_id, result.outcome, output, step.id)
        self._crash("after_route")
        self._recompute_status(work)

    def _comment_for_done(
        self, work: _Work, step: StepRow, node: Node | None, outcome: str, output: dict[str, Any]
    ) -> None:
        kind = getattr(node, "kind", None)
        if kind == "agent" and outcome in ("plan_ready", "pr_ready"):
            bullets = [f"node `{step.node_id}` (visit {step.visit})"]
            pr = output.get("pr")
            if outcome == "pr_ready" and isinstance(pr, dict) and pr.get("url"):
                bullets.append(f"PR {pr['url']}")
            work.comments.append(("plan ready" if outcome == "plan_ready" else "PR ready", bullets))
        elif kind == "review":
            bullets = [f"node `{step.node_id}` (visit {step.visit})"]
            for key in ("summary", "correction"):
                if output.get(key):
                    bullets.append(str(output[key]))
            work.comments.append((f"review {outcome}", bullets))

    def _commit_parallel(self, work: _Work, step: StepRow, node: ParallelNode) -> None:
        run = work.run
        prev = work.ctx.get("prev_for", {}).get(step.id)
        output = {"branches": list(node.branches)}
        update_step(
            self.store,
            step.id,
            status="done",
            outcome="forked",
            output=output,
            poll_after=None,
            ended_at=self.store.now(),
        )
        work.ctx.setdefault("nodes", {})[step.node_id] = output
        self._event(
            run,
            "step_done",
            {"step": step.id, "node": step.node_id, "visit": step.visit, "outcome": "forked"},
        )
        self._crash("after_step_update")
        for branch in node.branches:
            if work.status not in ACTIVE_RUN_STATUSES:
                break
            self._enter(work, branch, prev, via=step.node_id, source_step_id=step.id)
        self._crash("after_route")
        self._recompute_status(work)

    async def run_forever(self, stop: asyncio.Event, idle_sleep: float = 1.0) -> None:
        """Tick until ``stop`` is set. A failing tick is recorded as an event and does not end the loop."""
        while not stop.is_set():
            try:
                await self.tick()
            except SimulatedCrash:
                raise
            except Exception as exc:
                append_event(
                    self.store, kind="tick_error", data={"error": redact(f"{type(exc).__name__}: {exc}")}
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_sleep)
            except TimeoutError:
                pass

    # ---- run controls --------------------------------------------------------------------------------

    def _load_active(self, run_id: str) -> RunRow:
        run = get_run(self.store, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        return run

    def pause(self, run_id: str) -> None:
        with self.store.tx():
            run = self._load_active(run_id)
            if run.status in TERMINAL_STATUSES:
                raise ValueError(f"run {run_id} is {run.status}")
            if run.status == "paused":
                return
            work = _Work(
                run=run, ctx=copy.deepcopy(run.context), playbook=self._playbook_for(run), status="paused"
            )
            self._event(run, "paused", {})
            self._finalize(work, run.status)

    def resume(self, run_id: str, to: str | None = None) -> None:
        with self.store.tx():
            run = self._load_active(run_id)
            if run.status in TERMINAL_STATUSES:
                raise ValueError(f"run {run_id} is {run.status}")
            playbook = self._playbook_for(run)
            work = _Work(run=run, ctx=copy.deepcopy(run.context), playbook=playbook, status="running")
            if to is None:
                if run.status != "paused":
                    return
                self._event(run, "resumed", {"to": None})
                self._recompute_status(work)
                self._finalize(work, run.status)
                return
            if playbook is None or to not in playbook.nodes:
                raise ValueError(f"unknown node {to!r}")
            resolved_output: dict[str, Any] | None = None
            for s in steps_for_run(self.store, run_id):
                if s.status == "waiting" and self._wants_attention(s.output):
                    resolved_output = s.output
                    update_step(
                        self.store, s.id, status="done", outcome="resolved", ended_at=self.store.now()
                    )
            visits = work.ctx.setdefault("visits", {})
            visits[to] = visits.get(to, 0) + 1
            self._new_step(work, to, visits[to], resolved_output)
            self._event(run, "resumed", {"to": to, "visit": visits[to]})
            self._recompute_status(work)
            self._finalize(work, run.status)

    def retry_node(self, run_id: str, node_id: str) -> None:
        with self.store.tx():
            run = self._load_active(run_id)
            if run.status in ("done", "cancelled"):
                raise ValueError(f"run {run_id} is {run.status}")
            playbook = self._playbook_for(run)
            if playbook is None or node_id not in playbook.nodes:
                raise ValueError(f"unknown node {node_id!r}")
            node = playbook.nodes[node_id]
            steps = [s for s in steps_for_run(self.store, run_id) if s.node_id == node_id]
            if any(s.status in ("pending", "running", "waiting") for s in steps):
                raise ValueError(f"node {node_id!r} still has an active step")
            work = _Work(run=run, ctx=copy.deepcopy(run.context), playbook=playbook, status=run.status)
            visits = work.ctx.setdefault("visits", {})
            if node.max_visits is not None and visits.get(node_id, 0) + 1 > node.max_visits:
                raise ValueError(f"node {node_id!r} has reached max_visits={node.max_visits}")
            visits[node_id] = visits.get(node_id, 0) + 1
            previous = max(steps, key=lambda s: s.visit) if steps else None
            prev = work.ctx.get("prev_for", {}).get(previous.id) if previous else None
            self._new_step(work, node_id, visits[node_id], prev)
            if run.status == "failed":
                work.status = "running"
            self._event(run, "retry", {"node": node_id, "visit": visits[node_id]})
            self._finalize(work, run.status)

    async def cancel(self, run_id: str) -> None:
        run = self._load_active(run_id)
        if run.status in TERMINAL_STATUSES:
            return
        errors: list[tuple[str, str]] = []
        for step in steps_for_run(self.store, run_id):
            ref = step.ref
            if step.status != "waiting" or not isinstance(ref, dict) or "agent" not in ref or "id" not in ref:
                continue
            try:
                session = SessionRef(**{k: ref[k] for k in ("agent", "id", "url", "data") if k in ref})
                await self.adapters.get(ref["agent"]).cancel(session)
            except Exception as exc:
                errors.append((step.id, redact(f"{type(exc).__name__}: {exc}")))
        with self.store.tx():
            run = self._load_active(run_id)
            if run.status in TERMINAL_STATUSES:
                return
            work = _Work(
                run=run, ctx=copy.deepcopy(run.context), playbook=self._playbook_for(run), status="cancelled"
            )
            for step in steps_for_run(self.store, run_id):
                if step.status in ("pending", "running", "waiting"):
                    update_step(
                        self.store,
                        step.id,
                        status="failed",
                        outcome="cancelled",
                        poll_after=None,
                        ended_at=self.store.now(),
                    )
            for step_id, message in errors:
                self._event(run, "cancel_error", {"step": step_id, "error": message})
            work.reason = "cancelled by user"
            self._finalize(work, run.status)
