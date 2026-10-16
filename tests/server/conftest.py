from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx
import pytest

from harness.server import create_app
from harness.store.db import Store
from tests.fixtures.server_seed import Seed, seed_config, seed_store
from tests.server.helpers import AUTH, PORT, TOKEN, make_client, make_store


@pytest.fixture
def seeded() -> tuple[Store, Seed]:
    store = make_store()
    return store, seed_store(store)


@pytest.fixture
def store(seeded: tuple[Store, Seed]) -> Store:
    return seeded[0]


@pytest.fixture
def seed(seeded: tuple[Store, Seed]) -> Seed:
    return seeded[1]


@pytest.fixture
def app(store: Store):
    return create_app(store=store, config=seed_config(), token=TOKEN, port=PORT, tail_events=False)


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(app, headers=AUTH) as c:
        yield c


@pytest.fixture
async def anon(app) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(app) as c:
        yield c


def run_path(run_id: str) -> str:
    """Run ids contain '/', '#' and '@'; encode them the way a browser does."""
    return quote(run_id, safe="")
