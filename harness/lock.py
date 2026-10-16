"""The run lock: one orchestrator per repo, held in the control issue's ledger comment.

The lock is ``snapshot["lock"] = {"host", "pid", "token", "since", "heartbeat"}`` in the ledger comment of
the repo's control issue (every other snapshot key is preserved). It is written directly, not through the
outbox: acquiring must be synchronous and verified. After writing, the comment is read back and the lock
only counts when it carries our token, so two orchestrators racing for the same repo cannot both win the
re-read (the loser sees the winner's token and raises :class:`LockHeld`).
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from harness.github.client import GitHub
from harness.github.ledger import find_ledger, parse_ledger, render_ledger

LOCK_KEY = "lock"
DEFAULT_STALE_AFTER = 900.0
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LockHeld(Exception):
    """Another live orchestrator holds the lock. ``holder`` is its lock record."""

    def __init__(self, holder: dict[str, Any] | None) -> None:
        self.holder: dict[str, Any] = dict(holder or {})
        h = self.holder
        who = f"{h.get('host', '?')}:{h.get('pid', '?')}"
        super().__init__(
            f"lock held by {who} since {h.get('since', '?')} (last heartbeat {h.get('heartbeat', '?')})"
        )


@dataclass
class Lock:
    gh: GitHub
    repo: str
    control_issue: int
    host: str
    pid: int
    token: str
    since: str
    comment_id: int | None = None


def iso(timestamp: float) -> str:
    """Seconds since the epoch as the ``YYYY-MM-DDTHH:MM:SSZ`` string the lock stores."""
    return datetime.fromtimestamp(timestamp, UTC).strftime(_TIME_FORMAT)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_stale(holder: dict[str, Any], now_iso: str, stale_after: float) -> bool:
    heartbeat = _parse_time(holder.get("heartbeat"))
    now = _parse_time(now_iso)
    if heartbeat is None or now is None:
        return True  # a lock without a readable heartbeat can never be proven alive
    return (now - heartbeat).total_seconds() >= stale_after


def _summary(lock: dict[str, Any] | None) -> str:
    if not lock:
        return "Harness run lock: free."
    return (
        f"Harness run lock: held by `{lock.get('host')}` (pid {lock.get('pid')}) since {lock.get('since')}."
    )


async def _read(gh: GitHub, repo: str, control_issue: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The control issue's ledger comment (or None) and its parsed snapshot ({} if there is none)."""
    comment = find_ledger(await gh.comments(repo, control_issue))
    snapshot = (parse_ledger(comment.get("body") or "") if comment else None) or {}
    return comment, snapshot


async def _write(
    gh: GitHub, repo: str, control_issue: int, comment: dict[str, Any] | None, snapshot: dict[str, Any]
) -> int:
    body = render_ledger(snapshot, _summary(snapshot.get(LOCK_KEY)))
    if comment is not None:
        await gh.edit_comment(repo, comment["id"], body)
        return int(comment["id"])
    created = await gh.add_comment(repo, control_issue, body)
    return int(created["id"])


async def acquire(
    gh: GitHub,
    repo: str,
    control_issue: int,
    *,
    host: str,
    pid: int,
    now_iso: str,
    force: bool = False,
    stale_after: float = DEFAULT_STALE_AFTER,
) -> Lock:
    """Take the repo's lock. Raises :class:`LockHeld` when another live holder has it (unless ``force``)."""
    comment, snapshot = await _read(gh, repo, control_issue)
    holder = snapshot.get(LOCK_KEY)
    if isinstance(holder, dict) and not force:
        ours = holder.get("host") == host and holder.get("pid") == pid
        if not ours and not _is_stale(holder, now_iso, stale_after):
            raise LockHeld(holder)

    token = secrets.token_hex(8)
    snapshot[LOCK_KEY] = {"host": host, "pid": pid, "token": token, "since": now_iso, "heartbeat": now_iso}
    await _write(gh, repo, control_issue, comment, snapshot)

    # Read back: only the token written last survives, so exactly one racing writer sees its own.
    verified, current = await _read(gh, repo, control_issue)
    held = current.get(LOCK_KEY)
    if not isinstance(held, dict) or held.get("token") != token:
        raise LockHeld(held if isinstance(held, dict) else None)
    return Lock(
        gh=gh,
        repo=repo,
        control_issue=control_issue,
        host=host,
        pid=pid,
        token=token,
        since=now_iso,
        comment_id=int(verified["id"]) if verified else None,
    )


async def heartbeat(lock: Lock, now_iso: str) -> None:
    """Refresh the heartbeat. Raises :class:`LockHeld` if someone else took the lock over."""
    comment, snapshot = await _read(lock.gh, lock.repo, lock.control_issue)
    held = snapshot.get(LOCK_KEY)
    if not isinstance(held, dict) or held.get("token") != lock.token:
        raise LockHeld(held if isinstance(held, dict) else None)
    snapshot[LOCK_KEY] = {**held, "heartbeat": now_iso}
    lock.comment_id = await _write(lock.gh, lock.repo, lock.control_issue, comment, snapshot)


async def release(lock: Lock) -> None:
    """Remove our lock, keeping every other snapshot key. A lock someone else took over is left alone."""
    comment, snapshot = await _read(lock.gh, lock.repo, lock.control_issue)
    held = snapshot.get(LOCK_KEY)
    if comment is None or not isinstance(held, dict) or held.get("token") != lock.token:
        return
    del snapshot[LOCK_KEY]
    await _write(lock.gh, lock.repo, lock.control_issue, comment, snapshot)
