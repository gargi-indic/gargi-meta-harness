"""The notify executor notifies once per step, even when the step is re-advanced after a crash."""

from datetime import UTC, datetime

import pytest

from harness.engine import SimulatedCrash
from harness.engine.executors.notify import NotifyExecutor
from harness.ops.notifier import NotificationService, RecordingNotifier
from tests.engine.helpers import REPO, drive, make_issue, make_playbook, registry_for


async def test_notify_executor_notifies_once_after_a_crash(env):
    playbook = make_playbook({"n": {"kind": "notify", "message": "hello"}}, {"n.sent": "@done"})
    rec = RecordingNotifier()
    service = NotificationService(rec, store=env.store, clock=lambda: datetime.now(UTC))
    registry = registry_for(NotifyExecutor(), kinds=("notify",))
    first = env.runner(executors=registry, notify=service.submit)
    run_id = first.start_run(REPO, make_issue(), playbook, agent="fake")
    first.crash_after = "after_advance"
    with pytest.raises(SimulatedCrash):
        await first.tick()
    assert len(rec.sent) == 1

    second = env.runner(executors=registry_for(NotifyExecutor(), kinds=("notify",)), notify=service.submit)
    run = await drive(second, env, run_id)
    assert run.status == "done"
    assert [n.key for n in rec.sent] == [f"notify:{run_id}/n/1"]
    assert rec.sent[0].kind == "notify" and "hello" in rec.sent[0].body


async def test_notify_executor_without_a_service_is_unchanged(env):
    playbook = make_playbook({"n": {"kind": "notify", "message": "hello"}}, {"n.sent": "@done"})
    runner = env.runner(executors=registry_for(NotifyExecutor(), kinds=("notify",)))
    run_id = runner.start_run(REPO, make_issue(), playbook, agent="fake")
    assert (await drive(runner, env, run_id)).status == "done"
    assert [r["coalesce_key"] for r in env.outbox_rows("add_comment") if r["coalesce_key"]] == [
        f"notify:{run_id}/n/1"
    ]
