"""OpenAPI document: behind the cookie, with a snapshot of the path list so contract changes are visible (M2.2)."""

EXPECTED_PATHS = [
    "/api/board",
    "/api/campaigns",
    "/api/campaigns/{owner}/{repo}/graph",
    "/api/events",
    "/api/health",
    "/api/inbox",
    "/api/runs",
    "/api/runs/{run_id}",
    "/api/runs/{run_id}/activity",
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
