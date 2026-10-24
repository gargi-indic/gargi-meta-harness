"""FastAPI dependencies reading ``app.state``; later tasks override them through ``app.dependency_overrides``."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request

from harness.config import GlobalConfig
from harness.github.client import GitHub
from harness.server.events import EventBus
from harness.server.planner_service import PlannerService
from harness.store.db import Store


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_config(request: Request) -> GlobalConfig:
    return request.app.state.config


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus


def get_github(request: Request) -> GitHub | None:
    return getattr(request.app.state, "gh", None)


def get_gh_token(request: Request) -> str | None:
    """The token the server's GitHub client was built with; only used to redact error text."""
    return getattr(request.app.state, "gh_token", None)


def get_builtin_dir(request: Request) -> Path:
    return request.app.state.builtin_dir


def get_clock(request: Request) -> Callable[[], datetime]:
    return request.app.state.clock


def get_planner(request: Request) -> PlannerService:
    planner = getattr(request.app.state, "planner", None)
    if planner is None:
        raise HTTPException(status_code=503, detail="GitHub not configured")
    return planner
