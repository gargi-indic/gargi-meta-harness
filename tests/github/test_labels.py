import pytest
import respx

from harness.github.client import GitHub
from harness.github.labels import HARNESS_LABELS, ensure_harness_labels

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_ensure_harness_labels():
    gh = GitHub("token")
    repo = "user/repo"

    route = respx.post(f"https://api.github.com/repos/{repo}/labels").respond(201, json={"name": "test"})

    await ensure_harness_labels(gh, repo)
    assert route.call_count == len(HARNESS_LABELS)


@respx.mock
async def test_ensure_harness_labels_ignores_422():
    gh = GitHub("token")
    repo = "user/repo"

    route = respx.post(f"https://api.github.com/repos/{repo}/labels").respond(
        422, json={"message": "Already exists"}
    )

    await ensure_harness_labels(gh, repo)
    assert route.call_count == len(HARNESS_LABELS)
