"""Action API: inbox decisions and run controls.

The server process has no Runner and no outbox, so a gate decision is store-only (exactly like ``harness
approve``: the gate row and a ``gate_decided`` event) and every other action is queued as a control request that
the daemon's Runner applies on its next tick (``harness.engine.control``). Events reach the SSE bus only through
the event log, never directly.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from harness.engine.control import enqueue, get_request
from harness.engine.model import TERMINAL_STATUSES
from harness.github.commands import decide
from harness.server.deps import get_store
from harness.server.derive import needs_human, playbook_dict, repo_config_of
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow, get_gate, get_repo, get_run, steps_for_run

router = APIRouter(prefix="/api", tags=["actions"])

MAX_NOTE_CHARS = 2000
MAX_TEXT_CHARS = 20000

Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_NOTE_CHARS)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveBody(_Body):
    note: Note | None = Field(default=None, description="Optional note recorded with the decision.")
    to: str | None = Field(
        default=None,
        max_length=200,
        description="Escalation items only: the playbook node to resume at. Default: the node of the run's "
        "latest other finished step.",
    )


class RejectBody(_Body):
    message: Text = Field(description="Why the item is rejected; becomes the correction of the next step.")


class RerouteBody(_Body):
    agent: str = Field(max_length=200, description="Name of an enabled agent in the repo config.")


class ResumeBody(_Body):
    to: str | None = Field(
        default=None, max_length=200, description="Playbook node to resume at; default: continue."
    )


class MessageBody(_Body):
    text: Text = Field(description="Message for the run's active agent session.")


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_id: str = Field(description="Id of the gate that was decided.")
    status: str = Field(description="The gate's new status: approved or rejected.")


class RequestAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: int = Field(description="Id of the queued control request; poll /api/control-requests/{id}.")


class InboxAccepted(RequestAccepted):
    request_ids: list[int] = Field(description="Ids of every control request queued, in order.")


class ControlRequestOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(description="Control request id.")
    run_id: str = Field(description="Run the request targets.")
    step_id: str | None = Field(description="Step the request targets, if any.")
    kind: str = Field(description="pause, resume, cancel, retry_node, send_message or reroute.")
    status: str = Field(description="pending until the daemon applies it, then done or error.")
    result: dict[str, Any] | None = Field(description="Outcome recorded by the daemon; null while pending.")
    created_at: str = Field(description="When the request was queued (UTC, ISO 8601).")
    handled_at: str | None = Field(description="When the daemon claimed the request; null while pending.")


StoreDep = Annotated[Store, Depends(get_store)]


def _run_or_404(store: Store, run_id: str) -> RunRow:
    run = get_run(store, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return run


def _active_run(store: Store, run_id: str, *, allow_failed: bool = False) -> RunRow:
    run = _run_or_404(store, run_id)
    blocked = ("done", "cancelled") if allow_failed else tuple(TERMINAL_STATUSES)
    if run.status in blocked:
        raise HTTPException(status_code=409, detail=f"run is {run.status}")
    return run


def _item(store: Store, item_id: str) -> tuple[RunRow, StepRow]:
    """The run and step of a live inbox item; 404 when unknown or not listed, 409 when already resolved."""
    run: RunRow | None = None
    step: StepRow | None = None
    rows = store.query("SELECT run_id FROM steps WHERE id = ?", (item_id,))
    if rows:
        run = get_run(store, rows[0]["run_id"])
        step = next((s for s in steps_for_run(store, rows[0]["run_id"]) if s.id == item_id), None)
    if run is None or step is None or run.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=404, detail="unknown inbox item")
    if step.status != "waiting":
        raise HTTPException(status_code=409, detail="already resolved")
    if not needs_human(step):
        raise HTTPException(status_code=404, detail="unknown inbox item")
    gate_id = (step.output or {}).get("gate_id")
    if gate_id:
        gate = get_gate(store, str(gate_id))
        if gate is None:
            raise HTTPException(status_code=404, detail="unknown inbox item")
        if gate.status != "pending":
            raise HTTPException(status_code=409, detail="already resolved")
    else:
        queued = store.query(
            "SELECT id FROM control_requests WHERE step_id = ? AND handled_at IS NULL "
            "AND kind IN ('resume', 'reroute') LIMIT 1",
            (item_id,),
        )
        if queued:
            raise HTTPException(status_code=409, detail="already resolved")
    return run, step


def _gate_id(step: StepRow) -> str | None:
    value = (step.output or {}).get("gate_id")
    return str(value) if value else None


def _check_node(run: RunRow, node: str | None) -> None:
    nodes = (playbook_dict(run) or {}).get("nodes")
    if node is not None and not (isinstance(nodes, dict) and node in nodes):
        raise HTTPException(status_code=422, detail="unknown node")


def _decide(
    store: Store, gate_id: str, status: Literal["approved", "rejected"], note: str | None
) -> GateDecision:
    try:
        gate = decide(store, gate_id, status, note, "web")
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown inbox item") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="already resolved") from None
    return GateDecision(gate_id=gate.id, status=gate.status)


def _default_resume_node(store: Store, step: StepRow) -> str | None:
    others = [s for s in steps_for_run(store, step.run_id) if s.status == "done" and s.id != step.id]
    latest = max(others, key=lambda s: (s.ended_at or "", s.id), default=None)
    return latest.node_id if latest else None


@router.post("/inbox/{item_id:path}/approve", summary="Approve an inbox item")
async def approve_item(
    item_id: str, store: StoreDep, response: Response, body: ApproveBody | None = None
) -> GateDecision | InboxAccepted:
    body = body or ApproveBody()
    run, step = _item(store, item_id)
    gate_id = _gate_id(step)
    if gate_id:
        return _decide(store, gate_id, "approved", body.note or None)
    if (step.output or {}).get("plan_sha"):
        # A campaign plan gate: resuming at an earlier node would skip it, and the approval is a GitHub signal.
        raise HTTPException(
            status_code=422,
            detail="the campaign plan is approved on GitHub: add the harness:plan-approved label "
            "to the control issue or run `harness plan approve`",
        )
    to = body.to or _default_resume_node(store, step)
    if not to:
        raise HTTPException(status_code=422, detail="no node to resume at")
    _check_node(run, to)
    with store.tx():
        ids: list[int] = []
        if body.note:
            ids.append(enqueue(store, run.id, "send_message", {"text": body.note}))
        ids.append(enqueue(store, run.id, "resume", {"to": to}, step_id=step.id))
    response.status_code = 202
    return InboxAccepted(request_id=ids[-1], request_ids=ids)


@router.post("/inbox/{item_id:path}/reject", summary="Reject a gate item with a message")
async def reject_item(item_id: str, body: RejectBody, store: StoreDep) -> GateDecision:
    _, step = _item(store, item_id)
    gate_id = _gate_id(step)
    if not gate_id:
        raise HTTPException(status_code=422, detail="reject applies to gate items")
    return _decide(store, gate_id, "rejected", body.message)


@router.post(
    "/inbox/{item_id:path}/reroute", status_code=202, summary="Switch the agent of an inbox item's run"
)
async def reroute_item(item_id: str, body: RerouteBody, store: StoreDep) -> RequestAccepted:
    run, step = _item(store, item_id)
    stored = get_repo(store, run.repo)
    agents = repo_config_of(stored["config"] if stored else None).agents
    if body.agent not in agents or not agents[body.agent].enabled:
        raise HTTPException(status_code=422, detail="unknown or disabled agent")
    return RequestAccepted(
        request_id=enqueue(store, run.id, "reroute", {"agent": body.agent}, step_id=step.id)
    )


@router.post("/runs/{run_id:path}/pause", status_code=202, summary="Pause a run")
async def pause_run(run_id: str, store: StoreDep) -> RequestAccepted:
    _active_run(store, run_id)
    return RequestAccepted(request_id=enqueue(store, run_id, "pause", {}))


@router.post(
    "/runs/{run_id:path}/resume", status_code=202, summary="Resume a paused run, optionally at a node"
)
async def resume_run(run_id: str, store: StoreDep, body: ResumeBody | None = None) -> RequestAccepted:
    run = _active_run(store, run_id)
    to = body.to if body else None
    _check_node(run, to)
    return RequestAccepted(request_id=enqueue(store, run_id, "resume", {"to": to} if to else {}))


@router.post("/runs/{run_id:path}/cancel", status_code=202, summary="Cancel a run")
async def cancel_run(run_id: str, store: StoreDep) -> RequestAccepted:
    _active_run(store, run_id)
    return RequestAccepted(request_id=enqueue(store, run_id, "cancel", {}))


@router.post("/runs/{run_id:path}/nodes/{node}/retry", status_code=202, summary="Start a new visit of a node")
async def retry_node(run_id: str, node: str, store: StoreDep) -> RequestAccepted:
    run = _active_run(store, run_id, allow_failed=True)
    _check_node(run, node)
    return RequestAccepted(request_id=enqueue(store, run_id, "retry_node", {"node": node}))


@router.post("/runs/{run_id:path}/message", status_code=202, summary="Send a message to the run's agent")
async def message_run(run_id: str, body: MessageBody, store: StoreDep) -> RequestAccepted:
    _active_run(store, run_id)
    return RequestAccepted(request_id=enqueue(store, run_id, "send_message", {"text": body.text}))


@router.get("/control-requests/{request_id:int}", summary="Status and result of a control request")
async def get_control_request(request_id: int, store: StoreDep) -> ControlRequestOut:
    req = get_request(store, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown control request")
    return ControlRequestOut(
        id=req.id,
        run_id=req.run_id,
        step_id=req.step_id,
        kind=req.kind,
        status=req.status,
        result=req.result,
        created_at=req.created_at,
        handled_at=req.handled_at,
    )
