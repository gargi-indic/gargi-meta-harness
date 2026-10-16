"""Shared builders for the playbook API tests (M2.8)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

import harness
from harness.github.client import GitHub
from harness.server import create_app
from harness.store.db import Store
from tests.server.helpers import AUTH, PORT, TOKEN, make_client, make_config

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "playbooks"
REPO = "acme/repo0"
API = "https://api.github.com"
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
BRANCH = "harness/playbook-repo-flow-20261016120000"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def builtin_text() -> str:
    return (Path(harness.__file__).parent / "playbooks" / "plan-build-review.yaml").read_text(
        encoding="utf-8"
    )


async def no_sleep(_: float) -> None:
    return None


def make_gh(**kwargs: Any) -> GitHub:
    return GitHub(token="t", sleep=no_sleep, write_spacing=0.0, **kwargs)


def make_playbook_app(
    *, gh: GitHub | None = None, store: Store | None = None, builtin_dir: Path | None = None
):
    if store is None:
        store = Store(":memory:")
        store.migrate()
    return create_app(
        store=store,
        config=make_config(),
        token=TOKEN,
        port=PORT,
        tail_events=False,
        gh=gh,
        builtin_dir=builtin_dir,
        clock=lambda: NOW,
    )


def playbook_client(app) -> httpx.AsyncClient:
    return make_client(app, headers=AUTH)
