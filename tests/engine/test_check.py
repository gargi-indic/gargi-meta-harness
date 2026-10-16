"""Check executors (M1.7): ci, scope, pinned_versions, command. All GitHub traffic is mocked with respx."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import Response

from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig
from harness.engine import Done, ExecContext, Waiting
from harness.engine.executors.check import CheckExecutor
from harness.github.client import GitHub
from harness.playbook.models import CheckNode
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow
from tests.engine.helpers import REPO, FakeClock, make_playbook

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

API = "https://api.github.com"
PR = 7
SHA = "a" * 40
SHA2 = "b" * 40
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
async def gh() -> AsyncIterator[GitHub]:
    async def no_sleep(_: float) -> None:
        return None

    client = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    yield client
    await client.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock):
    s = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    s.migrate()
    yield s
    s.close()


def make_ctx(
    gh: GitHub | None,
    store: Store,
    clock: FakeClock,
    *,
    context: dict[str, Any] | None = None,
    config: RepoConfig | None = None,
) -> ExecContext:
    run = RunRow(
        id="run1",
        repo=REPO,
        issue=1,
        playbook_id="mini",
        playbook_sha=None,
        status="running",
        context={"pr": {"number": PR, "url": f"https://github.com/{REPO}/pull/{PR}"}}
        if context is None
        else context,
        ledger_comment_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    return ExecContext(
        store=store,
        outbox=None,
        gh=gh,
        adapters=AdapterRegistry(),
        repo_config=config or RepoConfig(),
        playbook=make_playbook(
            {"c": {"kind": "check", "check": "ci"}}, {"c.pass": "@done", "c.fail": "@failed"}
        ),
        run=run,
        template=Context(issue=IssueCtx(number=1, title="t", agent="jules"), run=RunCtx(id="run1")),
        clock=clock,
    )


def make_step(ref: dict[str, Any] | None = None) -> StepRow:
    return StepRow(
        id="run1/c/1",
        run_id="run1",
        node_id="c",
        visit=1,
        status="running",
        outcome=None,
        ref=ref,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )


def node(check: str) -> CheckNode:
    return CheckNode(kind="check", check=check)  # type: ignore[arg-type]


def mock_pull(router, sha: str = SHA):
    return router.get(f"{API}/repos/{REPO}/pulls/{PR}").mock(
        return_value=Response(200, json={"number": PR, "head": {"sha": sha}})
    )


def mock_ci(router, runs: list[dict[str, Any]], statuses: list[dict[str, Any]] | None = None, sha: str = SHA):
    router.get(f"{API}/repos/{REPO}/commits/{sha}/check-runs").mock(
        return_value=Response(200, json={"total_count": len(runs), "check_runs": runs})
    )
    router.get(f"{API}/repos/{REPO}/commits/{sha}/status").mock(
        return_value=Response(200, json={"state": "pending", "statuses": statuses or []})
    )


def run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": f"https://ci/{name}"}


def mock_files(router, files: list[dict[str, Any]]):
    router.get(f"{API}/repos/{REPO}/pulls/{PR}/files").mock(return_value=Response(200, json=files))


def patch_of(*lines: str) -> str:
    return f"@@ -1,{len(lines)} +1,{len(lines)} @@\n" + "\n".join(lines)


# ci -----------------------------------------------------------------------------------------------------


async def test_ci_pending_waits_then_passes(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    ex = CheckExecutor()
    ctx = make_ctx(gh, store, clock)

    mock_ci(respx_mock, [run("build"), run("test", status="in_progress", conclusion=None)])
    first = await ex.advance(make_step(), node("ci"), ctx)
    assert isinstance(first, Waiting)
    assert first.ref == {"sha": SHA, "since": clock.t}
    assert first.poll_after == 60.0

    respx_mock.routes.clear()
    mock_pull(respx_mock)
    mock_ci(respx_mock, [run("build"), run("test")], [{"context": "legacy", "state": "success"}])
    clock.advance(60)
    second = await ex.advance(make_step(first.ref), node("ci"), ctx)
    assert second == Done("pass", {"head_sha": SHA, "checks": 3})


async def test_ci_pending_status_waits(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_ci(respx_mock, [run("build")], [{"context": "legacy", "state": "pending"}])
    result = await CheckExecutor(poll=15.0).advance(make_step(), node("ci"), make_ctx(gh, store, clock))
    assert isinstance(result, Waiting)
    assert result.poll_after == 15.0


async def test_ci_failure_lists_details_and_correction(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_ci(
        respx_mock,
        [run("build"), run("lint", conclusion="failure"), run("deploy", conclusion="cancelled")],
        [
            {"context": "legacy/ci", "state": "failure", "target_url": "https://legacy/1"},
            {"context": "ok", "state": "success", "target_url": "https://legacy/2"},
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("ci"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert result.output["head_sha"] == SHA
    assert result.output["failed"] == [
        {"name": "lint", "conclusion": "failure", "url": "https://ci/lint"},
        {"name": "deploy", "conclusion": "cancelled", "url": "https://ci/deploy"},
        {"name": "legacy/ci", "conclusion": "failure", "url": "https://legacy/1"},
    ]
    assert result.output["correction"] == (
        "CI failed on aaaaaaa. Fix these checks and push to the same branch:\n"
        "1. lint: failure (https://ci/lint)\n"
        "2. deploy: cancelled (https://ci/deploy)\n"
        "3. legacy/ci: failure (https://legacy/1)"
    )


async def test_ci_failed_list_is_capped(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_ci(respx_mock, [run(f"job{i}", conclusion="failure") for i in range(30)])
    result = await CheckExecutor().advance(make_step(), node("ci"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert len(result.output["failed"]) == 20
    assert "10 more" in result.output["correction"]


async def test_ci_no_checks_waits_then_fails(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_ci(respx_mock, [])
    ex = CheckExecutor()
    ctx = make_ctx(gh, store, clock)
    start = clock.t

    first = await ex.advance(make_step(), node("ci"), ctx)
    assert isinstance(first, Waiting)
    assert first.ref == {"sha": SHA, "since": start}

    clock.advance(599)
    again = await ex.advance(make_step(first.ref), node("ci"), ctx)
    assert isinstance(again, Waiting)
    assert again.ref == {"sha": SHA, "since": start}

    clock.advance(1)
    final = await ex.advance(make_step(first.ref), node("ci"), ctx)
    assert isinstance(final, Done)
    assert final.outcome == "fail"
    assert final.output["note"] == "no CI reported"
    assert final.output["failed"] == []
    assert final.output["head_sha"] == SHA
    assert final.output["correction"].startswith("No CI reported for aaaaaaa within 10 minutes.")


async def test_ci_new_head_sha_resets_the_clock(respx_mock, gh, store, clock):
    mock_pull(respx_mock, SHA2)
    mock_ci(respx_mock, [], sha=SHA2)
    ctx = make_ctx(gh, store, clock)
    old_ref = {"sha": SHA, "since": clock.t - 5000}

    result = await CheckExecutor().advance(make_step(old_ref), node("ci"), ctx)
    assert isinstance(result, Waiting)
    assert result.ref == {"sha": SHA2, "since": clock.t}


async def test_ci_neutral_and_skipped_pass(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_ci(respx_mock, [run("a", conclusion="neutral"), run("b", conclusion="skipped"), run("c")])
    result = await CheckExecutor().advance(make_step(), node("ci"), make_ctx(gh, store, clock))
    assert result == Done("pass", {"head_sha": SHA, "checks": 3})


# scope --------------------------------------------------------------------------------------------------


async def test_scope_violations_including_rename(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            {"filename": "src/app.py", "status": "modified"},
            {"filename": ".harness/playbook.yml", "status": "modified"},
            {"filename": "docs/notes.md", "previous_filename": ".claude/notes.md", "status": "renamed"},
            {"filename": ".claude-code/x", "status": "added"},
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("scope"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert result.output["head_sha"] == SHA
    assert result.output["violations"] == [".harness/playbook.yml", ".claude/notes.md"]
    correction = result.output["correction"]
    assert correction.startswith("These files are protected and must not be changed: ")
    assert ".claude/notes.md" in correction
    assert correction.endswith("Revert them on the same branch.")


async def test_scope_entry_without_slash_matches_exact_or_prefix(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            {"filename": "Makefile", "status": "modified"},
            {"filename": "Makefile.bak", "status": "added"},
            {"filename": "infra/main.tf", "status": "modified"},
            {"filename": "infrastructure/x.tf", "status": "added"},
        ],
    )
    config = RepoConfig(protected_paths=["Makefile", "infra"])
    result = await CheckExecutor().advance(
        make_step(), node("scope"), make_ctx(gh, store, clock, config=config)
    )
    assert isinstance(result, Done)
    assert result.output["violations"] == ["Makefile", "infra/main.tf"]


async def test_scope_clean_pr_passes(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            {"filename": "src/app.py", "status": "modified"},
            {"filename": "tests/test_app.py", "status": "added"},
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("scope"), make_ctx(gh, store, clock))
    assert result == Done("pass", {"head_sha": SHA, "files": 2})


# pinned_versions: actions -------------------------------------------------------------------------------


def workflow_file(*uses: str, name: str = ".github/workflows/ci.yml") -> dict[str, Any]:
    return {"filename": name, "status": "modified", "patch": patch_of(*uses)}


async def test_pinned_missing_action_tag_fails(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            workflow_file(
                "+      - uses: actions/checkout@v4",
                "+      - uses: actions/cache@v4.1.2",
                "+      - uses: actions/checkout@v4",
            )
        ],
    )
    good = respx_mock.get(f"{API}/repos/actions/checkout/git/ref/tags/v4").mock(
        return_value=Response(200, json={"ref": "refs/tags/v4"})
    )
    bad = respx_mock.get(f"{API}/repos/actions/cache/git/ref/tags/v4.1.2").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert good.call_count == 1  # cached within one advance
    assert bad.call_count == 1
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert result.output["head_sha"] == SHA
    assert result.output["problems"] == [
        {
            "file": ".github/workflows/ci.yml",
            "line": "- uses: actions/cache@v4.1.2",
            "problem": "tag not found",
        }
    ]
    assert result.output["correction"].startswith("Unpinned or unknown versions in aaaaaaa.")
    assert (
        "1. .github/workflows/ci.yml: tag not found: - uses: actions/cache@v4.1.2"
        in result.output["correction"]
    )


async def test_pinned_existing_tag_passes(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            workflow_file(
                "+      - uses: actions/checkout@v4",
                '+      - uses: "owner/repo/sub/dir@v1.2.3"  # comment',
                "+      - uses: ./local-action",
                "+      - uses: docker://alpine:latest",
            )
        ],
    )
    respx_mock.get(f"{API}/repos/actions/checkout/git/ref/tags/v4").mock(return_value=Response(200, json={}))
    respx_mock.get(f"{API}/repos/owner/repo/git/ref/tags/v1.2.3").mock(return_value=Response(200, json={}))
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert result == Done("pass", {"head_sha": SHA, "problems": []})


@pytest.mark.parametrize("ref", ["main", "MASTER", "latest"])
async def test_pinned_floating_ref_fails_without_network(respx_mock, gh, store, clock, ref):
    mock_pull(respx_mock)
    mock_files(
        respx_mock, [workflow_file(f"+      - uses: actions/checkout@{ref}", "+      - uses: foo/bar")]
    )
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert [p["problem"] for p in result.output["problems"]] == ["unpinned ref", "unpinned ref"]
    assert result.output["problems"][0]["line"] == f"- uses: actions/checkout@{ref}"


async def test_pinned_full_sha_passes_without_network(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(respx_mock, [workflow_file(f"+      - uses: actions/checkout@{FULL_SHA} # v4")])
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert result == Done("pass", {"head_sha": SHA, "problems": []})


async def test_pinned_removed_and_context_lines_are_ignored(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            workflow_file(
                "-      - uses: actions/checkout@main",
                "       - uses: actions/setup-python@main",
                f"+      - uses: actions/checkout@{FULL_SHA}",
            ),
            {"filename": ".github/workflows/big.yml", "status": "modified"},  # no patch: skipped
            {"filename": "README.md", "status": "modified", "patch": patch_of("+uses: foo/bar@main")},
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert result == Done("pass", {"head_sha": SHA, "problems": []})


async def test_pinned_non_404_errors_propagate(respx_mock, gh, store, clock):
    from harness.github.client import GitHubError

    mock_pull(respx_mock)
    mock_files(respx_mock, [workflow_file("+      - uses: actions/checkout@v4")])
    respx_mock.get(f"{API}/repos/actions/checkout/git/ref/tags/v4").mock(
        return_value=Response(401, json={"message": "Bad credentials"})
    )
    with pytest.raises(GitHubError) as exc:
        await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert exc.value.status == 401


async def test_pinned_problems_are_capped(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(respx_mock, [workflow_file(*[f"+      - uses: a/b{i}@main" for i in range(25)])])
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert len(result.output["problems"]) == 20
    assert "5 more" in result.output["correction"]


# pinned_versions: images --------------------------------------------------------------------------------


async def test_pinned_unpinned_images_fail(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            {
                "filename": "Dockerfile",
                "status": "modified",
                "patch": patch_of("+FROM python:latest", "+FROM --platform=linux/amd64 node AS web"),
            },
            {
                "filename": "deploy/docker-compose.yml",
                "status": "added",
                "patch": patch_of(
                    "+services:", "+  cache:", "+    image: redis", "+  db:", "+    image: postgres:16"
                ),
            },
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert [(p["file"], p["line"], p["problem"]) for p in result.output["problems"]] == [
        ("Dockerfile", "FROM python:latest", "unpinned image"),
        ("Dockerfile", "FROM --platform=linux/amd64 node AS web", "unpinned image"),
        ("deploy/docker-compose.yml", "image: redis", "unpinned image"),
    ]
    assert "Unpinned or unknown versions" in result.output["correction"]


async def test_pinned_good_images_pass(respx_mock, gh, store, clock):
    digest = "sha256:" + "c" * 64
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            {
                "filename": "services/api/Dockerfile.prod",
                "status": "added",
                "patch": patch_of(
                    "+FROM python:3.12-slim AS base",
                    f"+FROM ghcr.io/x/y@{digest} AS tools",
                    "+FROM scratch",
                    "+FROM base",
                    "+FROM tools AS final",
                    "+FROM localhost:5000/team/app:1.0",
                    "+FROM ${BASE_IMAGE}",
                    "-FROM old:latest",
                ),
            },
            {
                "filename": "compose.yaml",
                "status": "modified",
                "patch": patch_of('+    image: "redis:7.2"', f"+    image: postgres@{digest}"),
            },
            {"filename": "build.dockerfile", "status": "added", "patch": patch_of("+FROM alpine:3.20")},
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert result == Done("pass", {"head_sha": SHA, "problems": []})


async def test_pinned_stage_alias_from_context_line_counts(respx_mock, gh, store, clock):
    mock_pull(respx_mock)
    mock_files(
        respx_mock,
        [
            {
                "filename": "Dockerfile",
                "status": "modified",
                "patch": patch_of(" FROM python:3.12 AS base", "+FROM base", "+FROM other"),
            }
        ],
    )
    result = await CheckExecutor().advance(make_step(), node("pinned_versions"), make_ctx(gh, store, clock))
    assert isinstance(result, Done)
    assert [p["line"] for p in result.output["problems"]] == ["FROM other"]


# command / preconditions --------------------------------------------------------------------------------


async def test_command_fails_with_m3_note_without_network(respx_mock, gh, store, clock):
    result = await CheckExecutor().advance(
        make_step(), CheckNode(kind="check", check="command", command="make test"), make_ctx(gh, store, clock)
    )
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert result.output["note"] == "command checks need a local workspace (M3)"
    assert result.output["correction"]


@pytest.mark.parametrize("check", ["ci", "scope", "pinned_versions"])
@pytest.mark.parametrize("context", [{}, {"pr": None}, {"pr": {"url": "x"}}, {"pr": {"number": "7"}}])
async def test_no_pr_in_context_fails(respx_mock, gh, store, clock, check, context):
    result = await CheckExecutor().advance(
        make_step(), node(check), make_ctx(gh, store, clock, context=context)
    )
    assert isinstance(result, Done)
    assert result.outcome == "fail"
    assert result.output["error"] == "no PR in run context"
    assert result.output["correction"]


async def test_missing_github_client_raises(store, clock):
    with pytest.raises(RuntimeError, match="check executors need a GitHub client"):
        await CheckExecutor().advance(make_step(), node("ci"), make_ctx(None, store, clock))
