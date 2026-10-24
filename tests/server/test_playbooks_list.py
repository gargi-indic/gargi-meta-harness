"""GET /api/playbooks, /api/playbooks/{id} and /api/playbooks/schema (M2.8)."""

from pathlib import Path

import pytest
from httpx import Response

from harness.playbook.schema import playbook_json_schema
from harness.store.etag_cache import SqliteEtagCache
from tests.server.playbooks_helpers import (
    REPO,
    builtin_text,
    fixture_text,
    make_gh,
    make_playbook_app,
    playbook_client,
)

pytestmark = pytest.mark.respx(
    assert_all_called=True, assert_all_mocked=True, base_url="https://api.github.com"
)

DIR = f"repos/{REPO}/contents/.harness/playbooks"
REPO_YAML = fixture_text("repo_playbook.yaml")
ENTRIES = [
    {"type": "file", "name": "repo-flow.yaml", "path": ".harness/playbooks/repo-flow.yaml", "sha": "sha-1"},
    {"type": "file", "name": "broken.yml", "path": ".harness/playbooks/broken.yml", "sha": "sha-2"},
    {"type": "file", "name": "README.md", "path": ".harness/playbooks/README.md", "sha": "sha-3"},
    {"type": "dir", "name": "sub.yaml", "path": ".harness/playbooks/sub.yaml", "sha": "sha-4"},
]
BUILTIN = {
    "id": "plan-build-review",
    "source": "builtin",
    "path": "playbooks/plan-build-review.yaml",
    "sha": None,
    "valid": True,
}


@pytest.fixture
async def client(respx_mock):
    async with playbook_client(make_playbook_app(gh=make_gh())) as c:
        yield c


def mock_repo_files(router, *, files=("repo-flow.yaml", "broken.yml")):
    router.get(DIR).mock(return_value=Response(200, json=ENTRIES))
    texts = {"repo-flow.yaml": REPO_YAML, "broken.yml": "nodes: [oops"}
    for name in files:
        router.get(f"{DIR}/{name}").mock(return_value=Response(200, text=texts[name]))


async def test_builtins_are_listed(client) -> None:
    response = await client.get("/api/playbooks")
    assert response.status_code == 200
    assert response.json() == [DECOMPOSE_EPIC, BUILTIN, TOURNAMENT]


async def test_builtin_files_come_from_the_injected_dir_sorted_by_id(tmp_path: Path) -> None:
    (tmp_path / "zzz.yaml").write_text(builtin_text().replace("id: plan-build-review", "id: alpha"))
    (tmp_path / "bad-one.yml").write_text("nodes: [oops")
    (tmp_path / "invalid.yaml").write_text(fixture_text("unrouted_port.yaml"))
    (tmp_path / "notes.txt").write_text("ignored")
    async with playbook_client(make_playbook_app(builtin_dir=tmp_path)) as client:
        listing = (await client.get("/api/playbooks")).json()
    assert [(p["id"], p["path"], p["valid"]) for p in listing] == [
        ("alpha", "playbooks/zzz.yaml", True),
        ("bad-one", "playbooks/bad-one.yml", False),
        ("unrouted", "playbooks/invalid.yaml", False),
    ]
    assert all(p["source"] == "builtin" and p["sha"] is None for p in listing)


async def test_a_missing_builtin_dir_lists_nothing(tmp_path: Path) -> None:
    async with playbook_client(make_playbook_app(builtin_dir=tmp_path / "nope")) as client:
        assert (await client.get("/api/playbooks")).json() == []


BROKEN = {
    "id": "broken",
    "source": "repo",
    "path": ".harness/playbooks/broken.yml",
    "sha": "sha-2",
    "valid": False,
}
TOURNAMENT = {**BUILTIN, "id": "tournament", "path": "playbooks/tournament.yaml"}
DECOMPOSE_EPIC = {**BUILTIN, "id": "decompose-epic", "path": "playbooks/decompose-epic.yaml"}
REPO_FLOW = {
    "id": "repo-flow",
    "source": "repo",
    "path": ".harness/playbooks/repo-flow.yaml",
    "sha": "sha-1",
    "valid": True,
}


async def test_repo_playbooks_come_through_the_contents_api(client, respx_mock) -> None:
    mock_repo_files(respx_mock)
    response = await client.get("/api/playbooks", params={"repo": REPO})
    assert response.status_code == 200
    # Built-ins first, then the repo's by id; non-YAML files and directories are skipped.
    assert response.json() == [DECOMPOSE_EPIC, BUILTIN, TOURNAMENT, BROKEN, REPO_FLOW]


async def test_a_second_request_revalidates_with_the_etag(respx_mock) -> None:
    seen: list[str | None] = []

    def serve(etag: str, **payload):
        def handler(request):
            seen.append(request.headers.get("if-none-match"))
            if request.headers.get("if-none-match") == etag:
                return Response(304)
            return Response(200, headers={"etag": etag}, **payload)

        return handler

    respx_mock.get(DIR).mock(side_effect=serve('"dir-1"', json=ENTRIES[:1]))
    respx_mock.get(f"{DIR}/repo-flow.yaml").mock(side_effect=serve('"file-1"', text=REPO_YAML))
    async with playbook_client(make_playbook_app(gh=make_gh())) as client:
        first = await client.get("/api/playbooks", params={"repo": REPO})
        second = await client.get("/api/playbooks", params={"repo": REPO})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == [DECOMPOSE_EPIC, BUILTIN, TOURNAMENT, REPO_FLOW]
    assert seen == [None, None, '"dir-1"', '"file-1"']


async def test_the_server_cache_is_the_sqlite_etag_cache(respx_mock) -> None:
    """`harness serve` builds the client with the SQLite cache; the 304 path works through it as well."""
    app = make_playbook_app()
    gh = make_gh(cache=SqliteEtagCache(app.state.store))
    app.state.gh = gh
    calls = []

    def handler(request):
        calls.append(request.headers.get("if-none-match"))
        return (
            Response(304)
            if request.headers.get("if-none-match")
            else Response(200, json=[], headers={"etag": '"e"'})
        )

    respx_mock.get(DIR).mock(side_effect=handler)
    async with playbook_client(app) as client:
        for _ in range(2):
            assert (await client.get("/api/playbooks", params={"repo": REPO})).json() == [
                DECOMPOSE_EPIC,
                BUILTIN,
                TOURNAMENT,
            ]
    assert calls == [None, '"e"']
    assert app.state.store.query("SELECT COUNT(*) AS n FROM http_cache")[0]["n"] == 1


async def test_a_missing_playbooks_directory_lists_only_the_builtins(client, respx_mock) -> None:
    respx_mock.get(DIR).mock(return_value=Response(404, json={"message": "Not Found"}))
    response = await client.get("/api/playbooks", params={"repo": REPO})
    assert response.json() == [DECOMPOSE_EPIC, BUILTIN, TOURNAMENT]


async def test_listing_errors(client, respx_mock) -> None:
    assert (await client.get("/api/playbooks", params={"repo": "other/repo"})).json() == {
        "detail": "unknown repo"
    }
    respx_mock.get(DIR).mock(return_value=Response(401, json={"message": "Bad credentials"}))
    response = await client.get("/api/playbooks", params={"repo": REPO})
    assert (response.status_code, response.json()) == (502, {"detail": "github: 401"})


async def test_listing_with_a_repo_but_no_github_is_a_503() -> None:
    async with playbook_client(make_playbook_app(gh=None)) as client:
        response = await client.get("/api/playbooks", params={"repo": REPO})
        assert (response.status_code, response.json()) == (503, {"detail": "GitHub not configured"})
        assert (await client.get("/api/playbooks")).status_code == 200  # built-ins need no GitHub


async def test_get_a_builtin(client) -> None:
    response = await client.get("/api/playbooks/plan-build-review")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "plan-build-review"
    assert (body["source"], body["path"], body["sha"]) == (
        "builtin",
        "playbooks/plan-build-review.yaml",
        None,
    )
    assert body["yaml"] == builtin_text()
    assert body["parsed"]["id"] == "plan-build-review"
    assert body["parsed"]["nodes"]["plan"]["kind"] == "agent"


async def test_get_prefers_the_repo_file(client, respx_mock) -> None:
    mock_repo_files(respx_mock, files=("repo-flow.yaml",))
    body = (await client.get("/api/playbooks/repo-flow", params={"repo": REPO})).json()
    assert (body["source"], body["sha"], body["path"]) == (
        "repo",
        "sha-1",
        ".harness/playbooks/repo-flow.yaml",
    )
    assert body["yaml"] == REPO_YAML
    assert body["parsed"]["name"] == "A playbook that lives in the repo"


async def test_get_reads_a_yml_file_and_returns_null_when_it_does_not_parse(client, respx_mock) -> None:
    mock_repo_files(respx_mock, files=("broken.yml",))
    body = (await client.get("/api/playbooks/broken", params={"repo": REPO})).json()
    assert (body["source"], body["path"]) == ("repo", ".harness/playbooks/broken.yml")
    assert body["yaml"] == "nodes: [oops"
    assert body["parsed"] is None


async def test_a_repo_file_shadows_the_builtin_of_the_same_id(client, respx_mock) -> None:
    override = builtin_text().replace("name: Plan, build, review, merge", "name: Overridden")
    respx_mock.get(DIR).mock(
        return_value=Response(
            200,
            json=[{"type": "file", "path": ".harness/playbooks/plan-build-review.yaml", "sha": "s9"}],
        )
    )
    respx_mock.get(f"{DIR}/plan-build-review.yaml").mock(return_value=Response(200, text=override))
    body = (await client.get("/api/playbooks/plan-build-review", params={"repo": REPO})).json()
    assert (body["source"], body["parsed"]["name"]) == ("repo", "Overridden")


async def test_get_falls_back_to_the_builtin_when_the_repo_has_none(client, respx_mock) -> None:
    respx_mock.get(DIR).mock(return_value=Response(404, json={"message": "Not Found"}))
    body = (await client.get("/api/playbooks/plan-build-review", params={"repo": REPO})).json()
    assert body["source"] == "builtin"


async def test_get_errors(client, respx_mock) -> None:
    missing = await client.get("/api/playbooks/nope")
    assert (missing.status_code, missing.json()) == (404, {"detail": "unknown playbook"})
    for bad in ("X", "a_b", "-a", "a" * 65, "a.b"):
        response = await client.get(f"/api/playbooks/{bad}")
        assert (response.status_code, response.json()) == (422, {"detail": "invalid playbook id"})
    unknown_repo = await client.get("/api/playbooks/plan-build-review", params={"repo": "other/repo"})
    assert (unknown_repo.status_code, unknown_repo.json()) == (404, {"detail": "unknown repo"})
    respx_mock.get(DIR).mock(return_value=Response(401, json={"message": "Bad credentials"}))
    failed = await client.get("/api/playbooks/plan-build-review", params={"repo": REPO})
    assert (failed.status_code, failed.json()) == (502, {"detail": "github: 401"})


async def test_the_schema_is_served_verbatim_and_not_taken_for_an_id(client) -> None:
    response = await client.get("/api/playbooks/schema")
    assert response.status_code == 200
    assert response.json() == playbook_json_schema()


async def test_the_playbook_routes_need_the_cookie(respx_mock) -> None:
    from tests.server.helpers import make_client

    async with make_client(make_playbook_app(gh=make_gh())) as anon:
        for method, path in (
            ("GET", "/api/playbooks"),
            ("GET", "/api/playbooks/schema"),
            ("POST", "/api/playbooks/validate"),
            ("POST", "/api/playbooks/dry-run"),
            ("POST", "/api/playbooks/save"),
        ):
            assert (await anon.request(method, path)).status_code == 401, path
