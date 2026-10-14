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
