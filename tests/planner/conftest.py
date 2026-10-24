import pytest

from harness.github.client import GitHub
from tests.planner.world import World


@pytest.fixture
def world(respx_mock) -> World:
    return World(respx_mock).install()


@pytest.fixture
async def gh(world: World):
    async def no_sleep(_: float) -> None:
        return None

    client = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    yield client
    await client.aclose()
