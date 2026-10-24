import zoneinfo
from datetime import UTC, datetime

import pytest

from harness.ops.budget import DailyBudget
from harness.ops.costs import UsageRepo
from harness.store.db import Store


@pytest.fixture
def store():
    store_obj = Store(":memory:")
    store_obj.migrate()
    try:
        yield store_obj
    finally:
        store_obj.close()


def test_daily_budget(store: Store) -> None:
    repo = UsageRepo(store)
    tz = zoneinfo.ZoneInfo("America/New_York")

    # Fake time: "2023-10-25 08:30:00 UTC" -> "2023-10-25 04:30:00 EDT"
    fake_now = datetime(2023, 10, 25, 8, 30, tzinfo=UTC)

    budget = DailyBudget(repo, limit_usd=50.0, tz=tz, clock=lambda: fake_now)

    # Midnight in EDT is "2023-10-25 00:00:00 EDT" -> "2023-10-25 04:00:00 UTC"
    status = budget.status()
    assert status.limit_usd == 50.0
    assert status.spent_usd == 0.0
    assert status.remaining_usd == 50.0
    assert status.resets_at == "2023-10-26T04:00:00Z"
    assert not status.exceeded
    assert status.unknown_cost_runs == 0

    # Let's add some usage BEFORE local midnight
    with store.tx():
        repo.add(
            run_id="run1",
            step_id="step1",
            source="test",
            adapter="test",
            model="test",
            input_tokens=0,
            output_tokens=0,
            cost_usd=10.0,
            estimated=False,
            created_at="2023-10-25T03:59:59Z",
        )

    status = budget.status()
    assert status.spent_usd == 0.0

    # Let's add some usage AFTER local midnight
    with store.tx():
        repo.add(
            run_id="run2",
            step_id="step1",
            source="test",
            adapter="test",
            model="test",
            input_tokens=0,
            output_tokens=0,
            cost_usd=20.0,
            estimated=False,
            created_at="2023-10-25T04:00:01Z",
        )

    status = budget.status()
    assert status.spent_usd == 20.0
    assert status.remaining_usd == 30.0
    assert not status.exceeded

    # Exceed budget
    with store.tx():
        repo.add(
            run_id="run3",
            step_id="step1",
            source="test",
            adapter="test",
            model="test",
            input_tokens=0,
            output_tokens=0,
            cost_usd=40.0,
            estimated=False,
            created_at="2023-10-25T05:00:00Z",
        )

    status = budget.status()
    assert status.spent_usd == 60.0
    assert status.remaining_usd == 0.0
    assert status.exceeded


def test_daily_budget_none_limit(store: Store) -> None:
    repo = UsageRepo(store)
    fake_now = datetime(2023, 10, 25, 8, 30, tzinfo=UTC)
    budget = DailyBudget(repo, limit_usd=None, tz=UTC, clock=lambda: fake_now)

    with store.tx():
        repo.add(
            run_id="run1",
            step_id="step1",
            source="test",
            adapter="test",
            model="test",
            input_tokens=0,
            output_tokens=0,
            cost_usd=1000.0,
            estimated=False,
            created_at="2023-10-25T01:00:00Z",
        )

    status = budget.status()
    assert status.limit_usd is None
    assert status.spent_usd == 1000.0
    assert status.remaining_usd is None
    assert not status.exceeded


def test_the_ledger_override_wins_over_the_config_limit(store: Store) -> None:
    repo = UsageRepo(store)
    now = datetime(2023, 10, 25, 8, 30, tzinfo=UTC)
    override: list[float | None] = [None]
    budget = DailyBudget(repo, limit_usd=50.0, override=lambda: override[0], tz=UTC, clock=lambda: now)
    repo.add(
        run_id="r", step_id="s", source="t", adapter="t", cost_usd=30.0, created_at="2023-10-25T01:00:00Z"
    )
    assert budget.status().limit_usd == 50.0 and not budget.status().exceeded
    override[0] = 20.0
    assert budget.status().limit_usd == 20.0 and budget.status().exceeded
    override[0] = None  # cleared: the config applies again
    assert budget.status().limit_usd == 50.0


def test_the_day_resets_at_local_midnight(store: Store) -> None:
    repo = UsageRepo(store)
    repo.add(
        run_id="r", step_id="s", source="t", adapter="t", cost_usd=60.0, created_at="2023-10-25T23:00:00Z"
    )
    before = DailyBudget(
        repo, limit_usd=50.0, tz=UTC, clock=lambda: datetime(2023, 10, 25, 23, 59, tzinfo=UTC)
    )
    after = DailyBudget(repo, limit_usd=50.0, tz=UTC, clock=lambda: datetime(2023, 10, 26, 0, 1, tzinfo=UTC))
    assert before.status().exceeded
    assert after.status().spent_usd == 0.0 and not after.status().exceeded
    assert after.status().resets_at == "2023-10-27T00:00:00Z"


def test_runs_started_today_without_any_reported_cost_are_counted(store: Store) -> None:
    from harness.store.repo import RunRow, insert_run

    def add_run(run_id: str, created_at: str, context: dict) -> None:
        insert_run(
            store,
            RunRow(
                id=run_id,
                repo="o/r",
                issue=1,
                playbook_id="p",
                playbook_sha=None,
                status="running",
                context=context,
                ledger_comment_id=None,
                created_at=created_at,
                updated_at=created_at,
            ),
        )

    usage = {"usage": {"steps": {"s1": {"cost_usd": 1.0}}}}
    add_run("o/r#1@1", "2023-10-25T05:00:00Z", {})  # today, nothing reported
    add_run("o/r#2@1", "2023-10-25T06:00:00Z", usage)  # today, reported
    add_run("o/r#3@1", "2023-10-24T06:00:00Z", {})  # yesterday
    budget = DailyBudget(
        UsageRepo(store), limit_usd=5.0, tz=UTC, clock=lambda: datetime(2023, 10, 25, 8, 0, tzinfo=UTC)
    )
    assert budget.status().unknown_cost_runs == 1
