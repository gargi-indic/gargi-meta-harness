"""Token-cookie auth, the Host guard and the absence of CORS headers (M2.1)."""

from http.cookies import SimpleCookie

import pytest

from harness import __version__
from tests.server.helpers import TOKEN, make_app, make_client

COOKIE = {"Cookie": f"harness_token={TOKEN}"}


async def test_health_requires_the_cookie() -> None:
    async with make_client(make_app()) as client:
        assert (await client.get("/api/health")).status_code == 401
        wrong = await client.get("/api/health", headers={"Cookie": "harness_token=nope"})
        assert wrong.status_code == 401
        assert wrong.json() == {"detail": "unauthorized"}
        ok = await client.get("/api/health", headers=COOKIE)
    assert ok.status_code == 200
    assert ok.json() == {"status": "ok", "version": __version__, "repo_count": 2}


async def test_unknown_api_path_is_404_after_auth() -> None:
    async with make_client(make_app()) as client:
        authed = await client.get("/api/nope", headers=COOKIE)
        anonymous = await client.get("/api/nope")
        bare = await client.post("/api", headers=COOKIE)
    assert authed.status_code == 404
    assert authed.json() == {"detail": "not found"}
    assert anonymous.status_code == 401
    assert bare.status_code == 404


async def test_auth_rejects_bad_tokens_without_a_cookie() -> None:
    async with make_client(make_app()) as client:
        wrong = await client.get("/auth", params={"token": "wrong"})
        missing = await client.get("/auth")
        unicode = await client.get("/auth", params={"token": "töken"})
    for response in (wrong, missing, unicode):
        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden"}
        assert "set-cookie" not in response.headers


async def test_auth_sets_the_session_cookie_and_redirects() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/auth", params={"token": TOKEN})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    morsel = cookie["harness_token"]
    assert morsel.value == TOKEN
    assert morsel["httponly"] is True
    assert morsel["samesite"].lower() == "strict"
    assert morsel["path"] == "/"
    assert not morsel["secure"]
    assert not morsel["max-age"]
    assert not morsel["expires"]


@pytest.mark.parametrize("path", ["/api/health", "/auth", "/"])
async def test_host_guard_rejects_foreign_hosts(path: str) -> None:
    async with make_client(make_app()) as client:
        for host in (
            "evil.example",
            "127.0.0.1:9999",
            "127.0.0.1",
            "localhost",
            "localhost.:7777",
            "[::1]:7777",
            "127.0.0.1:7777, evil.example",
            "127.0.0.1:7777,127.0.0.1:7777",
            "",
        ):
            response = await client.get(path, headers={"Host": host, **COOKIE})
            assert response.status_code == 400, host
            assert response.text == "bad host"


async def test_host_guard_accepts_loopback_names() -> None:
    async with make_client(make_app()) as client:
        for host in ("localhost:7777", "LOCALHOST:7777", "127.0.0.1:7777"):
            response = await client.get("/api/health", headers={"Host": host, **COOKIE})
            assert response.status_code == 200, host


async def test_no_cors_headers_anywhere() -> None:
    async with make_client(make_app()) as client:
        responses = [
            await client.get("/api/health", headers={"Origin": "http://evil.example", **COOKIE}),
            await client.get("/api/health", headers={"Origin": "http://evil.example"}),
            await client.get("/auth", headers={"Origin": "http://evil.example"}),
            await client.get("/", headers={"Origin": "http://evil.example"}),
            await client.options(
                "/api/health",
                headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET", **COOKIE},
            ),
        ]
    for response in responses:
        assert not [name for name in response.headers if name.lower().startswith("access-control-")]


async def test_docs_and_openapi_are_not_served() -> None:
    async with make_client(make_app()) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 503  # static fallback: no web build


async def _raw_request(app, headers: list[tuple[bytes, bytes]], path: str = "/api/health") -> dict:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "server": ("127.0.0.1", 7777),
        "client": ("127.0.0.1", 50000),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return {"status": start["status"], "body": body}


async def test_host_guard_rejects_a_missing_host_header() -> None:
    cookie = (b"cookie", f"harness_token={TOKEN}".encode())
    for path in ("/api/health", "/auth", "/"):
        response = await _raw_request(make_app(), [cookie], path)
        assert response == {"status": 400, "body": b"bad host"}, path


async def test_host_guard_rejects_duplicate_host_headers() -> None:
    cookie = (b"cookie", f"harness_token={TOKEN}".encode())
    host = (b"host", b"127.0.0.1:7777")
    response = await _raw_request(make_app(), [host, (b"host", b"evil.example"), cookie])
    assert response["status"] == 400
    assert (await _raw_request(make_app(), [host, cookie]))["status"] == 200
