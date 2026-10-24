"""Per-run cost tracking.

GitHub is the state store. The money a run has spent lives in ``run.context["usage"]``, which the runner
writes into the run's ledger snapshot (``RunSnapshot.usage``), so it survives a lost database. The ``usage``
table is the detailed local MIRROR for totals and reports; ``restore_usage`` refills it from a ledger snapshot
(``harness sync``) without double counting.

Shape of ``run.context["usage"]``::

    {"rev": 3,                                   # bumped on every change; tells the runner to mirror
     "steps": {step_id: {"source", "adapter", "model", "input_tokens", "output_tokens", "cost_usd", "estimated"}},
     "sessions": {session_id: {"input_tokens", "output_tokens", "cost_usd"}},   # last cumulative reading
     "budget": {"warned": bool, "unknown_warned": bool, "exceeded": bool}}
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.adapters.base import Usage
from harness.store.db import Store
from harness.store.repo import get_run, update_run

_SINCE_RE = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_EPS = 1e-12
RUN_STEP = "(run)"


def parse_since(text: str, now: datetime | None = None) -> str:
    """``24h`` / ``7d`` (relative to now) or an ISO date or datetime, as a store timestamp."""
    moment = now or datetime.now(UTC)
    match = _SINCE_RE.match(text.strip())
    if match:
        moment = moment - timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])
    else:
        try:
            parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid 'since' value {text!r}: use 24h, 7d or an ISO timestamp") from None
        moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def node_of_step(step_id: str | None) -> str:
    """The node id inside a step id ``{run}/{node}/{visit}``."""
    if not step_id:
        return RUN_STEP
    parts = step_id.rsplit("/", 2)
    return parts[1] if len(parts) == 3 else step_id


@dataclass(frozen=True)
class RunCost:
    usd: float
    estimated: bool
    by_node: dict[str, float]
    recorded: bool


class UsageRepo:
    """The ``usage`` table: append-only spend rows."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def add(
        self,
        *,
        run_id: str,
        step_id: str | None,
        source: str,
        adapter: str,
        cost_usd: float,
        estimated: bool = False,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        created_at: str | None = None,
    ) -> int:
        cursor = self.store.execute(
            """
            INSERT INTO usage (run_id, step_id, source, adapter, model, input_tokens, output_tokens,
                               cost_usd, estimated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step_id,
                source,
                adapter,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                int(estimated),
                created_at or self.store.now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def total_for_run(self, run_id: str) -> float:
        rows = self.store.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM usage WHERE run_id = ?", (run_id,)
        )
        return float(rows[0]["usd"])

    def run_cost(self, run_id: str) -> RunCost:
        rows = self.store.query(
            "SELECT step_id, SUM(cost_usd) AS usd, MAX(estimated) AS est FROM usage WHERE run_id = ? GROUP BY step_id",
            (run_id,),
        )
        by_node: dict[str, float] = {}
        for row in rows:
            node = node_of_step(row["step_id"])
            by_node[node] = by_node.get(node, 0.0) + float(row["usd"])
        return RunCost(
            usd=sum(by_node.values()),
            estimated=any(row["est"] for row in rows),
            by_node=by_node,
            recorded=bool(rows),
        )

    def total_since(self, since: str | None = None) -> float:
        rows = self.store.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM usage WHERE created_at >= ?", (since or "",)
        )
        return float(rows[0]["usd"])

    def by_adapter(self, since: str | None = None) -> dict[str, float]:
        rows = self.store.query(
            "SELECT adapter, SUM(cost_usd) AS usd FROM usage WHERE created_at >= ? GROUP BY adapter ORDER BY adapter",
            (since or "",),
        )
        return {row["adapter"]: float(row["usd"]) for row in rows}

    def by_run(self, since: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.query(
            """
            SELECT u.run_id AS run_id, r.issue AS issue, SUM(u.cost_usd) AS usd
            FROM usage u LEFT JOIN runs r ON r.id = u.run_id
            WHERE u.created_at >= ?
            GROUP BY u.run_id ORDER BY usd DESC, u.run_id
            """,
            (since or "",),
        )
        return [{"run_id": r["run_id"], "issue": r["issue"], "usd": float(r["usd"])} for r in rows]

    def step_totals(self, run_id: str) -> dict[str, dict[str, float | int]]:
        rows = self.store.query(
            """
            SELECT COALESCE(step_id, '') AS step_id, SUM(cost_usd) AS usd,
                   COALESCE(SUM(input_tokens), 0) AS tin, COALESCE(SUM(output_tokens), 0) AS tout
            FROM usage WHERE run_id = ? GROUP BY COALESCE(step_id, '')
            """,
            (run_id,),
        )
        return {
            r["step_id"]: {"usd": float(r["usd"]), "in": int(r["tin"]), "out": int(r["tout"])} for r in rows
        }


# ---- the ledger-carried record (run.context["usage"]) --------------------------------------------------


def new_record() -> dict[str, Any]:
    return {"rev": 0, "steps": {}, "sessions": {}, "budget": {}}


def usage_of(context: dict[str, Any]) -> dict[str, Any] | None:
    record = context.get("usage")
    return record if isinstance(record, dict) else None


def usage_rev(context: dict[str, Any]) -> int:
    record = usage_of(context)
    rev = record.get("rev") if record else 0
    return rev if isinstance(rev, int) else 0


def recorded_total(context: dict[str, Any]) -> float | None:
    """What the run has spent, or None when no usage was ever reported (unknown is not zero)."""
    record = usage_of(context)
    steps = record.get("steps") if record else None
    if not isinstance(steps, dict) or not steps:
        return None
    return float(sum(float(s.get("cost_usd", 0.0)) for s in steps.values() if isinstance(s, dict)))


def budget_state(context: dict[str, Any]) -> dict[str, bool]:
    record = usage_of(context)
    raw = record.get("budget") if record else None
    raw = raw if isinstance(raw, dict) else {}
    return {key: bool(raw.get(key)) for key in ("warned", "unknown_warned", "exceeded")}


def mark_budget(context: dict[str, Any], **flags: bool) -> None:
    """Set budget flags in a context dict that the caller persists (the runner does, inside its own tx)."""
    record = context.setdefault("usage", new_record())
    record.setdefault("budget", {}).update(flags)
    record["rev"] = usage_rev(context) + 1


def _apply(
    store: Store,
    run_id: str,
    step_id: str,
    *,
    source: str,
    adapter: str,
    model: str | None,
    delta_cost: float,
    delta_in: int | None,
    delta_out: int | None,
    estimated: bool,
    session: tuple[str, dict[str, Any]] | None,
) -> bool:
    """Add one delta to the run's record and the table in one transaction. False when there is nothing to add."""
    with store.tx():
        run = get_run(store, run_id)
        if run is None:
            return False
        context = dict(run.context)
        record = context.get("usage")
        record = dict(record) if isinstance(record, dict) else new_record()
        record["steps"] = dict(record.get("steps") or {})
        record["sessions"] = dict(record.get("sessions") or {})
        changed = False
        if session is not None and record["sessions"].get(session[0]) != session[1]:
            record["sessions"][session[0]] = session[1]
            changed = True
        if delta_cost > _EPS or (delta_in or 0) > 0 or (delta_out or 0) > 0:
            entry = dict(record["steps"].get(step_id) or {})
            entry.update(
                {
                    "source": source,
                    "adapter": adapter,
                    "model": model or entry.get("model"),
                    "input_tokens": (entry.get("input_tokens") or 0) + (delta_in or 0),
                    "output_tokens": (entry.get("output_tokens") or 0) + (delta_out or 0),
                    "cost_usd": float(entry.get("cost_usd", 0.0)) + delta_cost,
                    "estimated": bool(entry.get("estimated")) or estimated,
                }
            )
            record["steps"][step_id] = entry
            UsageRepo(store).add(
                run_id=run_id,
                step_id=step_id,
                source=source,
                adapter=adapter,
                model=model,
                input_tokens=delta_in,
                output_tokens=delta_out,
                cost_usd=delta_cost,
                estimated=estimated,
            )
            changed = True
        if not changed:
            return False
        record["rev"] = int(record.get("rev") or 0) + 1
        context["usage"] = record
        update_run(store, run_id, context=context)
        return True


def record_cumulative(
    store: Store, run_id: str, step_id: str, *, source: str, adapter: str, session_id: str, usage: Usage
) -> bool:
    """Record the part of a session's cumulative ``usage`` that has not been counted yet.

    The last reading per session is kept in the run record, so a replayed poll adds nothing. A reading below
    the previous one means the provider's counter restarted (a resumed session): then the whole reading is new.
    Returns True when the record changed.
    """
    run = get_run(store, run_id)
    if run is None:
        return False
    seen = (usage_of(run.context) or {}).get("sessions", {}).get(session_id) or {}
    now = {
        "cost_usd": float(usage.cost_usd),
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
    }

    def delta(key: str) -> float:
        before = float(seen.get(key) or 0)
        return now[key] - before if now[key] >= before else now[key]

    return _apply(
        store,
        run_id,
        step_id,
        source=source,
        adapter=adapter,
        model=usage.model,
        delta_cost=delta("cost_usd"),
        delta_in=int(delta("input_tokens")) or None,
        delta_out=int(delta("output_tokens")) or None,
        estimated=usage.estimated,
        session=(session_id, now),
    )


def record_delta(store: Store, run_id: str, step_id: str, *, source: str, adapter: str, usage: Usage) -> bool:
    """Record spend that is new by construction (one model call)."""
    return _apply(
        store,
        run_id,
        step_id,
        source=source,
        adapter=adapter,
        model=usage.model,
        delta_cost=float(usage.cost_usd),
        delta_in=usage.input_tokens,
        delta_out=usage.output_tokens,
        estimated=usage.estimated,
        session=None,
    )


LLM_ADAPTER = "reviewer"


def llm_usage_sink(store: Store, run_id: str, step_id: str, source: str) -> Callable[[Usage], None]:
    """The ``on_usage`` callback for a reviewer, selector or decomposer call made on behalf of a step."""

    def on_usage(usage: Usage) -> None:
        record_delta(store, run_id, step_id, source=source, adapter=LLM_ADAPTER, usage=usage)

    return on_usage


def restore_usage(store: Store, snapshot: dict[str, Any]) -> int:
    """Refill the usage table from a run's ledger snapshot; returns how many rows were added.

    Per step, only what the snapshot has beyond the table is added, so running it twice, or on a database
    that already has the rows, changes nothing. If the run exists locally without a record, it gets one.
    """
    run = snapshot.get("run")
    record = snapshot.get("usage")
    if not isinstance(run, dict) or not isinstance(record, dict) or not isinstance(run.get("id"), str):
        return 0
    run_id: str = run["id"]
    repo = UsageRepo(store)
    have = repo.step_totals(run_id)
    added = 0
    stamp = str(run.get("updated_at") or store.now())
    with store.tx():
        for step_id, entry in sorted((record.get("steps") or {}).items()):
            if not isinstance(entry, dict):
                continue
            known = have.get(step_id, {"usd": 0.0, "in": 0, "out": 0})
            missing_usd = float(entry.get("cost_usd") or 0.0) - float(known["usd"])
            missing_in = int(entry.get("input_tokens") or 0) - int(known["in"])
            missing_out = int(entry.get("output_tokens") or 0) - int(known["out"])
            if missing_usd <= _EPS and missing_in <= 0 and missing_out <= 0:
                continue
            repo.add(
                run_id=run_id,
                step_id=step_id,
                source=str(entry.get("source") or "restored"),
                adapter=str(entry.get("adapter") or "unknown"),
                model=entry.get("model"),
                input_tokens=max(missing_in, 0) or None,
                output_tokens=max(missing_out, 0) or None,
                cost_usd=max(missing_usd, 0.0),
                estimated=bool(entry.get("estimated")),
                created_at=stamp,
            )
            added += 1
        local = get_run(store, run_id)
        if local is not None and usage_of(local.context) is None:
            update_run(store, run_id, context={**local.context, "usage": record})
    return added
