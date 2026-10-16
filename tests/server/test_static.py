"""Static serving with SPA fallback and the 503 when there is no web build (M2.1)."""

from pathlib import Path

import pytest

from harness.server.static import NOT_BUILT, default_web_dist
from tests.server.helpers import TOKEN, make_app, make_client

INDEX = "<html>index</html>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret").write_text("TOP SECRET")
    return root


async def test_serves_index_assets_and_spa_fallback(dist: Path) -> None:
    async with make_client(make_app(web_dist=dist)) as client:
        root = await client.get("/")
        asset = await client.get("/assets/app.js")
        route = await client.get("/runs/abc")
        missing_asset = await client.get("/assets/nope.js")
        directory = await client.get("/assets")
    assert (root.status_code, root.text) == (200, INDEX)
    assert (asset.status_code, asset.text) == (200, "console.log(1)")
    assert (route.status_code, route.text) == (200, INDEX)
    assert missing_asset.text == INDEX
    assert directory.text == INDEX


async def test_traversal_never_leaves_the_dist_directory(dist: Path) -> None:
    async with make_client(make_app(web_dist=dist)) as client:
        for path in ("/../secret", "/%2e%2e/secret", "/..%2fsecret", "/assets/../../secret", "/%00"):
            response = await client.get(path)
            assert "TOP SECRET" not in response.text, path
            assert response.status_code in (200, 400, 404), path


async def test_api_and_auth_are_not_shadowed(dist: Path) -> None:
    async with make_client(make_app(web_dist=dist)) as client:
        api = await client.get("/api/nope", headers={"Cookie": f"harness_token={TOKEN}"})
        health = await client.get("/api/health", headers={"Cookie": f"harness_token={TOKEN}"})
        auth = await client.get("/auth", params={"token": "bad"})
    assert api.status_code == 404
    assert api.json() == {"detail": "not found"}
    assert health.json()["status"] == "ok"
    assert auth.status_code == 403


async def test_without_a_build_every_page_is_503(tmp_path: Path) -> None:
    for web_dist in (None, tmp_path / "missing"):
        async with make_client(make_app(web_dist=web_dist)) as client:
            for path in ("/", "/runs/abc"):
                response = await client.get(path)
                assert response.status_code == 503
                assert response.headers["content-type"].startswith("text/plain")
                assert response.text == NOT_BUILT == "web build not found: run `npm run build` in web/"


def test_default_web_dist_points_at_the_checkout_web_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness

    fake_pkg = tmp_path / "harness"
    fake_pkg.mkdir()
    monkeypatch.setattr(harness, "__file__", str(fake_pkg / "__init__.py"))
    assert default_web_dist() is None
    (tmp_path / "web" / "dist").mkdir(parents=True)
    assert default_web_dist() == (tmp_path / "web" / "dist").resolve()
