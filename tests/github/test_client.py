import pytest
import respx
from httpx import Response

from harness.github.client import GitHub, GitHubError, MemoryEtagCache


class FakeSleep:
    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, duration: float) -> None:
        self.durations.append(duration)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def github(fake_sleep: FakeSleep) -> GitHub:
    return GitHub(
        "secret-token",
        cache=MemoryEtagCache(),
        write_spacing=0.5,
        max_retries=3,
        sleep=fake_sleep,
    )


@pytest.mark.asyncio
@respx.mock
async def test_etag_hit(github: GitHub) -> None:
    # First response 200, second 304
    mock_route = respx.get("https://api.github.com/repos/gargi-indic/gargi-meta-harness")
    mock_route.side_effect = [Response(200, json={"id": 123}, headers={"etag": '"etag-1"'}), Response(304)]

    # First request
    repo1 = await github.repo("gargi-indic/gargi-meta-harness")
    assert repo1 == {"id": 123}

    # Second request
    repo2 = await github.repo("gargi-indic/gargi-meta-harness")
    assert repo2 == {"id": 123}

    assert "if-none-match" in mock_route.calls[1].request.headers
    assert mock_route.calls[1].request.headers["if-none-match"] == '"etag-1"'


@pytest.mark.asyncio
@respx.mock
async def test_pagination(github: GitHub) -> None:
    # Page 1
    respx.get(
        "https://api.github.com/repos/test/repo/issues", params={"state": "open", "per_page": "100"}
    ).mock(
        return_value=Response(
            200,
            json=[{"id": 1}],
            headers={"link": '<https://api.github.com/repos/test/repo/issues?page=2>; rel="next"'},
        )
    )

    # Page 2
    respx.get("https://api.github.com/repos/test/repo/issues", params={"page": "2"}).mock(
        return_value=Response(
            200,
            json={"items": [{"id": 2}]},  # Handle wrapped array for pagination search
            headers={"link": '<https://api.github.com/repos/test/repo/issues?page=3>; rel="next"'},
        )
    )

    # Page 3
    respx.get("https://api.github.com/repos/test/repo/issues", params={"page": "3"}).mock(
        return_value=Response(200, json=[{"id": 3}])
    )

    issues = await github.issues("test/repo")
    assert [i["id"] for i in issues] == [1, 2, 3]


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_retry_after(github: GitHub, fake_sleep: FakeSleep) -> None:
    route = respx.get("https://api.github.com/repos/test/repo")
    route.side_effect = [
        Response(429, headers={"retry-after": "1.5"}),
        Response(200, json={"id": 1}),
    ]

    repo = await github.repo("test/repo")
    assert repo == {"id": 1}
    assert fake_sleep.durations == [1.5]


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_secondary_rate_limit(github: GitHub, fake_sleep: FakeSleep) -> None:
    route = respx.get("https://api.github.com/repos/test/repo")
    route.side_effect = [
        Response(403, json={"message": "You have exceeded a secondary rate limit."}),
        Response(200, json={"id": 1}),
    ]

    repo = await github.repo("test/repo")
    assert repo == {"id": 1}
    assert fake_sleep.durations == [60.0]  # 60 * (attempt 0 + 1)


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_5xx(github: GitHub, fake_sleep: FakeSleep) -> None:
    route = respx.get("https://api.github.com/repos/test/repo")
    route.side_effect = [
        Response(502),
        Response(503),
        Response(200, json={"id": 1}),
    ]

    repo = await github.repo("test/repo")
    assert repo == {"id": 1}
    assert fake_sleep.durations == [1.0, 2.0]  # 2^0, 2^1


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_error(github: GitHub) -> None:
    respx.get("https://api.github.com/repos/test/repo").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(GitHubError) as exc:
        await github.repo("test/repo")

    assert exc.value.status == 404
    assert "Not Found" in exc.value.message


@pytest.mark.asyncio
@respx.mock
async def test_write_spacing(github: GitHub, fake_sleep: FakeSleep) -> None:
    # First write shouldn't wait if no previous write happened (or very fast)
    respx.post("https://api.github.com/repos/test/repo/issues").mock(
        return_value=Response(201, json={"id": 1})
    )
    respx.post("https://api.github.com/repos/test/repo/issues/1/comments").mock(
        return_value=Response(201, json={"id": 2})
    )

    await github.create_issue("test/repo", "Title")
    # Simulate zero time elapsed for the next call to trigger sleep

    # We patch time.time or just rely on fake_sleep getting called
    # With fast tests, elapsed will be < 0.5s, so sleep will be called.
    await github.add_comment("test/repo", 1, "body")

    assert len(fake_sleep.durations) == 1
    assert fake_sleep.durations[0] > 0.0


@pytest.mark.asyncio
@respx.mock
async def test_issues_dropping_prs(github: GitHub) -> None:
    respx.get(
        "https://api.github.com/repos/test/repo/issues", params={"state": "open", "per_page": "100"}
    ).mock(
        return_value=Response(
            200,
            json=[
                {"id": 1},
                {"id": 2, "pull_request": {"url": "..."}},
                {"id": 3},
            ],
        )
    )

    issues = await github.issues("test/repo")
    assert [i["id"] for i in issues] == [1, 3]


@pytest.mark.asyncio
@respx.mock
async def test_blocked_by_request_shape(github: GitHub) -> None:
    route = respx.post("https://api.github.com/repos/test/repo/issues/1/dependencies/blocked_by").mock(
        return_value=Response(201, json={"id": 2})
    )

    res = await github.add_blocked_by("test/repo", 1, 42)
    assert res == {"id": 2}

    req = route.calls[0].request
    import json

    assert json.loads(req.content) == {"issue_id": 42}


@pytest.mark.asyncio
@respx.mock
async def test_sub_issues_request_shape(github: GitHub) -> None:
    route = respx.post("https://api.github.com/repos/test/repo/issues/1/sub_issues").mock(
        return_value=Response(201, json={"id": 3})
    )

    res = await github.add_sub_issue("test/repo", 1, 43)
    assert res == {"id": 3}

    req = route.calls[0].request
    import json

    assert json.loads(req.content) == {"sub_issue_id": 43}


@pytest.mark.asyncio
@respx.mock
async def test_token_redacted_from_error() -> None:
    github = GitHub("secret-token-1234")
    respx.get("https://api.github.com/repos/test/repo").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(GitHubError) as exc:
        await github.repo("test/repo")

    # Token should not appear in the message
    assert "secret-token-1234" not in str(exc.value)
    assert "secret-token-1234" not in repr(github)
