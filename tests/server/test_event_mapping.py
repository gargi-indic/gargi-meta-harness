"""Event-log kinds publish the documented SSE types, in order, exactly once (M2.2)."""

import pytest

from harness.server.events import (
    EVT_ACTIVITY,
    EVT_INBOX_CHANGED,
    EVT_ISSUE_UPDATED,
    EVT_LOG,
    EVT_RUN_UPDATED,
    EVT_STEP_UPDATED,
    KIND_TO_TYPES,
    EventBus,
    EventLogTailer,
)
from harness.store.repo import append_event
from tests.server.helpers import make_store

RUN = (EVT_RUN_UPDATED, EVT_ISSUE_UPDATED)
RUN_ATTENTION = (EVT_RUN_UPDATED, EVT_ISSUE_UPDATED, EVT_INBOX_CHANGED)

EXPECTED = {
    "run_started": RUN,
    "run_running": RUN,
    "run_paused": RUN,
    "run_needs-you": RUN_ATTENTION,
    "run_done": RUN_ATTENTION,
    "run_failed": RUN_ATTENTION,
    "run_cancelled": RUN_ATTENTION,
    "paused": RUN,
    "resumed": RUN,
    "retry": RUN,
    "cancel_error": RUN,
    "step_waiting": (EVT_STEP_UPDATED, EVT_INBOX_CHANGED),
    "step_done": (EVT_STEP_UPDATED, EVT_INBOX_CHANGED),
    "executor_error": (EVT_STEP_UPDATED,),
    "exhausted": (EVT_STEP_UPDATED,),
    "no_route": (EVT_STEP_UPDATED,),
    "unknown_node": (EVT_STEP_UPDATED,),
    "routing_loop": (EVT_STEP_UPDATED,),
    "needs_you": (EVT_INBOX_CHANGED, EVT_STEP_UPDATED),
    "agent_activity": (EVT_ACTIVITY,),
    "gate_decided": (EVT_INBOX_CHANGED, EVT_STEP_UPDATED),
    "control_requested": (EVT_RUN_UPDATED,),
    "control_applied": (EVT_RUN_UPDATED, EVT_INBOX_CHANGED),
    "rerouted": (EVT_RUN_UPDATED, EVT_ISSUE_UPDATED),
    "playbook_missing": (EVT_LOG,),
    "ledger_too_large": (EVT_LOG,),
    "tick_error": (EVT_LOG,),
    "checklist_missing": (EVT_LOG,),
    "daemon_started": (EVT_LOG,),
    "sync_warning": (EVT_LOG,),
    "lock_lost": (EVT_LOG,),
    "something_new": (EVT_LOG,),
}


def test_documented_constants() -> None:
    assert (
        EVT_RUN_UPDATED,
        EVT_STEP_UPDATED,
        EVT_ACTIVITY,
        EVT_INBOX_CHANGED,
        EVT_ISSUE_UPDATED,
        EVT_LOG,
    ) == (
        "run.updated",
        "step.updated",
        "activity",
        "inbox.changed",
        "issue.updated",
        "log",
    )


@pytest.mark.parametrize("kind", sorted(EXPECTED))
async def test_each_kind_publishes_its_types_once_in_order(kind: str) -> None:
    store = make_store()
    bus = EventBus()
    tailer = EventLogTailer(store, bus)
    row_id = append_event(store, kind=kind, data={"x": 1}, repo="acme/r", issue=3, run_id="r1")
    assert await tailer.poll_once() == 1
    assert await tailer.poll_once() == 0
    bus.close()
    events = [e async for e in bus.subscribe(0)]
    assert tuple(e.type for e in events) == EXPECTED[kind]
    for event in events:
        assert event.data == {
            "id": row_id,
            "ts": event.data["ts"],
            "repo": "acme/r",
            "issue": 3,
            "run_id": "r1",
            "kind": kind,
            "data": {"x": 1},
        }
    assert [e.seq for e in events] == list(range(1, len(EXPECTED[kind]) + 1))


def test_the_table_matches_the_expectations() -> None:
    assert {k: v for k, v in EXPECTED.items() if k in KIND_TO_TYPES} == KIND_TO_TYPES


async def test_rows_publish_in_row_order() -> None:
    store = make_store()
    bus = EventBus()
    tailer = EventLogTailer(store, bus)
    append_event(store, kind="step_waiting", data={})
    append_event(store, kind="agent_activity", data={})
    append_event(store, kind="daemon_tick", data={})
    await tailer.poll_once()
    bus.close()
    assert [e.type for e in [e async for e in bus.subscribe(0)]] == [
        EVT_STEP_UPDATED,
        EVT_INBOX_CHANGED,
        EVT_ACTIVITY,
        EVT_LOG,
    ]
