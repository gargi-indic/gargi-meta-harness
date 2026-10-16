"""Shared builders for the server tests."""

import httpx

from harness.config import GlobalConfig, RepoEntry
from harness.server import create_app
from harness.server.events import EventBus
from harness.store.db import Store

TOKEN = "test-launch-token"
PORT = 7777
BASE_URL = f"http://127.0.0.1:{PORT}"
AUTH = {"Cookie": f"harness_token={TOKEN}"}


def make_store() -> Store:
    store = Store(":memory:")
    store.migrate()
    return store


def make_config(repos: int = 2) -> GlobalConfig:
    return GlobalConfig(repos=[RepoEntry(name=f"acme/repo{i}") for i in range(repos)], env_files=[])


def make_app(*, bus: EventBus | None = None, store: Store | None = None, **kwargs):
    kwargs.setdefault("tail_events", False)
    return create_app(
        store=store or make_store(), config=make_config(), token=TOKEN, port=PORT, bus=bus, **kwargs
    )


def make_client(app, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL, **kwargs)
