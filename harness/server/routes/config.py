from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from harness.config import GlobalConfig
from harness.server.deps import get_config, get_store
from harness.server.derive import repo_config_of
from harness.server.schemas import AgentsEnabled
from harness.store.db import Store
from harness.store.repo import get_repo

router = APIRouter(prefix="/api/agents", tags=["config"])


@router.get("/enabled", summary="List enabled agents for a repository")
async def list_enabled_agents(
    repo: str,
    store: Annotated[Store, Depends(get_store)],
    config: Annotated[GlobalConfig, Depends(get_config)],
) -> AgentsEnabled:
    if repo not in {entry.name for entry in config.repos}:
        raise HTTPException(status_code=404, detail="unknown repo")

    stored = get_repo(store, repo)
    repo_config = repo_config_of(stored["config"] if stored else None)

    agents = []
    for name, agent_config in sorted(repo_config.agents.items()):
        if agent_config.enabled:
            agents.append({"name": name, "enabled": True})

    return AgentsEnabled(agents=agents)
