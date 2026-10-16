"""POST /api/playbooks/save (M2.8): the exact GitHub call sequence, idempotency and the guards."""

import base64
import json

import httpx
import pytest
from httpx import Response

from tests.server.playbooks_helpers import (
    BRANCH,
    REPO,
    fixture_text,
    make_gh,
    make_playbook_app,
    playbook_client,
)

pytestmark = pytest.mark.respx(
    assert_all_called=True, assert_all_mocked=True, base_url="https://api.github.com"
)

URL = "/api/playbooks/save"
YAML = fixture_text("repo_playbook.yaml")
FILE = f"repos/{REPO}/contents/.harness/playbooks/repo-flow.yaml"
PR = {"number": 7, "html_url": f"https://github.com/{REPO}/pull/7"}


@pytest.fixture
async def client(respx_mock):
    async with playbook_client(make_playbook_app(gh=make_gh())) as c:
        yield c


def sequence(router) -> list[tuple[str, str]]:
    return [(c.request.method, c.request.url.path.removeprefix("/")) for c in router.calls]


def body_of(route, call: int = -1):
    return json.loads(route.calls[call].request.content)


def mock_create_flow(router, *, existing_sha: str | None = None, default_branch_lookup: bool = True):
    routes = {
        "pulls": router.get(f"repos/{REPO}/pulls", params={"state": "open", "per_page": "100"}).mock(
            return_value=Response(200, json=[])
        ),
        "ref": router.get(f"repos/{REPO}/git/ref/heads/main").mock(
            return_value=Response(200, json={"object": {"sha": "abc123"}})
        ),
        "refs": router.post(f"repos/{REPO}/git/refs").mock(return_value=Response(201, json={})),
        "file": router.get(FILE, params={"ref": "main"}).mock(
            return_value=Response(200, json={"sha": existing_sha}) if existing_sha else Response(404, json={})
        ),
        "put": router.put(FILE).mock(return_value=Response(201, json={"content": {}})),
        "pr": router.post(f"repos/{REPO}/pulls").mock(return_value=Response(201, json=PR)),
    }
    if default_branch_lookup:
        routes["repo"] = router.get(f"repos/{REPO}").mock(
            return_value=Response(200, json={"default_branch": "main"})
        )
    return routes


async def test_the_exact_call_sequence_and_the_returned_pr(client, respx_mock) -> None:
    routes = mock_create_flow(respx_mock)
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "pr": {"number": 7, "url": PR["html_url"]},
        "branch": BRANCH,
        "existing": False,
    }
    assert sequence(respx_mock) == [
        ("GET", f"repos/{REPO}"),
        ("GET", f"repos/{REPO}/pulls"),
        ("GET", f"repos/{REPO}/git/ref/heads/main"),
        ("POST", f"repos/{REPO}/git/refs"),
        ("GET", FILE),
        ("PUT", FILE),
        ("POST", f"repos/{REPO}/pulls"),
    ]
    assert body_of(routes["refs"]) == {"ref": f"refs/heads/{BRANCH}", "sha": "abc123"}
    put = body_of(routes["put"])
    assert put == {
        "message": "Playbook: repo-flow",
        "content": base64.b64encode(YAML.encode()).decode(),
        "branch": BRANCH,
    }
    assert base64.b64decode(put["content"]).decode() == YAML
    pull = body_of(routes["pr"])
    assert (pull["title"], pull["head"], pull["base"]) == ("Playbook: repo-flow", BRANCH, "main")
    assert pull["body"].startswith("Saved from the harness playbook editor.")
    assert "Validation" in pull["body"] and "no issues" in pull["body"]
    # Nothing is ever written to the base branch.
    assert all("heads/main" not in str(c.request.url) for c in respx_mock.calls if c.request.method != "GET")
    assert put["branch"] != "main"


async def test_message_base_and_the_existing_file_sha(client, respx_mock) -> None:
    routes = mock_create_flow(respx_mock, existing_sha="old-sha", default_branch_lookup=False)
    response = await client.post(
        URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML, "message": "Add repo flow", "base": "main"}
    )
    assert response.status_code == 200, response.text
    assert sequence(respx_mock)[0] == ("GET", f"repos/{REPO}/pulls")  # no default-branch lookup
    put = body_of(routes["put"])
    assert (put["message"], put["sha"], put["branch"]) == ("Add repo flow", "old-sha", BRANCH)


def mock_open_pr(router, *, branch_text: str, base: str = "main"):
    pulls = router.get(f"repos/{REPO}/pulls", params={"state": "open", "per_page": "100"}).mock(
        return_value=Response(
            200,
            json=[
                {"number": 3, "html_url": "x", "head": {"ref": "feature/unrelated"}, "base": {"ref": "main"}},
                {
                    "number": 4,
                    "html_url": "x",
                    "head": {"ref": "harness/playbook-repo-flow-extra-20260101000000"},
                },
                {**PR, "head": {"ref": BRANCH}, "base": {"ref": base}},
            ],
        )
    )
    file = router.get(FILE, params={"ref": BRANCH}).mock(return_value=Response(200, text=branch_text))
    return pulls, file


async def test_identical_content_returns_the_open_pr_and_writes_nothing(client, respx_mock) -> None:
    respx_mock.get(f"repos/{REPO}").mock(return_value=Response(200, json={"default_branch": "main"}))
    mock_open_pr(respx_mock, branch_text=YAML)
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert response.status_code == 200, response.text
    assert response.json() == {"pr": {"number": 7, "url": PR["html_url"]}, "branch": BRANCH, "existing": True}
    assert [m for m, _ in sequence(respx_mock)] == ["GET", "GET", "GET"]
    assert sequence(respx_mock)[-1] == ("GET", FILE)


async def test_saving_twice_creates_one_pr(respx_mock) -> None:
    """The first call opens the PR; the second sees it (and its file) and creates nothing."""
    async with playbook_client(make_playbook_app(gh=make_gh())) as client:
        routes = mock_create_flow(respx_mock)
        first = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
        assert first.json()["existing"] is False
        routes["pulls"].mock(
            return_value=Response(200, json=[{**PR, "head": {"ref": BRANCH}, "base": {"ref": "main"}}])
        )
        respx_mock.get(FILE, params={"ref": BRANCH}).mock(return_value=Response(200, text=YAML))
        second = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert second.json() == {**first.json(), "existing": True}
    assert routes["refs"].call_count == 1
    assert routes["pr"].call_count == 1
    assert routes["put"].call_count == 1


async def test_changed_content_opens_a_new_pr(client, respx_mock) -> None:
    routes = mock_create_flow(respx_mock)
    routes["pulls"].mock(
        return_value=Response(200, json=[{**PR, "head": {"ref": BRANCH}, "base": {"ref": "main"}}])
    )
    respx_mock.get(FILE, params={"ref": BRANCH}).mock(return_value=Response(200, text=YAML + "# older\n"))
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert response.status_code == 200, response.text
    assert response.json()["existing"] is False
    assert routes["pr"].call_count == 1


async def test_an_open_pr_against_another_base_is_not_reused(client, respx_mock) -> None:
    routes = mock_create_flow(respx_mock)
    routes["pulls"].mock(
        return_value=Response(200, json=[{**PR, "head": {"ref": BRANCH}, "base": {"ref": "release"}}])
    )
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert response.json()["existing"] is False


@pytest.mark.parametrize("bad_id", ["../x", "X", "a/b", "-a", "a_b", "", "a" * 65, "a b", "a%2Fb"])
async def test_a_bad_id_is_a_422_without_github_calls(client, respx_mock, bad_id: str) -> None:
    response = await client.post(URL, json={"repo": REPO, "id": bad_id, "yaml": YAML})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid playbook id"}
    assert respx_mock.calls.call_count == 0


async def test_invalid_yaml_is_a_422_without_github_calls(client, respx_mock) -> None:
    response = await client.post(
        URL, json={"repo": REPO, "id": "unrouted", "yaml": fixture_text("unrouted_port.yaml")}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "invalid playbook"
    assert [i["code"] for i in body["issues"]] == ["unrouted_port"]
    assert respx_mock.calls.call_count == 0


async def test_syntax_errors_are_a_422_too(client, respx_mock) -> None:
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": "nodes: [oops"})
    assert response.status_code == 422
    assert response.json()["issues"][0]["code"] == "yaml_syntax"
    assert respx_mock.calls.call_count == 0


async def test_the_body_id_must_match_the_playbook_id(client, respx_mock) -> None:
    response = await client.post(URL, json={"repo": REPO, "id": "other", "yaml": YAML})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "invalid playbook"
    assert [i["code"] for i in body["issues"]] == ["id_mismatch"]
    assert respx_mock.calls.call_count == 0


async def test_an_unknown_repo_is_a_404(client, respx_mock) -> None:
    response = await client.post(URL, json={"repo": "other/repo", "id": "repo-flow", "yaml": YAML})
    assert (response.status_code, response.json()) == (404, {"detail": "unknown repo"})
    assert respx_mock.calls.call_count == 0


async def test_without_github_it_is_a_503() -> None:
    async with playbook_client(make_playbook_app(gh=None)) as client:
        response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
        assert (response.status_code, response.json()) == (503, {"detail": "GitHub not configured"})
        # The order of the checks: a bad id still wins over the missing client.
        bad = await client.post(URL, json={"repo": REPO, "id": "X", "yaml": YAML})
        assert bad.status_code == 422


@pytest.mark.parametrize("base", ["../main", "a..b", "main/", "-x", "has space", "x?y=1", "a#b"])
async def test_a_hostile_base_is_a_422(client, respx_mock, base: str) -> None:
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML, "base": base})
    assert response.status_code == 422
    assert respx_mock.calls.call_count == 0


async def test_a_github_error_is_a_502_with_the_secret_redacted(
    client, respx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_supersecret12345")
    respx_mock.get(f"repos/{REPO}").mock(return_value=Response(200, json={"default_branch": "main"}))
    respx_mock.get(f"repos/{REPO}/pulls").mock(return_value=Response(200, json=[]))
    respx_mock.get(f"repos/{REPO}/git/ref/heads/main").mock(
        return_value=Response(200, json={"object": {"sha": "abc123"}})
    )
    respx_mock.post(f"repos/{REPO}/git/refs").mock(
        return_value=Response(422, json={"message": "Reference exists ghp_supersecret12345"})
    )
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert response.status_code == 502
    assert response.json()["detail"] == "github: 422 Reference exists [redacted]"
    assert "ghp_supersecret12345" not in response.text


async def test_a_transport_failure_is_a_502(client, respx_mock) -> None:
    respx_mock.get(f"repos/{REPO}").mock(side_effect=httpx.ConnectError("down"))
    response = await client.post(URL, json={"repo": REPO, "id": "repo-flow", "yaml": YAML})
    assert response.status_code == 502
    assert response.json()["detail"].startswith("github: 500")
