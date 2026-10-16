"""FastAPI dependencies reading ``app.state``; later tasks override them through ``app.dependency_overrides``."""

from fastapi import Request

from harness.config import GlobalConfig
from harness.server.events import EventBus
from harness.store.db import Store


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_config(request: Request) -> GlobalConfig:
    return request.app.state.config


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus
