"""The ``proposals`` mirror table (migration 3) and its row functions."""

import pytest

import harness.store.db as db_mod
from harness.store.db import SCHEMA_VERSION, Store
from harness.store.repo import (
    delete_proposal_rows_of_repo,
    get_proposal_row,
    list_proposal_rows,
    save_proposal_row,
    update_proposal_row,
)


def test_the_migration_applies_on_an_existing_database(tmp_path) -> None:
    path = tmp_path / "old.db"
    full = db_mod.MIGRATIONS
    db_mod.MIGRATIONS = full[:2]  # a database as M0.4 and the control queue left it
    try:
        old = Store(path)
        old.migrate()
        old.execute(
            "INSERT INTO repos (full_name, default_branch, config_json, synced_at) VALUES ('a/b','main','{}','t')"
        )
        assert old.query("SELECT name FROM sqlite_master WHERE name = 'proposals'") == []
        old.close()
    finally:
        db_mod.MIGRATIONS = full
    store = Store(path)
    store.migrate()
    assert (
        int(store.query("SELECT value FROM meta WHERE key = 'schema_version'")[0]["value"]) == SCHEMA_VERSION
    )
    assert store.query("SELECT name FROM sqlite_master WHERE name = 'proposals'")
    assert store.query("SELECT full_name FROM repos") == [{"full_name": "a/b"}]
    cols = [r["name"] for r in store.query("PRAGMA table_info(proposals)")]
    assert cols == ["id", "run_id", "plan_sha", "json", "status", "created_at"]
    store.close()


@pytest.fixture
def store():
    s = Store(":memory:")
    s.migrate()
    yield s
    s.close()


def test_rows_round_trip(store: Store) -> None:
    row = save_proposal_row(
        store, run_id="o/r#1@1", plan_sha="abc1", json_text='{"a":1}', status="proposed", created_at="t1"
    )
    assert row.id >= 1 and row.json_text == '{"a":1}' and row.status == "proposed" and row.created_at == "t1"
    assert get_proposal_row(store, "o/r#1@1", "abc1") == row
    assert get_proposal_row(store, "o/r#1@1") == row
    assert get_proposal_row(store, "o/r#1@1", "zzz") is None and get_proposal_row(store, "o/r#2@1") is None


def test_saving_again_replaces_the_data_and_keeps_the_creation_time(store: Store) -> None:
    first = save_proposal_row(
        store, run_id="r", plan_sha="s1", json_text="1", status="proposed", created_at="t1"
    )
    again = save_proposal_row(
        store, run_id="r", plan_sha="s1", json_text="2", status="created", created_at="t9"
    )
    assert (
        again.id == first.id
        and again.json_text == "2"
        and again.status == "created"
        and again.created_at == "t1"
    )
    assert len(list_proposal_rows(store)) == 1


def test_the_newest_row_of_a_run_answers_without_a_sha(store: Store) -> None:
    save_proposal_row(store, run_id="r", plan_sha="s1", json_text="1", status="proposed", created_at="t1")
    newer = save_proposal_row(
        store, run_id="r", plan_sha="s2", json_text="2", status="proposed", created_at="t2"
    )
    assert get_proposal_row(store, "r") == newer
    assert [r.plan_sha for r in list_proposal_rows(store, "r")] == ["s1", "s2"]


def test_update_changes_fields_and_unknown_rows_raise(store: Store) -> None:
    save_proposal_row(store, run_id="r", plan_sha="s1", json_text="1", status="proposed", created_at="t1")
    update_proposal_row(store, "r", "s1", status="approved")
    update_proposal_row(store, "r", "s1", json_text="9")
    update_proposal_row(store, "r", "s1")
    row = get_proposal_row(store, "r", "s1")
    assert row is not None and row.status == "approved" and row.json_text == "9"
    with pytest.raises(KeyError):
        update_proposal_row(store, "r", "nope", status="x")


def test_deleting_by_repo_only_touches_that_repo(store: Store) -> None:
    for run in ("o/r#1@1", "o/r#2@1", "o/rr#1@1", "x/y#1@1"):
        save_proposal_row(store, run_id=run, plan_sha="s", json_text="1", status="proposed", created_at="t")
    delete_proposal_rows_of_repo(store, "o/r")
    assert [r.run_id for r in list_proposal_rows(store)] == ["o/rr#1@1", "x/y#1@1"]
