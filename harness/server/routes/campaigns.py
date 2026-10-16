from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from harness.config import GlobalConfig
from harness.server.deps import get_config, get_store
from harness.server.derive import campaign_graph, campaign_summary, load_campaign
from harness.server.schemas import CampaignGraph, CampaignSummary
from harness.store.db import Store

router = APIRouter(prefix="/api", tags=["campaigns"])


@router.get("/campaigns", summary="List campaigns (one per registered repo)")
async def list_campaigns(
    store: Annotated[Store, Depends(get_store)], config: Annotated[GlobalConfig, Depends(get_config)]
) -> list[CampaignSummary]:
    return [campaign_summary(load_campaign(store, entry.name)) for entry in config.repos]


@router.get("/campaigns/{owner}/{repo}/graph", summary="Dependency graph of a campaign")
async def campaign_graph_route(
    owner: str,
    repo: str,
    store: Annotated[Store, Depends(get_store)],
    config: Annotated[GlobalConfig, Depends(get_config)],
) -> CampaignGraph:
    full_name = f"{owner}/{repo}"
    if full_name not in {entry.name for entry in config.repos}:
        raise HTTPException(status_code=404, detail="unknown campaign")
    return campaign_graph(load_campaign(store, full_name))
