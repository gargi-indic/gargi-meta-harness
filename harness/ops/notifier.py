"""Native notifications for things that wait on the human (M5.7).

The :class:`NotificationService` tails the SQLite event log (like :class:`harness.server.events.EventLogTailer`,
with its own cursor that starts at the newest row, so history is never replayed) and turns a few event kinds
into :class:`Notification` objects for a :class:`Notifier`. The daemon and ``harness serve`` are separate
processes that may both run the service, so every notification is recorded under a ``notified:<key>`` meta
key before it is sent: a restart or a second process never sends it again. That record is local on purpose: a
notification is an ephemeral local effect, not run state.

Notification text is sanitised (:func:`sanitize`) and handed to ``osascript`` as argv items, never
interpolated into the script source.
"""

import asyncio
import logging
import re
import sys
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, time, timedelta
from typing import Any, Protocol

from pydantic import BaseModel

from harness.config import DEFAULT_NOTIFICATION_KINDS, GlobalConfig
from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import events_since, get_meta, get_run, set_meta, steps_for_run

log = logging.getLogger(__name__)

TITLE = "Gargi Harness"
MAX_LEN = 200
REASON_LEN = 80
BURST_LIMIT = 3
BURST_WINDOW = timedelta(seconds=10)
# The notify node is authored by the playbook, so it is always allowed (it is not a configurable kind).
ALWAYS_KINDS = frozenset({"notify"})
_NEEDS_YOU_KINDS = {"plan_approval", "merge_approval", "gate", "question", "escalation", "handoff"}
_BUDGET_EVENTS = {"budget_warning", "budget_exceeded"}

_TOKEN_QUERY = re.compile(r"(token=)[^\s&#]*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def sanitize(text: str, max_len: int = MAX_LEN) -> str:
    """Redact secrets, drop control characters, collapse whitespace and cap the length with an ellipsis."""
    text = redact(str(text))
    text = _TOKEN_QUERY.sub(r"\1[redacted]", text)
    text = _SPACE.sub(" ", text)  # newlines and tabs become one space
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    text = _SPACE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max(max_len - 1, 0)].rstrip() + "…"
    return text


class Notification(BaseModel):
    title: str
    body: str
    kind: str
    key: str  # dedupe key


class Notifier(Protocol):
    async def send(self, n: Notification) -> None: ...


class NullNotifier:
    async def send(self, n: Notification) -> None:
        return None


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, n: Notification) -> None:
        self.sent.append(n)


Runner = Callable[[Sequence[str]], Awaitable[int]]

OSASCRIPT_LINES = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)


async def run_subprocess(argv: Sequence[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


class MacOSNotifier:
    """``osascript`` with the text passed as argv items: nothing is interpolated into the script source."""

    def __init__(self, runner: Runner = run_subprocess) -> None:
        self._runner = runner

    @staticmethod
    def argv(n: Notification) -> list[str]:
        out = ["osascript"]
        for line in OSASCRIPT_LINES:
            out += ["-e", line]
        return [*out, "--", sanitize(n.body), sanitize(n.title)]

    async def send(self, n: Notification) -> None:
        try:
            code = await self._runner(self.argv(n))
        except OSError as exc:
            log.warning("osascript failed to start: %s", exc)
            return
        if code != 0:
            log.warning("osascript exited with status %s", code)


def make_notifier(backend: str, platform: str | None = None) -> Notifier:
    platform = sys.platform if platform is None else platform  # read at call time (tests patch it)
    if backend == "recording":
        return RecordingNotifier()
    if backend == "null":
        return NullNotifier()
    if backend == "macos":
        if platform != "darwin":
            log.warning("notifications.backend = macos is only available on macOS; using the null notifier")
            return NullNotifier()
        return MacOSNotifier()
    if backend == "auto":
        return MacOSNotifier() if platform == "darwin" else NullNotifier()
    raise ValueError(f"unknown notification backend {backend!r}")


def _default_clock() -> datetime:
    return datetime.now().astimezone()


def in_quiet_hours(now: time, quiet: tuple[time, time]) -> bool:
    start, end = quiet
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # crosses midnight


class NotificationService:
    def __init__(
        self,
        notifier: Notifier,
        *,
        enabled_kinds: set[str] | None = None,
        dedupe_window: timedelta = timedelta(seconds=60),
        quiet_hours: tuple[time, time] | None = None,
        clock: Callable[[], datetime] = _default_clock,
        store: Store | None = None,
    ) -> None:
        self.notifier = notifier
        self.enabled_kinds = set(DEFAULT_NOTIFICATION_KINDS) if enabled_kinds is None else set(enabled_kinds)
        self.dedupe_window = dedupe_window
        self.quiet_hours = quiet_hours
        self.clock = clock
        self.store = store
        self._recent: dict[tuple[str, int | None], datetime] = {}
        self._sent_at: deque[datetime] = deque()
        self._pending: list[Notification] = []
        self._cursor: int | None = None

    @classmethod
    def from_config(
        cls,
        cfg: GlobalConfig,
        store: Store,
        notifier: Notifier,
        clock: Callable[[], datetime] = _default_clock,
    ) -> "NotificationService":
        from harness.config import parse_quiet_hours

        n = cfg.notifications
        return cls(
            notifier,
            enabled_kinds=set(n.kinds),
            dedupe_window=timedelta(seconds=n.dedupe_window_s),
            quiet_hours=parse_quiet_hours(n.quiet_hours) if n.quiet_hours else None,
            clock=clock,
            store=store,
        )

    # ---- submitting -------------------------------------------------------------------------------

    async def submit(self, n: Notification, *, issue: int | None = None, defer: bool = False) -> bool:
        """Queue ``n`` unless it is disabled, quiet, a duplicate or already sent; True when it was queued.

        Unless ``defer`` is set the queue is flushed at once (one notification per item, or one summary).
        """
        if n.kind not in self.enabled_kinds and n.kind not in ALWAYS_KINDS:
            return False
        n = n.model_copy(update={"title": sanitize(n.title), "body": sanitize(n.body)})
        now = self.clock()
        if (
            self.quiet_hours is not None
            and n.kind != "budget"
            and in_quiet_hours(now.time(), self.quiet_hours)
        ):
            return False
        recent_key = (n.kind, issue)
        last = self._recent.get(recent_key)
        # Two notify nodes of one issue are two deliberate messages: only the durable key dedupes them.
        if n.kind not in ALWAYS_KINDS and last is not None and now - last < self.dedupe_window:
            return False
        meta_key = f"notified:{n.key}"
        if self.store is not None:
            if get_meta(self.store, meta_key) is not None:
                return False
            set_meta(self.store, meta_key, now.isoformat())
        self._recent[recent_key] = now
        self._pending.append(n)
        if not defer:
            await self.flush()
        return True

    async def flush(self) -> None:
        """Send the queued notifications; more than three within ten seconds become one summary."""
        pending, self._pending = self._pending, []
        if not pending:
            return
        now = self.clock()
        while self._sent_at and now - self._sent_at[0] > BURST_WINDOW:
            self._sent_at.popleft()
        if len(self._sent_at) + len(pending) > BURST_LIMIT:
            summary = Notification(
                title=TITLE,
                body=f"{len(pending)} items need you",
                kind="summary",
                key=f"summary:{pending[0].key}",
            )
            pending = [summary]
        for n in pending:
            await self.notifier.send(n)
            self._sent_at.append(now)

    # ---- the event log ----------------------------------------------------------------------------

    def start_cursor(self) -> int:
        """Start at the newest event: history is never notified."""
        assert self.store is not None
        rows = self.store.query("SELECT MAX(id) AS m FROM events")
        self._cursor = int(rows[0]["m"] or 0)
        return self._cursor

    def notification_for(self, row: dict[str, Any]) -> Notification | None:
        """Map one event-log row to a notification, or None for an event that is ignored."""
        kind = row["kind"]
        issue = row.get("issue")
        data = row.get("data") or {}
        if kind == "needs_you":
            assert self.store is not None
            nk = self._needs_you_kind(row, data)
            reason = sanitize(str(data.get("reason") or ""), REASON_LEN)
            return self._make(nk, issue, reason, row)
        if kind == "run_failed":
            return self._make("failure", issue, sanitize(str(data.get("reason") or ""), REASON_LEN), row)
        if kind in _BUDGET_EVENTS:
            detail = kind.removeprefix("budget_").replace("_", " ")
            spent, cap = data.get("spent_usd"), data.get("budget_usd")
            if isinstance(spent, int | float) and isinstance(cap, int | float):
                detail += f": ${spent:.2f} of ${cap:.2f}"
            return self._make("budget", issue, sanitize(detail, REASON_LEN), row)
        return None

    def _needs_you_kind(self, row: dict[str, Any], data: dict[str, Any]) -> str:
        # Imported here: harness.server imports the engine, which imports this module.
        from harness.server.derive import inbox_kind, playbook_dict

        assert self.store is not None
        run = get_run(self.store, row["run_id"]) if row.get("run_id") else None
        steps = steps_for_run(self.store, run.id) if run is not None else []
        step = next((s for s in steps if s.id == data.get("step")), None)
        if run is None or step is None:
            return "escalation"
        kind = inbox_kind(step, steps, playbook_dict(run))
        return kind if kind in _NEEDS_YOU_KINDS else "escalation"

    @staticmethod
    def _make(kind: str, issue: int | None, detail: str, row: dict[str, Any]) -> Notification:
        label = f"#{issue} " if issue is not None else ""
        body = f"{label}{kind}: {detail}" if detail else f"{label}{kind}"
        return Notification(
            title=TITLE, body=sanitize(body), kind=kind, key=f"{kind}:{row.get('run_id')}:{row['id']}"
        )

    async def poll_once(self) -> int:
        """Handle the events after the cursor; returns how many rows were read."""
        assert self.store is not None
        if self._cursor is None:
            self.start_cursor()
        assert self._cursor is not None
        rows = events_since(self.store, self._cursor)
        for row in rows:
            self._cursor = row["id"]
            n = self.notification_for(row)
            if n is not None:
                await self.submit(n, issue=row.get("issue"), defer=True)
        await self.flush()
        return len(rows)

    async def run(
        self, *, interval: float = 1.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        """Poll until cancelled. Errors propagate: callers isolate them (the daemon records a daemon_error)."""
        self.start_cursor()
        while True:
            await self.poll_once()
            await sleep(interval)
