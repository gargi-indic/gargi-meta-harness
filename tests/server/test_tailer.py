"""EventLogTailer: SQLite event log to the bus, exactly once, redacted (M2.1)."""

import asyncio
from functools import partial

import pytest

from harness.server import app as app_module
from harness.server import events as events_module
from harness.server.events import EventBus, EventLogTailer
from harness.store.repo import append_event
from tests.server.helpers import make_app, make_store

SECRET = "sk-test-0123456789abcdef"
TIMEOUT = 2.0


async def buffered(bus: EventBus) -> list:
    bus.close()
    return [event async for event in bus.subscribe(0)]


async def test_publishes_new_rows_once_in_order() -> None:
    store = make_store()
    append_event(store, kind="before", data={})
    bus = EventBus()
    tailer = EventLogTailer(store, bus)
    first = append_event(store, kind="run_started", data={"a": 1}, repo="acme/r", issue=5, run_id="run1")
    second = append_event(store, kind="step_done", data={"b": [2]})
    assert await tailer.poll_once() == 2
    third = append_event(store, kind="run_done", data={})
    assert await tailer.poll_once() == 1
    assert await tailer.poll_once() == 0
    events = await buffered(bus)
    assert [e.type for e in events] == ["run_started", "step_done", "run_done"]
    assert [e.data["id"] for e in events] == [first, second, third]
    assert events[0].data["repo"] == "acme/r"
    assert events[0].data["issue"] == 5
    assert events[0].data["run_id"] == "run1"
    assert events[0].data["kind"] == "run_started"
    assert events[0].data["data"] == {"a": 1}
    assert events[0].data["ts"]


async def test_start_after_overrides_the_starting_point() -> None:
    store = make_store()
    append_event(store, kind="one", data={})
    append_event(store, kind="two", data={})
    bus = EventBus()
    assert await EventLogTailer(store, bus, start_after=0).poll_once() == 2


async def test_pages_past_five_hundred_rows() -> None:
    store = make_store()
    bus = EventBus(buffer_size=1000)
    tailer = EventLogTailer(store, bus)
    for i in range(600):
        append_event(store, kind="bulk", data={"i": i})
    assert await tailer.poll_once() == 600
    events = await buffered(bus)
    assert [e.data["data"]["i"] for e in events] == list(range(600))


async def test_secrets_are_redacted_before_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JULES_API_KEY", SECRET)
    store = make_store()
    bus = EventBus()
    tailer = EventLogTailer(store, bus)
    append_event(store, kind="agent_output", data={"text": f"key={SECRET}", "nested": [SECRET]})
    await tailer.poll_once()
    (event,) = await buffered(bus)
    assert SECRET not in event.model_dump_json()
    assert event.data["data"] == {"text": "key=[redacted]", "nested": ["[redacted]"]}


async def test_store_error_is_logged_and_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JULES_API_KEY", SECRET)
    store = make_store()
    bus = EventBus()
    tailer = EventLogTailer(store, bus)
    append_event(store, kind="one", data={})
    real = events_module.events_since
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"database is locked {SECRET}")
        return real(*args, **kwargs)

    monkeypatch.setattr(events_module, "events_since", flaky)
    assert await tailer.poll_once() == 0
    assert SECRET not in caplog.text
    assert "retry" in caplog.text
    assert await tailer.poll_once() == 1
    assert [e.type for e in await buffered(bus)] == ["one"]


async def test_run_polls_then_sleeps_until_cancelled() -> None:
    store = make_store()
    bus = EventBus()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            append_event(store, kind="late", data={})
        elif len(sleeps) == 3:
            raise asyncio.CancelledError

    tailer = EventLogTailer(store, bus, interval_s=0.25, sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tailer.run(), TIMEOUT)
    assert sleeps == [0.25, 0.25, 0.25]
    assert [e.type for e in await buffered(bus)] == ["late"]


async def test_lifespan_runs_the_tailer_and_stops_it_with_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store()
    bus = EventBus()
    slept = asyncio.Event()
    go = asyncio.Event()
    state = {"cancelled": False}

    async def gated_sleep(_seconds: float) -> None:
        slept.set()
        try:
            await go.wait()
            go.clear()  # one tick per release
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(app_module, "EventLogTailer", partial(EventLogTailer, sleep=gated_sleep))
    app = make_app(store=store, bus=bus, tail_events=True)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(slept.wait(), TIMEOUT)  # first poll done, tailer is sleeping
        assert bus.last_seq == 0
        slept.clear()
        append_event(store, kind="while_serving", data={})
        go.set()
        await asyncio.wait_for(slept.wait(), TIMEOUT)  # second poll done
        assert bus.last_seq == 1
    assert state["cancelled"] is True
    assert [e.type for e in await buffered(bus)] == ["while_serving"]  # and the bus was closed


async def test_a_row_that_cannot_be_published_is_skipped_not_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JULES_API_KEY", SECRET)

    class PickyBus(EventBus):
        def publish(self, type: str, data):
            if type == "poison":
                raise ValueError(f"cannot serialise {SECRET}")
            return super().publish(type, data)

    store = make_store()
    bus = PickyBus()
    tailer = EventLogTailer(store, bus)
    append_event(store, kind="good1", data={})
    append_event(store, kind="poison", data={})
    append_event(store, kind="good2", data={})
    assert await tailer.poll_once() == 2
    assert await tailer.poll_once() == 0
    assert [e.type for e in await buffered(bus)] == ["good1", "good2"]
    assert "skipped" in caplog.text
    assert SECRET not in caplog.text


async def test_redaction_walks_decoded_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = 'pa"ss\\word-0123456789'  # JSON escaping would change this text
    monkeypatch.setenv("JULES_API_KEY", secret)
    store = make_store()
    bus = EventBus()
    tailer = EventLogTailer(store, bus)
    append_event(store, kind="agent_output", data={"text": f"x {secret} y", secret: [secret, 3, None]})
    await tailer.poll_once()
    (event,) = await buffered(bus)
    assert secret not in event.model_dump_json()
    assert event.data["data"] == {"text": "x [redacted] y", "[redacted]": ["[redacted]", 3, None]}
