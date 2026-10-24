"""Notification service, sanitising, the macOS notifier and the factory. No real osascript, no sleeps."""

import sys
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from harness import secrets
from harness.config import GlobalConfig, NotificationsConfig, parse_quiet_hours
from harness.ops.notifier import (
    MacOSNotifier,
    Notification,
    NotificationService,
    NullNotifier,
    RecordingNotifier,
    make_notifier,
    sanitize,
)
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow, append_event, get_meta, insert_run, insert_step

REPO = "o/r"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def store():
    s = Store(":memory:")
    s.migrate()
    yield s
    s.close()


def add_run(
    store: Store, issue: int, playbook: dict[str, Any], steps: list[tuple[str, str, str | None]]
) -> str:
    run_id = f"{REPO}#{issue}@1"
    insert_run(
        store,
        RunRow(
            id=run_id,
            repo=REPO,
            issue=issue,
            playbook_id="custom",
            playbook_sha=None,
            status="needs-you",
            context={"playbook": playbook},
            ledger_comment_id=None,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ),
    )
    for i, (node, status, outcome) in enumerate(steps):
        insert_step(
            store,
            StepRow(
                id=f"{run_id}/{node}/1",
                run_id=run_id,
                node_id=node,
                visit=1,
                status=status,
                outcome=outcome,
                ref=None,
                output=None,
                poll_after=None,
                started_at=f"2026-01-01T00:00:0{i}Z",
                ended_at=None,
            ),
        )
    return run_id


PLAYBOOK = {
    "nodes": {
        "plan_gate": {"kind": "gate", "gate": "plan"},
        "merge_gate": {"kind": "gate", "gate": "merge"},
        "other_gate": {"kind": "gate", "gate": "custom"},
        "escalate": {"kind": "escalate"},
        "hand": {"kind": "handoff"},
    }
}


def needs_you(store: Store, run_id: str, issue: int, node: str, reason: str = "please look") -> int:
    return append_event(
        store,
        kind="needs_you",
        data={"step": f"{run_id}/{node}/1", "node": node, "reason": reason},
        repo=REPO,
        issue=issue,
        run_id=run_id,
    )


def make(
    store: Store, clock: Clock | None = None, **kwargs: Any
) -> tuple[NotificationService, RecordingNotifier]:
    rec = RecordingNotifier()
    service = NotificationService(rec, clock=clock or Clock(), store=store, **kwargs)
    service.start_cursor()
    return service, rec


async def test_needs_you_is_classified_from_the_step_and_playbook(store):
    clock = Clock()
    service, rec = make(store, clock, dedupe_window=timedelta(0))
    cases = {
        "plan_gate": "plan_approval",
        "merge_gate": "merge_approval",
        "other_gate": "gate",
        "escalate": "escalation",
        "hand": "handoff",
    }
    for i, (node, _) in enumerate(cases.items(), start=1):
        run = add_run(store, i, PLAYBOOK, [(node, "waiting", None)])
        needs_you(store, run, i, node, reason="why " * 40)
        await service.poll_once()
        clock.advance(11)  # keep these out of one burst
    assert [n.kind for n in rec.sent] == list(cases.values())
    first = rec.sent[0]
    assert first.title == "Gargi Harness"
    assert first.body.startswith("#1 plan_approval: why why")
    assert len(first.body) <= len("#1 plan_approval: ") + 80


async def test_escalate_after_needs_input_is_a_question(store):
    service, rec = make(store)
    run = add_run(store, 1, PLAYBOOK, [("hand", "done", "needs_input"), ("escalate", "waiting", None)])
    needs_you(store, run, 1, "escalate")
    await service.poll_once()
    assert [n.kind for n in rec.sent] == ["question"]


async def test_unknown_step_or_node_is_an_escalation(store):
    service, rec = make(store)
    run = add_run(store, 1, PLAYBOOK, [("ghost", "waiting", None)])
    needs_you(store, run, 1, "ghost")
    await service.poll_once()
    assert [n.kind for n in rec.sent] == ["escalation"]


async def test_failure_is_off_by_default_and_on_when_enabled(store):
    def fail() -> None:
        append_event(
            store, kind="run_failed", data={"reason": "plan.failed: boom"}, repo=REPO, issue=4, run_id="r"
        )

    service, rec = make(store)
    fail()
    await service.poll_once()
    assert rec.sent == []

    service, rec = make(store, enabled_kinds={"failure"})
    fail()
    await service.poll_once()
    assert [(n.kind, n.body) for n in rec.sent] == [("failure", "#4 failure: plan.failed: boom")]


async def test_budget_events_notify_and_other_events_are_ignored(store):
    service, rec = make(store, dedupe_window=timedelta(0))
    append_event(
        store,
        kind="budget_warning",
        data={"spent_usd": 0.8, "budget_usd": 1.0},
        repo=REPO,
        issue=2,
        run_id="r",
    )
    append_event(
        store,
        kind="budget_exceeded",
        data={"spent_usd": 1.2, "budget_usd": 1.0},
        repo=REPO,
        issue=2,
        run_id="r",
    )
    append_event(store, kind="budget_unknown", data={}, repo=REPO, issue=2, run_id="r")
    append_event(store, kind="step_done", data={}, repo=REPO, issue=2, run_id="r")
    await service.poll_once()
    assert [n.kind for n in rec.sent] == ["budget", "budget"]
    assert rec.sent[0].body == "#2 budget: warning: $0.80 of $1.00"
    assert rec.sent[1].body == "#2 budget: exceeded: $1.20 of $1.00"


async def test_history_is_not_replayed(store):
    run = add_run(store, 1, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run, 1, "plan_gate")
    service, rec = make(store)  # the cursor starts after the existing event
    await service.poll_once()
    assert rec.sent == []
    needs_you(store, run, 1, "plan_gate")
    await service.poll_once()
    assert len(rec.sent) == 1


async def test_restart_and_second_instance_do_not_resend(store):
    run = add_run(store, 1, PLAYBOOK, [("plan_gate", "waiting", None)])
    clock = Clock()
    first, rec1 = make(store, clock)
    second, rec2 = make(store, clock)
    row_id = needs_you(store, run, 1, "plan_gate")
    await first.poll_once()
    await second.poll_once()
    assert len(rec1.sent) == 1 and rec2.sent == []
    assert get_meta(store, f"notified:{rec1.sent[0].key}") is not None
    assert rec1.sent[0].key == f"plan_approval:{run}:{row_id}"

    # a restarted instance whose cursor was reset (here: rewound) still does not re-send
    third, rec3 = make(store, clock)
    third._cursor = 0
    await third.poll_once()
    assert rec3.sent == []


async def test_disabled_kinds_are_ignored(store):
    service, rec = make(store, enabled_kinds={"merge_approval"})
    run = add_run(store, 1, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run, 1, "plan_gate")
    await service.poll_once()
    assert rec.sent == []


async def test_dedupe_window_by_kind_and_issue(store):
    clock = Clock()
    service, rec = make(store, clock, dedupe_window=timedelta(seconds=60))
    run = add_run(store, 1, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run, 1, "plan_gate")
    await service.poll_once()
    clock.advance(30)
    needs_you(store, run, 1, "plan_gate")
    await service.poll_once()
    assert len(rec.sent) == 1
    clock.advance(31)
    needs_you(store, run, 1, "plan_gate")
    await service.poll_once()
    assert len(rec.sent) == 2


async def test_burst_of_more_than_three_is_one_summary(store):
    service, rec = make(store)
    for issue in range(1, 6):
        run = add_run(store, issue, PLAYBOOK, [("plan_gate", "waiting", None)])
        needs_you(store, run, issue, "plan_gate")
    await service.poll_once()
    assert [(n.title, n.body) for n in rec.sent] == [("Gargi Harness", "5 items need you")]


async def test_three_are_individual_and_the_fourth_within_ten_seconds_is_a_summary(store):
    clock = Clock()
    service, rec = make(store, clock)
    for issue in (1, 2, 3):
        run = add_run(store, issue, PLAYBOOK, [("plan_gate", "waiting", None)])
        needs_you(store, run, issue, "plan_gate")
        await service.poll_once()
        clock.advance(1)
    assert len(rec.sent) == 3
    run = add_run(store, 4, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run, 4, "plan_gate")
    await service.poll_once()
    assert rec.sent[-1].body == "1 items need you"
    clock.advance(20)
    run = add_run(store, 5, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run, 5, "plan_gate")
    await service.poll_once()
    assert rec.sent[-1].body.startswith("#5 plan_approval")


async def test_quiet_hours_suppress_everything_except_budget(store):
    quiet = parse_quiet_hours("22:00-08:00")
    assert quiet == (time(22, 0), time(8, 0))
    clock = Clock(datetime(2026, 3, 1, 23, 30, tzinfo=UTC))
    service, rec = make(store, clock, quiet_hours=quiet, enabled_kinds={"plan_approval", "budget"})
    run = add_run(store, 1, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run, 1, "plan_gate")
    append_event(store, kind="budget_exceeded", data={}, repo=REPO, issue=1, run_id=run)
    await service.poll_once()
    assert [n.kind for n in rec.sent] == ["budget"]

    clock.now = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # the end is exclusive
    run2 = add_run(store, 2, PLAYBOOK, [("plan_gate", "waiting", None)])
    needs_you(store, run2, 2, "plan_gate")
    await service.poll_once()
    assert [n.kind for n in rec.sent] == ["budget", "plan_approval"]


def test_quiet_hours_within_one_day_and_validation():
    assert parse_quiet_hours("09:00-17:30") == (time(9, 0), time(17, 30))
    for bad in ("", "9-17", "25:00-08:00", "22:00-08:61", "22:00", "a:b-c:d"):
        with pytest.raises(ValueError):
            parse_quiet_hours(bad)
    with pytest.raises(ValidationError):
        NotificationsConfig(quiet_hours="late")
    with pytest.raises(ValidationError):
        NotificationsConfig(kinds=["nope"])
    with pytest.raises(ValidationError):
        GlobalConfig(notifications={"enabled": True, "other": 1})


async def test_notifications_sent_via_submit_are_sanitised_and_durably_deduped(store):
    service, rec = make(store)
    n = Notification(title="T", body="a\nb\x07c", kind="notify", key="notify:s1")
    assert await service.submit(n, issue=1)
    assert not await service.submit(n, issue=1)
    assert [x.body for x in rec.sent] == ["a bc"]


# ---- sanitising -------------------------------------------------------------------------------------


def test_sanitize_redacts_secrets_from_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue123")
    assert sanitize("leak ghp_supersecretvalue123 here") == "leak [redacted] here"
    assert secrets.redact("x") == "x"


def test_sanitize_strips_control_characters_and_collapses_whitespace():
    assert sanitize("a\x00b\x1b[31m\x9fc\n\n d\t\te ") == "ab[31mc d e"


def test_sanitize_caps_length_with_an_ellipsis():
    out = sanitize("x" * 500)
    assert len(out) == 200 and out.endswith("…")
    assert sanitize("short") == "short"
    assert sanitize("y" * 100, max_len=10) == "y" * 9 + "…"


def test_sanitize_strips_token_query_values():
    out = sanitize("open http://127.0.0.1:7777/auth?token=abc123DEF&x=1 now")
    assert "abc123DEF" not in out and "token=[redacted]" in out


# ---- macOS notifier and factory ---------------------------------------------------------------------


class FakeRunner:
    def __init__(self, code: int = 0, exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.code, self.exc = code, exc

    async def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        if self.exc:
            raise self.exc
        return self.code


async def test_macos_argv_has_the_script_and_the_text_as_separate_items():
    runner = FakeRunner()
    body = 'say "hi"; \\ `x` $(y)\nend run'
    evil = '"; do shell script "touch /tmp/pwned" --'
    await MacOSNotifier(runner).send(Notification(title=evil, body=body, kind="gate", key="k"))
    (argv,) = runner.calls
    assert argv == [
        "osascript",
        "-e",
        "on run argv",
        "-e",
        "display notification (item 1 of argv) with title (item 2 of argv)",
        "-e",
        "end run",
        "--",
        'say "hi"; \\ `x` $(y) end run',
        evil,
    ]
    assert not any("touch" in part for part in argv[:7])  # the script lines never carry the text


async def test_macos_failures_are_logged_not_raised(caplog):
    await MacOSNotifier(FakeRunner(code=1)).send(Notification(title="t", body="b", kind="gate", key="k"))
    await MacOSNotifier(FakeRunner(exc=FileNotFoundError("osascript"))).send(
        Notification(title="t", body="b", kind="gate", key="k")
    )
    assert "status 1" in caplog.text and "failed to start" in caplog.text


def test_factory_selects_by_platform():
    assert isinstance(make_notifier("auto", "darwin"), MacOSNotifier)
    assert isinstance(make_notifier("auto", "linux"), NullNotifier)
    assert isinstance(make_notifier("macos", "darwin"), MacOSNotifier)
    assert isinstance(make_notifier("null", "darwin"), NullNotifier)
    assert isinstance(make_notifier("recording"), RecordingNotifier)
    with pytest.raises(ValueError):
        make_notifier("bogus")


def test_factory_default_platform_is_sys_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(make_notifier("auto"), NullNotifier)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(make_notifier("auto"), MacOSNotifier)


def test_macos_backend_on_another_platform_warns_once_and_is_null(caplog):
    assert isinstance(make_notifier("macos", "linux"), NullNotifier)
    assert caplog.text.count("only available on macOS") == 1
