"""The run snapshot stored in the ledger comment, and the inverse that rebuilds a run from it.

``to_snapshot`` serialises one run (run row, steps, gates and the routing state kept in ``run.context``).
Each step output is capped at 8 KB when serialised; larger outputs become ``{"truncated": true, "keys": [...]}``
(the full output stays in the local store). ``rehydrate`` inserts the run and steps into a store, leaving rows
that already exist alone, so ``harness sync`` can rebuild a lost database from the ledger alone.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.store.db import Store
from harness.store.repo import (
    GateRow,
    RunRow,
    StepRow,
    get_run,
    insert_gate,
    insert_run,
    insert_step,
    steps_for_run,
)

DEFAULT_OUTPUT_CAP = 8 * 1024
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
RUN_STATUSES = ("running", "needs-you", "paused", "done", "failed", "cancelled")
_ACTIVE_STEP_STATUSES = ("pending", "running", "waiting")
_MAX_TRUNCATED_KEYS = 50


class SnapshotRun(BaseModel):
    id: str
    repo: str
    issue: int
    playbook_id: str
    playbook_sha: str | None = None
    status: str
    created_at: str
    updated_at: str


class SnapshotStep(BaseModel):
    id: str
    node: str
    visit: int
    status: str
    outcome: str | None = None
    ref: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None


class SnapshotGate(BaseModel):
    id: str
    step_id: str
    name: str
    status: str
    note: str | None = None
    decided_by: str | None = None
    created_at: str
    decided_at: str | None = None


class RunSnapshot(BaseModel):
    v: Literal[1] = 1
    run: SnapshotRun
    steps: list[SnapshotStep] = Field(default_factory=list)
    visits: dict[str, int] = Field(default_factory=dict)
    prev_for: dict[str, dict[str, Any] | None] = Field(default_factory=dict)
    nodes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cursor: list[str] = Field(default_factory=list)
    gates: list[SnapshotGate] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    pr: dict[str, Any] | None = None
    # What the run has spent (see harness.ops.costs); absent on ledgers written before cost tracking.
    usage: dict[str, Any] | None = None


def _is_truncation_marker(output: dict[str, Any]) -> bool:
    return output.get("truncated") is True and set(output) == {"truncated", "keys"}


def cap_output(output: dict[str, Any] | None, limit: int) -> dict[str, Any] | None:
    """Return ``output`` unchanged if it serialises to at most ``limit`` bytes, else a truncation marker."""
    if output is None or _is_truncation_marker(output):
        return output
    size = len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size <= limit:
        return output
    return {"truncated": True, "keys": sorted(str(k) for k in output)[:_MAX_TRUNCATED_KEYS]}


def to_snapshot(store: Store, run_id: str, *, max_output_bytes: int = DEFAULT_OUTPUT_CAP) -> dict[str, Any]:
    run = get_run(store, run_id)
    if run is None:
        raise KeyError(f"run {run_id} not found")
    ctx = run.context
    steps = steps_for_run(store, run_id)
    gates = store.query("SELECT * FROM gates WHERE run_id = ? ORDER BY created_at, id", (run_id,))

    def cap(output: Any) -> dict[str, Any] | None:
        return cap_output(output if isinstance(output, dict) else None, max_output_bytes)

    snap = RunSnapshot(
        run=SnapshotRun(
            id=run.id,
            repo=run.repo,
            issue=run.issue,
            playbook_id=run.playbook_id,
            playbook_sha=run.playbook_sha,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
        ),
        steps=[
            SnapshotStep(
                id=s.id,
                node=s.node_id,
                visit=s.visit,
                status=s.status,
                outcome=s.outcome,
                ref=s.ref,
                output=cap(s.output),
                started_at=s.started_at,
                ended_at=s.ended_at,
            )
            for s in steps
        ],
        visits=dict(ctx.get("visits", {})),
        prev_for={k: cap(v) for k, v in ctx.get("prev_for", {}).items()},
        nodes={k: cap(v) or {} for k, v in ctx.get("nodes", {}).items()},
        sessions=dict(ctx.get("sessions", {})),
        cursor=[s.id for s in steps if s.status in _ACTIVE_STEP_STATUSES],
        gates=[SnapshotGate(**{k: g[k] for k in SnapshotGate.model_fields if k in g}) for g in gates],
        inputs=dict(ctx.get("inputs", {})),
        agent=ctx.get("agent"),
        pr=ctx.get("pr"),
        usage=ctx.get("usage") if isinstance(ctx.get("usage"), dict) else None,
    )
    dumped = snap.model_dump(mode="json")
    if dumped["usage"] is None:
        del dumped["usage"]  # keep ledgers of runs without cost data byte-identical to before
    return dumped


def rehydrate(store: Store, snapshot: dict[str, Any]) -> str:
    """Insert the run, its steps and gates into ``store``; rows that already exist are left alone.

    Returns the run id. ``poll_after`` is not part of the snapshot, so rehydrated waiting steps are due at
    once. The playbook itself is not in the snapshot: give the Runner a ``playbooks`` mapping to resolve it.
    """
    snap = RunSnapshot.model_validate(snapshot)
    r = snap.run
    context: dict[str, Any] = {
        "inputs": snap.inputs,
        "agent": snap.agent,
        "visits": snap.visits,
        "sessions": snap.sessions,
        "nodes": snap.nodes,
        "prev_for": snap.prev_for,
    }
    if snap.pr is not None:
        context["pr"] = snap.pr
    if snap.usage is not None:
        context["usage"] = snap.usage
    with store.tx():
        if get_run(store, r.id) is None:
            insert_run(
                store,
                RunRow(
                    id=r.id,
                    repo=r.repo,
                    issue=r.issue,
                    playbook_id=r.playbook_id,
                    playbook_sha=r.playbook_sha,
                    status=r.status,
                    context=context,
                    ledger_comment_id=None,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                ),
            )
        have = {s.id for s in steps_for_run(store, r.id)}
        for s in snap.steps:
            if s.id in have:
                continue
            insert_step(
                store,
                StepRow(
                    id=s.id,
                    run_id=r.id,
                    node_id=s.node,
                    visit=s.visit,
                    status=s.status,
                    outcome=s.outcome,
                    ref=s.ref,
                    output=s.output,
                    poll_after=None,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                ),
            )
        have_gates = {row["id"] for row in store.query("SELECT id FROM gates WHERE run_id = ?", (r.id,))}
        for g in snap.gates:
            if g.id not in have_gates:
                insert_gate(store, GateRow(run_id=r.id, **g.model_dump()))
    return r.id
