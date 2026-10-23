"""The daemon's opt-in dispatch job (M3.6): off by default, one run per ready issue when switched on."""

import asyncio
from pathlib import Path

from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.app import App, build_app
from harness.config import AgentConfig, GlobalConfig, RepoConfig, RepoEntry
from harness.llm.backend import FakeBackend
from harness.store.db import Store
from harness.store.repo import IssueRow, list_runs, upsert_issue, upsert_repo
from tests.test_daemon import T0, DaemonGH, start, until


def make_dispatch_app(tmp_path: Path, gh: DaemonGH) -> App:
    cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name="o/r")])
    adapters = AdapterRegistry()
    adapters.register(FakeAdapter())
    application = build_app(
        cfg,
        gh=gh,
        adapters=adapters,
        review_backend=FakeBackend([]),
        clock=lambda: T0,  # type: ignore[arg-type]
    )
    config = RepoConfig(control_issue=1, default_agent="fake", agents={"fake": AgentConfig()})
    upsert_repo(application.store, "o/r", "main", config.model_dump())
    application.set_repo_config("o/r", config)
    upsert_issue(
        application.store,
        IssueRow(
            repo="o/r",
            number=7,
            id=7,
            title="Ready issue",
            body="",
            state="open",
            labels=["agent:fake"],
            parent=None,
            blocked_by=[],
            updated_at="2023-01-01T00:00:00Z",
        ),
    )
    return application


def runs_from_disk(application: App) -> list[str]:
    """Run ids read through a fresh connection: run_daemon closes the app's own store when it stops."""
    store = Store(application.cfg.db_path)
    try:
        return [r.id for r in list_runs(store)]
    finally:
        store.close()


async def test_dispatch_is_off_by_default(tmp_path: Path) -> None:
    gh = DaemonGH()
    application = make_dispatch_app(tmp_path, gh)
    stop = asyncio.Event()
    before = asyncio.all_tasks()
    task = start(application, stop, "host-a", sync_every=3600)
    await until(lambda: gh.lock() is not None)
    assert not [t for t in asyncio.all_tasks() - before if "dispatch" in repr(t.get_coro())]
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)

    assert runs_from_disk(application) == []


async def test_dispatch_every_starts_exactly_one_run_for_a_ready_issue(tmp_path: Path) -> None:
    gh = DaemonGH()
    application = make_dispatch_app(tmp_path, gh)
    stop = asyncio.Event()
    task = start(application, stop, "host-a", dispatch_every=0.01)
    await until(lambda: len(list_runs(application.store)) >= 1)
    await asyncio.sleep(0.1)  # several more dispatch ticks must not start a second run
    stop.set()
    await asyncio.wait_for(task, 2)

    assert runs_from_disk(application) == ["o/r#7@1"]
