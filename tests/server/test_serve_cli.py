"""`harness serve`: loopback-only config, flag handling and the printed login URL (M2.1)."""

import re
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from harness import config as config_module
from harness.cli import app, build_server_config
from harness.config import GlobalConfig, save_global
from tests.server.helpers import make_app

cli = CliRunner()
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/auth\?token=(\S+)")


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    save_global(GlobalConfig(port=8123, data_dir=tmp_path / "data", env_files=[]))
    state: dict = {"runs": [], "opened": []}

    def fake_run(self: uvicorn.Server) -> None:
        state["runs"].append(self.config)

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: state["opened"].append(url) or True)
    return state


def test_serve_help() -> None:
    result = cli.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--no-open" in result.output
    assert "--host" not in result.output


def test_server_config_binds_loopback_only() -> None:
    config = build_server_config(make_app(), 7777)
    assert config.host == "127.0.0.1"
    assert config.port == 7777


def test_with_daemon_is_rejected(served: dict) -> None:
    result = cli.invoke(app, ["serve", "--with-daemon"])
    assert result.exit_code == 2
    assert "--with-daemon is not implemented yet; run `harness daemon` separately" in result.output
    assert served["runs"] == []


def test_no_open_prints_the_url_and_starts_the_server(served: dict, tmp_path: Path) -> None:
    result = cli.invoke(app, ["serve", "--no-open"])
    assert result.exit_code == 0, result.output
    match = URL_RE.search(result.output)
    assert match and match.group(1) == "8123"
    assert served["opened"] == []
    assert len(served["runs"]) == 1
    assert served["runs"][0].host == "127.0.0.1"
    assert served["runs"][0].port == 8123
    assert (tmp_path / "data" / "harness.db").exists()


def test_without_no_open_the_browser_gets_the_same_url(served: dict) -> None:
    result = cli.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    match = URL_RE.search(result.output)
    assert match
    assert served["opened"] == [match.group(0)]
    assert len(served["runs"]) == 1


def test_port_option_overrides_the_config(served: dict) -> None:
    result = cli.invoke(app, ["serve", "--no-open", "--port", "9001"])
    assert result.exit_code == 0, result.output
    match = URL_RE.search(result.output)
    assert match and match.group(1) == "9001"
    assert served["runs"][0].port == 9001
    app_state = served["runs"][0].app.state
    assert app_state.port == 9001
    assert app_state.token == match.group(2)
