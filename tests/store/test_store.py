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


def test_tx_rollback(store):
    with pytest.raises(ValueError, match="abort"):
        with store.tx():
            store.execute(
                "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('a/b', 'main', '{}', 'now')"
            )
            raise ValueError("abort")

    rows = store.query("SELECT * FROM repos")
    assert not rows


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

    runs = list_runs(store, status="running")
    assert len(runs) == 1
    assert list_runs(store, status="starting") == []


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
    assert rows[0]["status"] == "approved"
    assert rows[0]["note"] == "LGTM"
    assert rows[0]["decided_by"] == "alice"


def test_events(store):
    ev1 = append_event(store, kind="run_started", data={"k": "v"}, run_id="run-1")
    ev2 = append_event(store, kind="step_done", data={"k": "v2"}, run_id="run-1")

    events = events_since(store, ev1)
    assert len(events) == 1
    assert events[0]["id"] == ev2
    assert events[0]["kind"] == "step_done"
    assert events[0]["data"] == {"k": "v2"}
    assert events[0]["ts"] == "2026-10-14T16:24:05+00:00"
