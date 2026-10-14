from pydantic import BaseModel, ConfigDict

from harness.config import RepoConfig, parse_repo_config
from harness.github.client import GitHub
from harness.github.labels import HARNESS_LABEL_PREFIX
from harness.github.ledger import LedgerCorrupt, find_ledger, parse_ledger
from harness.store.db import Store
from harness.store.repo import IssueRow, get_repo, upsert_issue, upsert_repo


class SyncReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    default_branch: str
    config: RepoConfig
    issues_synced: int
    full: bool
    ledgers: dict[int, dict]
    errors: list[str]


async def sync_repo(gh: GitHub, store: Store, repo: str, *, full: bool = False) -> SyncReport:
    start_time = store.now()
    errors: list[str] = []

    repo_info = await gh.repo(repo)
    default_branch = repo_info.get("default_branch", "main")

    config_text = await gh.file_text(repo, ".harness/config.yaml", ref=default_branch)
    try:
        config = parse_repo_config(config_text)
    except ValueError as e:
        errors.append(f"Failed to parse config: {e}")
        config = RepoConfig()

    since = None
    if not full:
        existing_repo = get_repo(store, repo)
        if existing_repo and existing_repo.get("synced_at"):
            since = existing_repo["synced_at"]

    issues = await gh.issues(repo, state="all", since=since)

    issues_synced = 0
    ledgers = {}

    for issue in issues:
        if "pull_request" in issue:
            continue

        number = issue["number"]

        # Parent issue
        parent_info = await gh.parent(repo, number)
        parent_id = parent_info["number"] if parent_info else None

        # Blocked by
        blocked_info = await gh.blocked_by(repo, number)
        blocked_by = [b["number"] for b in blocked_info] if blocked_info else []

        labels = [lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict)]

        issue_row = IssueRow(
            repo=repo,
            number=number,
            id=issue["id"],
            title=issue["title"],
            body=issue.get("body") or "",
            state=issue["state"],
            labels=labels,
            parent=parent_id,
            blocked_by=blocked_by,
            updated_at=issue.get("updated_at") or start_time,
        )

        upsert_issue(store, issue_row)
        issues_synced += 1

        has_harness_label = any(lbl.startswith(HARNESS_LABEL_PREFIX) for lbl in labels)
        if has_harness_label:
            comments = await gh.comments(repo, number)
            ledger_comment = find_ledger(comments)
            if ledger_comment:
                try:
                    snapshot = parse_ledger(ledger_comment.get("body") or "")
                    if snapshot:
                        ledgers[number] = snapshot
                except LedgerCorrupt as e:
                    errors.append(f"Issue {number} ledger corrupt: {e}")

    upsert_repo(
        store,
        full_name=repo,
        default_branch=default_branch,
        config=config.model_dump(),
        synced_at=start_time,
    )

    return SyncReport(
        repo=repo,
        default_branch=default_branch,
        config=config,
        issues_synced=issues_synced,
        full=full,
        ledgers=ledgers,
        errors=errors,
    )
