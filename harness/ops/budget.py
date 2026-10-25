from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from harness.ops.costs import UsageRepo, recorded_total
from harness.store.repo import list_runs


@dataclass(frozen=True)
class BudgetStatus:
    limit_usd: float | None
    spent_usd: float
    remaining_usd: float | None
    resets_at: str
    exceeded: bool
    unknown_cost_runs: int


class DailyBudget:
    """Spend since local midnight against a daily limit.

    The limit is the ledger override (``override()``, set by ``harness budget --set``) when there is one, else
    ``limit_usd`` (the ``[budget] daily_usd`` config). ``unknown_cost_runs`` counts runs created today for
    which no agent ever reported usage: their spend is unknown, not zero.
    """

    def __init__(
        self,
        repo: UsageRepo,
        *,
        limit_usd: float | None,
        override: Callable[[], float | None] | None = None,
        tz: tzinfo | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.limit_usd = limit_usd
        self.override = override
        self.tz = tz or datetime.now().astimezone().tzinfo or UTC
        self.clock = clock or (lambda: datetime.now(UTC))

    def _local_midnight(self) -> datetime:
        now = self.clock().astimezone(self.tz)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def day_start(self) -> str:
        """The last local midnight, as a store timestamp."""
        return self._local_midnight().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def status(self) -> BudgetStatus:
        midnight = self._local_midnight()
        midnight_utc = self.day_start()

        next_midnight = midnight + timedelta(days=1)
        resets_at = next_midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        spent_usd = self.repo.total_since(midnight_utc)

        unknown_cost_runs = 0
        all_runs = list_runs(self.repo.store)
        for run in all_runs:
            if run.created_at >= midnight_utc:
                if recorded_total(run.context) is None:
                    unknown_cost_runs += 1

        override = self.override() if self.override is not None else None
        limit = override if override is not None else self.limit_usd
        remaining_usd = None
        exceeded = False
        if limit is not None:
            remaining_usd = max(0.0, limit - spent_usd)
            exceeded = spent_usd >= limit

        return BudgetStatus(
            limit_usd=limit,
            spent_usd=spent_usd,
            remaining_usd=remaining_usd,
            resets_at=resets_at,
            exceeded=exceeded,
            unknown_cost_runs=unknown_cost_runs,
        )
