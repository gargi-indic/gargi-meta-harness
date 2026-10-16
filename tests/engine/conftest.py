from datetime import UTC, datetime

import pytest

from harness.adapters.base import AdapterRegistry
from harness.github.client import GitHub
from harness.store.db import Store
from tests.engine.helpers import Env, FakeClock, RecordingOutbox


@pytest.fixture
async def env():
    clock = FakeClock()
    store = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    store.migrate()

    async def no_sleep(_: float) -> None:
        return None

    gh = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    outbox = RecordingOutbox(store, gh, clock=clock, sleep=no_sleep)
    yield Env(store=store, clock=clock, gh=gh, outbox=outbox, adapters=AdapterRegistry())
    await gh.aclose()
    store.close()
