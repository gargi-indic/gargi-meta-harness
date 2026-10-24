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
