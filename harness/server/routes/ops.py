from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from harness.engine.control import enqueue
from harness.engine.model import TERMINAL_STATUSES
from harness.server.deps import get_store
from harness.server.schemas import (
    BudgetRequest,
    BudgetStatusOut,
    HaltInfoOut,
    KillReportOut,
    KillRequest,
    OpsStatusOut,
)
from harness.store.db import Store
from harness.store.repo import list_runs

router = APIRouter(prefix="/api", tags=["ops"])


@router.get("/ops/status", summary="Get halt and budget status")
async def ops_status(request: Request, store: Annotated[Store, Depends(get_store)]) -> OpsStatusOut:
    halt_state = request.app.state.halt_state
    daily_budget = request.app.state.daily_budget

    halted = halt_state.halted()
    halt_info = None
    if halted:
        halt_info = HaltInfoOut(reason=halted.reason, since=halted.since, by=halted.by)

    status = daily_budget.status()
    budget_info = BudgetStatusOut(
        limit_usd=status.limit_usd,
        spent_usd=status.spent_usd,
        remaining_usd=status.remaining_usd,
        resets_at=status.resets_at,
        exceeded=status.exceeded,
        unknown_cost_runs=status.unknown_cost_runs,
    )

    return OpsStatusOut(halted=halt_info, budget=budget_info)


@router.post("/ops/kill", summary="Activate kill switch and cancel all active runs")
async def ops_kill(
    request: Request, store: Annotated[Store, Depends(get_store)], body: KillRequest
) -> KillReportOut:
    if body.confirm != "KILL":
        raise HTTPException(status_code=422, detail="confirm must be exactly 'KILL'")

    halt_state = request.app.state.halt_state

    # write the halt record first
    halt_state.halt("kill_switch", "api")

    # queue per-run cancel control requests
    active_runs = [r for r in list_runs(store) if r.status not in TERMINAL_STATUSES]
    for run in active_runs:
        enqueue(store, run.id, "cancel", {})

    return KillReportOut(errors=[])


@router.post("/ops/resume", summary="Clear halt and resume paused runs")
async def ops_resume(request: Request, store: Annotated[Store, Depends(get_store)]) -> None:
    halt_state = request.app.state.halt_state
    halt_state.resume("api")

    active_runs = [r for r in list_runs(store) if r.status not in TERMINAL_STATUSES]
    for run in active_runs:
        # Enqueue a resume request for all runs, if they were paused it will resume them.
        if run.status == "paused":
            enqueue(store, run.id, "resume", {})

    return None


@router.post("/ops/budget", summary="Set or clear the daily budget override")
async def ops_budget(
    request: Request, store: Annotated[Store, Depends(get_store)], body: BudgetRequest
) -> None:
    halt_state = request.app.state.halt_state
    halt_state.set_daily_budget(body.daily_usd)
    return None
