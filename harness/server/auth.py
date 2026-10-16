"""Per-launch token auth and the Host guard (DNS-rebinding defence) for the local server.

The guard is a pure ASGI middleware so streaming responses (SSE) pass through untouched. Every request is
checked against the allowed Host values; ``/api/*`` additionally needs the ``harness_token`` cookie.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

COOKIE = "harness_token"

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(candidate: str | None, token: str) -> bool:
    """Constant-time comparison; a missing candidate never matches."""
    if candidate is None:
        return False
    return hmac.compare_digest(candidate.encode(), token.encode())


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class TokenGuard:
    """Reject foreign Host headers on every path and unauthenticated requests on ``/api/*``."""

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app = app
        self.token = token
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        host = request.headers.get("host")
        if host is None or host.lower() not in self.allowed_hosts:
            await PlainTextResponse("bad host", status_code=400)(scope, receive, send)
            return
        if _is_api_path(scope["path"]) and not token_matches(request.cookies.get(COOKIE), self.token):
            await JSONResponse({"detail": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)
