"""Agents API: health, concurrency in use, active sessions and cost per run (the Agents screen)."""

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from harness.adapters.health import HealthCache, check_health
from harness.config import AgentConfig, GlobalConfig
from harness.engine.dispatcher import count_active_per_agent
from harness.engine.model import TERMINAL_STATUSES
from harness.ops.costs import UsageRepo, parse_since
from harness.server.deps import get_config, get_store
from harness.server.derive import repo_config_of, run_cost_out
from harness.server.planner_service import KNOWN_AGENT_CAPS
from harness.server.schemas import (
    AgentCapsOut,
    AgentHealthOut,
    AgentOut,
    AgentRunCostRow,
    AgentSessionOut,
)
from harness.store.db import Store
from harness.store.repo import active_steps, get_repo, get_run, list_runs

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _agent_configs(store: Store, config: GlobalConfig) -> dict[str, list[AgentConfig]]:
    """Every repo's ``agents.<name>`` entries, by agent name."""
    found: dict[str, list[AgentConfig]] = {}
    for entry in config.repos:
        stored = get_repo(store, entry.name)
        for name, agent in repo_config_of(stored["config"] if stored else None).agents.items():
            found.setdefault(name, []).append(agent)
    return found


def _pick(entries: list[AgentConfig]) -> AgentConfig | None:
    """The entry that stands for the agent: the enabled one with the most concurrency, else any."""
    on = [e for e in entries if e.enabled]
    if on:
        return max(on, key=lambda e: e.max_concurrency)
    return entries[0] if entries else None


@router.get("", summary="Agents: health, concurrency in use, active sessions and today's cost")
async def list_agents(
    request: Request,
    store: Annotated[Store, Depends(get_store)],
    config: Annotated[GlobalConfig, Depends(get_config)],
) -> list[AgentOut]:
    env: Mapping[str, str] = getattr(request.app.state, "agent_env", None) or os.environ
    cache: HealthCache | None = getattr(request.app.state, "health_cache", None)
    if cache is None:
        cache = request.app.state.health_cache = HealthCache()
    checker = getattr(request.app.state, "health_checker", None) or check_health
    entries = _agent_configs(store, config)

    runs = {run.id: run for run in list_runs(store) if run.status not in TERMINAL_STATUSES}
    names = list(KNOWN_AGENT_CAPS)
    in_use = count_active_per_agent(list(runs.values()), names)
    sessions: dict[str, list[AgentSessionOut]] = {name: [] for name in names}
    for step in active_steps(store):
        run = runs.get(step.run_id)
        agent = (step.ref or {}).get("agent")
        if run is None or agent not in sessions:
            continue
        sessions[agent].append(
            AgentSessionOut(
                run_id=run.id,
                issue=run.issue,
                step=step.id,
                started_at=step.started_at,
                state=step.status,
                cost_usd=run_cost_out(store, run.id).usd,
            )
        )

    today = UsageRepo(store).by_adapter(request.app.state.daily_budget.day_start())

    out: list[AgentOut] = []
    for name in names:
        chosen = _pick(entries.get(name, []))
        health = await checker(name, chosen, env, cache=cache)
        caps = KNOWN_AGENT_CAPS[name]
        out.append(
            AgentOut(
                name=name,
                enabled=health.enabled,
                health=AgentHealthOut(
                    status=health.status, detail=health.detail, checked_at=health.checked_at
                ),
                caps=AgentCapsOut(
                    plan_approval=caps.plan_approval,
                    messaging=caps.messaging,
                    opens_pr=caps.opens_pr,
                    local_workspace=caps.local_workspace,
                ),
                max_concurrency=chosen.max_concurrency if chosen else caps.max_concurrency,
                in_use=in_use[name],
                active_sessions=sessions[name],
                today_cost_usd=today.get(name),
            )
        )
    return out


@router.get("/costs", summary="What each run spent, with its agent and whether the cost was estimated")
async def agent_costs(
    request: Request, store: Annotated[Store, Depends(get_store)], since: str | None = None
) -> list[AgentRunCostRow]:
    start: str | None = None
    if since:
        now: datetime = request.app.state.clock()
        try:
            start = parse_since(since, now)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    repo = UsageRepo(store)
    rows: list[AgentRunCostRow] = []
    for row in repo.by_run(start):
        run = get_run(store, row["run_id"])
        agent = run.context.get("agent") if run else None
        cost = repo.run_cost(row["run_id"])
        rows.append(
            AgentRunCostRow(
                run_id=row["run_id"],
                issue=row["issue"],
                agent=agent if isinstance(agent, str) else None,
                usd=row["usd"],
                estimated=cost.estimated,
            )
        )
    return rows
