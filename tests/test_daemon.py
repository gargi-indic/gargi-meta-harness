"""The daemon (M1.11): lock first, concurrent loops, isolated failures, clean stop. GitHub is an in-memory stand-in."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from harness import config as config_module
from harness.adapters.base import AdapterRegistry
from harness.app import App, build_app
from harness.cli import app as cli_app
from harness.config import GlobalConfig, RepoConfig, RepoEntry, save_global
from harness.daemon import run_daemon
from harness.github.ledger import is_ledger, parse_ledger, render_ledger
from harness.llm.backend import FakeBackend
from harness.lock import LockHeld, iso
from harness.store.db import Store
from harness.store.repo import upsert_repo

T0 = 1_800_000_000.0
FAST = {"sync_every": 0.01, "commands_every": 0.01, "heartbeat_every": 0.01, "idle": 0.005}
SLOW = {"sync_every": 3600, "commands_every": 3600, "heartbeat_every": 3600, "idle": 0.005}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class DaemonGH:
    """The GitHub calls the daemon makes with no runs and no issues: lock comments, sync reads, command polls."""

    def __init__(self, config_text: str | None = None) -> None:
        self.comments_by: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.config_text = config_text
        self.fail_repo = 0
        self.repo_calls = 0
        self.closed = False

    async def comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [dict(c) for c in self.comments_by.get((repo, number), [])]

    async def add_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        lst = self.comments_by.setdefault((repo, number), [])
        comment = {
            "id": 1000 + sum(len(v) for v in self.comments_by.values()),
            "body": body,
            "created_at": "x",
        }
        lst.append(comment)
        return dict(comment)

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        for lst in self.comments_by.values():
            for comment in lst:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return dict(comment)
        raise KeyError(comment_id)

    async def paginate(self, path: str, **params: Any):
        return
        yield

    async def repo(self, repo: str) -> dict[str, Any]:
        self.repo_calls += 1
        if self.fail_repo > 0:
            self.fail_repo -= 1
            raise RuntimeError("GitHub is down")
        return {"default_branch": "main"}

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str | None:
        return self.config_text

    async def issues(self, repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def aclose(self) -> None:
        self.closed = True

    def lock(self, repo: str = "o/r", number: int = 1) -> dict[str, Any] | None:
        for comment in self.comments_by.get((repo, number), []):
            if is_ledger(comment["body"]):
                snap = parse_ledger(comment["body"]) or {}
                return snap.get("lock")
        return None


def make_app(
    tmp_path: Path, name: str, gh: DaemonGH, now: float = T0, repos: dict[str, int | None] | None = None
) -> App:
    repos = {"o/r": 1} if repos is None else repos
    cfg = GlobalConfig(data_dir=tmp_path / name, env_files=[], repos=[RepoEntry(name=repo) for repo in repos])
    application = build_app(
        cfg,
        gh=gh,
        adapters=AdapterRegistry(),
        review_backend=FakeBackend([]),
        clock=lambda: now,  # type: ignore[arg-type]
    )
    for repo, control in repos.items():
        config = RepoConfig(control_issue=control)
        upsert_repo(application.store, repo, "main", config.model_dump())
        application.set_repo_config(repo, config)
    return application


def events(application: App, kind: str | None = None) -> list[dict[str, Any]]:
    """Events from a fresh connection: run_daemon closes the app's own store when it stops."""
    store = Store(application.cfg.db_path)
    try:
        rows = store.query("SELECT * FROM events ORDER BY id")
    finally:
        store.close()
    out = [{**r, "data": json.loads(r["data_json"])} for r in rows]
    return [e for e in out if kind is None or e["kind"] == kind]


async def until(condition: Callable[[], bool | Awaitable[bool]], timeout: float = 2.0) -> None:
    async def poll() -> None:
        while True:
            result = condition()
            if (await result) if isinstance(result, Awaitable) else result:
                return
            await asyncio.sleep(0.002)

    await asyncio.wait_for(poll(), timeout)


def start(
    application: App, stop: asyncio.Event, host: str, pid: int = 1, **kwargs: Any
) -> asyncio.Task[None]:
    return asyncio.create_task(run_daemon(application, stop, host=host, pid=pid, **{**SLOW, **kwargs}))


async def test_a_second_daemon_refuses_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    gh = DaemonGH()
    first, second = make_app(tmp_path, "one", gh), make_app(tmp_path, "two", gh, now=T0 + 10)
    stop1 = asyncio.Event()
    task = start(first, stop1, "host-a")
    await until(lambda: gh.lock() is not None)

    with pytest.raises(LockHeld) as info:
        await run_daemon(second, asyncio.Event(), host="host-b", pid=2, **SLOW)
    assert info.value.holder["host"] == "host-a"
    assert gh.lock()["host"] == "host-a"  # type: ignore[index]
    assert not events(second, "daemon_started")  # nothing was started

    stop1.set()
    await asyncio.wait_for(task, 2)


async def test_force_unlock_takes_over_and_the_old_daemon_steps_down(tmp_path: Path) -> None:
    gh = DaemonGH()
    first, second = make_app(tmp_path, "one", gh), make_app(tmp_path, "two", gh, now=T0 + 10)
    task1 = start(first, asyncio.Event(), "host-a", heartbeat_every=0.01)
    await until(lambda: gh.lock() is not None)

    stop2 = asyncio.Event()
    task2 = start(second, stop2, "host-b", pid=2, force_unlock=True)
    await until(lambda: (gh.lock() or {}).get("host") == "host-b")
    # The first daemon notices at its next heartbeat that the lock is gone and stops by itself.
    await asyncio.wait_for(task1, 2)
    lost = events(first, "lock_lost")
    assert lost and lost[0]["data"]["holder"] == "host-b:2"
    assert (gh.lock() or {}).get("host") == "host-b"  # the old daemon released nothing of ours

    stop2.set()
    await asyncio.wait_for(task2, 2)
    assert gh.lock() is None


async def test_stop_ends_every_loop_and_releases_the_lock(tmp_path: Path) -> None:
    gh = DaemonGH()
    application = make_app(tmp_path, "one", gh)
    stop = asyncio.Event()
    others = asyncio.all_tasks()
    task = start(application, stop, "host-a", **FAST)
    await until(lambda: gh.lock() is not None and gh.repo_calls >= 2)  # sync ran more than once
    heartbeats = [(gh.lock() or {}).get("heartbeat")]
    assert heartbeats[0] == iso(T0)

    stop.set()
    await asyncio.wait_for(task, 2)
    assert asyncio.all_tasks() == others  # no loop is left behind
    assert gh.lock() is None and gh.closed
    kinds = [e["kind"] for e in events(application)]
    assert kinds[0] == "daemon_started" and kinds[-1] == "daemon_stopped"
    # the ledger comment stays (other snapshot keys live there); only the lock is gone
    assert parse_ledger(gh.comments_by[("o/r", 1)][0]["body"]) == {}


async def test_heartbeats_refresh_the_lock(tmp_path: Path) -> None:
    gh = DaemonGH()
    clock = [T0]
    application = make_app(tmp_path, "one", gh)
    application.clock = lambda: clock[0]
    stop = asyncio.Event()
    task = start(application, stop, "host-a", heartbeat_every=0.01)
    await until(lambda: gh.lock() is not None)
    clock[0] = T0 + 300
    await until(lambda: (gh.lock() or {}).get("heartbeat") == iso(T0 + 300))
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_a_failing_periodic_job_is_recorded_and_the_daemon_continues(tmp_path: Path) -> None:
    gh = DaemonGH()
    gh.fail_repo = 3
    application = make_app(tmp_path, "one", gh)
    stop = asyncio.Event()
    task = start(application, stop, "host-a", sync_every=0.01, heartbeat_every=0.01)
    await until(lambda: gh.repo_calls >= 5)  # the sync job kept going after three failures
    assert not task.done()
    assert (gh.lock() or {}).get("host") == "host-a"  # other jobs are unaffected
    stop.set()
    await asyncio.wait_for(task, 2)

    errors = events(application, "daemon_error")
    assert len(errors) == 3
    assert {e["data"]["job"] for e in errors} == {"sync"}
    assert errors[0]["repo"] == "o/r" and "GitHub is down" in errors[0]["data"]["error"]
    assert gh.lock() is None


async def test_a_failing_heartbeat_is_recorded_without_stopping(tmp_path: Path) -> None:
    gh = DaemonGH()
    application = make_app(tmp_path, "one", gh)
    stop = asyncio.Event()
    task = start(application, stop, "host-a", heartbeat_every=0.01)
    await until(lambda: gh.lock() is not None)
    original = gh.edit_comment
    calls = {"n": 0}

    async def flaky(repo: str, comment_id: int, body: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("write failed")
        return await original(repo, comment_id, body)

    gh.edit_comment = flaky  # type: ignore[method-assign]
    await until(lambda: calls["n"] >= 4)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert [e["data"]["job"] for e in events(application, "daemon_error")][:2] == ["heartbeat", "heartbeat"]


async def test_a_repo_without_a_control_issue_runs_unlocked_with_a_warning(tmp_path: Path) -> None:
    gh = DaemonGH()
    application = make_app(tmp_path, "one", gh, repos={"o/r": None})
    stop = asyncio.Event()
    task = start(application, stop, "host-a")
    await until(lambda: any(e["kind"] == "daemon_started" for e in events(application)))
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
    warnings = events(application, "daemon_warning")
    assert (
        len(warnings) == 1 and warnings[0]["repo"] == "o/r" and "without a run lock" in json.dumps(warnings)
    )
    assert gh.comments_by == {}  # no lock comment was written anywhere


async def test_a_held_repo_releases_the_locks_taken_before_it(tmp_path: Path) -> None:
    gh = DaemonGH()
    gh.comments_by[("o/b", 2)] = [
        {
            "id": 1,
            "created_at": "x",
            "body": render_ledger(
                {"lock": {"host": "other", "pid": 9, "token": "t", "since": iso(T0), "heartbeat": iso(T0)}},
                "x",
            ),
        }
    ]
    application = make_app(tmp_path, "one", gh, repos={"o/a": 1, "o/b": 2})
    with pytest.raises(LockHeld):
        await run_daemon(application, asyncio.Event(), host="me", pid=1, **SLOW)
    assert gh.lock("o/a", 1) is None  # o/a was locked first, then given back
    assert gh.lock("o/b", 2)["host"] == "other"  # type: ignore[index]
    assert gh.closed


async def test_an_unsynced_repo_is_synced_first_to_learn_its_control_issue(tmp_path: Path) -> None:
    gh = DaemonGH(config_text="control_issue: 5\n")
    cfg = GlobalConfig(data_dir=tmp_path / "one", env_files=[], repos=[RepoEntry(name="o/r")])
    application = build_app(
        cfg,
        gh=gh,
        adapters=AdapterRegistry(),
        review_backend=FakeBackend([]),
        clock=lambda: T0,  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    task = start(application, stop, "host-a")
    await until(lambda: gh.lock("o/r", 5) is not None)
    stop.set()
    await asyncio.wait_for(task, 2)


# ---- the CLI command --------------------------------------------------------------------------------------


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GlobalConfig:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name="o/r")])
    save_global(cfg)
    return cfg


def test_cli_daemon_without_repos_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    result = CliRunner().invoke(cli_app, ["daemon"])
    assert result.exit_code == 1
    assert "no repos registered" in ANSI.sub("", result.output)


def test_cli_daemon_refuses_when_the_lock_is_held(
    cli_home: GlobalConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = DaemonGH()
    gh.comments_by[("o/r", 1)] = [
        {
            "id": 1,
            "created_at": "x",
            "body": render_ledger(
                {
                    "lock": {
                        "host": "mac-mini",
                        "pid": 4242,
                        "token": "t",
                        "since": iso(T0),
                        "heartbeat": iso(T0),
                    }
                },
                "x",
            ),
        }
    ]

    def factory(cfg: GlobalConfig, **kwargs: Any) -> App:
        application = build_app(
            cfg,
            gh=gh,
            adapters=AdapterRegistry(),
            review_backend=FakeBackend([]),
            clock=lambda: T0 + 5,  # type: ignore[arg-type]
        )
        config = RepoConfig(control_issue=1)
        upsert_repo(application.store, "o/r", "main", config.model_dump())
        application.set_repo_config("o/r", config)
        return application

    monkeypatch.setattr("harness.cli.build_app", factory)
    result = CliRunner().invoke(cli_app, ["daemon"])
    out = ANSI.sub("", result.output)
    assert result.exit_code == 1
    assert "mac-mini:4242" in out and "--force-unlock" in out
    assert "daemon stopped" not in out
    assert gh.lock()["host"] == "mac-mini"  # type: ignore[index]


@pytest.mark.parametrize(("args", "forced"), [(["daemon"], False), (["daemon", "--force-unlock"], True)])
def test_cli_daemon_passes_force_unlock_on(
    cli_home: GlobalConfig, monkeypatch: pytest.MonkeyPatch, args: list[str], forced: bool
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run_daemon(application: App, stop: asyncio.Event, **kwargs: Any) -> None:
        seen.update(kwargs)
        stop.set()
        await application.aclose()

    monkeypatch.setattr("harness.cli.run_daemon", fake_run_daemon)
    monkeypatch.setattr(
        "harness.cli.build_app",
        lambda cfg, **kw: build_app(
            cfg, gh=DaemonGH(), adapters=AdapterRegistry(), review_backend=FakeBackend([])
        ),  # type: ignore[arg-type]
    )
    result = CliRunner().invoke(cli_app, args)
    assert result.exit_code == 0, result.output
    assert seen["force_unlock"] is forced and seen["pid"] > 0 and seen["host"]
    assert "daemon stopped" in ANSI.sub("", result.output)
