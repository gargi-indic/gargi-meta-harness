"""Read-only API routers, registered in ``create_app`` before the static catch-all."""

from fastapi import APIRouter

from harness.server.routes import actions, board, campaigns, inbox, playbooks, runs


def api_routers() -> list[APIRouter]:
    return [campaigns.router, board.router, inbox.router, runs.router, actions.router, playbooks.router]
