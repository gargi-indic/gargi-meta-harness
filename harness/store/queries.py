"""Read-only queries for the web API that the repository functions in ``store.repo`` do not cover."""

import json
from typing import Any

from harness.store.db import Store
from harness.store.repo import RunRow


def activity_for_run(store: Store, run_id: str, *, after: int, limit: int) -> list[dict[str, Any]]:
    """``agent_activity`` event rows of a run with ``id > after``, oldest first; ``data`` is decoded."""
    rows = store.query(
        "SELECT * FROM events WHERE kind = 'agent_activity' AND run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after, limit),
    )
    for row in rows:
        row["data"] = json.loads(row.pop("data_json")) if row["data_json"] else {}
    return rows


def latest_runs_by_issue(store: Store, repo: str) -> dict[int, RunRow]:
    """The latest run of each issue of ``repo``, ordered by ``created_at`` and then id."""
    rows = store.query("SELECT * FROM runs WHERE repo = ? ORDER BY created_at, id", (repo,))
    latest: dict[int, RunRow] = {}
    for r in rows:
        latest[r["issue"]] = RunRow(
            id=r["id"],
            repo=r["repo"],
            issue=r["issue"],
            playbook_id=r["playbook_id"],
            playbook_sha=r["playbook_sha"],
            status=r["status"],
            context=json.loads(r["context_json"]) if r["context_json"] else {},
            ledger_comment_id=r["ledger_comment_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
    return latest
