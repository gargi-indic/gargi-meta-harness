from datetime import UTC, datetime

import pytest

from harness.adapters.base import Usage
from harness.ops.costs import (
    UsageRepo,
    node_of_step,
    parse_since,
    record_cumulative,
    record_delta,
    recorded_total,
    restore_usage,
)
from harness.store.db import MIGRATIONS, Store
from harness.store.repo import RunRow, get_run, insert_run

RUN = "o/r#7@1"
STEP = f"{RUN}/plan/1"


@pytest.fixture
def store():
    s = Store(":memory:")
    s.migrate()
    yield s
    s.close()


def add_run(store: Store, run_id: str = RUN, issue: int = 7) -> None:
    insert_run(
        store,
        RunRow(
            id=run_id,
            repo="o/r",
            issue=issue,
            playbook_id="p",
            playbook_sha=None,
            status="running",
            context={},
            ledger_comment_id=None,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ),
    )


def test_migration_creates_the_usage_table(store):
    assert store.query("SELECT value FROM meta WHERE key = 'schema_version'")[0]["value"] == str(
        len(MIGRATIONS)
    )
    assert store.query("SELECT COUNT(*) AS n FROM usage")[0]["n"] == 0


def test_totals_since_filter_and_grouping(store):
    add_run(store)
    add_run(store, "o/r#8@1", 8)
    repo = UsageRepo(store)
    repo.add(
        run_id=RUN,
        step_id=STEP,
        source="agent",
        adapter="claude",
        cost_usd=1.0,
        created_at="2026-01-01T00:00:00Z",
    )
    repo.add(
        run_id=RUN,
        step_id=f"{RUN}/rev/1",
        source="review",
        adapter="reviewer",
        cost_usd=0.5,
        created_at="2026-01-02T00:00:00Z",
    )
    repo.add(
        run_id="o/r#8@1",
        step_id=None,
        source="agent",
        adapter="claude",
        cost_usd=2.0,
        estimated=True,
        created_at="2026-01-03T00:00:00Z",
    )

    assert repo.total_for_run(RUN) == pytest.approx(1.5)
    assert repo.total_for_run("nope") == 0.0
    assert repo.total_since() == pytest.approx(3.5)
    assert repo.total_since("2026-01-02T00:00:00Z") == pytest.approx(2.5)
    assert repo.by_adapter() == {"claude": pytest.approx(3.0), "reviewer": pytest.approx(0.5)}
    assert repo.by_adapter("2026-01-03T00:00:00Z") == {"claude": pytest.approx(2.0)}
    assert [(r["run_id"], r["issue"]) for r in repo.by_run()] == [("o/r#8@1", 8), (RUN, 7)]
    cost = repo.run_cost(RUN)
    assert cost.by_node == {"plan": pytest.approx(1.0), "rev": pytest.approx(0.5)}
    assert cost.estimated is False and repo.run_cost("o/r#8@1").estimated is True
    assert repo.run_cost("none").recorded is False


def test_node_of_step():
    assert node_of_step(STEP) == "plan"
    assert node_of_step(None) == "(run)"


def test_cumulative_readings_add_only_the_difference_and_survive_a_restart(store):
    add_run(store)
    first = Usage(input_tokens=100, output_tokens=10, cost_usd=1.0, model="m")
    assert record_cumulative(store, RUN, STEP, source="agent", adapter="claude", session_id="s1", usage=first)
    # A replayed poll (for example after a restart) reports the same cumulative reading: nothing to add.
    assert not record_cumulative(
        store, RUN, STEP, source="agent", adapter="claude", session_id="s1", usage=first
    )
    more = Usage(input_tokens=150, output_tokens=30, cost_usd=1.75, model="m")
    assert record_cumulative(store, RUN, STEP, source="agent", adapter="claude", session_id="s1", usage=more)

    assert UsageRepo(store).total_for_run(RUN) == pytest.approx(1.75)
    assert recorded_total(get_run(store, RUN).context) == pytest.approx(1.75)
    entry = get_run(store, RUN).context["usage"]["steps"][STEP]
    assert (entry["input_tokens"], entry["output_tokens"]) == (150, 30)


def test_a_counter_that_restarts_counts_the_new_reading_in_full(store):
    add_run(store)
    record_cumulative(
        store, RUN, STEP, source="agent", adapter="claude", session_id="s1", usage=Usage(cost_usd=2.0)
    )
    # The session was resumed in a new process: its counter starts from zero again.
    record_cumulative(
        store, RUN, STEP, source="agent", adapter="claude", session_id="s1", usage=Usage(cost_usd=0.5)
    )
    assert UsageRepo(store).total_for_run(RUN) == pytest.approx(2.5)


def test_two_steps_of_one_session_are_counted_once(store):
    add_run(store)
    other = f"{RUN}/implement/1"
    record_cumulative(
        store, RUN, STEP, source="agent", adapter="claude", session_id="s1", usage=Usage(cost_usd=1.0)
    )
    record_cumulative(
        store, RUN, other, source="agent", adapter="claude", session_id="s1", usage=Usage(cost_usd=1.5)
    )
    assert UsageRepo(store).run_cost(RUN).by_node == {
        "plan": pytest.approx(1.0),
        "implement": pytest.approx(0.5),
    }


def test_a_run_without_usage_has_no_total(store):
    add_run(store)
    assert recorded_total(get_run(store, RUN).context) is None
    assert not record_delta(
        store, "missing-run", STEP, source="review", adapter="reviewer", usage=Usage(cost_usd=1)
    )


def test_restore_refills_a_wiped_table_and_is_idempotent(store):
    add_run(store)
    record_cumulative(
        store,
        RUN,
        STEP,
        source="agent",
        adapter="claude",
        session_id="s1",
        usage=Usage(input_tokens=5, cost_usd=1.0),
    )
    record_delta(store, RUN, f"{RUN}/rev/1", source="review", adapter="reviewer", usage=Usage(cost_usd=0.25))
    run = get_run(store, RUN)
    snapshot = {"run": {"id": RUN, "updated_at": "2026-02-02T00:00:00Z"}, "usage": run.context["usage"]}

    # The table is a mirror: it comes back from the snapshot, and a second restore adds nothing.
    other = Store(":memory:")
    other.migrate()
    assert restore_usage(other, snapshot) == 2
    assert restore_usage(other, snapshot) == 0
    assert UsageRepo(other).total_for_run(RUN) == pytest.approx(1.25)
    assert UsageRepo(other).run_cost(RUN).by_node == {"plan": pytest.approx(1.0), "rev": pytest.approx(0.25)}
    # The original database already has the rows: nothing is added there either.
    assert restore_usage(store, snapshot) == 0
    assert UsageRepo(store).total_for_run(RUN) == pytest.approx(1.25)
    other.close()


def test_restore_ignores_snapshots_without_usage(store):
    assert restore_usage(store, {"run": {"id": RUN}}) == 0
    assert restore_usage(store, {"run": {"id": RUN}, "usage": "junk"}) == 0


def test_parse_since():
    now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)
    assert parse_since("24h", now) == "2026-01-09T12:00:00Z"
    assert parse_since("7d", now) == "2026-01-03T12:00:00Z"
    assert parse_since("2026-01-05", now) == "2026-01-05T00:00:00Z"
    assert parse_since("2026-01-05T10:00:00Z", now) == "2026-01-05T10:00:00Z"
    with pytest.raises(ValueError, match="invalid 'since'"):
        parse_since("yesterday", now)
