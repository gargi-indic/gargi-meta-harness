from typing import Annotated

from fastapi import APIRouter, Depends

from harness.engine.model import TERMINAL_STATUSES
from harness.server.deps import get_store
from harness.server.derive import inbox_item, needs_human
from harness.server.schemas import InboxItem
from harness.store.db import Store
from harness.store.repo import get_issue, list_runs, steps_for_run

router = APIRouter(prefix="/api", tags=["inbox"])


@router.get("/inbox", summary="Steps that wait for a human decision")
async def get_inbox(store: Annotated[Store, Depends(get_store)]) -> list[InboxItem]:
    items: list[InboxItem] = []
    for run in list_runs(store):
        if run.status in TERMINAL_STATUSES:
            continue
        steps = steps_for_run(store, run.id)
        waiting = [s for s in steps if needs_human(s)]
        if not waiting:
            continue
        issue = get_issue(store, run.repo, run.issue)
        items.extend(inbox_item(run, step, steps, issue.title if issue else None) for step in waiting)
    return sorted(items, key=lambda item: (item.created_at, item.id))
