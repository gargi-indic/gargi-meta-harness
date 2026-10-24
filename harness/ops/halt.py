"""The global halt (kill switch, daily cap) and the daily budget override: GitHub is the state store.

The record ``{"halt": {"reason", "since", "by"} | null, "daily_usd": number | null}`` lives under the ledger key
``ops`` of the control issue of EVERY registered repo. Writing it only enqueues an outbox ``merge_ledger`` op per
repo (exactly like the plan approval); the local mirror (``ledger_mirror_key(repo, issue, "ops")``) is written
by the outbox after GitHub accepted the op, and ``harness sync`` rebuilds it from the ledger.

The read side treats the harness as halted when any repo says so. While an op is still queued the queued value
is used instead of the (older) mirror, so a halt takes effect at once even when GitHub is unreachable.

A halt set by the daily cap (``reason == "daily_budget"``) is never cleared by itself: neither a new day nor a
raised cap lifts it. ``harness resume`` does, explicitly; if the cap is still exceeded the daemon sets it again.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from harness.config import GlobalConfig
from harness.engine.control import enqueue
from harness.engine.dispatcher import StartGate
from harness.engine.model import TERMINAL_STATUSES
from harness.engine.runner import DAILY_CAP_KEY, Runner
from harness.github.ledger import render_event
from harness.github.outbox import Outbox, ledger_mirror_key
from harness.ops.budget import DailyBudget
from harness.ops.costs import UsageRepo
from harness.store.db import Store
from harness.store.repo import append_event, get_meta, get_repo, list_runs

__all__ = [
    "OPS_KEY",
    "DailyCapPaused",
    "HaltInfo",
    "HaltState",
    "KillReport",
    "NoControlIssue",
    "OpsGate",
    "StartGate",
    "control_issues_of",
    "enforce_daily_cap",
    "kill_all",
    "make_daily_budget",
    "queue_kill",
    "queue_resume",
]

OPS_KEY = "ops"

Reason = Literal["kill_switch", "daily_budget"]
ControlIssues = Sequence[tuple[str, int]] | Callable[[], Sequence[tuple[str, int]]]


class NoControlIssue(ValueError):
    """No registered repo has a (synced) control issue, so a halt or an override has nowhere to be recorded."""


@dataclass(frozen=True)
class HaltInfo:
    reason: Reason
    since: str
    by: str


@dataclass(frozen=True)
class KillReport:
    """Sessions that could not be cancelled, as (run id, error) pairs."""

    errors: list[tuple[str, str]] = field(default_factory=list)


def control_issues_of(store: Store, cfg: GlobalConfig) -> list[tuple[str, int]]:
    """(repo, control issue) of every registered repo whose synced config has a control issue."""
    found: list[tuple[str, int]] = []
    for entry in cfg.repos:
        stored = get_repo(store, entry.name)
        issue = ((stored or {}).get("config") or {}).get("control_issue")
        if isinstance(issue, int):
            found.append((entry.name, issue))
    return found


def _loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class HaltState:
    def __init__(self, store: Store, outbox: Outbox, control_issues: ControlIssues) -> None:
        self.store = store
        self.outbox = outbox
        self._control_issues = control_issues

    @property
    def control_issues(self) -> list[tuple[str, int]]:
        issues = self._control_issues() if callable(self._control_issues) else self._control_issues
        return list(issues)

    @staticmethod
    def _coalesce_key(repo: str, issue: int) -> str:
        return f"ops:{repo}#{issue}"

    def _record(self, repo: str, issue: int) -> dict[str, Any]:
        """The newest known ``ops`` record of one repo: the queued op's value, else the mirror."""
        rows = self.store.query(
            "SELECT payload_json FROM outbox WHERE coalesce_key = ? AND status = 'pending'",
            (self._coalesce_key(repo, issue),),
        )
        if rows:
            value = _loads(rows[0]["payload_json"]).get("value")
            return value if isinstance(value, dict) else {}
        return _loads(get_meta(self.store, ledger_mirror_key(repo, issue, OPS_KEY)))

    def _records(self) -> list[dict[str, Any]]:
        return [self._record(repo, issue) for repo, issue in self.control_issues]

    @staticmethod
    def _halt_of(record: dict[str, Any]) -> HaltInfo | None:
        halt = record.get("halt")
        if isinstance(halt, dict) and halt.get("reason") in ("kill_switch", "daily_budget"):
            return HaltInfo(
                reason=halt["reason"], since=str(halt.get("since", "")), by=str(halt.get("by", ""))
            )
        return None

    def halted(self) -> HaltInfo | None:
        """Global halt: if any repo says halted so is the harness (a kill switch outranks the daily cap)."""
        infos = [info for info in map(self._halt_of, self._records()) if info is not None]
        for info in infos:
            if info.reason == "kill_switch":
                return info
        return infos[0] if infos else None

    def get_budget_override(self) -> float | None:
        """The daily budget override (first repo that has one), or None."""
        for record in self._records():
            value = record.get("daily_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def _write(
        self, update: Callable[[dict[str, Any]], dict[str, Any]], event: tuple[str, dict[str, Any]]
    ) -> None:
        """Queue one ``merge_ledger`` op per repo and one event-log row; nothing touches the mirror here."""
        if not self.control_issues:
            raise NoControlIssue(
                "no repo with a control issue is synced: the halt has nowhere to be recorded"
            )
        with self.store.tx():
            for repo, issue in self.control_issues:
                self.outbox.enqueue(
                    "merge_ledger",
                    {
                        "repo": repo,
                        "issue": issue,
                        "key": OPS_KEY,
                        "value": update(self._record(repo, issue)),
                    },
                    coalesce_key=self._coalesce_key(repo, issue),
                )
            append_event(self.store, kind=event[0], data=event[1])

    def halt(self, reason: Reason, by: str) -> bool:
        """Halt every repo. False (and nothing written) when all of them already carry this halt or a kill switch."""
        records = self._records()
        if records and all(
            (info := self._halt_of(r)) is not None and (info.reason == reason or info.reason == "kill_switch")
            for r in records
        ):
            return False
        since = self.store.now()

        def update(record: dict[str, Any]) -> dict[str, Any]:
            return {**record, "halt": {"reason": reason, "since": since, "by": by}}

        self._write(update, ("ops_halted", {"reason": reason, "by": by}))
        return True

    def resume(self, by: str) -> bool:
        """Clear the halt everywhere. False (and nothing written) when nothing is halted."""
        if not any(self._halt_of(r) is not None for r in self._records()):
            return False

        def update(record: dict[str, Any]) -> dict[str, Any]:
            return {**record, "halt": None}

        self._write(update, ("ops_resumed", {"by": by}))
        return True

    def set_daily_budget(self, daily_usd: float | None) -> None:
        """Set (or, for None, clear) the ledger override of the daily budget."""
        if daily_usd is not None and daily_usd < 0:
            raise ValueError("daily_usd must be >= 0")

        def update(record: dict[str, Any]) -> dict[str, Any]:
            return {**record, "daily_usd": daily_usd}

        self._write(update, ("ops_budget", {"daily_usd": daily_usd}))


class OpsGate:
    """The dispatcher's :class:`StartGate`: halted, or the daily cap is spent."""

    def __init__(self, halt_state: HaltState, budget: DailyBudget) -> None:
        self.halt_state = halt_state
        self.budget = budget

    def reason(self) -> str | None:
        if self.halt_state.halted() is not None:
            return "halted"
        if self.budget.status().exceeded:
            return "daily_budget"
        return None


def make_daily_budget(
    cfg: GlobalConfig, store: Store, halt_state: HaltState, clock: Callable[[], float] | None = None
) -> DailyBudget:
    """The daily budget: ``[budget]`` config, the ledger override on top, the configured time zone (else local)."""
    tz = ZoneInfo(cfg.budget.timezone) if cfg.budget.timezone else None
    return DailyBudget(
        UsageRepo(store),
        limit_usd=cfg.budget.daily_usd,
        override=halt_state.get_budget_override,
        tz=tz,
        clock=(lambda: datetime.fromtimestamp(clock(), UTC)) if clock is not None else None,
    )


def _announce_halt(store: Store, halt_state: HaltState, by: str) -> None:
    """One ledger event comment on the control issue of every repo (through the outbox)."""
    date = store.now()[:10]
    bullets = [f"kill switch pulled by `{by}`", "no new run starts until `harness resume`"]
    with store.tx():
        for repo, issue in halt_state.control_issues:
            halt_state.outbox.enqueue(
                "add_comment",
                {"repo": repo, "issue": issue, "body": render_event(date, issue, "halted", bullets)},
            )


def _active_runs(store: Store) -> list[Any]:
    return [r for r in list_runs(store) if r.status not in TERMINAL_STATUSES]


async def kill_all(runner: Runner, store: Store, halt_state: HaltState, *, by: str) -> KillReport:
    """Kill switch: halt, then cancel every non-terminal run (4 at a time). Errors are reported, never raised.

    Idempotent: when the kill switch is already set and no run is left, nothing happens and nothing is written.
    """
    active = _active_runs(store)
    info = halt_state.halted()
    if not active and info is not None and info.reason == "kill_switch":
        return KillReport()

    if halt_state.halt("kill_switch", by):
        _announce_halt(store, halt_state, by)

    semaphore = asyncio.Semaphore(4)
    errors: list[tuple[str, str]] = []

    async def cancel_one(run_id: str) -> None:
        async with semaphore:
            try:
                before = _cancel_error_count(store, run_id)
                await runner.cancel(run_id)  # records a cancel_error event per session it could not cancel
                errors.extend(_cancel_errors(store, run_id)[before:])
            except Exception as exc:
                errors.append((run_id, f"{type(exc).__name__}: {exc}"))

    await asyncio.gather(*(cancel_one(run.id) for run in active))
    return KillReport(errors=errors)


def _cancel_errors(store: Store, run_id: str) -> list[tuple[str, str]]:
    rows = store.query(
        "SELECT data_json FROM events WHERE run_id = ? AND kind = 'cancel_error' ORDER BY id", (run_id,)
    )
    return [(run_id, str(_loads(row["data_json"]).get("error", ""))) for row in rows]


def _cancel_error_count(store: Store, run_id: str) -> int:
    return len(_cancel_errors(store, run_id))


def queue_kill(store: Store, halt_state: HaltState, *, by: str) -> int:
    """Kill without a Runner (the web server): halt first, then queue a cancel request per non-terminal run.

    Returns how many cancel requests were queued; the daemon's Runner applies them on its next tick.
    """
    if halt_state.halt("kill_switch", by):
        _announce_halt(store, halt_state, by)
    queued = 0
    for run in _active_runs(store):
        pending = store.query(
            "SELECT 1 FROM control_requests WHERE run_id = ? AND kind = 'cancel' AND handled_at IS NULL",
            (run.id,),
        )
        if not pending:
            enqueue(store, run.id, "cancel", {})
            queued += 1
    return queued


def queue_resume(store: Store, halt_state: HaltState, *, by: str) -> list[str]:
    """Clear the halt and queue a ``resume`` for every run the daily cap paused; returns those run ids."""
    halt_state.resume(by)
    resumed: list[str] = []
    for run in list_runs(store, "paused"):
        if run.context.get(DAILY_CAP_KEY) is True:
            enqueue(store, run.id, "resume", {})
            resumed.append(run.id)
    return resumed


@dataclass(frozen=True)
class DailyCapPaused:
    run_ids: list[str]


def enforce_daily_cap(
    runner: Runner, store: Store, halt_state: HaltState, budget: DailyBudget
) -> DailyCapPaused:
    """When the daily cap is spent: halt (reason ``daily_budget``, by ``system``) and pause every running run once.

    Each run gets one ``pause`` control request and one budget-style inbox escalation; a run that is already
    paused or has a pause request pending is left alone, so calling this on every tick is safe.
    """
    status = budget.status()
    if not status.exceeded:
        return DailyCapPaused([])
    with contextlib.suppress(NoControlIssue):  # nowhere to record it: the dispatcher gate still holds
        halt_state.halt("daily_budget", "system")
    paused: list[str] = []
    summary = (
        f"spent ${status.spent_usd:.2f} of the ${status.limit_usd or 0:.2f} daily budget; "
        "`harness resume` continues the run"
    )
    for run in _active_runs(store):
        if run.status == "paused":
            continue
        pending = store.query(
            "SELECT 1 FROM control_requests WHERE run_id = ? AND kind = 'pause' AND handled_at IS NULL",
            (run.id,),
        )
        if pending:
            continue
        enqueue(store, run.id, "pause", {"reason": "daily_budget"})
        runner.escalate_daily_cap(run.id, summary)
        paused.append(run.id)
    return DailyCapPaused(paused)
