"""CLI entrypoint for Gargi Harness."""

import asyncio
import json
import os
import re
import signal
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import typer
import yaml

from harness import __version__
from harness.app import App, build_app
from harness.config import RepoConfig, RepoEntry, load_global, save_global
from harness.daemon import run_daemon
from harness.dag import blocked, ready_set
from harness.engine import RunExists, Runner
from harness.engine.model import TERMINAL_STATUSES
from harness.github.client import GitHub, GitHubError
from harness.github.commands import allowed_logins, decide, default_retry_node, resolve_gate
from harness.github.labels import ensure_harness_labels
from harness.github.outbox import Outbox, OutboxOpFailed, ledger_mirror_key
from harness.lock import LockHeld
from harness.planner.materialize import MaterializeContext, MaterializeError, materialize, plan_run_key
from harness.planner.plan_doc import PlanDoc, PlanDocError, load_plan_doc
from harness.planner.plan_gate import (
    APPROVED_LABEL,
    PLAN_KEY,
    PlanApprovalStatus,
    check_plan_approval,
    label_cursor,
    record_plan_approval,
)
from harness.planner.proposal import ProposedPlan
from harness.playbook.load import PlaybookError, builtin_playbooks, load_playbook
from harness.playbook.models import Playbook
from harness.playbook.validate import validate
from harness.secrets import github_token, load_env_files, redact
from harness.store.db import Store
from harness.store.etag_cache import SqliteEtagCache
from harness.store.repo import (
    IssueRow,
    RunRow,
    StepRow,
    events_since,
    get_gate,
    get_issue,
    get_meta,
    get_repo,
    get_run,
    list_issues,
    list_runs,
    pending_gates,
    steps_for_run,
)
from harness.sync import sync_repo

if TYPE_CHECKING:
    import uvicorn
    from fastapi import FastAPI

app = typer.Typer(help="Gargi Harness CLI")


def _make_github() -> GitHub:
    return GitHub(token=github_token())


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        help="Print the version.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Gargi Harness."""
    global_cfg = load_global()
    load_env_files(global_cfg.env_files)


@app.command()
def init(repo: str) -> None:
    """Register the repo in the global config and ensure harness:* labels exist."""
    global_cfg = load_global()
    if not any(r.name == repo for r in global_cfg.repos):
        global_cfg.repos.append(RepoEntry(name=repo))
        save_global(global_cfg)

    gh = _make_github()

    async def _ensure():
        try:
            await ensure_harness_labels(gh, repo)
        finally:
            await gh.aclose()

    asyncio.run(_ensure())

    config = RepoConfig()
    print(yaml.safe_dump(config.model_dump(), sort_keys=False), end="")


@app.command()
def sync(repo: str = typer.Argument(None), full: bool = typer.Option(False, "--full")) -> None:
    """Sync all registered repos, or the one given."""
    global_cfg = load_global()

    if not repo and not global_cfg.repos:
        print("no repos registered: run harness init <owner/repo>")
        raise typer.Exit(1)

    repos_to_sync = [repo] if repo else [r.name for r in global_cfg.repos]

    global_cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(global_cfg.db_path)
    store.migrate()

    gh = _make_github()

    async def _sync_all():
        try:
            for r in repos_to_sync:
                report = await sync_repo(gh, store, r, full=full)
                print(f"Synced {r}: {report.issues_synced} issues")
        finally:
            await gh.aclose()

    try:
        asyncio.run(_sync_all())
    finally:
        store.close()


@app.command()
def status(repo: str = typer.Argument(None)) -> None:
    """Print the harness status."""
    global_cfg = load_global()

    target_repo = repo
    if not target_repo:
        if not global_cfg.repos:
            print("no repos registered: run harness init <owner/repo>")
            raise typer.Exit(1)
        target_repo = global_cfg.repos[0].name

    if not global_cfg.db_path.exists():
        print("not synced: run harness sync")
        raise typer.Exit(1)

    store = Store(global_cfg.db_path)
    try:
        store.migrate()

        repo_row = get_repo(store, target_repo)
        if not repo_row:
            print("not synced: run harness sync")
            raise typer.Exit(1)

        issues = list_issues(store, target_repo)

        runs = list_runs(store)
        running_issues = {r.issue for r in runs if r.status == "running" and r.repo == target_repo}

        config = repo_row.get("config", {})
        escalate_labels = config.get("escalate", {}).get("labels", ["needs-discussion"])

        ready_issues = set(ready_set(issues, running_issues, escalate_labels))
        blocked_map = blocked(issues)
        parent_numbers = {i.parent for i in issues if i.parent is not None}

        # Table header
        print(f"{'#':<5} {'STATE':<8} {'TITLE':<50} {'BLOCKERS':<10} {'READY':<7} EPIC")

        for issue in issues:
            number = str(issue.number)
            state = issue.state.upper()
            title = issue.title
            if len(title) > 47:
                title = title[:47] + "..."

            blockers = ",".join(map(str, blocked_map.get(issue.number, [])))
            if not blockers:
                blockers = "-"

            epic_str = "EPIC" if issue.number in parent_numbers else ""

            if issue.state != "open":
                ready_str = "-"
            elif issue.number in ready_issues:
                ready_str = "READY"
            else:
                ready_str = "BLOCKED"

            print(f"{number:<5} {state:<8} {title:<50} {blockers:<10} {ready_str:<7} {epic_str}")

        ready_str_list = " ".join(f"#{num}" for num in sorted(list(ready_issues)))
        print(f"ready: {ready_str_list}")
    finally:
        store.close()


# ---- gates and run controls ------------------------------------------------------------------------------


def _fail(message: str) -> typer.Exit:
    print(message)
    return typer.Exit(1)


def _open_store() -> Store:
    path = load_global().db_path
    if not path.exists():
        raise _fail("not synced: run harness sync")
    store = Store(path)
    store.migrate()
    return store


def _error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _single_repo(repo: str | None) -> str | None:
    if repo:
        return repo
    repos = load_global().repos
    return repos[0].name if len(repos) == 1 else None


@app.command()
def gates(repo: str = typer.Option(None, "--repo", help="Only gates of this repo.")) -> None:
    """List pending gates."""
    store = _open_store()
    try:
        print(f"{'GATE':<40} {'RUN':<20} {'ISSUE':<7} {'NAME':<12} CREATED")
        for gate in pending_gates(store):
            run = get_run(store, gate.run_id)
            if repo and (run is None or run.repo != repo):
                continue
            issue = f"#{run.issue}" if run else "-"
            print(f"{gate.id:<40} {gate.run_id:<20} {issue:<7} {gate.name:<12} {gate.created_at}")
    finally:
        store.close()


def _decide(ref: str, status: Literal["approved", "rejected"], note: str | None, repo: str | None) -> None:
    store = _open_store()
    try:
        gate = resolve_gate(store, _single_repo(repo), ref)
        decide(store, gate.id, status, note, "cli")
        print(f"{status}: {gate.id}")
    except (KeyError, ValueError) as exc:
        raise _fail(_error_message(exc)) from None
    finally:
        store.close()


@app.command()
def approve(
    ref: str = typer.Argument(..., help="Gate id, or an issue number (needs --repo)."),
    note: str = typer.Option(None, "--note", help="Optional note."),
    repo: str = typer.Option(None, "--repo"),
) -> None:
    """Approve a pending gate."""
    _decide(ref, "approved", note, repo)


@app.command()
def reject(
    ref: str = typer.Argument(..., help="Gate id, or an issue number (needs --repo)."),
    note: str = typer.Option(..., "--note", help="Why the gate is rejected."),
    repo: str = typer.Option(None, "--repo"),
) -> None:
    """Reject a pending gate."""
    _decide(ref, "rejected", note, repo)


def _control(action: Callable[[Runner], Awaitable[str]]) -> None:
    """Run a control action. The outbox only enqueues here (a daemon drains it), so no real token is needed."""
    cfg = load_global()
    if not cfg.db_path.exists():
        raise _fail("not synced: run harness sync")
    application = build_app(cfg, gh=GitHub(token="unused"))

    async def _go() -> str:
        try:
            return await action(application.runner)
        finally:
            await application.aclose()

    try:
        print(asyncio.run(_go()))
    except (KeyError, ValueError) as exc:
        raise _fail(_error_message(exc)) from None


@app.command()
def pause(run: str) -> None:
    """Pause a run."""

    async def action(runner: Runner) -> str:
        runner.pause(run)
        return f"paused: {run}"

    _control(action)


@app.command()
def resume(
    run: str,
    to: str = typer.Option(None, "--to", help="Resume at this node (a human override)."),
) -> None:
    """Resume a paused run, or restart it at a node."""

    async def action(runner: Runner) -> str:
        runner.resume(run, to=to)
        return f"resumed: {run}" + (f" at {to}" if to else "")

    _control(action)


@app.command()
def cancel(run: str) -> None:
    """Cancel a run."""

    async def action(runner: Runner) -> str:
        await runner.cancel(run)
        return f"cancelled: {run}"

    _control(action)


@app.command()
def retry(run: str, node: str = typer.Argument(None, help="Node to retry (default: latest failed).")) -> None:
    """Retry a node of a run."""

    async def action(runner: Runner) -> str:
        if get_run(runner.store, run) is None:
            raise KeyError(f"run {run} not found")
        target = node or default_retry_node(runner.store, run)
        runner.retry_node(run, target)
        return f"retry: {run} {target}"

    _control(action)


# ---- run, daemon, runs, show -------------------------------------------------------------------------------

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MAX_DELAY = 5.0
_DRAIN_ATTEMPTS = 70
_OUTPUT_CHARS = 200
_LAST_EVENTS = 50


def _epoch(value: str) -> float | None:
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _label_value(labels: list[str], prefix: str) -> str | None:
    for label in labels:
        if label.startswith(prefix) and len(label) > len(prefix):
            return label[len(prefix) :]
    return None


def _event_line(event: dict[str, Any]) -> str:
    parts = []
    for key, value in (event.get("data") or {}).items():
        if value is None or not isinstance(value, str | int | float | bool):
            continue
        text = " ".join(str(value).split())
        parts.append(f"{key}={text[:77] + '...' if len(text) > 80 else text}")
    return redact(" ".join([str(event["ts"]), str(event["kind"]), *parts]))


def _is_run_event(event: dict[str, Any], run: RunRow) -> bool:
    """The run's own events, plus the issue's events that carry no run (for example ``outbox_failed``)."""
    if event["run_id"] is not None:
        return event["run_id"] == run.id
    return event["repo"] == run.repo and event["issue"] == run.issue


def _print_events(application: App, run_id: str, after: int) -> int:
    """Print the new events of the run (one line each); returns the id of the last event seen."""
    run = get_run(application.store, run_id)
    last = after
    for event in events_since(application.store, after):
        last = max(last, event["id"])
        if run is not None and _is_run_event(event, run):
            print(_event_line(event))
    return last


def _last_event_id(application: App) -> int:
    return application.store.query("SELECT COALESCE(MAX(id), 0) AS last FROM events")[0]["last"]


def _next_delay(application: App, run_id: str) -> float:
    """Seconds until the earliest step of the run is due, at most ``_MAX_DELAY``."""
    now = application.clock()
    delay = _MAX_DELAY
    for step in steps_for_run(application.store, run_id):
        if step.status == "pending":
            return 0.0
        if step.status == "waiting":
            due = _epoch(step.poll_after) if step.poll_after else None
            if due is None:
                return 0.0
            delay = min(delay, max(0.0, due - now))
    return delay


def _needs_you(step: StepRow) -> bool:
    return step.status == "waiting" and bool(step.output and step.output.get("needs_you"))


def _awaiting_resolution(application: App, run_id: str, baseline: dict[str, str | None]) -> bool:
    """True while a decided gate has not been picked up by the runner yet (its step was not polled again)."""
    for step in steps_for_run(application.store, run_id):
        if not _needs_you(step):
            continue
        gate = get_gate(application.store, (step.output or {}).get("gate_id") or "")
        if (
            gate is not None
            and gate.status != "pending"
            and baseline.get(step.id, object()) == step.poll_after
        ):
            return True
    return False


def _print_needs_you(application: App, run_id: str) -> None:
    for step in steps_for_run(application.store, run_id):
        if not _needs_you(step):
            continue
        output = step.output or {}
        print(f"needs you at {step.node_id}: {redact(' '.join(str(output.get('reason', '')).split()))}")
        if output.get("gate_id"):
            print(f"gate: {output['gate_id']}")
        for link in output.get("links") or []:
            print(f"link: {redact(str(link))}")


async def _drain_all(application: App) -> None:
    """Send everything that is due. Rate-limited rows wait (via the injected sleep) for the next window."""
    for _ in range(_DRAIN_ATTEMPTS):
        sent = await application.outbox.drain_once()
        due = application.store.query(
            "SELECT 1 FROM outbox WHERE status = 'pending' AND (not_before IS NULL OR not_before <= ?) LIMIT 1",
            (application.store.now(),),
        )
        if not due:
            return
        if not sent:
            await application.sleep(1.0)


async def _resolve_playbook(
    application: App, repo: str, playbook_id: str, ref: str | None
) -> tuple[Playbook, str | None]:
    """The repo's ``.harness/playbooks/<id>.yaml`` if it has one, else the built-in. Raises ValueError."""
    if not _ID_RE.fullmatch(playbook_id):
        raise ValueError(f"invalid playbook id {playbook_id!r}")
    text = await application.gh.file_text(repo, f".harness/playbooks/{playbook_id}.yaml", ref=ref)
    if text is not None:
        try:
            playbook = load_playbook(text)
        except PlaybookError as exc:
            raise ValueError(
                f"playbook {playbook_id} of {repo} is invalid: {' '.join(str(exc).split())[:200]}"
            ) from None
    else:
        found = builtin_playbooks().get(playbook_id)
        if found is None:
            raise ValueError(
                f"unknown playbook {playbook_id!r}: not in {repo}/.harness/playbooks/ nor built in"
            )
        playbook = found
    if not playbook.nodes:
        raise ValueError(f"playbook {playbook_id} has no nodes")
    problems = [
        i for i in validate(playbook, agent_caps=application.adapters.caps_by_name()) if i.severity == "error"
    ]
    if problems:
        first = problems[0]
        raise ValueError(f"playbook {playbook_id} is invalid: {first.path}: {first.code}: {first.message}")
    known = set(application.runner.executors.kinds()) | {"join", "parallel"}
    missing = sorted({n.kind for n in playbook.nodes.values()} - known)
    if missing:
        raise ValueError(f"playbook {playbook_id} uses node kinds without an executor: {', '.join(missing)}")
    # The contents API's blob sha is not exposed by gh.file_text, so no playbook sha is recorded.
    return playbook, None


def _active_run(application: App, repo: str, number: int) -> RunRow | None:
    for run in list_runs(application.store):
        if run.repo == repo and run.issue == number and run.status not in TERMINAL_STATUSES:
            return run
    return None


async def _start_or_continue(
    application: App,
    repo: str,
    number: int,
    playbook_flag: str | None,
    agent_flag: str | None,
    issue: IssueRow | None,
) -> str:
    """The id of the run to drive. Raises ValueError with a one-line message when it cannot be started."""
    existing = _active_run(application, repo, number)
    if existing is not None:
        print(f"continuing {existing.id}")
        return existing.id
    if issue is None:
        raise ValueError(f"issue #{number} not found in {repo}")
    if issue.state != "open":
        raise ValueError(f"issue #{number} is {issue.state}")
    config = application.repo_configs.get(repo) or RepoConfig()
    playbook_id = playbook_flag or _label_value(issue.labels, "playbook:") or config.default_playbook
    agent = agent_flag or _label_value(issue.labels, "agent:") or config.default_agent
    stored = get_repo(application.store, repo) or {}
    playbook, sha = await _resolve_playbook(application, repo, playbook_id, stored.get("default_branch"))
    if agent not in application.adapters.names():
        known = ", ".join(application.adapters.names())
        raise ValueError(f"agent {agent!r} is not available (known: {known}); is its API key set?")
    try:
        run_id = application.runner.start_run(repo, issue, playbook, playbook_sha=sha, agent=agent)
    except RunExists as exc:
        print(f"continuing {exc.run_id}")
        return exc.run_id
    print(f"started {run_id} ({playbook.id}, agent {agent})")
    return run_id


async def _drive(application: App, run_id: str, max_ticks: int | None, last_event: int) -> RunRow:
    """Tick and drain in the foreground until the run is terminal, paused, waits for a human or ticks run out."""
    store = application.store
    baseline = {s.id: s.poll_after for s in steps_for_run(store, run_id) if s.status == "waiting"}
    ticks = 0
    while True:
        await application.runner.tick()
        ticks += 1
        await application.outbox.drain_once()
        last_event = _print_events(application, run_id, last_event)
        run = get_run(store, run_id)
        assert run is not None
        if run.status in TERMINAL_STATUSES or run.status == "paused":
            break
        if run.status == "needs-you" and not _awaiting_resolution(application, run_id, baseline):
            break
        if max_ticks is not None and ticks >= max_ticks:
            print(f"stopped after {ticks} ticks; run continues with: harness run")
            break
        await application.sleep(_next_delay(application, run_id))
    await _drain_all(application)
    _print_events(application, run_id, last_event)
    run = get_run(store, run_id)
    assert run is not None
    return run


async def _run_command(
    application: App,
    repo: str,
    number: int,
    playbook_flag: str | None,
    agent_flag: str | None,
    max_ticks: int | None,
) -> int:
    try:
        report = await sync_repo(application.gh, application.store, repo)
        application.set_repo_config(repo, report.config)
        for error in report.errors:
            print(f"warning: {redact(error)}")
        issue = get_issue(application.store, repo, number)
        last_event = _last_event_id(application)
        run_id = await _start_or_continue(application, repo, number, playbook_flag, agent_flag, issue)
        run = await _drive(application, run_id, max_ticks, last_event)
        if run.status == "needs-you":
            _print_needs_you(application, run_id)
        elif run.status == "paused":
            print(f"paused: harness resume {run_id}")
        print(f"run {run_id}: {run.status}")
        return 1 if run.status in ("failed", "cancelled") else 0
    except (ValueError, GitHubError) as exc:
        print(f"error: {redact(str(exc))}")
        return 1
    finally:
        await application.aclose()


def _build(**kwargs: Any) -> App:
    try:
        return build_app(load_global(), **kwargs)
    except RuntimeError as exc:
        raise _fail(f"error: {redact(str(exc))}") from None


@app.command()
def run(
    repo: str = typer.Argument(..., help="owner/repo"),
    issue: int = typer.Argument(..., help="Issue number."),
    playbook: str = typer.Option(None, "--playbook", help="Playbook id (default: label, then repo default)."),
    agent: str = typer.Option(None, "--agent", help="Agent name (default: label, then repo default)."),
    max_ticks: int = typer.Option(None, "--max-ticks", min=1, help="Stop after this many ticks."),
) -> None:
    """Start (or continue) a run for an issue and drive it in the foreground."""
    application = _build()
    raise typer.Exit(asyncio.run(_run_command(application, repo, issue, playbook, agent, max_ticks)))


@app.command()
def daemon(
    force_unlock: bool = typer.Option(False, "--force-unlock", help="Take the run lock even if it is held."),
) -> None:
    """Run the runner, outbox, command polling and sync for all registered repos."""
    cfg = load_global()
    if not cfg.repos:
        raise _fail("no repos registered: run harness init <owner/repo>")
    application = _build()
    host, pid = socket.gethostname(), os.getpid()

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        print(f"daemon {host}:{pid} for {', '.join(r.name for r in cfg.repos)}: Ctrl-C to stop")
        try:
            await run_daemon(application, stop, host=host, pid=pid, force_unlock=force_unlock)
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)

    try:
        asyncio.run(_main())
    except LockHeld as exc:
        raise _fail(f"{exc}; stop that orchestrator, or use --force-unlock") from None
    print("daemon stopped")


@app.command()
def runs(
    repo: str = typer.Option(None, "--repo", help="Only runs of this repo."),
    status: str = typer.Option(None, "--status", help="Only runs with this status."),
) -> None:
    """List runs."""
    store = _open_store()
    try:
        print(f"{'RUN':<28} {'ISSUE':<7} {'PLAYBOOK':<22} {'STATUS':<10} {'ACTIVE':<24} PR")
        for item in list_runs(store, status):
            if repo and item.repo != repo:
                continue
            active = ",".join(
                s.node_id
                for s in steps_for_run(store, item.id)
                if s.status in ("pending", "running", "waiting")
            )
            pr = item.context.get("pr")
            pr_text = f"#{pr['number']}" if isinstance(pr, dict) and "number" in pr else "-"
            print(
                f"{item.id:<28} {'#' + str(item.issue):<7} {item.playbook_id:<22} {item.status:<10} "
                f"{active or '-':<24} {pr_text}"
            )
    finally:
        store.close()


def _output_summary(output: dict[str, Any] | None) -> str:
    if not output:
        return ""
    return redact(json.dumps(output, sort_keys=True, ensure_ascii=False, default=str))[:_OUTPUT_CHARS]


@app.command()
def show(run: str = typer.Argument(..., help="Run id, for example owner/repo#7@1.")) -> None:
    """Show a run: its fields, steps and last events."""
    store = _open_store()
    try:
        item = get_run(store, run)
        if item is None:
            raise _fail(f"run {run} not found")
        pr = item.context.get("pr")
        print(f"run: {item.id}")
        print(f"repo: {item.repo}  issue: #{item.issue}  status: {item.status}")
        print(
            f"playbook: {item.playbook_id}  sha: {item.playbook_sha or '-'}  agent: {item.context.get('agent') or '-'}"
        )
        print(
            f"pr: {redact(str(pr.get('url') or '#' + str(pr['number']))) if isinstance(pr, dict) and 'number' in pr else '-'}"
        )
        print(f"created: {item.created_at}  updated: {item.updated_at}")
        print("steps:")
        print(f"  {'NODE':<14} {'VISIT':<6} {'STATUS':<9} {'OUTCOME':<10} OUTPUT")
        for step in steps_for_run(store, item.id):
            print(
                f"  {step.node_id:<14} {step.visit:<6} {step.status:<9} {step.outcome or '-':<10} "
                f"{_output_summary(step.output)}"
            )
        print(f"events (last {_LAST_EVENTS}):")
        rows = store.query(
            "SELECT * FROM events WHERE run_id = ? OR (run_id IS NULL AND repo = ? AND issue = ?) "
            "ORDER BY id DESC LIMIT ?",
            (item.id, item.repo, item.issue, _LAST_EVENTS),
        )
        for row in reversed(rows):
            row["data"] = json.loads(row["data_json"]) if row["data_json"] else {}
            print(f"  {_event_line(row)}")
    finally:
        store.close()


playbook_app = typer.Typer(help="Playbook commands")
app.add_typer(playbook_app, name="playbook")


@playbook_app.command("validate")
def playbook_validate(file: str) -> None:
    """Validate a playbook file."""
    from harness.playbook.load import PlaybookError, load_file
    from harness.playbook.validate import validate

    try:
        pb = load_file(file)
    except PlaybookError as e:
        print(f"Error loading playbook: {e}")
        raise typer.Exit(1) from None

    issues = validate(pb)

    for issue in issues:
        print(f"{issue.path}: {issue.code}: {issue.message}")
    if any(i.severity == "error" for i in issues):
        raise typer.Exit(1)
    print(f"ok: {pb.id} ({len(pb.nodes)} nodes)")


@playbook_app.command("schema")
def playbook_schema() -> None:
    """Export the playbook JSON schema."""
    import json

    from harness.playbook.schema import playbook_json_schema

    schema = playbook_json_schema()
    print(json.dumps(schema, indent=2))


@playbook_app.command("list")
def playbook_list() -> None:
    """List built-in playbooks."""
    from harness.playbook.load import builtin_playbooks

    playbooks = builtin_playbooks()
    for pb_id in playbooks:
        print(pb_id)


# ---- web UI ----------------------------------------------------------------------------------------------


# ---- plan gate -------------------------------------------------------------------------------------------

plan_app = typer.Typer(help="Campaign plan commands")
app.add_typer(plan_app, name="plan")


def _stored_config(store: Store, target: str) -> RepoConfig:
    """The repo config of the last sync (the defaults when there is none or it cannot be read)."""
    stored = get_repo(store, target)
    try:
        return RepoConfig.model_validate((stored or {}).get("config") or {})
    except ValueError:
        return RepoConfig()


def _plan_target(
    store: Store, repo: str | None, control_issue: int | None
) -> tuple[str, int | None, list[str]]:
    """The repo, its control issue and the plan allowlist, from the options and the last synced config."""
    target = _single_repo(repo)
    if not target:
        raise _fail("give --repo owner/name")
    config = _stored_config(store, target)
    return target, control_issue if control_issue is not None else config.control_issue, config.command_users


async def _with_github(gh: GitHub, action: Callable[[], Awaitable[None]]) -> None:
    try:
        await action()
    finally:
        await gh.aclose()


def _plan_source(path: str | None, issue: int | None) -> None:
    if (path is None) == (issue is None):
        raise _fail("give exactly one of --path or --issue as the plan source")


def _describe_approval(approval: Any) -> str:
    text = approval.status.value.upper()
    if approval.status is PlanApprovalStatus.APPROVED:
        text += f" by @{approval.approved_by} via {approval.via} (sha {str(approval.approved_sha)[:12]})"
    elif approval.status is PlanApprovalStatus.STALE:
        text += f": approved sha {str(approval.approved_sha)[:12]}, the plan has changed since"
    if approval.note:
        text += f" ({approval.note})"
    return text


@plan_app.command("show")
def plan_show(
    repo: str = typer.Option(None, "--repo"),
    path: str = typer.Option(None, "--path", help="Plan file in the repo, for example docs/PLAN.md."),
    issue: int = typer.Option(None, "--issue", help="Plan issue number (instead of --path)."),
    ref: str = typer.Option(None, "--ref", help="Branch or commit of the plan file (default branch)."),
    control_issue: int = typer.Option(None, "--control-issue"),
) -> None:
    """Show the plan document and whether it is approved."""
    _plan_source(path, issue)
    store = _open_store()
    gh = _make_github()

    async def _show() -> None:
        target, control, allowlist = _plan_target(store, repo, control_issue)
        plan = await load_plan_doc(gh, target, path=path, issue=issue, ref=ref)
        print(f"plan:     {plan.source}" + (f" @ {plan.ref}" if plan.ref else ""))
        print(f"title:    {plan.title}")
        print(f"sha:      {plan.sha}")
        print(f"headings: {len(plan.headings)}")
        for heading in plan.headings:
            print(f"  - {heading}")
        if control is None:
            print("approval: unknown (no control issue: pass --control-issue)")
            return
        approval = await check_plan_approval(
            gh, repo=target, control_issue=control, plan=plan, owner_allowlist=allowlist, store=store
        )
        print(f"approval: {_describe_approval(approval)}")

    try:
        asyncio.run(_with_github(gh, _show))
    except (PlanDocError, GitHubError, ValueError) as exc:
        raise _fail(f"error: {redact(_error_message(exc))}") from None
    finally:
        store.close()


async def _approve_plan(
    gh: GitHub, store: Store, target: str, control: int, allowlist: list[str], plan: PlanDoc
) -> None:
    actor = str((await gh.get("user")).get("login") or "")
    allowed = allowed_logins(target, allowlist) | {target.split("/")[0].lower()}
    if actor.lower() not in allowed:
        raise _fail(
            f"@{actor or '?'} is not allowed to approve plans for {target}: "
            f"only the repo owner or command_users ({', '.join(sorted(allowed))})"
        )
    approval = await check_plan_approval(
        gh, repo=target, control_issue=control, plan=plan, owner_allowlist=allowlist, store=store
    )
    if approval.status is PlanApprovalStatus.APPROVED and not approval.fresh:
        print(f"already approved: {_describe_approval(approval)}")
        return
    outbox = Outbox(store, gh)
    outbox.enqueue(
        "set_labels",
        {"repo": target, "issue": control, "add": [APPROVED_LABEL], "remove": []},
        coalesce_key=f"plan-label:{target}#{control}",
    )
    await outbox.drain_once()
    cursor = await label_cursor(gh, target, control)
    record_plan_approval(
        store, outbox, repo=target, control_issue=control, plan=plan, by=actor, via="cli", label_cursor=cursor
    )
    await outbox.drain_once()
    if get_meta(store, ledger_mirror_key(target, control, PLAN_KEY)):
        print(f"approved {plan.source} (sha {plan.sha[:12]}) as @{actor}")
    else:
        print(f"approval of {plan.source} queued as @{actor}; it is not on GitHub yet (the daemon retries)")


@plan_app.command("approve")
def plan_approve(
    repo: str = typer.Option(None, "--repo"),
    control_issue: int = typer.Option(None, "--control-issue"),
    path: str = typer.Option(None, "--path", help="Plan file in the repo, for example docs/PLAN.md."),
    issue: int = typer.Option(None, "--issue", help="Plan issue number (instead of --path)."),
    ref: str = typer.Option(None, "--ref", help="Branch or commit of the plan file (default branch)."),
) -> None:
    """Approve the current version of the plan: label the control issue and record the approval in its ledger."""
    _plan_source(path, issue)
    store = _open_store()
    gh = _make_github()

    async def _approve() -> None:
        target, control, allowlist = _plan_target(store, repo, control_issue)
        if control is None:
            raise _fail("no control issue: pass --control-issue or set control_issue in .harness/config.yaml")
        plan = await load_plan_doc(gh, target, path=path, issue=issue, ref=ref)
        await _approve_plan(gh, store, target, control, allowlist, plan)

    try:
        asyncio.run(_with_github(gh, _approve))
    except (PlanDocError, GitHubError, ValueError) as exc:
        raise _fail(f"error: {redact(_error_message(exc))}") from None
    finally:
        store.close()


async def _known_playbooks(gh: GitHub, target: str, plan: ProposedPlan) -> set[str]:
    """The built-in playbook ids plus those of the plan that the repo has under ``.harness/playbooks/``."""
    known = set(builtin_playbooks())
    for playbook_id in dict.fromkeys(issue.playbook for issue in plan.issues):
        if playbook_id not in known and re.fullmatch(r"[A-Za-z0-9_.-]+", playbook_id):
            if await gh.file_text(target, f".harness/playbooks/{playbook_id}.yaml") is not None:
                known.add(playbook_id)
    return known


@plan_app.command("create")
def plan_create(
    proposal: str = typer.Option(..., "--proposal", help="JSON file with a ProposedPlan."),
    repo: str = typer.Option(None, "--repo"),
    control_issue: int = typer.Option(None, "--control-issue"),
    run_key: str = typer.Option(
        None, "--run-key", help="Names this materialization (default: from the plan)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would be created."),
) -> None:
    """Create the epic, its sub-issues, links and labels from a proposed plan. Repeating it creates nothing twice."""
    try:
        plan = ProposedPlan.model_validate_json(Path(proposal).read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail(f"error: cannot read {proposal}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise _fail(f"error: {proposal} is not a valid proposed plan: {redact(str(exc))}") from None
    store = _open_store()
    gh = _make_github()

    async def _create() -> None:
        target, control, _ = _plan_target(store, repo, control_issue)
        config = _stored_config(store, target)
        ctx = MaterializeContext(
            repo=target,
            gh=gh,
            outbox=Outbox(store, gh),
            store=store,
            control_issue=control,
            run_key=run_key or plan_run_key(plan),
            agents=[name for name, agent in config.agents.items() if agent.enabled],
            playbooks=await _known_playbooks(gh, target, plan),
        )
        result = await materialize(plan, ctx, dry_run=dry_run)
        verb = "would create" if dry_run else "created"
        print(f"run:      {ctx.run_key}" + (" (dry run, nothing written)" if dry_run else ""))
        print("epic:     " + (f"#{result.epic_number}" if result.epic_number else "(not created yet)"))
        print(f"{verb}:   {', '.join(result.created) or '-'}")
        print(f"reused:   {', '.join(result.reused) or '-'}")
        print(f"links:    {result.links_created} {'to create' if dry_run else 'created'}")
        print(f"labels:   {', '.join(result.labels_ensured) or '-'}")
        for key, number in result.issues.items():
            print(f"  {key} -> #{number}")

    try:
        asyncio.run(_with_github(gh, _create))
    except (GitHubError, MaterializeError, OutboxOpFailed, ValueError) as exc:
        raise _fail(f"error: {redact(_error_message(exc))}") from None
    finally:
        store.close()


def build_server_config(application: "FastAPI", port: int) -> "uvicorn.Config":
    """The uvicorn config; the host is fixed to loopback on purpose (no ``--host`` option).

    The access log is off because it would write ``GET /auth?token=...`` (the launch token) to the terminal.
    """
    import uvicorn

    return uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=3,
    )


def _serve_github(store: Store) -> tuple[GitHub | None, str | None]:
    """The server's GitHub client and its token, or (None, None) with one warning line when there is no token."""
    try:
        token = github_token()
    except RuntimeError:
        print("GitHub not configured: repo playbooks and save are disabled")
        return None, None
    return GitHub(token=token, cache=SqliteEtagCache(store)), token


def _serve_demo(port: int | None, no_open: bool) -> None:
    """``serve --demo``: refuse when real repos and a GitHub token are configured, else serve the demo app."""
    import webbrowser

    from harness.server.demo import create_demo_app
    from harness.server.runner import HarnessServer

    cfg = load_global()
    load_env_files(cfg.env_files)
    if cfg.repos:
        try:
            github_token()
        except RuntimeError:
            pass
        else:
            print(
                "refusing to start --demo: a GitHub token and real repos are configured. "
                "Demo mode is only for an environment with neither (use another GARGI_HARNESS_CONFIG).",
            )
            raise typer.Exit(1)
    listen_port = port if port is not None else 8799
    web, token = create_demo_app(port=listen_port)
    url = f"http://127.0.0.1:{listen_port}/auth?token={token}"

    def announce() -> None:
        print(url, flush=True)
        if not no_open:
            webbrowser.open(url)

    HarnessServer(build_server_config(web, listen_port), bus=web.state.bus, on_started=announce).run()


@app.command()
def serve(
    port: int = typer.Option(None, "--port", help="Port to listen on (default: the global config port)."),
    no_open: bool = typer.Option(False, "--no-open", help="Print the login URL without opening a browser."),
    with_daemon: bool = typer.Option(False, "--with-daemon", help="Not implemented yet."),
    demo: bool = typer.Option(
        False, "--demo", help="Serve seeded in-memory demo data: no network, no token file, no real repos."
    ),
) -> None:
    """Serve the local web UI on 127.0.0.1 and print the one-time login URL."""
    import webbrowser

    from harness.server import create_app
    from harness.server.auth import generate_token
    from harness.server.runner import HarnessServer
    from harness.server.static import default_web_dist

    if with_daemon:
        print("--with-daemon is not implemented yet; run `harness daemon` separately")
        raise typer.Exit(2)
    if demo:
        _serve_demo(port, no_open)
        return
    cfg = load_global()
    load_env_files(cfg.env_files)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.migrate()
    listen_port = port if port is not None else cfg.port
    token = generate_token()
    gh, gh_token = _serve_github(store)
    web = create_app(
        store=store, config=cfg, token=token, web_dist=default_web_dist(), port=listen_port, gh=gh
    )
    web.state.gh_token = gh_token
    url = f"http://127.0.0.1:{listen_port}/auth?token={token}"

    def announce() -> None:
        # Only after the socket is bound: a port clash must never hand the token to another process.
        print(url, flush=True)
        if not no_open:
            webbrowser.open(url)

    server = HarnessServer(build_server_config(web, listen_port), bus=web.state.bus, on_started=announce)
    try:
        server.run()
    finally:
        if gh is not None:
            try:
                asyncio.run(gh.aclose())
            except Exception:
                pass  # the client's connections belonged to the server's event loop, which is gone
        store.close()
