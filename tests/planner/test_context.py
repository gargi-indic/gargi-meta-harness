"""build_repo_context: context files within budgets and open issue titles, GitHub mocked with respx."""

from collections.abc import AsyncIterator, Iterator

import pytest
import respx
from httpx import Response

from harness.config import RepoConfig, parse_repo_config
from harness.github.client import GitHub
from harness.planner.context import build_repo_context

API = "https://api.github.com"
REPO = "o/r"


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
async def gh() -> AsyncIterator[GitHub]:
    async def no_sleep(_: float) -> None:
        return None

    client = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    yield client
    await client.aclose()


def put(api: respx.MockRouter, path: str, text: str) -> respx.Route:
    return api.get(f"{API}/repos/{REPO}/contents/{path}").mock(return_value=Response(200, text=text))


def issues(api: respx.MockRouter, titles: list[str], *, prs: tuple[str, ...] = ()) -> respx.Route:
    body = [{"number": n, "title": t} for n, t in enumerate(titles, 1)]
    body += [{"number": 900 + n, "title": t, "pull_request": {}} for n, t in enumerate(prs)]
    return api.get(f"{API}/repos/{REPO}/issues").mock(return_value=Response(200, json=body))


async def test_reads_the_configured_files_and_open_issue_titles(api, gh):
    config = parse_repo_config("context_files: [README.md, docs/ARCH.md]\n")
    readme = put(api, "README.md", "# Readme\n")
    put(api, "docs/ARCH.md", "Arch\n")
    listing = issues(api, ["First", "Second"], prs=("A pull request",))
    context = await build_repo_context(gh, REPO, config, ref="release")
    assert context.context_files == {"README.md": "# Readme\n", "docs/ARCH.md": "Arch\n"}
    assert context.existing_issue_titles == ["First", "Second"]  # pull requests are not issues
    assert context.notes == []
    assert readme.calls[0].request.url.params["ref"] == "release"
    assert listing.calls[0].request.url.params["state"] == "open"


async def test_each_file_is_capped_and_marked_truncated(api, gh):
    put(api, "big.md", "a" * 50_000)
    issues(api, [])
    context = await build_repo_context(gh, REPO, RepoConfig(context_files=["big.md"]))
    text = context.context_files["big.md"]
    assert len(text.encode()) <= 20 * 1024
    assert text.endswith("[truncated]\n")
    assert context.notes == ["big.md: truncated"]


async def test_the_cut_never_splits_a_multibyte_character(api, gh):
    put(api, "u.md", "é" * 30_000)  # 2 bytes each
    issues(api, [])
    context = await build_repo_context(gh, REPO, RepoConfig(context_files=["u.md"]), max_file_bytes=1001)
    text = context.context_files["u.md"]
    assert len(text.encode()) <= 1001
    assert "�" not in text


async def test_the_total_is_capped_and_later_files_are_skipped(api, gh):
    names = [f"f{n}.md" for n in range(8)]
    for name in names:
        put(api, name, "x" * 20_000)
    issues(api, [])
    context = await build_repo_context(gh, REPO, RepoConfig(context_files=names))
    total = sum(len(t.encode()) for t in context.context_files.values())
    assert total <= 100 * 1024
    assert list(context.context_files) == names[:6]  # five whole files, the sixth cut to what is left
    assert context.notes == [
        "f5.md: truncated",
        "f6.md: skipped, the total context budget of 102400 bytes is used",
        "f7.md: skipped, the total context budget of 102400 bytes is used",
    ]


async def test_missing_files_and_duplicates_are_handled(api, gh):
    route = put(api, "a.md", "A")
    api.get(f"{API}/repos/{REPO}/contents/gone.md").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    issues(api, [])
    context = await build_repo_context(gh, REPO, RepoConfig(context_files=["a.md", "gone.md", "a.md", " "]))
    assert context.context_files == {"a.md": "A"}
    assert context.notes == ["gone.md: not found"]
    assert route.call_count == 1


async def test_titles_are_limited_and_tidied(api, gh):
    issues(api, [f"Title {n}" for n in range(300)] + ["x"])
    context = await build_repo_context(gh, REPO, RepoConfig())
    assert len(context.existing_issue_titles) == 200
    assert context.existing_issue_titles[0] == "Title 0"

    api.reset()
    issues(api, ["multi\nline  title " + "y" * 500])
    context = await build_repo_context(gh, REPO, RepoConfig())
    assert context.existing_issue_titles[0].startswith("multi line title y")
    assert len(context.existing_issue_titles[0]) == 200


async def test_no_context_files_means_no_content_requests(api, gh):
    issues(api, ["Only"])
    context = await build_repo_context(gh, REPO, RepoConfig())
    assert context.context_files == {} and context.existing_issue_titles == ["Only"]
