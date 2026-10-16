"""CLI entrypoint for Gargi Harness."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import typer
import yaml

from harness import __version__
from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig, RepoEntry, load_global, save_global
from harness.dag import blocked, ready_set
from harness.engine import ExecutorRegistry, Runner
from harness.github.client import GitHub
from harness.github.commands import decide, default_retry_node, resolve_gate
from harness.github.labels import ensure_harness_labels
from harness.github.outbox import Outbox
from harness.secrets import github_token, load_env_files
from harness.store.db import Store
from harness.store.repo import get_repo, get_run, list_issues, list_runs, pending_gates
from harness.sync import sync_repo

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


def _control_runner(store: Store) -> tuple[Runner, GitHub]:
    """A runner for controls only: the outbox just enqueues (a daemon drains it), so no real token is needed."""
    configs: dict[str, RepoConfig] = {}
    for row in store.query("SELECT full_name FROM repos"):
        stored = get_repo(store, row["full_name"])
        try:
            configs[row["full_name"]] = RepoConfig.model_validate((stored or {}).get("config") or {})
        except ValueError:
            continue  # an unreadable stored config falls back to the defaults
    gh = GitHub(token="unused")
    runner = Runner(store, Outbox(store, gh), ExecutorRegistry(), AdapterRegistry(), repo_configs=configs)
    return runner, gh


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
    store = _open_store()
    runner, gh = _control_runner(store)

    async def _go() -> str:
        try:
            return await action(runner)
        finally:
            await gh.aclose()

    try:
        print(asyncio.run(_go()))
    except (KeyError, ValueError) as exc:
        raise _fail(_error_message(exc)) from None
    finally:
        store.close()


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

    if issues:
        for issue in issues:
            print(f"{issue.path}: {issue.code}: {issue.message}")
        raise typer.Exit(1)
    else:
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
