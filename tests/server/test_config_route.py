from tests.fixtures.server_seed import REPO


async def test_agents_enabled_route_requires_auth(anon):
    res = await anon.get(f"/api/agents/enabled?repo={REPO}")
    assert res.status_code == 401


async def test_agents_enabled_unknown_repo(client):
    res = await client.get("/api/agents/enabled?repo=unknown/repo")
    assert res.status_code == 404
    assert res.json()["detail"] == "unknown repo"


async def test_agents_enabled(client):
    res = await client.get(f"/api/agents/enabled?repo={REPO}")
    assert res.status_code == 200
    data = res.json()
    assert "agents" in data

    # Check that at least jules is enabled and present
    agents = data["agents"]
    assert len(agents) >= 1
    names = [a["name"] for a in agents]
    assert "jules" in names

    jules = next(a for a in agents if a["name"] == "jules")
    assert jules["enabled"] is True
