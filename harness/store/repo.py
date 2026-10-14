import json
from typing import Any, Literal

from pydantic import BaseModel

from harness.store.db import Store


class IssueRow(BaseModel, extra="forbid"):
    repo: str
    number: int
    id: int
    title: str
    body: str | None
    state: str
    labels: list[str]
    parent: int | None
    blocked_by: list[int]
    updated_at: str


class RunRow(BaseModel, extra="forbid"):
    id: str
    repo: str
    issue: int
    playbook_id: str
    playbook_sha: str | None
    status: str
    context: dict[str, Any]
    ledger_comment_id: int | None
    created_at: str
    updated_at: str


class StepRow(BaseModel, extra="forbid"):
    id: str
    run_id: str
    node_id: str
    visit: int
    status: str
    outcome: str | None
    ref: dict[str, Any] | None
    output: dict[str, Any] | None
    poll_after: str | None
    started_at: str | None
    ended_at: str | None


class GateRow(BaseModel, extra="forbid"):
    id: str
    run_id: str
    step_id: str
    name: str
    status: str
    note: str | None
    decided_by: str | None
    created_at: str
    decided_at: str | None


def _parse_json_col(val: str | None, default: Any = None) -> Any:
    if val is None:
        return default
    return json.loads(val)


def upsert_repo(
    store: Store, full_name: str, default_branch: str | None, config: dict[str, Any] | None
) -> None:
    now = store.clock().isoformat()
    config_json = json.dumps(config) if config is not None else None

    store.execute(
        """
        INSERT INTO repos (full_name, default_branch, config_json, synced_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(full_name) DO UPDATE SET
            default_branch = excluded.default_branch,
            config_json = excluded.config_json,
            synced_at = excluded.synced_at
        """,
        (full_name, default_branch, config_json, now),
    )


def upsert_issue(store: Store, issue: IssueRow) -> None:
    store.execute(
        """
        INSERT INTO issues (repo, number, id, title, body, state, labels_json, parent, blocked_by_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, number) DO UPDATE SET
            id = excluded.id,
            title = excluded.title,
            body = excluded.body,
            state = excluded.state,
            labels_json = excluded.labels_json,
            parent = excluded.parent,
            blocked_by_json = excluded.blocked_by_json,
            updated_at = excluded.updated_at
        """,
        (
            issue.repo,
            issue.number,
            issue.id,
            issue.title,
            issue.body,
            issue.state,
            json.dumps(issue.labels),
            issue.parent,
            json.dumps(issue.blocked_by),
            issue.updated_at,
        ),
    )


def get_issue(store: Store, repo: str, number: int) -> IssueRow | None:
    rows = store.query("SELECT * FROM issues WHERE repo = ? AND number = ?", (repo, number))
    if not rows:
        return None
    r = rows[0]
    return IssueRow(
        repo=r["repo"],
        number=r["number"],
        id=r["id"],
        title=r["title"],
        body=r["body"],
        state=r["state"],
        labels=_parse_json_col(r["labels_json"], []),
        parent=r["parent"],
        blocked_by=_parse_json_col(r["blocked_by_json"], []),
        updated_at=r["updated_at"],
    )


def list_issues(store: Store, repo: str) -> list[IssueRow]:
    rows = store.query("SELECT * FROM issues WHERE repo = ? ORDER BY number", (repo,))
    return [
        IssueRow(
            repo=r["repo"],
            number=r["number"],
            id=r["id"],
            title=r["title"],
            body=r["body"],
            state=r["state"],
            labels=_parse_json_col(r["labels_json"], []),
            parent=r["parent"],
            blocked_by=_parse_json_col(r["blocked_by_json"], []),
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def insert_run(store: Store, run: RunRow) -> None:
    store.execute(
        """
        INSERT INTO runs (id, repo, issue, playbook_id, playbook_sha, status, context_json, ledger_comment_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run.id,
            run.repo,
            run.issue,
            run.playbook_id,
            run.playbook_sha,
            run.status,
            json.dumps(run.context),
            run.ledger_comment_id,
            run.created_at,
            run.updated_at,
        ),
    )


def update_run(store: Store, run_id: str, **fields: Any) -> None:
    if not fields:
        return

    allowed_fields = {"status", "context", "ledger_comment_id"}
    for k in fields:
        if k not in allowed_fields:
            raise ValueError(f"Unknown field {k} for run update")

    now = store.clock().isoformat()
    fields["updated_at"] = now

    set_clauses = []
    params = []
    for k, v in fields.items():
        if k == "context":
            set_clauses.append("context_json = ?")
            params.append(json.dumps(v))
        else:
            set_clauses.append(f"{k} = ?")
            params.append(v)

    params.append(run_id)

    with store.tx():
        cursor = store.execute(f"UPDATE runs SET {', '.join(set_clauses)} WHERE id = ?", params)
        if cursor.rowcount == 0:
            raise KeyError(f"Run {run_id} not found")


def get_run(store: Store, run_id: str) -> RunRow | None:
    rows = store.query("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not rows:
        return None
    r = rows[0]
    return RunRow(
        id=r["id"],
        repo=r["repo"],
        issue=r["issue"],
        playbook_id=r["playbook_id"],
        playbook_sha=r["playbook_sha"],
        status=r["status"],
        context=_parse_json_col(r["context_json"], {}),
        ledger_comment_id=r["ledger_comment_id"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def list_runs(store: Store, status: str | None = None) -> list[RunRow]:
    if status is not None:
        rows = store.query("SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC", (status,))
    else:
        rows = store.query("SELECT * FROM runs ORDER BY created_at DESC")

    return [
        RunRow(
            id=r["id"],
            repo=r["repo"],
            issue=r["issue"],
            playbook_id=r["playbook_id"],
            playbook_sha=r["playbook_sha"],
            status=r["status"],
            context=_parse_json_col(r["context_json"], {}),
            ledger_comment_id=r["ledger_comment_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def insert_step(store: Store, step: StepRow) -> None:
    store.execute(
        """
        INSERT INTO steps (id, run_id, node_id, visit, status, outcome, ref_json, output_json, poll_after, started_at, ended_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step.id,
            step.run_id,
            step.node_id,
            step.visit,
            step.status,
            step.outcome,
            json.dumps(step.ref) if step.ref is not None else None,
            json.dumps(step.output) if step.output is not None else None,
            step.poll_after,
            step.started_at,
            step.ended_at,
        ),
    )


def update_step(store: Store, step_id: str, **fields: Any) -> None:
    if not fields:
        return

    allowed_fields = {"status", "outcome", "ref", "output", "poll_after", "started_at", "ended_at"}
    for k in fields:
        if k not in allowed_fields:
            raise ValueError(f"Unknown field {k} for step update")

    set_clauses = []
    params = []
    for k, v in fields.items():
        if k in ("ref", "output"):
            set_clauses.append(f"{k}_json = ?")
            params.append(json.dumps(v) if v is not None else None)
        else:
            set_clauses.append(f"{k} = ?")
            params.append(v)

    params.append(step_id)

    with store.tx():
        cursor = store.execute(f"UPDATE steps SET {', '.join(set_clauses)} WHERE id = ?", params)
        if cursor.rowcount == 0:
            raise KeyError(f"Step {step_id} not found")


def steps_for_run(store: Store, run_id: str) -> list[StepRow]:
    rows = store.query("SELECT * FROM steps WHERE run_id = ? ORDER BY started_at, id", (run_id,))
    return [
        StepRow(
            id=r["id"],
            run_id=r["run_id"],
            node_id=r["node_id"],
            visit=r["visit"],
            status=r["status"],
            outcome=r["outcome"],
            ref=_parse_json_col(r["ref_json"]),
            output=_parse_json_col(r["output_json"]),
            poll_after=r["poll_after"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
        )
        for r in rows
    ]


def active_steps(store: Store) -> list[StepRow]:
    rows = store.query(
        "SELECT * FROM steps WHERE status IN ('running', 'waiting') ORDER BY poll_after IS NULL, poll_after"
    )
    return [
        StepRow(
            id=r["id"],
            run_id=r["run_id"],
            node_id=r["node_id"],
            visit=r["visit"],
            status=r["status"],
            outcome=r["outcome"],
            ref=_parse_json_col(r["ref_json"]),
            output=_parse_json_col(r["output_json"]),
            poll_after=r["poll_after"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
        )
        for r in rows
    ]


def insert_gate(store: Store, gate: GateRow) -> None:
    store.execute(
        """
        INSERT INTO gates (id, run_id, step_id, name, status, note, decided_by, created_at, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gate.id,
            gate.run_id,
            gate.step_id,
            gate.name,
            gate.status,
            gate.note,
            gate.decided_by,
            gate.created_at,
            gate.decided_at,
        ),
    )


def decide_gate(
    store: Store,
    gate_id: str,
    status: Literal["approved", "rejected"],
    note: str | None,
    decided_by: str | None,
) -> None:
    with store.tx():
        rows = store.query("SELECT status FROM gates WHERE id = ?", (gate_id,))
        if not rows:
            raise KeyError(f"Gate {gate_id} not found")
        if rows[0]["status"] != "pending":
            raise ValueError(f"Gate {gate_id} is not pending")

        now = store.clock().isoformat()
        store.execute(
            "UPDATE gates SET status = ?, note = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (status, note, decided_by, now, gate_id),
        )


def pending_gates(store: Store) -> list[GateRow]:
    rows = store.query("SELECT * FROM gates WHERE status = 'pending' ORDER BY created_at")
    return [
        GateRow(
            id=r["id"],
            run_id=r["run_id"],
            step_id=r["step_id"],
            name=r["name"],
            status=r["status"],
            note=r["note"],
            decided_by=r["decided_by"],
            created_at=r["created_at"],
            decided_at=r["decided_at"],
        )
        for r in rows
    ]


def append_event(
    store: Store,
    *,
    kind: str,
    data: dict[str, Any],
    repo: str | None = None,
    issue: int | None = None,
    run_id: str | None = None,
) -> int:
    now = store.clock().isoformat()
    cursor = store.execute(
        "INSERT INTO events (ts, repo, issue, run_id, kind, data_json) VALUES (?, ?, ?, ?, ?, ?)",
        (now, repo, issue, run_id, kind, json.dumps(data)),
    )
    return cursor.lastrowid  # type: ignore


def events_since(store: Store, after_id: int, limit: int = 500) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
    # Unpack JSON here to be helpful, matching the _json drop pattern
    for r in rows:
        r["data"] = _parse_json_col(r["data_json"])
        del r["data_json"]
    return rows
