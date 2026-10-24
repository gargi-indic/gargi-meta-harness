from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from harness.ops.costs import UsageRepo, parse_since
from harness.server.deps import get_store
from harness.server.schemas import CostsSummary, RunCostRow
from harness.store.db import Store

router = APIRouter(prefix="/api", tags=["costs"])


@router.get("/costs/summary", summary="What the runs spent, in total, per adapter and per run")
async def costs_summary(
    request: Request, store: Annotated[Store, Depends(get_store)], since: str | None = None
) -> CostsSummary:
    start: str | None = None
    if since:
        now: datetime = request.app.state.clock()
        try:
            start = parse_since(since, now)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    repo = UsageRepo(store)
    return CostsSummary(
        total_usd=repo.total_since(start),
        by_adapter=repo.by_adapter(start),
        by_run=[RunCostRow(**row) for row in repo.by_run(start)],
    )
