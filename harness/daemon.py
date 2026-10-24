"""The long-running daemon: runner, outbox drain, command polling, periodic sync and lock heartbeats.

Before any work starts, the run lock of every registered repo is taken (see :mod:`harness.lock`); if one is
held by another live orchestrator nothing is started and :class:`~harness.lock.LockHeld` propagates. Every
periodic job isolates its own failures: the exception is recorded as a ``daemon_error`` event and the loop
carries on. Only a lost lock stops the daemon (somebody forced it away, so we must not keep acting).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from harness.app import App
from harness.config import RepoConfig
from harness.dag import ready_set
from harness.engine.dispatcher import Dispatcher
from harness.engine.model import TERMINAL_STATUSES
from harness.github.commands import poll_commands
from harness.lock import Lock, LockHeld, acquire, heartbeat, iso, release
from harness.ops.notifier import NotificationService, make_notifier
from harness.playbook.models import Playbook
from harness.secrets import redact
from harness.store.repo import IssueRow, append_event, get_repo, list_issues, list_runs
from harness.sync import sync_repo


def _record(app: App, job: str, exc: BaseException, repo: str | None = None) -> None:
    """Record a failed job as an event. Never raises: the store is the last place an error can go."""
    with contextlib.suppress(Exception):  # nothing is left to report to
        append_event(
            app.store,
            kind="daemon_error",
            data={"job": job, "error": redact(f"{type(exc).__name__}: {exc}")[:500]},
            repo=repo,
        )


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; True as soon as ``stop`` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return stop.is_set()
    return True


async def _every(
    app: App,
    stop: asyncio.Event,
    job: str,
    interval: float,
    action: Callable[[], Awaitable[object]],
    *,
    first: bool,
    repo: str | None = None,
) -> None:
    if not first and await _wait(stop, interval):
        return
    while not stop.is_set():
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record(app, job, exc, repo)
        if await _wait(stop, interval):
            return


async def _sync_one(app: App, repo: str) -> None:
    report = await sync_repo(app.gh, app.store, repo)
    app.set_repo_config(repo, report.config)
    for error in report.errors:
        append_event(app.store, kind="sync_warning", data={"error": redact(error)[:500]}, repo=repo)


async def _acquire_all(app: App, host: str, pid: int, force: bool) -> dict[str, Lock]:
    locks: dict[str, Lock] = {}
    try:
        for entry in app.cfg.repos:
            repo = entry.name
            if get_repo(app.store, repo) is None:
                # The control issue is only known from the repo's config; read it before locking.
                try:
                    await _sync_one(app, repo)
                except Exception as exc:
                    _record(app, "sync", exc, repo)
            config = app.repo_configs.get(repo) or RepoConfig()
            if config.control_issue is None:
                append_event(
                    app.store,
                    kind="daemon_warning",
                    data={"warning": "no control_issue configured: running without a run lock"},
                    repo=repo,
                )
                continue
            locks[repo] = await acquire(
                app.gh,
                repo,
                config.control_issue,
                host=host,
                pid=pid,
                now_iso=iso(app.clock()),
                force=force,
            )
    except BaseException:
        await _release_all(app, locks)
        raise
    return locks


async def _release_all(app: App, locks: dict[str, Lock]) -> None:
    for repo, lock in locks.items():
        try:
            await release(lock)
        except Exception as exc:
            _record(app, "release", exc, repo)
    locks.clear()


async def run_daemon(
    app: App,
    stop: asyncio.Event,
    *,
    host: str,
    pid: int,
    force_unlock: bool = False,
    sync_every: float = 300,
    commands_every: float = 30,
    heartbeat_every: float = 60,
    idle: float = 1.0,
    dispatch_every: float | None = None,
    notify_every: float = 1.0,
) -> None:
    """Run until ``stop`` is set. Raises :class:`LockHeld` (having started nothing) if a repo is locked."""
    locks: dict[str, Lock] = {}
    tasks: list[asyncio.Task[None]] = []
    started = False
    try:
        locks = await _acquire_all(app, host, pid, force_unlock)
        started = True
        append_event(
            app.store,
            kind="daemon_started",
            data={"host": host, "pid": pid, "repos": [r.name for r in app.cfg.repos]},
        )

        async def drain() -> object:
            return await app.outbox.drain_once()

        if app.cfg.notifications.enabled:
            service = NotificationService.from_config(
                app.cfg, app.store, make_notifier(app.cfg.notifications.backend)
            )
            service.start_cursor()  # history is never notified
            app.runner.notify = service.submit
            tasks.append(
                asyncio.create_task(
                    _every(app, stop, "notifications", notify_every, service.poll_once, first=True)
                )
            )

        tasks.append(asyncio.create_task(app.runner.run_forever(stop, idle_sleep=idle)))
        tasks.append(asyncio.create_task(_every(app, stop, "outbox", idle, drain, first=True)))
        for entry in app.cfg.repos:
            repo = entry.name
            tasks.append(
                asyncio.create_task(
                    _every(
                        app, stop, "commands", commands_every, _commands_job(app, repo), first=True, repo=repo
                    )
                )
            )
            tasks.append(
                asyncio.create_task(
                    _every(app, stop, "sync", sync_every, _sync_job(app, repo), first=False, repo=repo)
                )
            )
        if dispatch_every is not None:
            for entry in app.cfg.repos:
                repo_name = entry.name
                tasks.append(
                    asyncio.create_task(
                        _every(
                            app,
                            stop,
                            "dispatch",
                            dispatch_every,
                            _dispatch_job(app, repo_name),
                            first=True,
                            repo=repo_name,
                        )
                    )
                )
        for repo, lock in locks.items():
            tasks.append(
                asyncio.create_task(
                    _every(
                        app,
                        stop,
                        "heartbeat",
                        heartbeat_every,
                        _heartbeat_job(app, stop, repo, lock),
                        first=False,
                        repo=repo,
                    )
                )
            )
        await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _release_all(app, locks)
        if started:
            with contextlib.suppress(Exception):
                append_event(app.store, kind="daemon_stopped", data={"host": host, "pid": pid})
        await app.aclose()


def _commands_job(app: App, repo: str) -> Callable[[], Awaitable[object]]:
    async def job() -> object:
        return await poll_commands(
            app.gh,
            app.store,
            repo,
            app.runner,
            repo_config=app.repo_configs.get(repo) or RepoConfig(),
            outbox=app.outbox,
        )

    return job


def _sync_job(app: App, repo: str) -> Callable[[], Awaitable[object]]:
    async def job() -> object:
        return await _sync_one(app, repo)

    return job


def _heartbeat_job(app: App, stop: asyncio.Event, repo: str, lock: Lock) -> Callable[[], Awaitable[object]]:
    async def job() -> object:
        try:
            return await heartbeat(lock, iso(app.clock()))
        except LockHeld as exc:
            append_event(
                app.store,
                kind="lock_lost",
                data={"holder": f"{exc.holder.get('host')}:{exc.holder.get('pid')}"},
                repo=repo,
            )
            stop.set()  # somebody forced the lock away: stop acting on this repo (and the rest)
            return None

    return job


def _dispatch_job(app: App, repo: str) -> Callable[[], Awaitable[object]]:
    async def job() -> object:
        config = app.repo_configs.get(repo) or RepoConfig()
        caps = {name: agent.max_concurrency for name, agent in config.agents.items() if agent.enabled}
        dispatcher = Dispatcher(store=app.store, caps=caps)

        async def ready_provider() -> list[IssueRow]:
            issues = list_issues(app.store, repo)
            running_issues = {
                r.issue for r in list_runs(app.store) if r.repo == repo and r.status not in TERMINAL_STATUSES
            }
            ready_numbers = set(ready_set(issues, running_issues, config.escalate.labels))
            return [i for i in issues if i.number in ready_numbers]

        async def resolve_playbook(repo_name: str, playbook_id: str) -> tuple[Playbook, str | None]:
            # Imported here: harness.cli imports this module, so a top-level import would be a cycle.
            from harness.cli import _resolve_playbook

            return await _resolve_playbook(app, repo_name, playbook_id, None)

        return await dispatcher.tick(app.runner, ready_provider, resolve_playbook)

    return job
