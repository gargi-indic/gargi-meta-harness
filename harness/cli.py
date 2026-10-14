"""CLI entrypoint for Gargi Harness."""

import asyncio

import typer
import yaml

from harness import __version__
from harness.config import RepoConfig, RepoEntry, load_global, save_global
from harness.dag import blocked, ready_set
from harness.github.client import GitHub
from harness.github.labels import ensure_harness_labels
from harness.secrets import github_token, load_env_files
from harness.store.db import Store
from harness.store.repo import get_repo, list_issues, list_runs
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
    asyncio.run(ensure_harness_labels(gh, repo))

    config = RepoConfig()
    print(yaml.safe_dump(config.model_dump(), sort_keys=False), end="")


@app.command()
def sync(repo: str = typer.Argument(None), full: bool = typer.Option(False, "--full")) -> None:
    """Sync all registered repos, or the one given."""
    global_cfg = load_global()
    repos_to_sync = [repo] if repo else [r.name for r in global_cfg.repos]

    global_cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(global_cfg.db_path)
    store.migrate()

    gh = _make_github()

    async def _sync_all():
        for r in repos_to_sync:
            report = await sync_repo(gh, store, r, full=full)
            print(f"Synced {r}: {report.issues_synced} issues")

    asyncio.run(_sync_all())
    store.close()


@app.command()
def status(repo: str = typer.Argument(None)) -> None:
    """Print the harness status."""
    global_cfg = load_global()
    if not global_cfg.db_path.exists():
        print("not synced: run harness sync")
        raise typer.Exit(1)

    store = Store(global_cfg.db_path)

    target_repo = repo
    if not target_repo:
        if not global_cfg.repos:
            print("not synced: run harness sync")
            raise typer.Exit(1)
        target_repo = global_cfg.repos[0].name

    repo_row = get_repo(store, target_repo)
    if not repo_row:
        print("not synced: run harness sync")
        raise typer.Exit(1)

    issues = list_issues(store, target_repo)

    runs = list_runs(store)
    running_issues = {r.issue for r in runs if r.status == "running" and r.repo == target_repo}

    config = repo_row.get("config", {})
    escalate_labels = config.get("escalate", {}).get("labels", ["needs-discussion"])

    ready_issues = ready_set(issues, running_issues, escalate_labels)
    blocked_map = blocked(issues)
    parent_numbers = {i.parent for i in issues if i.parent is not None}

    # Table header
    print(f"{'#':<5} {'STATE':<8} {'TITLE':<50} {'BLOCKERS':<10} STATUS")

    for issue in issues:
        number = str(issue.number)
        state = issue.state.upper()
        title = issue.title
        if len(title) > 47:
            title = title[:47] + "..."

        blockers = ",".join(map(str, blocked_map.get(issue.number, [])))
        if not blockers:
            blockers = "-"

        if issue.number in ready_issues:
            status_str = "READY"
        elif issue.number in parent_numbers:
            status_str = "EPIC"
        else:
            status_str = "BLOCKED"

        print(f"{number:<5} {state:<8} {title:<50} {blockers:<10} {status_str}")

    ready_str = " ".join(f"#{num}" for num in ready_issues)
    print(f"ready: {ready_str}")

    store.close()
