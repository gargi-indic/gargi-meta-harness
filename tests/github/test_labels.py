import pytest

from harness.github.client import GitHub
from harness.github.labels import HARNESS_LABELS, ensure_harness_labels

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.respx(assert_all_called=True, assert_all_mocked=True),
]


async def test_ensure_harness_labels(respx_mock):
    async def fake_sleep(duration):
        pass

    gh = GitHub("test-token", write_spacing=0.0, sleep=fake_sleep)
    repo = "user/repo"

    route = respx_mock.post(f"https://api.github.com/repos/{repo}/labels").respond(201, json={"name": "test"})

    await ensure_harness_labels(gh, repo)
    assert route.call_count == len(HARNESS_LABELS)


async def test_ensure_harness_labels_ignores_422(respx_mock):
    async def fake_sleep(duration):
        pass

    gh = GitHub("test-token", write_spacing=0.0, sleep=fake_sleep)
    repo = "user/repo"

    route = respx_mock.post(f"https://api.github.com/repos/{repo}/labels").respond(
        422, json={"message": "Already exists"}
    )

    await ensure_harness_labels(gh, repo)
    assert route.call_count == len(HARNESS_LABELS)
