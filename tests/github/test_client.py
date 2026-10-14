import json
from collections.abc import Iterator

import pytest
import respx
from httpx import Response

from harness.github.client import GitHub, GitHubError, MemoryEtagCache

API = "https://api.github.com"
REPO = "test/repo"
TOKEN = "ghp_secret-token-1234"


class FakeClock:
    """Monotonic clock that only moves when told to (or when the fake sleep is awaited)."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.durations: list[float] = []
        self._clock = clock

    async def __call__(self, duration: float) -> None:
        self.durations.append(duration)
        if self._clock is not None:
            self._clock.now += duration


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def wall() -> FakeClock:
    return FakeClock(1_700_000_000.0)


@pytest.fixture
def fake_sleep(clock: FakeClock) -> FakeSleep:
    return FakeSleep(clock)


@pytest.fixture
def github(fake_sleep: FakeSleep, clock: FakeClock, wall: FakeClock) -> GitHub:
    return GitHub(
        TOKEN,
        write_spacing=0.5,
        max_retries=3,
        sleep=fake_sleep,
        clock=clock,
        wall_clock=wall,
    )


# --- ETag cache -----------------------------------------------------------------------------


async def test_etag_hit_returns_cached_body_as_200(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [
        Response(200, json={"id": 123}, headers={"etag": '"etag-1"'}),
        Response(304, headers={"etag": '"etag-1"', "x-extra": "304-header"}),
    ]

    first = await github.request("GET", f"repos/{REPO}")
    second = await github.request("GET", f"repos/{REPO}")

    assert (first.status, first.body, first.cached) == (200, {"id": 123}, False)
    assert (second.status, second.body, second.cached) == (200, {"id": 123}, True)
    assert second.headers["x-extra"] == "304-header"
    assert "if-none-match" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["if-none-match"] == '"etag-1"'


async def test_repo_helper_uses_etag(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(200, json={"id": 123}, headers={"etag": '"e"'}), Response(304)]

    assert await github.repo(REPO) == {"id": 123}
    assert await github.repo(REPO) == {"id": 123}


async def test_etag_cache_key_includes_query_string(api: respx.MockRouter, github: GitHub) -> None:
    open_route = api.get(f"{API}/repos/{REPO}/issues", params={"state": "open"})
    closed_route = api.get(f"{API}/repos/{REPO}/issues", params={"state": "closed"})
    open_route.side_effect = [
        Response(200, json=[{"id": 1}], headers={"etag": '"open"'}),
        Response(304),
    ]
    closed_route.side_effect = [Response(200, json=[{"id": 2}], headers={"etag": '"closed"'})]

    assert await github.get(f"repos/{REPO}/issues", state="open") == [{"id": 1}]
    # Same path, different query: must not send the other query's ETag.
    assert await github.get(f"repos/{REPO}/issues", state="closed") == [{"id": 2}]
    assert "if-none-match" not in closed_route.calls[0].request.headers
    # Back to the first query: its own ETag is used and the cached body returned.
    assert await github.get(f"repos/{REPO}/issues", state="open") == [{"id": 1}]
    assert open_route.calls[1].request.headers["if-none-match"] == '"open"'


async def test_post_is_never_cached(api: respx.MockRouter, fake_sleep: FakeSleep) -> None:
    cache = MemoryEtagCache()
    gh = GitHub(TOKEN, cache=cache, sleep=fake_sleep, clock=FakeClock(), write_spacing=0.0)
    route = api.post(f"{API}/repos/{REPO}/issues")
    route.side_effect = [
        Response(201, json={"id": 1}, headers={"etag": '"post-etag"'}),
        Response(201, json={"id": 2}, headers={"etag": '"post-etag"'}),
    ]

    await gh.create_issue(REPO, "one")
    await gh.create_issue(REPO, "two")

    assert cache.get(f"{API}/repos/{REPO}/issues") is None
    assert "if-none-match" not in route.calls[1].request.headers


async def test_304_without_cache_entry_retries_once_without_etag(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(304), Response(200, json={"id": 7})]

    assert await github.repo(REPO) == {"id": 7}
    assert route.call_count == 2
    assert all("if-none-match" not in c.request.headers for c in route.calls)
    assert fake_sleep.durations == []


async def test_304_without_cache_entry_twice_raises(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(304), Response(304)]

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)
    assert exc.value.status == 304
    assert route.call_count == 2


async def test_clients_do_not_share_etag_cache(api: respx.MockRouter) -> None:
    first_route = api.get(f"{API}/repos/{REPO}")
    first_route.side_effect = [Response(200, json={"id": 1}, headers={"etag": '"shared?"'})]
    one = GitHub(TOKEN)
    two = GitHub(TOKEN)

    assert await one.repo(REPO) == {"id": 1}
    # The second client has never seen this URL, so it must not send If-None-Match.
    first_route.side_effect = [Response(200, json={"id": 2})]
    assert await two.repo(REPO) == {"id": 2}

    assert "if-none-match" not in first_route.calls[1].request.headers
    await one.aclose()
    await two.aclose()


async def test_injected_cache_is_used(api: respx.MockRouter) -> None:
    cache = MemoryEtagCache()
    cache.put(f"{API}/repos/{REPO}", '"pre"', {"id": 9})
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(304)]

    async with GitHub(TOKEN, cache=cache) as gh:
        assert await gh.repo(REPO) == {"id": 9}
    assert route.calls[0].request.headers["if-none-match"] == '"pre"'


# --- Retries --------------------------------------------------------------------------------


async def test_retry_on_429_retry_after(api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep) -> None:
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(429, headers={"retry-after": "1.5"}),
        Response(200, json={"id": 1}),
    ]

    assert await github.repo(REPO) == {"id": 1}
    assert fake_sleep.durations == [1.5]


async def test_retry_after_is_capped_at_300(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(403, headers={"retry-after": "9999"}),
        Response(200, json={}),
    ]

    await github.repo(REPO)
    assert fake_sleep.durations == [300.0]


async def test_non_numeric_retry_after_falls_back_to_60(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(429, headers={"retry-after": "soon"}),
        Response(200, json={}),
    ]

    await github.repo(REPO)
    assert fake_sleep.durations == [60.0]


@pytest.mark.parametrize(
    ("reset_offset", "expected"),
    [(30, 30.0), (-100, 1.0), (0.2, 1.0), (5000, 900.0)],
    ids=["future", "past-min-1", "sub-second-min-1", "capped-900"],
)
async def test_rate_limit_reset_wait(
    api: respx.MockRouter,
    github: GitHub,
    fake_sleep: FakeSleep,
    wall: FakeClock,
    reset_offset: float,
    expected: float,
) -> None:
    reset = int(wall.now + reset_offset) if reset_offset != 0.2 else wall.now + 0.2
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
            json={"message": "API rate limit exceeded"},
        ),
        Response(200, json={}),
    ]

    await github.repo(REPO)
    assert fake_sleep.durations == [expected]


async def test_retry_after_takes_precedence_over_reset(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep, wall: FakeClock
) -> None:
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(
            429,
            headers={
                "retry-after": "7",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(wall.now + 500)),
            },
        ),
        Response(200, json={}),
    ]

    await github.repo(REPO)
    assert fake_sleep.durations == [7.0]


async def test_retry_on_secondary_rate_limit_scales_with_attempt(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(403, json={"message": "You have exceeded a Secondary Rate Limit."}),
        Response(429, json={"message": "secondary rate limit"}),
        Response(200, json={"id": 1}),
    ]

    assert await github.repo(REPO) == {"id": 1}
    assert fake_sleep.durations == [60.0, 120.0]


async def test_plain_403_is_not_retried(api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(403, json={"message": "Resource not accessible"})]

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)
    assert exc.value.status == 403
    assert route.call_count == 1
    assert fake_sleep.durations == []


async def test_secondary_rate_limit_text_only_counts_for_403_429(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(422, json={"message": "secondary rate limit"})]

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)
    assert exc.value.status == 422
    assert fake_sleep.durations == []


async def test_retry_on_5xx_then_success(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    api.get(f"{API}/repos/{REPO}").side_effect = [
        Response(502),
        Response(503),
        Response(200, json={"id": 1}),
    ]

    assert await github.repo(REPO) == {"id": 1}
    assert fake_sleep.durations == [1.0, 2.0]  # 2**0, 2**1


async def test_5xx_exhaustion_raises_with_last_status(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(500), Response(502), Response(503), Response(504, text="upstream down")]

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)

    assert exc.value.status == 504
    assert route.call_count == 4  # first try + max_retries (3) retries
    assert fake_sleep.durations == [1.0, 2.0, 4.0]
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(github)


async def test_5xx_retry_applies_only_to_listed_statuses(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(501, json={"message": "Not Implemented"})]

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)
    assert exc.value.status == 501
    assert route.call_count == 1
    assert fake_sleep.durations == []


async def test_rate_limit_exhaustion_raises_with_last_status(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    route = api.get(f"{API}/repos/{REPO}")
    route.side_effect = [Response(429, headers={"retry-after": "1"}, json={"message": "slow down"})] * 4

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)

    assert exc.value.status == 429
    assert route.call_count == 4
    assert fake_sleep.durations == [1.0, 1.0, 1.0]


async def test_other_4xx_raises_immediately_without_sleeping(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep
) -> None:
    route = api.post(f"{API}/repos/{REPO}/issues")
    route.side_effect = [
        Response(
            422,
            json={"message": "Validation Failed", "errors": [{"resource": "Issue", "code": "missing_field"}]},
        )
    ]

    with pytest.raises(GitHubError) as exc:
        await github.create_issue(REPO, "")

    assert exc.value.status == 422
    assert route.call_count == 1
    assert fake_sleep.durations == []


async def test_404_raises_error(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}").mock(return_value=Response(404, json={"message": "Not Found"}))

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)

    assert exc.value.status == 404
    assert exc.value.method == "GET"
    assert exc.value.path == f"repos/{REPO}"
    assert "Not Found" in exc.value.message


# --- Errors ---------------------------------------------------------------------------------


async def test_error_message_has_status_method_path_message_and_errors(
    api: respx.MockRouter, github: GitHub
) -> None:
    api.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=Response(422, json={"message": "Validation Failed", "errors": [{"code": "invalid"}]})
    )

    with pytest.raises(GitHubError) as exc:
        await github.create_issue(REPO, "x")

    text = str(exc.value)
    assert "422" in text
    assert "POST" in text
    assert f"repos/{REPO}/issues" in text
    assert "Validation Failed" in text
    assert "invalid" in text
    assert TOKEN not in text
    assert "authorization" not in text.lower()


async def test_error_with_empty_body_uses_reason_phrase(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}").mock(return_value=Response(404))

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)

    assert exc.value.message == "Not Found"


async def test_token_absent_from_error_and_repr_after_retry_exhaustion(
    api: respx.MockRouter, github: GitHub
) -> None:
    api.get(f"{API}/repos/{REPO}").mock(return_value=Response(503, json={"message": "Service Unavailable"}))

    with pytest.raises(GitHubError) as exc:
        await github.repo(REPO)

    assert exc.value.status == 503
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value)
    assert TOKEN not in repr(github)
    assert not hasattr(github, "_token")


# --- Write spacing --------------------------------------------------------------------------


async def test_write_spacing_exact_durations(
    api: respx.MockRouter, clock: FakeClock, wall: FakeClock
) -> None:
    sleep = FakeSleep()  # records only; the test moves the clock by hand
    gh = GitHub(TOKEN, write_spacing=1.0, sleep=sleep, clock=clock, wall_clock=wall)
    api.post(f"{API}/repos/{REPO}/issues").mock(return_value=Response(201, json={"id": 1}))
    api.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json={"id": 1}))
    api.post(f"{API}/repos/{REPO}/issues/1/comments").mock(return_value=Response(201, json={"id": 2}))

    await gh.create_issue(REPO, "Title")
    assert sleep.durations == []  # the first write never waits

    clock.now += 0.25
    await gh.repo(REPO)  # a GET neither waits nor counts as a write
    assert sleep.durations == []

    await gh.add_comment(REPO, 1, "body")
    assert sleep.durations == [0.75]


async def test_write_spacing_not_needed_when_enough_time_passed(
    api: respx.MockRouter, clock: FakeClock, wall: FakeClock
) -> None:
    sleep = FakeSleep()
    gh = GitHub(TOKEN, write_spacing=1.0, sleep=sleep, clock=clock, wall_clock=wall)
    api.post(f"{API}/repos/{REPO}/issues").mock(return_value=Response(201, json={"id": 1}))

    await gh.create_issue(REPO, "one")
    clock.now += 1.5
    await gh.create_issue(REPO, "two")

    assert sleep.durations == []


async def test_write_spacing_measured_from_previous_write_after_sleep(
    api: respx.MockRouter, github: GitHub, fake_sleep: FakeSleep, clock: FakeClock
) -> None:
    # github fixture: write_spacing=0.5 and a sleep that advances the fake clock.
    api.post(f"{API}/repos/{REPO}/issues").mock(return_value=Response(201, json={"id": 1}))

    await github.create_issue(REPO, "one")
    clock.now += 0.1
    await github.create_issue(REPO, "two")
    await github.create_issue(REPO, "three")

    assert fake_sleep.durations == [pytest.approx(0.4), 0.5]


# --- Pagination -----------------------------------------------------------------------------


async def test_pagination_three_pages_params_only_on_first_request(
    api: respx.MockRouter, github: GitHub
) -> None:
    route = api.get(url__startswith=f"{API}/repos/{REPO}/issues")
    next2 = f"{API}/repos/{REPO}/issues?state=open&per_page=100&page=2"
    next3 = f"{API}/repos/{REPO}/issues?state=open&per_page=100&page=3"
    route.side_effect = [
        Response(200, json=[{"id": 1}], headers={"link": f'<{next2}>; rel="next", <{next3}>; rel="last"'}),
        Response(200, json=[{"id": 2}], headers={"link": f'<{next3}>; rel="next"'}),
        Response(200, json=[{"id": 3}]),
    ]

    issues = await github.issues(REPO)

    assert [i["id"] for i in issues] == [1, 2, 3]
    urls = [str(c.request.url) for c in route.calls]
    assert dict(route.calls[0].request.url.params) == {"state": "open", "per_page": "100"}
    # Absolute next URLs are requested exactly as given (no base_url prefixing, no extra params).
    assert urls[1:] == [next2, next3]


async def test_paginate_sends_extra_params_on_first_request_only(
    api: respx.MockRouter, github: GitHub
) -> None:
    route = api.get(url__startswith=f"{API}/repos/{REPO}/pulls")
    next_url = f"{API}/repositories/1/pulls?head=o%3Ab&state=all&per_page=100&page=2"
    route.side_effect = [Response(200, json=[{"n": 1}], headers={"link": f'<{next_url}>; rel="next"'})]
    # The next URL has a different path, so it needs its own route.
    second = api.get(next_url)
    second.side_effect = [Response(200, json=[{"n": 2}])]

    pulls = [p async for p in github.paginate(f"repos/{REPO}/pulls", head="o:b", state="all")]

    assert pulls == [{"n": 1}, {"n": 2}]
    first = route.calls[0].request.url
    assert dict(first.params) == {"head": "o:b", "state": "all", "per_page": "100"}
    assert str(second.calls[0].request.url) == next_url


@pytest.mark.parametrize("key", ["items", "check_runs", "workflow_runs", "anything_else"])
async def test_paginate_dict_page_yields_first_list_value(
    api: respx.MockRouter, github: GitHub, key: str
) -> None:
    api.get(url__startswith=f"{API}/search").mock(
        return_value=Response(200, json={"total_count": 2, "incomplete": False, key: [{"id": 1}, {"id": 2}]})
    )

    got = [x async for x in github.paginate("search")]
    assert got == [{"id": 1}, {"id": 2}]


async def test_check_runs_helper(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}/commits/abc/check-runs", params={"per_page": "100"}).mock(
        return_value=Response(200, json={"total_count": 1, "check_runs": [{"id": 5}]})
    )

    assert await github.check_runs(REPO, "abc") == [{"id": 5}]


async def test_issues_dropping_prs_and_filters(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(
        f"{API}/repos/{REPO}/issues",
        params={"state": "closed", "labels": "a,b", "since": "2026-01-01T00:00:00Z", "per_page": "100"},
    ).mock(
        return_value=Response(
            200,
            json=[{"id": 1}, {"id": 2, "pull_request": {"url": "..."}}, {"id": 3}],
        )
    )

    issues = await github.issues(REPO, state="closed", labels="a,b", since="2026-01-01T00:00:00Z")
    assert [i["id"] for i in issues] == [1, 3]
    assert route.call_count == 1


# --- Typed helpers --------------------------------------------------------------------------


async def test_parent_uses_issue_parent_path(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(f"{API}/repos/{REPO}/issues/5/parent").mock(
        return_value=Response(200, json={"number": 2})
    )

    assert await github.parent(REPO, 5) == {"number": 2}
    assert route.calls[0].request.url.path == f"/repos/{REPO}/issues/5/parent"


async def test_parent_returns_none_on_404(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}/issues/5/parent").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )

    assert await github.parent(REPO, 5) is None


async def test_file_text_returns_raw_text_not_parsed_json(api: respx.MockRouter, github: GitHub) -> None:
    raw = '{"name": "pkg", "version": "1.0.0"}\n'
    route = api.get(f"{API}/repos/{REPO}/contents/package.json", params={"ref": "main"}).mock(
        return_value=Response(200, text=raw, headers={"content-type": "application/json"})
    )

    result = await github.file_text(REPO, "package.json", ref="main")

    assert result == raw
    assert isinstance(result, str)
    assert route.calls[0].request.headers["accept"] == "application/vnd.github.raw+json"


async def test_file_text_without_ref_and_nested_path(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(f"{API}/repos/{REPO}/contents/docs/PLAN.md").mock(
        return_value=Response(200, text="# Plan\n")
    )

    assert await github.file_text(REPO, "docs/PLAN.md") == "# Plan\n"
    assert route.calls[0].request.url.query == b""


async def test_file_text_returns_none_on_404(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}/contents/missing.txt").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )

    assert await github.file_text(REPO, "missing.txt") is None


async def test_file_text_etag_hit_still_returns_raw_text(api: respx.MockRouter, github: GitHub) -> None:
    raw = '{"a": 1}'
    route = api.get(f"{API}/repos/{REPO}/contents/a.json")
    route.side_effect = [Response(200, text=raw, headers={"etag": '"f"'}), Response(304)]

    assert await github.file_text(REPO, "a.json") == raw
    assert await github.file_text(REPO, "a.json") == raw


async def test_remove_label_quotes_label_and_ignores_404(api: respx.MockRouter, github: GitHub) -> None:
    route = api.delete(url__startswith=f"{API}/repos/{REPO}/issues/3/labels/").mock(
        return_value=Response(404, json={"message": "Label does not exist"})
    )

    await github.remove_label(REPO, 3, "area/github")

    assert route.calls[0].request.url.raw_path == f"/repos/{REPO}/issues/3/labels/area%2Fgithub".encode()


async def test_remove_label_other_errors_propagate(api: respx.MockRouter, github: GitHub) -> None:
    api.delete(url__startswith=f"{API}/repos/{REPO}/issues/3/labels/").mock(
        return_value=Response(403, json={"message": "Forbidden"})
    )

    with pytest.raises(GitHubError):
        await github.remove_label(REPO, 3, "bug")


async def test_ensure_label_posts_only_non_none_fields(api: respx.MockRouter, github: GitHub) -> None:
    route = api.post(f"{API}/repos/{REPO}/labels").mock(
        return_value=Response(201, json={"name": "area/github"})
    )

    assert await github.ensure_label(REPO, "area/github", "ededed", "GitHub area") == {"name": "area/github"}
    await github.ensure_label(REPO, "bare")
    await github.ensure_label(REPO, "no-desc", "ffffff", None)

    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies == [
        {"name": "area/github", "color": "ededed", "description": "GitHub area"},
        {"name": "bare", "description": ""},
        {"name": "no-desc", "color": "ffffff"},
    ]


async def test_ensure_label_ignores_422_and_does_not_patch(api: respx.MockRouter, github: GitHub) -> None:
    route = api.post(f"{API}/repos/{REPO}/labels").mock(
        return_value=Response(
            422, json={"message": "Validation Failed", "errors": [{"code": "already_exists"}]}
        )
    )

    assert await github.ensure_label(REPO, "area/github", "ededed") is None
    assert route.call_count == 1  # assert_all_mocked would fail on any PATCH


async def test_ensure_label_other_errors_propagate(api: respx.MockRouter, github: GitHub) -> None:
    api.post(f"{API}/repos/{REPO}/labels").mock(return_value=Response(403, json={"message": "Forbidden"}))

    with pytest.raises(GitHubError):
        await github.ensure_label(REPO, "x")


@pytest.mark.parametrize("status", [404, 422])
async def test_delete_branch_ignores_404_and_422(api: respx.MockRouter, github: GitHub, status: int) -> None:
    route = api.delete(f"{API}/repos/{REPO}/git/refs/heads/feat/x").mock(
        return_value=Response(status, json={"message": "Reference does not exist"})
    )

    await github.delete_branch(REPO, "feat/x")
    assert route.call_count == 1


async def test_delete_branch_success_and_other_errors(api: respx.MockRouter, github: GitHub) -> None:
    ok = api.delete(f"{API}/repos/{REPO}/git/refs/heads/done").mock(return_value=Response(204))
    forbidden = api.delete(f"{API}/repos/{REPO}/git/refs/heads/protected").mock(
        return_value=Response(403, json={"message": "Forbidden"})
    )

    await github.delete_branch(REPO, "done")
    with pytest.raises(GitHubError):
        await github.delete_branch(REPO, "protected")
    assert ok.call_count == forbidden.call_count == 1


async def test_blocked_by_request_shape(api: respx.MockRouter, github: GitHub) -> None:
    route = api.post(f"{API}/repos/{REPO}/issues/1/dependencies/blocked_by").mock(
        return_value=Response(201, json={"id": 2})
    )

    assert await github.add_blocked_by(REPO, 1, 42) == {"id": 2}
    assert json.loads(route.calls[0].request.content) == {"issue_id": 42}


async def test_blocked_by_listing(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}/issues/1/dependencies/blocked_by", params={"per_page": "100"}).mock(
        return_value=Response(200, json=[{"number": 9}])
    )

    assert await github.blocked_by(REPO, 1) == [{"number": 9}]


async def test_sub_issues_request_shape(api: respx.MockRouter, github: GitHub) -> None:
    route = api.post(f"{API}/repos/{REPO}/issues/1/sub_issues").mock(
        return_value=Response(201, json={"id": 3})
    )

    assert await github.add_sub_issue(REPO, 1, 43) == {"id": 3}
    assert json.loads(route.calls[0].request.content) == {"sub_issue_id": 43}


async def test_sub_issues_listing(api: respx.MockRouter, github: GitHub) -> None:
    api.get(f"{API}/repos/{REPO}/issues/1/sub_issues", params={"per_page": "100"}).mock(
        return_value=Response(200, json=[{"number": 4}])
    )

    assert await github.sub_issues(REPO, 1) == [{"number": 4}]


async def test_merge_pull_and_pulls_for_head(api: respx.MockRouter, github: GitHub) -> None:
    merge = api.put(f"{API}/repos/{REPO}/pulls/7/merge").mock(
        return_value=Response(200, json={"merged": True})
    )
    pulls = api.get(
        f"{API}/repos/{REPO}/pulls", params={"head": "test:feat/x", "state": "all", "per_page": "100"}
    ).mock(return_value=Response(200, json=[{"number": 7}]))

    assert await github.merge_pull(REPO, 7, sha="abc") == {"merged": True}
    assert json.loads(merge.calls[0].request.content) == {"merge_method": "squash", "sha": "abc"}
    assert await github.pulls_for_head(REPO, "feat/x") == [{"number": 7}]
    assert pulls.call_count == 1


async def test_default_headers(api: respx.MockRouter, github: GitHub) -> None:
    route = api.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json={}))

    await github.repo(REPO)

    headers = route.calls[0].request.headers
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["user-agent"] == "gargi-harness"


async def test_async_context_manager_closes_client(api: respx.MockRouter) -> None:
    async with GitHub(TOKEN) as gh:
        assert not gh._client.is_closed
    assert gh._client.is_closed
