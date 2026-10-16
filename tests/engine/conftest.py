import pytest

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.engine.executors.base import ExecutorRegistry
from harness.engine.runner import Runner
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.store.db import Store
from tests.engine.support import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> Store:
    db = Store(":memory:", clock=clock.datetime)
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def gh(respx_mock) -> GitHub:
    # Requesting respx_mock means any accidental network call fails the test. The outbox is never drained.
    async def no_sleep(_: float) -> None:
        return None

    return GitHub("test-token", write_spacing=0.0, sleep=no_sleep)


@pytest.fixture
def outbox(store: Store, gh: GitHub) -> Outbox:
    return Outbox(store, gh)


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def adapters(adapter: FakeAdapter) -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(adapter)
    return reg


@pytest.fixture
def make_runner(store, outbox, adapters, clock):
    def factory(executor=None, **kwargs) -> Runner:
        executors = ExecutorRegistry()
        if executor is not None:
            for kind in (
                "agent",
                "check",
                "review",
                "gate",
                "branch",
                "notify",
                "merge",
                "escalate",
                "decompose",
            ):
                executors.register(kind, executor)
        kwargs.setdefault("clock", clock)
        return Runner(store, outbox, executors, adapters, **kwargs)

    return factory
