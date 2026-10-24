"""GET /api/planner/plan and /options, and the guards every planner route shares (M4.5)."""

import pytest

from harness.playbook.load import builtin_playbooks
from tests.server.helpers import make_client
from tests.server.planner_helpers import CODEX, JULES, PLAN_PATH, PLAN_TEXT, REPO, Hub, make_rig
from tests.server.playbooks_helpers import fixture_text

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)


@pytest.fixture
async def rig(respx_mock):
    r = make_rig(respx_mock)
    yield r
    await r.aclose()


async def test_the_plan_is_loaded_with_its_approval_pending(rig) -> None:
    response = await rig.client.get("/api/planner/plan", params={"repo": REPO, "path": PLAN_PATH})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo": REPO,
        "path": PLAN_PATH,
        "issue": None,
        "ref": None,
        "sha": rig.hub.plan_sha(),
        "title": "The plan",
        "markdown": PLAN_TEXT,
        "headings": ["The plan", "Steps"],
        "approval": {"status": "pending", "via": None, "approved_by": None, "note": None},
    }


async def test_an_approved_plan_names_the_approver_and_a_changed_plan_is_stale(rig) -> None:
    rig.hub.approve_plan()
    approved = (await rig.client.get("/api/planner/plan", params={"repo": REPO, "path": PLAN_PATH})).json()
    assert approved["approval"] == {"status": "approved", "via": "cli", "approved_by": "acme", "note": None}
    rig.hub.put_plan(PLAN_TEXT + "- two\n", version=2)
    stale = (await rig.client.get("/api/planner/plan", params={"repo": REPO, "path": PLAN_PATH})).json()
    assert stale["approval"]["status"] == "stale" and stale["sha"] == rig.hub.plan_sha()


async def test_an_issue_can_be_the_plan(rig) -> None:
    issue = rig.hub._add_issue("Plan issue", "# Plan issue\n\n## Goal\n", [])
    response = await rig.client.get("/api/planner/plan", params={"repo": REPO, "issue": issue["number"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] is None and body["issue"] == issue["number"] and body["title"] == "Plan issue"
    assert body["approval"]["status"] == "pending"


async def test_no_control_issue_means_no_approval(respx_mock) -> None:
    rig = make_rig(respx_mock, control_issue=None)
    try:
        response = await rig.client.get("/api/planner/plan", params={"repo": REPO, "path": PLAN_PATH})
        assert response.status_code == 200 and response.json()["approval"] is None
    finally:
        await rig.aclose()


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"repo": REPO}, 422),
        ({"repo": REPO, "path": PLAN_PATH, "issue": 3}, 422),
        ({"repo": REPO, "path": "../secrets"}, 422),
        ({"repo": REPO, "path": "/etc/passwd"}, 422),
        ({"repo": REPO, "path": "docs/NOPE.md"}, 404),
        ({"repo": REPO, "issue": 99}, 404),
        ({"repo": "other/repo", "path": PLAN_PATH}, 404),
    ],
)
async def test_bad_plan_sources(rig, params, status) -> None:
    assert (await rig.client.get("/api/planner/plan", params=params)).status_code == status


async def test_options_reflect_the_enabled_agents_their_caps_and_the_playbooks(respx_mock) -> None:
    hub = Hub(respx_mock).install()
    hub.put_plan()
    hub.repo_playbooks["repo-flow.yaml"] = fixture_text("repo_playbook.yaml")
    rig = make_rig(respx_mock, hub=hub, agent_caps={"jules": JULES, "codex": CODEX})
    try:
        response = await rig.client.get("/api/planner/options", params={"repo": REPO})
        assert response.status_code == 200, response.text
        body = response.json()
        # codex is switched off by the repo config, jules is on; both keep their capabilities.
        assert body["agents"] == [
            {"name": "codex", "enabled": False, "caps": ["local"]},
            {"name": "jules", "enabled": True, "caps": ["message", "plan", "pr"]},
        ]
        assert body["playbooks"] == sorted({*builtin_playbooks(), "repo-flow"})
        assert (await rig.client.get("/api/planner/options", params={"repo": "x/y"})).status_code == 404
    finally:
        await rig.aclose()


async def test_options_list_agents_the_config_names_but_the_server_does_not_know(respx_mock) -> None:
    rig = make_rig(respx_mock, agent_caps={"jules": JULES})
    try:
        body = (await rig.client.get("/api/planner/options", params={"repo": REPO})).json()
        assert {"name": "codex", "enabled": False, "caps": []} in body["agents"]
    finally:
        await rig.aclose()


ROUTES = [
    ("GET", "/api/planner/plan?repo=acme/repo0&path=docs/PLAN.md"),
    ("GET", "/api/planner/options?repo=acme/repo0"),
    ("POST", "/api/planner/decompose"),
    ("GET", "/api/planner/proposals/acme~repo0~0123456789ab"),
    ("PUT", "/api/planner/proposals/acme~repo0~0123456789ab"),
    ("POST", "/api/planner/proposals/acme~repo0~0123456789ab/validate"),
    ("POST", "/api/planner/proposals/acme~repo0~0123456789ab/approve"),
    ("GET", "/api/planner/jobs/acme~repo0~0123456789ab.create"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_needs_the_cookie(rig, method, path) -> None:
    async with make_client(rig.app) as anon:
        assert (await anon.request(method, path)).status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_without_github_the_routes_answer_503(method, path) -> None:
    from tests.server.helpers import AUTH, make_app

    async with make_client(make_app(), headers=AUTH) as client:
        response = await client.request(method, path, json={})
        assert response.status_code in (503, 422), response.text
        if response.status_code == 422:  # body validation runs before the dependency for POST decompose
            assert "planner" not in response.text


def test_the_default_agents_are_those_with_a_key_in_the_environment() -> None:
    from harness.server.planner_service import KNOWN_AGENT_CAPS, default_agent_caps

    assert default_agent_caps({}) == {}
    assert set(default_agent_caps({"JULES_API_KEY": "x", "CODEX_API_KEY": "y", "OTHER": "z"})) == {
        "jules",
        "codex",
    }
    assert default_agent_caps({"ANTHROPIC_API_KEY": "k"}) == {"claude": KNOWN_AGENT_CAPS["claude"]}
