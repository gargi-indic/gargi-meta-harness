"""OpenAPI document: behind the cookie, with a snapshot of the path list so contract changes are visible (M2.2)."""

EXPECTED_PATHS = [
    "/api/agents/enabled",
    "/api/board",
    "/api/campaigns",
    "/api/campaigns/{owner}/{repo}/graph",
    "/api/control-requests/{request_id}",
    "/api/events",
    "/api/health",
    "/api/inbox",
    "/api/inbox/{item_id}/approve",
    "/api/inbox/{item_id}/reject",
    "/api/inbox/{item_id}/reroute",
    "/api/playbooks",
    "/api/playbooks/dry-run",
    "/api/playbooks/save",
    "/api/playbooks/schema",
    "/api/playbooks/validate",
    "/api/playbooks/{playbook_id}",
    "/api/runs",
    "/api/runs/{run_id}",
    "/api/runs/{run_id}/activity",
    "/api/runs/{run_id}/cancel",
    "/api/runs/{run_id}/message",
    "/api/runs/{run_id}/nodes/{node}/retry",
    "/api/runs/{run_id}/pause",
    "/api/runs/{run_id}/resume",
    "/auth",
]


async def test_openapi_requires_the_cookie(anon) -> None:
    assert (await anon.get("/api/openapi.json")).status_code == 401


async def test_openapi_path_snapshot(client) -> None:
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200
    assert sorted(response.json()["paths"]) == EXPECTED_PATHS


async def test_docs_pages_stay_off(client) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = await client.get(path)
        assert response.status_code == 503  # the static catch-all: web build not found


async def test_every_schema_field_is_documented(client) -> None:
    schemas = (await client.get("/api/openapi.json")).json()["components"]["schemas"]
    undocumented = [
        f"{name}.{field}"
        for name, schema in schemas.items()
        if "HTTPValidationError" not in name and name != "ValidationError"
        for field, spec in schema.get("properties", {}).items()
        if not spec.get("description")
    ]
    assert undocumented == []
