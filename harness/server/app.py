"""App factory for the local web server: auth, health, SSE and static serving. No domain routes yet."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from harness import __version__
from harness.config import GlobalConfig
from harness.server.auth import COOKIE, TokenGuard, generate_token, token_matches
from harness.server.deps import get_bus, get_config
from harness.server.events import EventBus, EventLogTailer, parse_last_event_id, sse_stream
from harness.server.static import register_static
from harness.store.db import Store

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(
    *,
    store: Store,
    config: GlobalConfig,
    token: str | None = None,
    web_dist: Path | None = None,
    port: int | None = None,
    bus: EventBus | None = None,
    tail_events: bool = True,
    keepalive_s: float = 15.0,
    tail_interval_s: float = 0.5,
) -> FastAPI:
    token = token if token is not None else generate_token()
    port = port if port is not None else config.port
    bus = bus if bus is not None else EventBus()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if tail_events:
            task = asyncio.create_task(EventLogTailer(store, bus, interval_s=tail_interval_s).run())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            bus.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.config = config
    app.state.bus = bus
    app.state.token = token
    app.state.port = port
    app.state.keepalive_s = keepalive_s
    app.add_middleware(TokenGuard, token=token, port=port)

    @app.get("/auth")
    async def auth(request: Request) -> Response:
        if not token_matches(request.query_params.get("token"), token):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/health")
    async def health(cfg: Annotated[GlobalConfig, Depends(get_config)]) -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "repo_count": len(cfg.repos)}

    @app.get("/api/events")
    async def events(request: Request, event_bus: Annotated[EventBus, Depends(get_bus)]) -> StreamingResponse:
        last_event_id = parse_last_event_id(request.headers.get("last-event-id"), event_bus.last_seq)
        stream = sse_stream(event_bus, last_event_id, keepalive_s=request.app.state.keepalive_s)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.api_route("/api", methods=_ALL_METHODS, include_in_schema=False)
    @app.api_route("/api/{rest:path}", methods=_ALL_METHODS, include_in_schema=False)
    async def api_not_found(rest: str = "") -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    register_static(app, web_dist)
    return app
