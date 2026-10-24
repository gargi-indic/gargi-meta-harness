import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from harness.engine.model import TERMINAL_STATUSES
from harness.engine.runner import Runner
from harness.github.ledger import render_event
from harness.github.outbox import Outbox, ledger_mirror_key
from harness.store.db import Store
from harness.store.repo import get_meta, list_runs

OPS_KEY = "ops"


@dataclass(frozen=True)
class HaltInfo:
    reason: Literal["kill_switch", "daily_budget"]
    since: str
    by: str


@dataclass(frozen=True)
class KillReport:
    errors: list[tuple[str, str]]


class StartGate(Protocol):
    def reason(self) -> str | None: ...


class HaltState:
    def __init__(self, store: Store, outbox: Outbox, control_issues: Sequence[tuple[str, int]]) -> None:
        self.store = store
        self.outbox = outbox
        self.control_issues = control_issues

    def _read_mirrors(self) -> list[dict[str, Any]]:
        mirrors = []
        for repo, issue in self.control_issues:
            key = ledger_mirror_key(repo, issue, OPS_KEY)
            raw = get_meta(self.store, key)
            if raw:
                try:
                    mirrors.append(json.loads(raw))
                except ValueError:
                    pass
        return mirrors

    def halted(self) -> HaltInfo | None:
        """Global halt: if any repo's mirror says halted, the whole harness is halted."""
        for m in self._read_mirrors():
            halt = m.get("halt")
            if isinstance(halt, dict) and halt.get("reason"):
                return HaltInfo(
                    reason=halt["reason"],  # type: ignore
                    since=halt.get("since", ""),
                    by=halt.get("by", ""),
                )
        return None

    def get_budget_override(self) -> float | None:
        """Read the daily budget override. If multiple exist, return the first valid one."""
        for m in self._read_mirrors():
            val = m.get("daily_usd")
            if val is not None and isinstance(val, (int, float)):
                return float(val)
        return None

    def _enqueue_ops(self, update_fn: Any) -> None:
        """Update each repo's ledger via outbox ops."""
        for repo, issue in self.control_issues:
            key = ledger_mirror_key(repo, issue, OPS_KEY)
            raw = get_meta(self.store, key)
            current = {}
            if raw:
                try:
                    current = json.loads(raw)
                except ValueError:
                    pass
            new_val = update_fn(current)
            with self.store.tx():
                self.outbox.enqueue(
                    "merge_ledger",
                    {"repo": repo, "issue": issue, "key": OPS_KEY, "value": new_val},
                    coalesce_key=f"ops:{repo}#{issue}",
                )

    def halt(self, reason: Literal["kill_switch", "daily_budget"], by: str) -> None:
        now = self.store.now()

        def update(current: dict) -> dict:
            new_val = dict(current)
            new_val["halt"] = {"reason": reason, "since": now, "by": by}
            return new_val

        self._enqueue_ops(update)

    def resume(self, by: str) -> None:
        def update(current: dict) -> dict:
            new_val = dict(current)
            new_val["halt"] = None
            return new_val

        self._enqueue_ops(update)

        for repo, issue in self.control_issues:
            with self.store.tx():
                self.store.execute(
                    "INSERT INTO events (ts, repo, issue, run_id, kind, data_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.store.now(), repo, issue, "", "ops_resumed", json.dumps({"by": by})),
                )

    def set_daily_budget(self, daily_usd: float | None) -> None:
        def update(current: dict) -> dict:
            new_val = dict(current)
            new_val["daily_usd"] = daily_usd
            return new_val

        self._enqueue_ops(update)
        for repo, issue in self.control_issues:
            with self.store.tx():
                self.store.execute(
                    "INSERT INTO events (ts, repo, issue, run_id, kind, data_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.store.now(), repo, issue, "", "ops_budget", json.dumps({"daily_usd": daily_usd})),
                )


async def kill_all(runner: Runner, store: Store, halt_state: HaltState, *, by: str) -> KillReport:
    """Cancels every active adapter session and halts."""
    active_runs = [r for r in list_runs(store) if r.status not in TERMINAL_STATUSES]
    halt_info = halt_state.halted()
    if not active_runs and halt_info and halt_info.reason == "kill_switch":
        return KillReport(errors=[])

    halt_state.halt("kill_switch", by)

    sem = asyncio.Semaphore(4)
    all_errors: list[tuple[str, str]] = []

    async def _cancel_one(run_id: str) -> None:
        async with sem:
            try:
                # the runner.cancel handles both cancelling the session and updating rows
                await runner.cancel(run_id)
            except Exception as e:
                all_errors.append((run_id, str(e)))

    if active_runs:
        await asyncio.gather(*[_cancel_one(r.id) for r in active_runs])

    # write one ledger event comment per affected repo
    affected_repos = {r.repo for r in active_runs}
    if not affected_repos and not active_runs and halt_state.control_issues:
        affected_repos = {halt_state.control_issues[0][0]}

    date_str = store.now()[:10]
    for repo, issue in halt_state.control_issues:
        if repo in affected_repos:
            bullets = [f"Kill switch activated by `{by}`"]
            comment_body = render_event(date_str, issue, "halted", bullets)
            with store.tx():
                halt_state.outbox.enqueue(
                    "add_comment",
                    {"repo": repo, "issue": issue, "body": comment_body},
                    coalesce_key=None,
                )
                store.execute(
                    "INSERT INTO events (ts, repo, issue, run_id, kind, data_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        store.now(),
                        repo,
                        issue,
                        "",
                        "ops_halted",
                        json.dumps({"reason": "kill_switch", "by": by}),
                    ),
                )

    return KillReport(errors=all_errors)
