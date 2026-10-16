from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from harness.engine.model import RUN_STATUSES
from harness.server.deps import get_store
from harness.server.derive import (
    activity_item,
    ordered_steps,
    playbook_dict,
    run_summary,
    step_out,
    visit_counts,
)
from harness.server.schemas import ActivityPage, RunDetail, RunSummary
from harness.store.db import Store
from harness.store.queries import activity_for_run
from harness.store.repo import get_issue, get_run, list_runs, steps_for_run

router = APIRouter(prefix="/api", tags=["runs"])


def _title(store: Store, repo: str, number: int) -> str | None:
    issue = get_issue(store, repo, number)
    return issue.title if issue else None


@router.get("/runs", summary="List runs, newest first")
async def list_runs_route(
    store: Annotated[Store, Depends(get_store)],
    status: Annotated[str | None, Query(pattern=f"^({'|'.join(RUN_STATUSES)})$")] = None,
    issue: int | None = None,
    repo: str | None = None,
) -> list[RunSummary]:
    runs = [
        run
        for run in list_runs(store, status)
        if (issue is None or run.issue == issue) and (repo is None or run.repo == repo)
    ]
    runs.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return [
        run_summary(run, steps_for_run(store, run.id), _title(store, run.repo, run.issue)) for run in runs
    ]


# Run ids contain slashes (owner/repo#n@k), so both routes take a path; the longer one is declared first.
@router.get("/runs/{run_id:path}/activity", summary="Agent activity of a run, paged by sequence number")
async def run_activity(
    run_id: str,
    store: Annotated[Store, Depends(get_store)],
    after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> ActivityPage:
    if get_run(store, run_id) is None:
        raise HTTPException(status_code=404, detail="unknown run")
    items = [activity_item(row) for row in activity_for_run(store, run_id, after=after, limit=limit)]
    return ActivityPage(items=items, next=items[-1].seq if items else after)


@router.get("/runs/{run_id:path}", summary="One run with its playbook and steps")
async def get_run_route(run_id: str, store: Annotated[Store, Depends(get_store)]) -> RunDetail:
    run = get_run(store, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    steps = ordered_steps(steps_for_run(store, run.id))
    return RunDetail(
        run=run_summary(run, steps, _title(store, run.repo, run.issue)),
        playbook=playbook_dict(run),
        steps=[step_out(s) for s in steps],
        visit_counts=visit_counts(steps),
    )
