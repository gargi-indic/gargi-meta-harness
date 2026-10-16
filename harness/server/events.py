"""In-process SSE event bus, the wire format, and the tailer that feeds it from the SQLite event log.

SSE event types (the ``event:`` field on the wire). Every payload is
``{id, ts, repo, issue, run_id, kind, data}``: the event-log row, redacted.

- ``run.updated``: a run started or changed status (pause, resume, retry, done, failed, cancelled, needs-you).
- ``step.updated``: a step changed (waiting, done) or the engine hit a step-level problem.
- ``activity``: the agent reported an activity item; fetch it from ``/api/runs/{id}/activity``.
- ``inbox.changed``: the set of items that need the human may have changed; refetch ``/api/inbox``.
- ``issue.updated``: an issue's derived status or run may have changed; refetch board or graph.
- ``log``: any other event-log row (daemon, sync and engine diagnostics).

One event-log row publishes one bus event per mapped type, in the order of ``KIND_TO_TYPES``.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import events_since

log = logging.getLogger(__name__)

_PAGE = 500

EVT_RUN_UPDATED = "run.updated"
EVT_STEP_UPDATED = "step.updated"
EVT_ACTIVITY = "activity"
EVT_INBOX_CHANGED = "inbox.changed"
EVT_ISSUE_UPDATED = "issue.updated"
EVT_LOG = "log"

_RUN = (EVT_RUN_UPDATED, EVT_ISSUE_UPDATED)
_RUN_ATTENTION = (EVT_RUN_UPDATED, EVT_ISSUE_UPDATED, EVT_INBOX_CHANGED)

# Event-log ``kind`` to the SSE types it publishes. Any kind not listed publishes a single ``log`` event.
KIND_TO_TYPES: dict[str, tuple[str, ...]] = {
    "run_started": _RUN,
    "run_running": _RUN,
    "run_paused": _RUN,
    "run_needs-you": _RUN_ATTENTION,
    "run_done": _RUN_ATTENTION,
    "run_failed": _RUN_ATTENTION,
    "run_cancelled": _RUN_ATTENTION,
    "paused": _RUN,
    "resumed": _RUN,
    "retry": _RUN,
    "cancel_error": _RUN,
    "step_waiting": (EVT_STEP_UPDATED, EVT_INBOX_CHANGED),
    "step_done": (EVT_STEP_UPDATED, EVT_INBOX_CHANGED),
    "executor_error": (EVT_STEP_UPDATED,),
    "exhausted": (EVT_STEP_UPDATED,),
    "no_route": (EVT_STEP_UPDATED,),
    "unknown_node": (EVT_STEP_UPDATED,),
    "routing_loop": (EVT_STEP_UPDATED,),
    "needs_you": (EVT_INBOX_CHANGED, EVT_STEP_UPDATED),
    "agent_activity": (EVT_ACTIVITY,),
}


def types_for_kind(kind: str) -> tuple[str, ...]:
    """The SSE types an event-log kind publishes; ``run_<other status>`` behaves like ``run_running``."""
    mapped = KIND_TO_TYPES.get(kind)
    if mapped is not None:
        return mapped
    if kind.startswith("run_"):
        return _RUN
    return (EVT_LOG,)


class ServerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int
    type: str
    data: dict[str, Any]
    ts: str


class _Closed:
    """Sentinel put on every subscriber queue by ``EventBus.close``."""


_CLOSED = _Closed()


class EventBus:
    """Monotonic ``seq`` per event, a ring buffer for ``Last-Event-ID`` replay, one queue per subscriber."""

    def __init__(self, *, buffer_size: int = 500, clock: Callable[[], datetime] | None = None) -> None:
        self._buffer: deque[ServerEvent] = deque(maxlen=buffer_size)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._queues: set[asyncio.Queue[ServerEvent | _Closed]] = set()
        self._seq = 0
        self._closed = False

    @property
    def last_seq(self) -> int:
        return self._seq

    def publish(self, type: str, data: dict[str, Any]) -> int:
        self._seq += 1
        ts = self._clock().astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event = ServerEvent(seq=self._seq, type=type, data=data, ts=ts)
        self._buffer.append(event)
        for queue in self._queues:
            queue.put_nowait(event)
        return event.seq

    def close(self) -> None:
        """End every subscription; later ``subscribe`` calls replay their buffer and then stop."""
        if self._closed:
            return
        self._closed = True
        for queue in self._queues:
            queue.put_nowait(_CLOSED)

    async def subscribe(self, last_event_id: int | None = None) -> AsyncIterator[ServerEvent]:
        # No await between registering the queue and snapshotting the buffer, so no event is lost
        # between replay and live; the seq check below drops any that is in both.
        queue: asyncio.Queue[ServerEvent | _Closed] = asyncio.Queue()
        if not self._closed:
            self._queues.add(queue)
        replay = [] if last_event_id is None else [e for e in self._buffer if e.seq > last_event_id]
        seen = last_event_id if last_event_id is not None else self._seq
        try:
            for event in replay:
                seen = max(seen, event.seq)
                yield event
            if self._closed:
                return
            while True:
                item = await queue.get()
                if isinstance(item, _Closed):
                    return
                if item.seq <= seen:
                    continue
                seen = item.seq
                yield item
        finally:
            self._queues.discard(queue)


def _redact_value(value: Any) -> Any:
    """Redact every string (keys included) in a decoded JSON value, so JSON escaping cannot hide a secret."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {_redact_value(key): _redact_value(item) for key, item in value.items()}
    return value


def parse_last_event_id(value: str | None, last_seq: int) -> int | None:
    """The ``Last-Event-ID`` header as an int; invalid, or newer than the bus (an earlier launch), is None."""
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    if parsed is not None and parsed > last_seq:
        return None
    return parsed


def format_sse(event: ServerEvent) -> str:
    payload = json.dumps(event.data, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


async def sse_stream(bus: EventBus, last_event_id: int | None, *, keepalive_s: float) -> AsyncIterator[str]:
    """SSE text frames for the bus; a comment line whenever ``keepalive_s`` passes without an event."""
    events = bus.subscribe(last_event_id)
    pending: asyncio.Future[ServerEvent] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(events.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=keepalive_s)
            if not done:
                yield ": keepalive\n\n"
                continue
            finished, pending = pending, None
            try:
                event = finished.result()
            except StopAsyncIteration:
                return
            yield format_sse(event)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await events.aclose()


class EventLogTailer:
    """Polls the ``events`` table and publishes each new row to the bus exactly once, in id order."""

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        *,
        interval_s: float = 0.5,
        start_after: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._bus = bus
        self._interval_s = interval_s
        self._sleep = sleep
        if start_after is None:
            start_after = store.query("SELECT COALESCE(MAX(id), 0) AS last FROM events")[0]["last"]
        self._after: int = start_after

    def _publish_row(self, row: dict[str, Any]) -> bool:
        """Publish one row; a row that cannot be published is logged and skipped, never retried."""
        try:
            payload = {key: row[key] for key in ("id", "ts", "repo", "issue", "run_id", "kind")}
            payload["data"] = row["data"]
            redacted = _redact_value(payload)
            for event_type in types_for_kind(str(row["kind"])):
                self._bus.publish(event_type, redacted)
        except Exception as exc:
            log.warning(
                "event %s skipped, it could not be published: %s",
                row.get("id"),
                redact(f"{type(exc).__name__}: {exc}"),
            )
            return False
        finally:
            self._after = row["id"]
        return True

    async def poll_once(self) -> int:
        """Publish the rows after the last one seen; returns how many rows (not bus events).

        A failure is logged and retried.
        """
        published = 0
        try:
            while True:
                rows = events_since(self._store, self._after, limit=_PAGE)
                for row in rows:
                    if self._publish_row(row):
                        published += 1
                if len(rows) < _PAGE:
                    break
        except Exception as exc:
            log.warning("event log tail failed (will retry): %s", redact(f"{type(exc).__name__}: {exc}"))
        return published

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await self._sleep(self._interval_s)
