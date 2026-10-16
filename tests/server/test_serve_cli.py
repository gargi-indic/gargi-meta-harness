"""`harness serve`: loopback-only config, flag handling and the printed login URL (M2.1)."""

import asyncio
import re
import signal
from pathlib import Path

import pytest
import uvicorn
from typer.testing import CliRunner

from harness import config as config_module
from harness.cli import app, build_server_config
from harness.config import GlobalConfig, save_global
from harness.server.events import EventBus
from harness.server.runner import HarnessServer
from tests.server.helpers import make_app

cli = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PLAIN_ENV = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/auth\?token=(\S+)")


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """`Server.run` runs only the (patched) startup, as if the socket bound, or fails to bind."""
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    save_global(GlobalConfig(port=8123, data_dir=tmp_path / "data", env_files=[]))
    state: dict = {"runs": [], "opened": [], "bound": True}

    async def fake_startup(self: uvicorn.Server, sockets=None) -> None:
        self.started = state["bound"]

    def fake_run(self: uvicorn.Server) -> None:
        state["runs"].append(self.config)
        asyncio.run(self.startup())

    monkeypatch.setattr(uvicorn.Server, "startup", fake_startup)
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: state["opened"].append(url) or True)
    return state


def test_serve_help() -> None:
    result = cli.invoke(app, ["serve", "--help"], env=PLAIN_ENV)
    assert result.exit_code == 0
    output = ANSI_RE.sub("", result.output)
    assert "--no-open" in output
    assert "--host" not in output


def test_server_config_binds_loopback_only() -> None:
    config = build_server_config(make_app(), 7777)
    assert config.host == "127.0.0.1"
    assert config.port == 7777
    assert config.access_log is False  # the access log would print GET /auth?token=<token>
    assert config.timeout_graceful_shutdown == 3


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


def test_a_failed_bind_neither_prints_the_token_nor_opens_the_browser(served: dict) -> None:
    served["bound"] = False
    result = cli.invoke(app, ["serve"])
    assert URL_RE.search(result.output) is None
    assert served["opened"] == []
    assert len(served["runs"]) == 1


async def test_handle_exit_closes_the_bus_so_open_streams_end() -> None:
    bus = EventBus()
    server = HarnessServer(build_server_config(make_app(bus=bus), 7777), bus=bus, on_started=lambda: None)
    stream = bus.subscribe(None)
    waiter = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    server.handle_exit(signal.SIGINT, None)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiter, 2.0)
    assert server.should_exit is True


WARNING = "GitHub not configured: repo playbooks and save are disabled"


def test_serve_without_a_github_token_warns_and_still_starts(
    served: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_token() -> str:
        raise RuntimeError("no GitHub token: set GH_TOKEN or run `gh auth login`")

    monkeypatch.setattr("harness.cli.github_token", no_token)
    result = cli.invoke(app, ["serve", "--no-open"])
    assert result.exit_code == 0, result.output
    assert result.output.count(WARNING) == 1
    assert URL_RE.search(result.output)
    assert len(served["runs"]) == 1
    assert served["runs"][0].app.state.gh is None


def test_serve_with_a_token_builds_the_client_and_never_prints_it(
    served: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harness.cli.github_token", lambda: "ghp_secret-token-123456")
    result = cli.invoke(app, ["serve", "--no-open"])
    assert result.exit_code == 0, result.output
    assert WARNING not in result.output
    assert "ghp_secret-token-123456" not in result.output
    gh = served["runs"][0].app.state.gh
    assert gh is not None
    assert "ghp_secret-token-123456" not in repr(gh)
    assert type(gh._cache).__name__ == "SqliteEtagCache"
