"""Local web server (M2.1): app factory, token-cookie auth, static serving and the SSE event bus."""

from harness.server.app import create_app

__all__ = ["create_app"]
