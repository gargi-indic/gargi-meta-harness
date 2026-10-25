"""Read-only API routers, registered in ``create_app`` before the static catch-all."""

from fastapi import APIRouter

from harness.server.routes import (
    actions,
    agents,
    board,
    campaigns,
    config,
    costs,
    inbox,
    ops,
    planner,
    playbooks,
    runs,
)


def api_routers() -> list[APIRouter]:
    return [
        campaigns.router,
        agents.router,
        board.router,
        inbox.router,
        costs.router,
        ops.router,
        runs.router,
        actions.router,
        playbooks.router,
        planner.router,
        config.router,
    ]
