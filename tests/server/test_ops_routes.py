import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_ops_routes(client: AsyncClient):
    resp = await client.get("/api/ops/status")
    assert resp.status_code == 200
    assert "halted" in resp.json()
    assert "budget" in resp.json()

    # Kill (422 test)
    resp = await client.post("/api/ops/kill", json={"confirm": "not kill"})
    assert resp.status_code == 422

    # Kill
    resp = await client.post("/api/ops/kill", json={"confirm": "KILL"})
    assert resp.status_code == 200
    assert "errors" in resp.json()

    # Resume
    resp = await client.post("/api/ops/resume")
    assert resp.status_code == 200

    # Budget
    resp = await client.post("/api/ops/budget", json={"daily_usd": 40.0})
    assert resp.status_code == 200

    # Budget clear
    resp = await client.post("/api/ops/budget", json={"daily_usd": None})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ops_routes_unauth(anon: AsyncClient):
    assert (await anon.get("/api/ops/status")).status_code == 401
    assert (await anon.post("/api/ops/kill", json={"confirm": "KILL"})).status_code == 401
    assert (await anon.post("/api/ops/resume")).status_code == 401
    assert (await anon.post("/api/ops/budget", json={"daily_usd": 40.0})).status_code == 401
