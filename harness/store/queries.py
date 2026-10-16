"""Read-only queries for the web API that the repository functions in ``store.repo`` do not cover."""

import json
from typing import Any

from harness.store.db import Store
from harness.store.repo import RunRow


def _json_object(text: str | None) -> dict[str, Any]:
    """A JSON column decoded as an object; NULL, corrupt JSON or a non-object value is ``{}``."""
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def activity_for_run(store: Store, run_id: str, *, after: int, limit: int) -> list[dict[str, Any]]:
    """``agent_activity`` event rows of a run with ``id > after``, oldest first; ``data`` is decoded."""
    rows = store.query(
        "SELECT * FROM events WHERE kind = 'agent_activity' AND run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after, limit),
    )
    for row in rows:
        row["data"] = _json_object(row.pop("data_json"))
    return rows


def latest_runs_by_issue(store: Store, repo: str) -> dict[int, RunRow]:
    """The latest run of each issue of ``repo``, ordered by ``created_at`` and then id."""
    rows = store.query(
        """
        SELECT * FROM runs r
        WHERE r.repo = ?
          AND r.id = (
            SELECT r2.id FROM runs r2
            WHERE r2.repo = r.repo AND r2.issue = r.issue
            ORDER BY r2.created_at DESC, r2.id DESC
            LIMIT 1
          )
        """,
        (repo,),
    )
    return {
        r["issue"]: RunRow(
            id=r["id"],
            repo=r["repo"],
            issue=r["issue"],
            playbook_id=r["playbook_id"],
            playbook_sha=r["playbook_sha"],
            status=r["status"],
            context=_json_object(r["context_json"]),
            ledger_comment_id=r["ledger_comment_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    }
