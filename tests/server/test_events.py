"""EventBus, the SSE wire format and the /api/events route (M2.1)."""

import asyncio
from datetime import UTC, datetime

from harness.server.events import EventBus, ServerEvent, parse_last_event_id, sse_stream
from tests.server.helpers import TOKEN, make_app, make_client

TIMEOUT = 2.0
FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_bus(**kwargs) -> EventBus:
    return EventBus(clock=lambda: FIXED, **kwargs)


async def take(subscription, count: int) -> list[ServerEvent]:
    async def collect() -> list[ServerEvent]:
        out = []
        async for event in subscription:
            out.append(event)
            if len(out) == count:
                break
        return out

    return await asyncio.wait_for(collect(), TIMEOUT)


def test_publish_assigns_increasing_seq() -> None:
    bus = make_bus()
    assert bus.last_seq == 0
    assert [bus.publish("a", {"n": i}) for i in range(3)] == [1, 2, 3]
    assert bus.last_seq == 3


async def test_subscribe_replays_after_last_event_id_then_goes_live() -> None:
    bus = make_bus()
    for i in (1, 2, 3):
        bus.publish("tick", {"n": i})
    task = asyncio.create_task(take(bus.subscribe(1), 3))
    await asyncio.sleep(0)
    bus.publish("tick", {"n": 4})
    events = await asyncio.wait_for(task, TIMEOUT)
    assert [(e.seq, e.data["n"]) for e in events] == [(2, 2), (3, 3), (4, 4)]
    assert events[0].ts == "2026-01-02T03:04:05.000Z"
    assert events[0].type == "tick"


async def test_subscribe_without_id_gets_only_live_events() -> None:
    bus = make_bus()
    bus.publish("old", {})
    task = asyncio.create_task(take(bus.subscribe(None), 1))
    await asyncio.sleep(0)
    bus.publish("new", {})
    (event,) = await asyncio.wait_for(task, TIMEOUT)
    assert (event.seq, event.type) == (2, "new")


async def test_ring_buffer_keeps_only_the_newest_events() -> None:
    bus = make_bus(buffer_size=3)
    for i in range(5):
        bus.publish("tick", {"n": i})
    bus.close()
    events = [e async for e in bus.subscribe(0)]
    assert [e.seq for e in events] == [3, 4, 5]


async def test_event_published_during_replay_is_neither_lost_nor_duplicated() -> None:
    bus = make_bus()
    for i in (1, 2, 3):
        bus.publish("tick", {"n": i})
    seen: list[int] = []

    async def consume() -> None:
        async for event in bus.subscribe(0):
            seen.append(event.seq)
            if event.seq == 1:
                bus.publish("tick", {"n": 4})
            if len(seen) == 4:
                return

    await asyncio.wait_for(consume(), TIMEOUT)
    assert seen == [1, 2, 3, 4]


async def test_close_ends_subscriptions_and_later_subscribers_only_replay() -> None:
    bus = make_bus()
    task = asyncio.create_task(take(bus.subscribe(None), 5))
    await asyncio.sleep(0)
    bus.publish("a", {})
    bus.close()
    bus.close()
    assert [e.seq for e in await asyncio.wait_for(task, TIMEOUT)] == [1]
    assert [e.seq async for e in bus.subscribe(0)] == [1]
    assert [e async for e in bus.subscribe(None)] == []


async def test_cancelled_subscriber_is_unregistered() -> None:
    bus = make_bus()
    task = asyncio.create_task(take(bus.subscribe(None), 1))
    await asyncio.sleep(0)
    assert len(bus._queues) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not bus._queues


async def test_sse_stream_wire_format() -> None:
    bus = make_bus()
    bus.publish("run.updated", {"id": "r1", "note": "naïve", "n": [1, 2]})
    bus.close()
    frames = await asyncio.wait_for(_collect(sse_stream(bus, 0, keepalive_s=5)), TIMEOUT)
    assert frames == ['id: 1\nevent: run.updated\ndata: {"id":"r1","note":"naïve","n":[1,2]}\n\n']


async def _collect(stream) -> list[str]:
    return [frame async for frame in stream]


async def test_sse_stream_emits_keepalives_when_idle() -> None:
    bus = make_bus()
    stream = sse_stream(bus, None, keepalive_s=0.01)
    first = await asyncio.wait_for(stream.__anext__(), TIMEOUT)
    second = await asyncio.wait_for(stream.__anext__(), TIMEOUT)
    assert first == second == ": keepalive\n\n"
    bus.publish("x", {})
    assert (await asyncio.wait_for(stream.__anext__(), TIMEOUT)).startswith("id: 1\nevent: x\n")
    bus.close()
    await asyncio.wait_for(_drain(stream), TIMEOUT)
    assert not bus._queues


async def _drain(stream) -> None:
    async for frame in stream:
        assert frame == ": keepalive\n\n"


async def test_sse_stream_close_while_idle_unregisters() -> None:
    bus = make_bus()
    stream = sse_stream(bus, None, keepalive_s=0.01)
    await asyncio.wait_for(stream.__anext__(), TIMEOUT)
    await stream.aclose()
    assert not bus._queues


async def test_events_route_streams_from_last_event_id() -> None:
    bus = make_bus()
    bus.publish("one", {"n": 1})
    bus.publish("two", {"n": 2})
    bus.close()
    async with make_client(make_app(bus=bus)) as client:
        response = await asyncio.wait_for(
            client.get("/api/events", headers={"Cookie": f"harness_token={TOKEN}", "Last-Event-ID": "1"}),
            TIMEOUT,
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == 'id: 2\nevent: two\ndata: {"n":2}\n\n'


async def test_events_route_ignores_an_invalid_last_event_id_and_needs_auth() -> None:
    bus = make_bus()
    bus.publish("one", {})
    bus.close()
    async with make_client(make_app(bus=bus)) as client:
        anonymous = await client.get("/api/events")
        response = await asyncio.wait_for(
            client.get("/api/events", headers={"Cookie": f"harness_token={TOKEN}", "Last-Event-ID": "abc"}),
            TIMEOUT,
        )
    assert anonymous.status_code == 401
    assert response.status_code == 200
    assert response.text == ""  # invalid id means live only, and the closed bus has nothing live


def test_parse_last_event_id() -> None:
    assert parse_last_event_id(None, 5) is None
    assert parse_last_event_id("abc", 5) is None
    assert parse_last_event_id("3", 5) == 3
    assert parse_last_event_id("5", 5) == 5
    assert parse_last_event_id("6", 5) is None  # from an earlier launch: live only
    assert parse_last_event_id("99", 0) is None


async def test_events_route_treats_a_stale_last_event_id_as_live_only() -> None:
    seen: list[int | None] = []

    class SpyBus(EventBus):
        def subscribe(self, last_event_id=None):
            seen.append(last_event_id)
            return super().subscribe(last_event_id)

    bus = SpyBus(clock=lambda: FIXED)
    bus.publish("one", {})
    bus.close()
    async with make_client(make_app(bus=bus)) as client:
        for header in ("99", "1"):
            await asyncio.wait_for(
                client.get(
                    "/api/events", headers={"Cookie": f"harness_token={TOKEN}", "Last-Event-ID": header}
                ),
                TIMEOUT,
            )
    assert seen == [None, 1]
