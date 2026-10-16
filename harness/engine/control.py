"""Control requests: how the web server (no Runner, no outbox) asks the daemon's Runner to act on a run.

The server inserts a row with :func:`enqueue`; the Runner calls :func:`apply_pending` as the first thing in every
``tick()``. A row is claimed with a single ``UPDATE ... WHERE handled_at IS NULL``, so each request is applied
at most once, even across processes and repeated calls. A request is never retried: if the process dies after
the claim and before the result is recorded, the row stays handled with no result and reads as
``{"ok": false, "error": "interrupted"}``. A failing request never breaks the tick: its error is recorded in
``result_json`` (``{"ok": false, "error": ...}``) and in the ``control_applied`` event.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harness.adapters.base import SessionRef
from harness.config import RepoConfig
from harness.engine.model import TERMINAL_STATUSES
from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow, append_event, get_issue, get_run, steps_for_run, update_run

if TYPE_CHECKING:
    from harness.engine.runner import Runner

CONTROL_KINDS = ("pause", "resume", "cancel", "retry_node", "send_message", "reroute")
MAX_ERROR_CHARS = 500
_LIVE = ("running", "waiting")


@dataclass(frozen=True)
class ControlRequest:
    id: int
    run_id: str
    step_id: str | None
    kind: str
    payload: dict[str, Any]
    created_at: str
    handled_at: str | None
    result: dict[str, Any] | None

    @property
    def status(self) -> str:
        if self.handled_at is None:
            return "pending"
        return "done" if self.result is not None and self.result.get("ok") is True else "error"


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _request_from_row(row: Mapping[str, Any]) -> ControlRequest:
    payload = _loads(row["payload_json"])
    result = _loads(row["result_json"])
    result = result if isinstance(result, dict) else None
    if result is None and row["handled_at"] is not None:
        result = {"ok": False, "error": "interrupted"}
    return ControlRequest(
        id=row["id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        kind=row["kind"],
        payload=payload if isinstance(payload, dict) else {},
        created_at=row["created_at"],
        handled_at=row["handled_at"],
        result=result,
    )


def enqueue(store: Store, run_id: str, kind: str, payload: dict[str, Any], step_id: str | None = None) -> int:
    """Queue a control request. ValueError: unknown kind. KeyError: unknown run."""
    if kind not in CONTROL_KINDS:
        raise ValueError(f"unknown control kind {kind!r}; known: {', '.join(CONTROL_KINDS)}")
    with store.tx():
        run = get_run(store, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        cursor = store.execute(
            "INSERT INTO control_requests (run_id, step_id, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, step_id, kind, json.dumps(payload), store.now()),
        )
        request_id = int(cursor.lastrowid or 0)
        append_event(
            store,
            kind="control_requested",
            data={"request_id": request_id, "kind": kind},
            repo=run.repo,
            issue=run.issue,
            run_id=run_id,
        )
    return request_id


def get_request(store: Store, request_id: int) -> ControlRequest | None:
    rows = store.query("SELECT * FROM control_requests WHERE id = ?", (request_id,))
    return _request_from_row(rows[0]) if rows else None


def _error_text(exc: Exception) -> str:
    text = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    text = " ".join(redact(text).split())
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 3] + "..."
    return text or type(exc).__name__


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing {key!r}")
    return value


def _session_of(step: StepRow) -> SessionRef | None:
    """The agent session a step holds, built like ``Runner.cancel`` builds it; None when it holds none."""
    ref = step.ref
    if not isinstance(ref, dict) or "agent" not in ref or "id" not in ref:
        return None
    return SessionRef(**{k: ref[k] for k in ("agent", "id", "url", "data") if k in ref})


def _message_target(steps: list[StepRow], step_id: str | None) -> StepRow | None:
    sessions = [s for s in steps if _session_of(s) is not None]
    if step_id is not None:
        return next((s for s in sessions if s.id == step_id), None)
    waiting = [s for s in sessions if s.status == "waiting"]
    pool = waiting or sessions
    return max(pool, key=lambda s: (s.started_at or "", s.id)) if pool else None


async def _send_message(runner: "Runner", req: ControlRequest) -> dict[str, Any]:
    text = _text(req.payload, "text")
    target = _message_target(steps_for_run(runner.store, req.run_id), req.step_id)
    session = _session_of(target) if target is not None else None
    if target is None or session is None:
        raise ValueError("no agent session to message")
    await runner.adapters.get(session.agent).send(session, text)
    return {"ok": True, "step": target.id}


def _current_agent(runner: "Runner", run: RunRow) -> str:
    """The agent the next agent step would use: context, then the issue's ``agent:`` label, then the default."""
    agent = run.context.get("agent")
    if isinstance(agent, str) and agent:
        return agent
    issue = get_issue(runner.store, run.repo, run.issue)
    for label in issue.labels if issue else []:
        if label.startswith("agent:") and len(label) > len("agent:"):
            return label[len("agent:") :]
    return (runner.repo_configs.get(run.repo) or RepoConfig()).default_agent


def _reroute(runner: "Runner", req: ControlRequest) -> dict[str, Any]:
    agent = _text(req.payload, "agent")
    if agent not in runner.adapters.names():
        raise ValueError(f"unknown agent {agent!r}")
    store = runner.store
    with store.tx():
        run = get_run(store, req.run_id)
        if run is None:
            raise KeyError(f"run {req.run_id} not found")
        if run.status in TERMINAL_STATUSES:
            raise ValueError(f"run {run.id} is {run.status}")
        started = any(
            s.status in _LIVE and isinstance(s.ref, dict) and "id" in s.ref
            for s in steps_for_run(store, run.id)
        )
        if started:
            raise ValueError("agent session already started")
        previous = _current_agent(runner, run)
        update_run(store, run.id, context={**run.context, "agent": agent})
        append_event(
            store,
            kind="rerouted",
            data={"from": previous, "to": agent},
            repo=run.repo,
            issue=run.issue,
            run_id=run.id,
        )
    return {"ok": True, "from": previous, "to": agent}


def _visit(run: RunRow | None, node: str) -> int | None:
    visits = run.context.get("visits") if run is not None else None
    value = visits.get(node) if isinstance(visits, dict) else None
    return value if isinstance(value, int) else None


async def _apply(runner: "Runner", req: ControlRequest) -> dict[str, Any]:
    run_id = req.run_id
    if req.kind in ("resume", "reroute") and req.step_id is not None:
        # A second request for an inbox item must not repeat the first one's effect.
        step = next((s for s in steps_for_run(runner.store, run_id) if s.id == req.step_id), None)
        if step is None or step.status != "waiting":
            raise ValueError("already resolved")
    if req.kind == "pause":
        runner.pause(run_id)
    elif req.kind == "resume":
        to = req.payload.get("to")
        runner.resume(run_id, to=to if isinstance(to, str) and to else None)
    elif req.kind == "cancel":
        await runner.cancel(run_id)
    elif req.kind == "retry_node":
        node = _text(req.payload, "node")
        runner.retry_node(run_id, node)
        return {"ok": True, "node": node, "visit": _visit(get_run(runner.store, run_id), node)}
    elif req.kind == "send_message":
        return await _send_message(runner, req)
    elif req.kind == "reroute":
        return _reroute(runner, req)
    else:
        raise ValueError(f"unknown control kind {req.kind!r}")
    return {"ok": True}


async def apply_pending(runner: "Runner", store: Store) -> int:
    """Apply every unhandled request in id order, each at most once; returns how many were processed."""
    processed = 0
    for row in store.query("SELECT id FROM control_requests WHERE handled_at IS NULL ORDER BY id"):
        cursor = store.execute(
            "UPDATE control_requests SET handled_at = ? WHERE id = ? AND handled_at IS NULL",
            (store.now(), row["id"]),
        )
        if cursor.rowcount != 1:
            continue  # another process claimed it
        req = _request_from_row(store.query("SELECT * FROM control_requests WHERE id = ?", (row["id"],))[0])
        try:
            result = await _apply(runner, req)
        except Exception as exc:
            result = {"ok": False, "error": _error_text(exc)}
        try:
            run = get_run(store, req.run_id)
            with store.tx():
                store.execute(
                    "UPDATE control_requests SET result_json = ? WHERE id = ?", (json.dumps(result), req.id)
                )
                append_event(
                    store,
                    kind="control_applied",
                    data={"request_id": req.id, "kind": req.kind, "ok": result["ok"]},
                    repo=run.repo if run else None,
                    issue=run.issue if run else None,
                    run_id=req.run_id,
                )
        except Exception as exc:
            try:
                append_event(
                    store,
                    kind="tick_error",
                    data={"error": redact(f"control request {req.id}: {type(exc).__name__}: {exc}")},
                )
            except Exception:
                pass
            continue
        processed += 1
    return processed
