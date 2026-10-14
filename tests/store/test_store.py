import sqlite3
from datetime import UTC, datetime

import pytest

from harness.store.db import Store
from harness.store.repo import (
    GateRow,
    IssueRow,
    RunRow,
    StepRow,
    active_steps,
    append_event,
    decide_gate,
    events_since,
    get_issue,
    get_run,
    insert_gate,
    insert_run,
    insert_step,
    list_issues,
    list_runs,
    pending_gates,
    steps_for_run,
    update_run,
    update_step,
    upsert_issue,
    upsert_repo,
)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    if request.param == "memory":
        s = Store(":memory:", clock=lambda: datetime(2026, 10, 14, 16, 24, 5, tzinfo=UTC))
    else:
        s = Store(tmp_path / "test.db", clock=lambda: datetime(2026, 10, 14, 16, 24, 5, tzinfo=UTC))
    s.migrate()
    yield s
    s.close()


def test_migrate_idempotent(store):
    store.migrate()
    rows = store.query("SELECT value FROM meta WHERE key = 'schema_version'")
    assert int(rows[0]["value"]) == 1


def test_failing_migration_rolls_back(tmp_path):
    import harness.store.db as db_mod

    original_migrations = db_mod.MIGRATIONS.copy()
    db_mod.MIGRATIONS = [
        ["CREATE TABLE dummy (id INT)", "INVALID SQL SYNTAX"],
    ]

    s = Store(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            s.migrate()

        rows = s.query("SELECT * FROM sqlite_master WHERE type='table' AND name='dummy'")
        assert len(rows) == 0

        rows = s.query("SELECT value FROM meta WHERE key = 'schema_version'")
        assert len(rows) == 0
    finally:
        db_mod.MIGRATIONS = original_migrations
        s.close()


def test_tx_rollback(store):
    with pytest.raises(ValueError, match="abort"):
        with store.tx():
            store.execute(
                "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('a/b', 'main', '{}', 'now')"
            )
            raise ValueError("abort")

    rows = store.query("SELECT * FROM repos")
    assert not rows


def test_reentrant_tx_nested_commit(store):
    with store.tx():
        store.execute(
            "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('inner/commit', 'main', '{}', 'now')"
        )
        with store.tx():
            store.execute(
                "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('inner/nested', 'main', '{}', 'now')"
            )

    rows = store.query("SELECT full_name FROM repos ORDER BY full_name")
    assert len(rows) == 2
    assert rows[0]["full_name"] == "inner/commit"
    assert rows[1]["full_name"] == "inner/nested"


def test_reentrant_tx_inner_rollback(store):
    with store.tx():
        store.execute(
            "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('outer/keep', 'main', '{}', 'now')"
        )
        try:
            with store.tx():
                store.execute(
                    "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('inner/discard', 'main', '{}', 'now')"
                )
                raise ValueError("abort inner")
        except ValueError:
            pass

    rows = store.query("SELECT full_name FROM repos")
    assert len(rows) == 1
    assert rows[0]["full_name"] == "outer/keep"


def test_reentrant_tx_outer_rollback(store):
    with pytest.raises(ValueError, match="abort outer"):
        with store.tx():
            with store.tx():
                store.execute(
                    "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('inner/ok', 'main', '{}', 'now')"
                )
            raise ValueError("abort outer")

    rows = store.query("SELECT * FROM repos")
    assert not rows


def test_tx_atomicity_update_and_event(store):
    insert_run(
        store,
        RunRow(
            id="run-1",
            repo="owner/repo",
            issue=1,
            playbook_id="pb",
            playbook_sha="abc",
            status="starting",
            context={},
            ledger_comment_id=None,
            created_at="now",
            updated_at="now",
        ),
    )

    with pytest.raises(ValueError, match="fake error"):
        with store.tx():
            update_run(store, "run-1", status="running")
            append_event(store, kind="run_started", data={}, run_id="run-1")
            raise ValueError("fake error")

    run = get_run(store, "run-1")
    assert run.status == "starting"
    events = store.query("SELECT * FROM events")
    assert len(events) == 0


def test_repo_issue_lifecycle(store):
    upsert_repo(store, "owner/repo", "main", {"setting": True})

    issue = IssueRow(
        repo="owner/repo",
        number=1,
        id=101,
        title="Title",
        body="Body",
        state="open",
        labels=["bug"],
        parent=None,
        blocked_by=[2],
        updated_at="2026-10-14T16:24:05Z",
    )
    upsert_issue(store, issue)

    saved = get_issue(store, "owner/repo", 1)
    assert saved == issue

    issues = list_issues(store, "owner/repo")
    assert len(issues) == 1
    assert issues[0].id == 101


def test_run_lifecycle(store):
    run = RunRow(
        id="run-1",
        repo="owner/repo",
        issue=1,
        playbook_id="pb1",
        playbook_sha="abc",
        status="starting",
        context={"foo": "bar"},
        ledger_comment_id=1001,
        created_at="2026-10-14T16:24:05Z",
        updated_at="2026-10-14T16:24:05Z",
    )
    insert_run(store, run)

    saved = get_run(store, "run-1")
    assert saved == run

    update_run(store, "run-1", status="running", context={"foo": "baz"})
    updated = get_run(store, "run-1")
    assert updated.status == "running"
    assert updated.context == {"foo": "baz"}
    assert updated.updated_at == "2026-10-14T16:24:05Z"

    runs = list_runs(store, status="running")
    assert len(runs) == 1
    assert list_runs(store, status="starting") == []

    with pytest.raises(KeyError):
        update_run(store, "missing", status="running")

    with pytest.raises(ValueError):
        update_run(store, "run-1", unknown="x")

    with pytest.raises(ValueError, match="context cannot be None"):
        update_run(store, "run-1", context=None)


def test_step_lifecycle(store):
    insert_run(
        store,
        RunRow(
            id="run-1",
            repo="owner/repo",
            issue=1,
            playbook_id="pb",
            playbook_sha="abc",
            status="running",
            context={},
            ledger_comment_id=None,
            created_at="now",
            updated_at="now",
        ),
    )

    step = StepRow(
        id="step-1",
        run_id="run-1",
        node_id="nodeA",
        visit=1,
        status="running",
        outcome=None,
        ref={"session_id": "s1"},
        output=None,
        poll_after=None,
        started_at="now",
        ended_at=None,
    )
    insert_step(store, step)

    steps = steps_for_run(store, "run-1")
    assert len(steps) == 1
    assert steps[0] == step

    active = active_steps(store)
    assert len(active) == 1

    update_step(store, "step-1", status="done", outcome="success", output={"result": 42})

    updated = steps_for_run(store, "run-1")[0]
    assert updated.status == "done"
    assert updated.outcome == "success"
    assert updated.output == {"result": 42}

    assert len(active_steps(store)) == 0

    with pytest.raises(KeyError):
        update_step(store, "missing", status="done")

    with pytest.raises(ValueError):
        update_step(store, "step-1", unknown="x")


def test_step_unique_constraint(store):
    insert_run(
        store,
        RunRow(
            id="run-1",
            repo="owner/repo",
            issue=1,
            playbook_id="pb",
            playbook_sha="abc",
            status="running",
            context={},
            ledger_comment_id=None,
            created_at="now",
            updated_at="now",
        ),
    )

    step = StepRow(
        id="step-1",
        run_id="run-1",
        node_id="nodeA",
        visit=1,
        status="running",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    insert_step(store, step)

    step2 = StepRow(
        id="step-2",
        run_id="run-1",
        node_id="nodeA",
        visit=1,
        status="running",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_step(store, step2)


def test_step_foreign_key(store):
    step = StepRow(
        id="step-1",
        run_id="missing-run",
        node_id="nodeA",
        visit=1,
        status="running",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_step(store, step)


def test_gate_lifecycle(store):
    gate = GateRow(
        id="gate-1",
        run_id="run-1",
        step_id="step-1",
        name="plan",
        status="pending",
        note=None,
        decided_by=None,
        created_at="now",
        decided_at=None,
    )
    insert_gate(store, gate)

    pending = pending_gates(store)
    assert len(pending) == 1

    decide_gate(store, "gate-1", "approved", "LGTM", "alice")

    pending = pending_gates(store)
    assert len(pending) == 0

    rows = store.query("SELECT * FROM gates WHERE id = 'gate-1'")
    assert isinstance(rows[0], dict)
    assert rows[0]["status"] == "approved"
    assert rows[0]["note"] == "LGTM"
    assert rows[0]["decided_by"] == "alice"

    with pytest.raises(KeyError):
        decide_gate(store, "missing", "approved", None, None)

    with pytest.raises(ValueError, match="is not pending"):
        decide_gate(store, "gate-1", "rejected", None, None)

    with pytest.raises(ValueError, match="Invalid status"):
        decide_gate(store, "gate-2", "invalid", None, None)  # type: ignore


def test_events(store):
    ev1 = append_event(store, kind="run_started", data={"k": "v"}, run_id="run-1")
    ev2 = append_event(store, kind="step_done", data={"k": "v2"}, run_id="run-1")

    events = events_since(store, ev1)
    assert len(events) == 1
    assert isinstance(events[0], dict)
    assert events[0]["id"] == ev2
    assert events[0]["kind"] == "step_done"
    assert events[0]["data"] == {"k": "v2"}
    assert events[0]["ts"] == "2026-10-14T16:24:05Z"

    for i in range(10):
        append_event(store, kind="loop", data={"i": i})

    events = events_since(store, ev2, limit=5)
    assert len(events) == 5


def test_active_steps_ordering(store):
    insert_run(
        store,
        RunRow(
            id="run-1",
            repo="owner/repo",
            issue=1,
            playbook_id="pb",
            playbook_sha="abc",
            status="running",
            context={},
            ledger_comment_id=None,
            created_at="now",
            updated_at="now",
        ),
    )

    step_null = StepRow(
        id="step-null",
        run_id="run-1",
        node_id="A",
        visit=1,
        status="waiting",
        outcome=None,
        ref=None,
        output=None,
        poll_after=None,
        started_at=None,
        ended_at=None,
    )
    step_dated = StepRow(
        id="step-dated",
        run_id="run-1",
        node_id="B",
        visit=1,
        status="waiting",
        outcome=None,
        ref=None,
        output=None,
        poll_after="2026-10-14T18:00:00Z",
        started_at=None,
        ended_at=None,
    )

    insert_step(store, step_dated)
    insert_step(store, step_null)

    active = active_steps(store)
    assert len(active) == 2
    assert active[0].id == "step-null"
    assert active[1].id == "step-dated"


def test_wal_active_for_file_db(tmp_path):
    Store(":memory:").close()

    file_store = Store(tmp_path / "test.db")
    rows = file_store.query("PRAGMA journal_mode")
    assert rows[0]["journal_mode"].lower() == "wal"
    file_store.close()
