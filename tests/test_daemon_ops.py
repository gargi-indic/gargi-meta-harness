"""The daemon and the halt (M5.6): HALTED on start, dispatch gated, the daily cap, the outbox writing the ledger."""

import asyncio
from datetime import UTC
from pathlib import Path

from harness.config import BudgetConfig
from harness.github.ledger import find_ledger, parse_ledger
from harness.github.outbox import ledger_mirror_key
from harness.ops.costs import UsageRepo
from harness.store.repo import get_meta, list_runs
from tests.test_daemon import DaemonGH, events, start, until
from tests.test_daemon_dispatch import make_dispatch_app


async def test_a_halted_daemon_says_so_and_starts_nothing_until_resumed(tmp_path: Path, capsys) -> None:
    gh = DaemonGH()
    application = make_dispatch_app(tmp_path, gh)
    application.halt_state.halt("kill_switch", "cli")
    stop = asyncio.Event()
    task = start(application, stop, "host-a", dispatch_every=0.01, budget_every=0.01)
    await until(lambda: get_meta(application.store, ledger_mirror_key("o/r", 1, "ops")) is not None)

    assert "HALTED (kill_switch)" in capsys.readouterr().out
    assert any("HALTED (kill_switch)" in e["data"]["warning"] for e in events(application, "daemon_warning"))
    # the outbox wrote the record to the control issue ledger, then the mirror
    ledger = parse_ledger(find_ledger(gh.comments_by[("o/r", 1)])["body"])  # type: ignore[index]
    assert ledger["ops"]["halt"]["reason"] == "kill_switch"
    await asyncio.sleep(0.1)  # several dispatch ticks
    assert list_runs(application.store) == []

    application.halt_state.resume("cli")
    await until(lambda: len(list_runs(application.store)) == 1)
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_the_daily_cap_halts_the_daemon_and_gates_dispatch(tmp_path: Path) -> None:
    gh = DaemonGH()
    application = make_dispatch_app(tmp_path, gh)
    application.cfg.budget = BudgetConfig(daily_usd=5.0)
    application.daily_budget.limit_usd = 5.0
    application.daily_budget.tz = UTC
    UsageRepo(application.store).add(  # "today" on the app clock (T0 = 2027-01-15 08:00 UTC)
        run_id="x",
        step_id="x/s/1",
        source="agent",
        adapter="fake",
        cost_usd=6.0,
        created_at="2027-01-15T01:00:00Z",
    )
    stop = asyncio.Event()
    task = start(application, stop, "host-a", dispatch_every=0.01, budget_every=0.01)
    await until(lambda: application.halt_state.halted() is not None)
    await asyncio.sleep(0.1)
    halt = application.halt_state.halted()
    assert halt is not None and (halt.reason, halt.by) == ("daily_budget", "system")
    assert list_runs(application.store) == []  # gated, never started
    stop.set()
    await asyncio.wait_for(task, 2)
