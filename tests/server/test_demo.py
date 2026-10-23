"""Demo mode (M2.10): the scripted run, the in-memory GitHub, the health flag and the CLI guard. No network."""

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from typer.testing import CliRunner

from harness import config as config_module
from harness.cli import app as cli_app
from harness.config import GlobalConfig, RepoEntry, save_global
from harness.server.demo import DemoDriver, DemoGitHub, create_demo_app
from harness.store.repo import get_run, steps_for_run
from tests.server.helpers import make_app, make_client

PORT = 8799
cli = CliRunner()


def demo_client(app, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"Cookie": f"harness_token={token}"},
    )


async def test_driver_reaches_the_merge_gate_then_done_after_approval() -> None:
    app, token = create_demo_app(token="t", port=PORT, interval_s=0)
    driver: DemoDriver = app.state.driver
    store = driver.store

    run_id = driver.start_run(background=False)
    assert run_id == "acme/widgets#7@1"
    assert driver.start_run(background=False) == run_id  # idempotent

    for _ in range(60):
        await driver.step()
        waiting = [
            s for s in steps_for_run(store, run_id) if s.node_id == "merge_gate" and s.status == "waiting"
        ]
        if waiting:
            break
    else:
        pytest.fail("the run never reached the merge gate")
    assert get_run(store, run_id).status == "needs-you"

    async with demo_client(app, token) as client:
        inbox = (await client.get("/api/inbox")).json()
        item = next(i for i in inbox if i["id"] == waiting[0].id)
        assert item["kind"] == "merge_approval"
        response = await client.post(f"/api/inbox/{quote(item['id'], safe='')}/approve", json={})
        assert response.status_code == 200

    for _ in range(60):
        await driver.step()
        if get_run(store, run_id).status == "done":
            break
    assert get_run(store, run_id).status == "done"
    assert [s.outcome for s in steps_for_run(store, run_id) if s.node_id == "merge"] == ["merged"]
    # the seeded runs are left exactly as seeded
    seed = app.state.seed
    assert get_run(store, seed.run_a).status == "needs-you"
    assert get_run(store, seed.run_b).status == "needs-you"
    assert [s.status for s in steps_for_run(store, seed.run_a) if s.node_id == "plan_gate"] == ["waiting"]


async def test_start_run_route_and_health_in_the_demo_app() -> None:
    app, token = create_demo_app(token="t", port=PORT, interval_s=0)
    async with demo_client(app, token) as client:
        health = (await client.get("/api/health")).json()
        assert health["demo"] is True
        assert health["status"] == "ok" and "version" in health and "repo_count" in health
        response = await client.post("/api/demo/start-run")
        assert response.status_code == 200
        assert response.json() == {"run_id": "acme/widgets#7@1"}
    await app.state.driver.stop()


async def test_non_demo_app_has_no_demo_route_and_reports_demo_false() -> None:
    async with make_client(make_app(), headers={"Cookie": "harness_token=test-launch-token"}) as client:
        assert (await client.get("/api/health")).json()["demo"] is False
        assert (await client.post("/api/demo/start-run")).status_code == 404


async def test_demo_github_creates_example_invalid_prs_and_records_them() -> None:
    gh = DemoGitHub()
    first = await gh.create_pull("acme/widgets", "t1", "head", "main")
    second = await gh.create_pull("acme/widgets", "t2", "head", "main")
    assert first["html_url"] == "https://example.invalid/pr/1"
    assert second["html_url"] == "https://example.invalid/pr/2"
    assert [pr["title"] for pr in gh.created_prs] == ["t1", "t2"]


async def test_save_playbook_in_the_demo_app_returns_the_demo_pr() -> None:
    app, token = create_demo_app(token="t", port=PORT, interval_s=0)
    async with demo_client(app, token) as client:
        detail = (await client.get("/api/playbooks/plan-build-review")).json()
        response = await client.post(
            "/api/playbooks/save",
            json={"repo": "acme/widgets", "id": "plan-build-review", "yaml": detail["yaml"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["pr"]["url"] == "https://example.invalid/pr/1"
    assert len(app.state.demo_gh.created_prs) == 1


def test_demo_token_comes_from_the_environment_only_in_the_demo_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DEMO_TOKEN", "from-env")
    assert create_demo_app(port=PORT)[1] == "from-env"
    monkeypatch.delenv("HARNESS_DEMO_TOKEN")
    assert len(create_demo_app(port=PORT)[1]) > 20  # generated


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    monkeypatch.delenv("HARNESS_DEMO_TOKEN", raising=False)
    started: list[object] = []
    monkeypatch.setattr("harness.server.runner.HarnessServer.run", lambda self: started.append(self.config))
    return {"started": started}


def test_demo_boots_without_a_token_file(cli_env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_token() -> str:
        raise RuntimeError("no GitHub token")

    monkeypatch.setattr("harness.cli.github_token", no_token)
    save_global(GlobalConfig(env_files=[]))
    result = cli.invoke(cli_app, ["serve", "--demo", "--no-open", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert len(cli_env["started"]) == 1
    assert cli_env["started"][0].port == 8123
    assert cli_env["started"][0].host == "127.0.0.1"


def test_demo_refuses_when_a_token_and_real_repos_are_configured(
    cli_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harness.cli.github_token", lambda: "ghp_test-token-000000")
    save_global(GlobalConfig(env_files=[], repos=[RepoEntry(name="acme/real")]))
    result = cli.invoke(cli_app, ["serve", "--demo", "--no-open"])
    assert result.exit_code == 1
    assert "refusing to start --demo" in result.output
    assert "ghp_test" not in result.output
    assert cli_env["started"] == []
