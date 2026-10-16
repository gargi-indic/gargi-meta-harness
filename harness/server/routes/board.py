from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from harness.config import GlobalConfig
from harness.server.deps import get_config, get_store
from harness.server.derive import STATUSES, board_cards, load_campaign
from harness.server.schemas import Board, BoardColumns, Card
from harness.store.db import Store

router = APIRouter(prefix="/api", tags=["board"])


@router.get("/board", summary="Kanban columns for one campaign, or for all registered repos")
async def get_board(
    store: Annotated[Store, Depends(get_store)],
    config: Annotated[GlobalConfig, Depends(get_config)],
    campaign: Annotated[
        str | None, Query(description="Campaign id (owner/repo); omit for all repos.")
    ] = None,
) -> Board:
    registered = [entry.name for entry in config.repos]
    if campaign is not None and campaign not in registered:
        raise HTTPException(status_code=404, detail="unknown campaign")
    columns: dict[str, list[Card]] = {status.replace("-", "_"): [] for status in STATUSES}
    for repo in [campaign] if campaign is not None else registered:
        for column, cards in board_cards(load_campaign(store, repo)).items():
            columns[column].extend(cards)
    if campaign is None:
        for cards in columns.values():
            cards.sort(key=lambda c: (c.repo, c.issue))
            cards.sort(key=lambda c: c.updated_at, reverse=True)
    return Board(columns=BoardColumns(**columns))
