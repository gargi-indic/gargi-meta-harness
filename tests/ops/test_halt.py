import json
from unittest.mock import AsyncMock, Mock

import pytest

from harness.github.outbox import ledger_mirror_key
from harness.ops.halt import HaltState, kill_all
from harness.store.db import Store


@pytest.fixture
def store():
    store = Store(":memory:")
    store.migrate()
    try:
        yield store
    finally:
        store.close()


def test_halt_state_persist_across_reopen(store: Store) -> None:
    # 2. HaltState persists across reopen; halt/resume idempotent.
    outbox = Mock()
    control_issues = [("owner/repo1", 42), ("owner/repo2", 43)]

    halt_state = HaltState(store, outbox, control_issues)

    assert halt_state.halted() is None
    assert halt_state.get_budget_override() is None

    halt_state.halt("kill_switch", "test_user")

    # Verify the outbox enqueued the merge_ledger operations
    assert outbox.enqueue.call_count == 2

    # We must manually simulate the outbox mirror effect to test read side:
    for repo, issue in control_issues:
        key = ledger_mirror_key(repo, issue, "ops")
        val = {"halt": {"reason": "kill_switch", "by": "test_user", "since": store.now()}}
        store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(val)))

    # Read again
    halt_info = halt_state.halted()
    assert halt_info is not None
    assert halt_info.reason == "kill_switch"
    assert halt_info.by == "test_user"

    # Overwrite Daily Budget
    halt_state.set_daily_budget(40.0)
    for repo, issue in control_issues:
        key = ledger_mirror_key(repo, issue, "ops")
        # include previous halt
        val = {"halt": {"reason": "kill_switch", "by": "test_user", "since": store.now()}, "daily_usd": 40.0}
        store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(val)))

    # Persists across reopen
    new_halt_state = HaltState(store, outbox, control_issues)

    halt_info2 = new_halt_state.halted()
    assert halt_info2 is not None
    assert halt_info2.reason == "kill_switch"
    assert new_halt_state.get_budget_override() == 40.0

    # Resume
    new_halt_state.resume("api")
    for repo, issue in control_issues:
        key = ledger_mirror_key(repo, issue, "ops")
        val = {"halt": None, "daily_usd": 40.0}
        store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(val)))

    assert new_halt_state.halted() is None
    assert new_halt_state.get_budget_override() == 40.0


@pytest.mark.asyncio
async def test_kill_all_cancels_active_runs(store: Store):

    # Pre-setup Store state
    store.execute(
        "INSERT INTO runs (id, repo, issue, playbook_id, status, context_json, created_at, updated_at) VALUES ('r1', 'repo', 1, 'playbook', 'running', '{}', '2023-01-01', '2023-01-01')"
    )
    store.execute(
        "INSERT INTO runs (id, repo, issue, playbook_id, status, context_json, created_at, updated_at) VALUES ('r2', 'repo', 2, 'playbook', 'running', '{}', '2023-01-01', '2023-01-01')"
    )
    store.execute(
        "INSERT INTO runs (id, repo, issue, playbook_id, status, context_json, created_at, updated_at) VALUES ('r3', 'repo', 3, 'playbook', 'done', '{}', '2023-01-01', '2023-01-01')"
    )

    runner = Mock()

    # we need runner.cancel() to be async
    async def fake_cancel(run_id):
        if run_id == "r2":
            raise ValueError("Test error on r2")

    runner.cancel = AsyncMock(side_effect=fake_cancel)

    outbox = Mock()
    control_issues = [("repo", 99)]
    halt_state = HaltState(store, outbox, control_issues)

    report = await kill_all(runner, store, halt_state, by="test")

    assert runner.cancel.call_count == 2
    # Verify r2 had an error collected
    assert len(report.errors) == 1
    assert report.errors[0][0] == "r2"
    assert "Test error on r2" in report.errors[0][1]

    # Halt state is activated
    # we must manually mirror it to test read side:
    for repo, issue in control_issues:
        key = ledger_mirror_key(repo, issue, "ops")
        val = {"halt": {"reason": "kill_switch", "by": "test", "since": store.now()}}
        store.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(val)))

    assert halt_state.halted().reason == "kill_switch"

    # Add comment in outbox
    assert outbox.enqueue.call_count > 0
