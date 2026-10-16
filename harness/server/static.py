"""Static serving of the built web UI with SPA fallback to ``index.html``."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, Response

import harness

NOT_BUILT = "web build not found: run `npm run build` in web/"


def default_web_dist() -> Path | None:
    dist = Path(harness.__file__).resolve().parent.parent / "web" / "dist"
    return dist if dist.is_dir() else None


def _resolve_file(root: Path, rel_path: str) -> Path | None:
    """The file ``rel_path`` names inside ``root``, or None (missing, a directory, or outside ``root``)."""
    try:
        candidate = (root / rel_path).resolve()
    except (OSError, ValueError):
        return None
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None


def register_static(app: FastAPI, web_dist: Path | None) -> None:
    """Add the catch-all GET route. Register it last: ``/auth`` and ``/api/*`` are declared before it."""
    root = web_dist.resolve() if web_dist is not None and web_dist.is_dir() else None

    @app.get("/{path:path}", include_in_schema=False)
    async def static_files(path: str) -> Response:
        if root is None:
            return PlainTextResponse(NOT_BUILT, status_code=503)
        file = _resolve_file(root, path) if path else None
        if file is None:
            file = _resolve_file(root, "index.html")
        if file is None:
            return PlainTextResponse(NOT_BUILT, status_code=503)
        return FileResponse(file)
