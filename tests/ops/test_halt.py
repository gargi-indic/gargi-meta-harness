"""HaltState: GitHub first. Every write is an outbox ``merge_ledger`` op per repo; the mirror is the outbox's."""

import json
from datetime import UTC, datetime

import pytest

from harness.github.client import GitHub
from harness.github.outbox import Outbox, ledger_mirror_key
from harness.ops.halt import HaltState, NoControlIssue
from harness.store.db import Store
from harness.store.repo import get_meta
from harness.sync import sync_repo
from tests.planner.world import CONTROL, REPO, World

ISSUES = [("o/a", 1), ("o/b", 2)]


@pytest.fixture
def store():
    db = Store(":memory:", clock=lambda: datetime(2026, 3, 1, 13, 0, 0, tzinfo=UTC))
    db.migrate()
    yield db
    db.close()


def ops_rows(store: Store) -> list[dict]:
    rows = store.query("SELECT * FROM outbox WHERE op = 'merge_ledger' ORDER BY id")
    return [json.loads(r["payload_json"]) for r in rows]


def events(store: Store, kind: str) -> int:
    return len(store.query("SELECT 1 FROM events WHERE kind = ?", (kind,)))


def mirror(store: Store, repo: str, issue: int) -> str | None:
    return get_meta(store, ledger_mirror_key(repo, issue, "ops"))


def test_halt_queues_one_merge_ledger_per_repo_and_leaves_the_mirror_alone(store: Store) -> None:
    state = HaltState(store, Outbox(store, None), ISSUES)  # type: ignore[arg-type]
    assert state.halted() is None
    assert state.halt("kill_switch", "cli") is True

    rows = ops_rows(store)
    assert [(r["repo"], r["issue"], r["key"]) for r in rows] == [("o/a", 1, "ops"), ("o/b", 2, "ops")]
    for row in rows:
        assert row["value"]["halt"] == {"reason": "kill_switch", "since": "2026-03-01T13:00:00Z", "by": "cli"}
    assert [mirror(store, *i) for i in ISSUES] == [None, None]  # only GitHub accepting the op writes it
    info = state.halted()  # a queued halt counts at once
    assert info is not None and (info.reason, info.by) == ("kill_switch", "cli")
    assert events(store, "ops_halted") == 1


def test_halt_is_idempotent_and_a_kill_switch_is_not_downgraded(store: Store) -> None:
    state = HaltState(store, Outbox(store, None), ISSUES)  # type: ignore[arg-type]
    assert state.halt("daily_budget", "system") is True
    assert state.halt("daily_budget", "system") is False
    assert state.halt("kill_switch", "cli") is True  # upgrades the reason
    assert state.halt("daily_budget", "system") is False
    assert state.halted().reason == "kill_switch"  # type: ignore[union-attr]
    assert len(ops_rows(store)) == 2  # still one pending op per repo (coalesced)
    assert events(store, "ops_halted") == 2


def test_resume_and_budget_override_merge_into_one_record(store: Store) -> None:
    state = HaltState(store, Outbox(store, None), ISSUES)  # type: ignore[arg-type]
    assert state.resume("cli") is False  # nothing to clear: nothing written
    assert ops_rows(store) == [] and events(store, "ops_resumed") == 0

    state.halt("kill_switch", "cli")
    state.set_daily_budget(40.0)  # before anything was sent: must not lose the halt
    assert state.halted() is not None and state.get_budget_override() == 40.0
    state.resume("api")
    assert state.halted() is None and state.get_budget_override() == 40.0
    state.set_daily_budget(None)
    assert state.get_budget_override() is None
    assert ops_rows(store)[0]["value"] == {"halt": None, "daily_usd": None}
    assert (events(store, "ops_halted"), events(store, "ops_resumed"), events(store, "ops_budget")) == (
        1,
        1,
        2,
    )
    with pytest.raises(ValueError):
        state.set_daily_budget(-1)


def test_any_repo_halted_means_halted(store: Store) -> None:
    state = HaltState(store, Outbox(store, None), ISSUES)  # type: ignore[arg-type]
    record = {"halt": {"reason": "daily_budget", "since": "x", "by": "system"}, "daily_usd": None}
    store.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        (ledger_mirror_key("o/b", 2, "ops"), json.dumps(record)),
    )
    assert state.halted().reason == "daily_budget"  # type: ignore[union-attr]
    # the other repo lacks the halt: halting again heals it
    assert state.halt("daily_budget", "system") is True


def test_nowhere_to_record_is_an_error(store: Store) -> None:
    state = HaltState(store, Outbox(store, None), [])  # type: ignore[arg-type]
    with pytest.raises(NoControlIssue):
        state.halt("kill_switch", "cli")
    assert state.halted() is None


async def test_the_mirror_is_written_by_the_outbox_and_sync_restores_it_after_a_db_wipe(
    world: World, gh: GitHub, store: Store
) -> None:
    world.put_file(".harness/config.yaml", f"control_issue: {CONTROL}\n")
    outbox = Outbox(store, gh)
    state = HaltState(store, outbox, [(REPO, CONTROL)])
    state.halt("kill_switch", "cli")
    state.set_daily_budget(12.5)
    assert mirror(store, REPO, CONTROL) is None
    await outbox.drain_once()
    assert json.loads(mirror(store, REPO, CONTROL) or "")["halt"]["by"] == "cli"
    ledger = world.ledger()
    assert ledger is not None and ledger["ops"]["halt"]["reason"] == "kill_switch"
    assert ledger["ops"]["daily_usd"] == 12.5
    before = state.halted()
    store.close()

    wiped = Store(":memory:")
    wiped.migrate()
    fresh = HaltState(wiped, Outbox(wiped, gh), [(REPO, CONTROL)])
    assert fresh.halted() is None  # the cache is gone
    report = await sync_repo(gh, wiped, REPO)
    assert report.errors == []
    assert fresh.halted() == before
    assert fresh.get_budget_override() == 12.5
    wiped.close()
