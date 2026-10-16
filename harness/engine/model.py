"""The run snapshot stored in the GitHub ledger comment, and the functions that build and restore it.

``to_snapshot`` reads a run from the store; ``rehydrate`` inserts a snapshot back into a (possibly empty)
store, which is what ``harness sync`` uses to rebuild runs and steps after the local DB is lost.

Step outputs are capped when serialized: an output whose JSON is larger than ``MAX_OUTPUT_BYTES`` is replaced
by ``{"truncated": true, "keys": [...]}``. The full output stays in the local store.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harness.store.db import Store
from harness.store.repo import (
    RunRow,
    StepRow,
    get_run,
    insert_run,
    insert_step,
    steps_for_run,
)

SNAPSHOT_VERSION = 1
MAX_OUTPUT_BYTES = 8 * 1024


class SnapshotRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    repo: str
    issue: int
    playbook_id: str
    playbook_sha: str | None = None
    status: str
    created_at: str
    updated_at: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    pr: dict[str, Any] | None = None


class SnapshotStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    node: str
    visit: int
    status: str
    outcome: str | None = None
    ref: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    poll_after: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: int = SNAPSHOT_VERSION
    run: SnapshotRun
    steps: list[SnapshotStep] = Field(default_factory=list)
    visits: dict[str, int] = Field(default_factory=dict)
    prev_for: dict[str, Any] = Field(default_factory=dict)
    nodes: dict[str, Any] = Field(default_factory=dict)
    sessions: dict[str, Any] = Field(default_factory=dict)


def cap_output(output: dict[str, Any] | None, max_bytes: int = MAX_OUTPUT_BYTES) -> dict[str, Any] | None:
    """Return ``output`` unchanged if it serializes to at most ``max_bytes``, else a truncation marker."""
    if output is None:
        return None
    size = len(json.dumps(output, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size <= max_bytes:
        return output
    return {"truncated": True, "keys": sorted(str(k) for k in output)}


def to_snapshot(store: Store, run_id: str, *, max_output_bytes: int = MAX_OUTPUT_BYTES) -> dict[str, Any]:
    """Build the ledger snapshot for ``run_id`` as a plain dict. Raises ``KeyError`` for an unknown run."""
    run = get_run(store, run_id)
    if run is None:
        raise KeyError(f"run {run_id} not found")
    ctx = run.context
    steps = [
        SnapshotStep(
            id=s.id,
            node=s.node_id,
            visit=s.visit,
            status=s.status,
            outcome=s.outcome,
            ref=s.ref,
            output=cap_output(s.output, max_output_bytes),
            poll_after=s.poll_after,
            started_at=s.started_at,
            ended_at=s.ended_at,
        )
        for s in steps_for_run(store, run_id)
    ]
    snapshot = RunSnapshot(
        run=SnapshotRun(
            id=run.id,
            repo=run.repo,
            issue=run.issue,
            playbook_id=run.playbook_id,
            playbook_sha=run.playbook_sha,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
            inputs=ctx.get("inputs") or {},
            agent=ctx.get("agent"),
            pr=ctx.get("pr"),
        ),
        steps=steps,
        visits=dict(ctx.get("visits") or {}),
        prev_for={k: cap_output(v, max_output_bytes) for k, v in (ctx.get("prev_for") or {}).items()},
        nodes={k: cap_output(v, max_output_bytes) for k, v in (ctx.get("nodes") or {}).items()},
        sessions=dict(ctx.get("sessions") or {}),
    )
    return snapshot.model_dump(mode="json")


def rehydrate(store: Store, snapshot: dict[str, Any]) -> str:
    """Insert the run and steps of ``snapshot`` into ``store`` and return the run id.

    Idempotent: a run or step that already exists is left alone. The playbook is not part of the snapshot,
    so a rehydrated run is only runnable once its playbook is registered with the ``Runner``.
    """
    snap = RunSnapshot.model_validate(snapshot)
    if snap.v != SNAPSHOT_VERSION:
        raise ValueError(f"unsupported snapshot version {snap.v}")
    run = snap.run
    context: dict[str, Any] = {
        "inputs": run.inputs,
        "agent": run.agent,
        "visits": snap.visits,
        "sessions": snap.sessions,
        "nodes": snap.nodes,
        "prev_for": snap.prev_for,
    }
    if run.pr is not None:
        context["pr"] = run.pr
    with store.tx():
        if get_run(store, run.id) is None:
            insert_run(
                store,
                RunRow(
                    id=run.id,
                    repo=run.repo,
                    issue=run.issue,
                    playbook_id=run.playbook_id,
                    playbook_sha=run.playbook_sha,
                    status=run.status,
                    context=context,
                    ledger_comment_id=None,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                ),
            )
        existing = {s.id for s in steps_for_run(store, run.id)}
        for s in snap.steps:
            if s.id in existing:
                continue
            insert_step(
                store,
                StepRow(
                    id=s.id,
                    run_id=run.id,
                    node_id=s.node,
                    visit=s.visit,
                    status=s.status,
                    outcome=s.outcome,
                    ref=s.ref,
                    output=s.output,
                    poll_after=s.poll_after,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                ),
            )
    return run.id
