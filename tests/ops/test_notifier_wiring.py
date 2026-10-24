"""Config round trip, notify executor idempotency, notify-test CLI, daemon job isolation, serve lifespan flag."""

import asyncio
import stat
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harness import cli as cli_module
from harness import config as config_module
from harness.config import GlobalConfig, NotificationsConfig, load_global, save_global
from harness.daemon import run_daemon
from harness.ops.notifier import NotificationService, NullNotifier, RecordingNotifier
from harness.server import create_app
from harness.store.db import Store
from tests.test_daemon import SLOW, DaemonGH, events, make_app, until


def test_config_round_trip_and_defaults(tmp_path: Path):
    assert GlobalConfig().notifications == NotificationsConfig()
    assert NotificationsConfig().enabled is False
    cfg = GlobalConfig(
        data_dir=tmp_path,
        env_files=[],
        notifications=NotificationsConfig(
            enabled=True,
            kinds=["gate", "failure"],
            quiet_hours="22:00-08:00",
            backend="macos",
            dedupe_window_s=5,
        ),
    )
    path = save_global(cfg, tmp_path / "config.toml")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_global(path) == cfg
    assert "[notifications]" in path.read_text()
    default = save_global(GlobalConfig(data_dir=tmp_path, env_files=[]), tmp_path / "d.toml")
    assert load_global(default).notifications == NotificationsConfig()


def test_notifications_table_comes_before_repos(tmp_path: Path):
    from harness.config import RepoEntry

    cfg = GlobalConfig(data_dir=tmp_path, env_files=[], repos=[RepoEntry(name="o/r")])
    path = save_global(cfg, tmp_path / "c.toml")
    text = path.read_text()
    assert text.index("[notifications]") < text.index("[[repos]]")
    assert load_global(path) == cfg


def test_notify_test_command_uses_the_configured_factory(tmp_path: Path, monkeypatch):
    path = save_global(GlobalConfig(data_dir=tmp_path, env_files=[]), tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path)
    monkeypatch.setattr(cli_module, "load_global", lambda: load_global(path))
    rec = RecordingNotifier()
    backends: list[str] = []

    def factory(backend: str):
        backends.append(backend)
        return rec

    monkeypatch.setattr(cli_module, "make_notifier", factory)
    result = CliRunner().invoke(cli_module.app, ["notify-test", "--kind", "gate"])
    assert result.exit_code == 0, result.output
    assert "sent via RecordingNotifier" in result.output
    assert backends == ["auto"]  # works with notifications.enabled = false
    (n,) = rec.sent
    assert (n.title, n.body, n.kind) == ("Gargi Harness", "Test notification", "gate")
    assert n.key.startswith("test:")


def test_notify_test_command_on_linux_says_null(tmp_path: Path, monkeypatch):
    path = save_global(GlobalConfig(data_dir=tmp_path, env_files=[]), tmp_path / "config.toml")
    monkeypatch.setattr(cli_module, "load_global", lambda: load_global(path))
    monkeypatch.setattr(cli_module, "make_notifier", lambda backend: NullNotifier())
    result = CliRunner().invoke(cli_module.app, ["notify-test"])
    assert "sent via NullNotifier" in result.output


async def test_a_failing_notification_job_is_recorded_and_the_daemon_continues(tmp_path: Path, monkeypatch):
    calls = 0

    async def boom(self) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(NotificationService, "poll_once", boom)
    gh = DaemonGH()
    application = make_app(tmp_path, "one", gh)
    application.cfg.notifications.enabled = True
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_daemon(application, stop, host="h", pid=1, **{**SLOW, "heartbeat_every": 0.01}, notify_every=0.01)
    )
    await until(lambda: calls >= 3)
    assert not task.done()
    assert (gh.lock() or {}).get("host") == "h"
    stop.set()
    await asyncio.wait_for(task, 2)
    errors = events(application, "daemon_error")
    assert errors and {e["data"]["job"] for e in errors} == {"notifications"}
    assert "notifier exploded" in errors[0]["data"]["error"]


def _web(tmp_path: Path, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    cfg = GlobalConfig(data_dir=tmp_path, env_files=[], notifications=NotificationsConfig(enabled=True))
    return create_app(store=store, config=cfg, token="t", port=7777, tail_events=False, **kwargs), store


def test_serve_lifespan_starts_the_service_only_when_flagged(tmp_path: Path):
    app, store = _web(tmp_path / "a")
    with TestClient(app):
        assert app.state.notify_task is None  # default: existing behaviour, no task
    store.close()

    app, store = _web(tmp_path / "b", notifications=True, notification_interval_s=0.01)
    with TestClient(app):
        task = app.state.notify_task
        assert task is not None and not task.done()
    assert task.cancelled() or task.done()
    store.close()


def test_serve_lifespan_flag_needs_enabled_config(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    app = create_app(
        store=store,
        config=GlobalConfig(data_dir=tmp_path, env_files=[]),
        token="t",
        port=7777,
        tail_events=False,
        notifications=True,
    )
    with TestClient(app):
        assert app.state.notify_task is None
    store.close()
