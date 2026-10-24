"""Ops API: the halt, the kill switch and the daily budget.

The server has no Runner, so each action only writes the ``ops`` ledger record through the outbox (the daemon
drains it) and queues control requests for the runs (the daemon's Runner applies them), like the run actions.
The kill switch records the halt first, so no new run starts at once, and then queues one ``cancel`` per run.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from harness.ops.halt import HaltState, NoControlIssue, queue_kill, queue_resume
from harness.server.deps import get_store
from harness.server.schemas import (
    BudgetRequest,
    BudgetStatusOut,
    HaltInfoOut,
    KillOut,
    KillRequest,
    OpsStatusOut,
    ResumeOut,
)
from harness.store.db import Store

router = APIRouter(prefix="/api", tags=["ops"])


def _budget_out(request: Request) -> BudgetStatusOut:
    status = request.app.state.daily_budget.status()
    return BudgetStatusOut(
        limit_usd=status.limit_usd,
        spent_usd=status.spent_usd,
        remaining_usd=status.remaining_usd,
        resets_at=status.resets_at,
        exceeded=status.exceeded,
        unknown_cost_runs=status.unknown_cost_runs,
    )


@router.get(
    "/ops/status", summary="Whether the harness is halted, and today's spend against the daily budget"
)
async def ops_status(request: Request) -> OpsStatusOut:
    halt_state: HaltState = request.app.state.halt_state
    info = halt_state.halted()
    halted = HaltInfoOut(reason=info.reason, since=info.since, by=info.by) if info else None
    return OpsStatusOut(halted=halted, budget=_budget_out(request))


@router.post("/ops/kill", summary="Kill switch: halt the harness and cancel every active run")
async def ops_kill(
    request: Request, store: Annotated[Store, Depends(get_store)], body: KillRequest
) -> KillOut:
    try:
        queued = queue_kill(store, request.app.state.halt_state, by="api")
    except NoControlIssue as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return KillOut(halted=True, cancels_queued=queued)


@router.post("/ops/resume", summary="Clear the halt and resume the runs the daily cap paused")
async def ops_resume(request: Request, store: Annotated[Store, Depends(get_store)]) -> ResumeOut:
    try:
        return ResumeOut(resumed_runs=queue_resume(store, request.app.state.halt_state, by="api"))
    except NoControlIssue as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/ops/budget", summary="Set or clear the daily budget override")
async def ops_budget(request: Request, body: BudgetRequest) -> BudgetStatusOut:
    try:
        request.app.state.halt_state.set_daily_budget(body.daily_usd)
    except NoControlIssue as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _budget_out(request)
