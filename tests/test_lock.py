"""The run lock in the control issue's ledger comment (M1.11). GitHub is an in-memory stand-in."""

from typing import Any

import pytest

from harness.github.ledger import is_ledger, parse_ledger, render_ledger
from harness.lock import LockHeld, acquire, heartbeat, iso, release

REPO = "o/r"
CONTROL = 1
T0 = 1_800_000_000.0
NOW = iso(T0)


class FakeGH:
    """The three comment calls the lock uses, over an in-memory comment list."""

    def __init__(self, after_write: Any = None) -> None:
        self.comments_list: list[dict[str, Any]] = []
        self.writes = 0
        self.after_write = after_write

    async def comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        assert (repo, number) == (REPO, CONTROL)
        return [dict(c) for c in self.comments_list]

    async def add_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        comment = {"id": 100 + len(self.comments_list), "body": body, "created_at": NOW}
        self.comments_list.append(comment)
        self._wrote()
        return dict(comment)

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        comment = next(c for c in self.comments_list if c["id"] == comment_id)
        comment["body"] = body
        self._wrote()
        return dict(comment)

    def _wrote(self) -> None:
        self.writes += 1
        if self.after_write is not None:
            self.after_write(self)

    def snapshot(self) -> dict[str, Any]:
        (comment,) = [c for c in self.comments_list if is_ledger(c["body"])]
        snap = parse_ledger(comment["body"])
        assert snap is not None
        return snap

    def seed(self, snapshot: dict[str, Any]) -> None:
        self.comments_list.append({"id": 7, "body": render_ledger(snapshot, "seed"), "created_at": NOW})


async def take(gh: FakeGH, host: str = "a", pid: int = 1, now: float = T0, **kwargs: Any):
    return await acquire(gh, REPO, CONTROL, host=host, pid=pid, now_iso=iso(now), **kwargs)  # type: ignore[arg-type]


async def test_acquire_on_an_empty_control_issue_creates_the_ledger_lock() -> None:
    gh = FakeGH()
    lock = await take(gh)
    assert len(gh.comments_list) == 1 and is_ledger(gh.comments_list[0]["body"])
    held = gh.snapshot()["lock"]
    assert held == {"host": "a", "pid": 1, "token": lock.token, "since": NOW, "heartbeat": NOW}
    assert lock.comment_id == gh.comments_list[0]["id"]


async def test_acquire_keeps_other_snapshot_keys() -> None:
    gh = FakeGH()
    gh.seed({"campaign": {"name": "m1"}})
    await take(gh)
    snap = gh.snapshot()
    assert snap["campaign"] == {"name": "m1"} and snap["lock"]["host"] == "a"
    assert len(gh.comments_list) == 1


async def test_second_holder_raises_lock_held_with_the_holder() -> None:
    gh = FakeGH()
    await take(gh, host="a", pid=1)
    with pytest.raises(LockHeld) as info:
        await take(gh, host="b", pid=2, now=T0 + 60)
    assert info.value.holder["host"] == "a" and info.value.holder["pid"] == 1
    assert "a:1" in str(info.value)
    assert gh.snapshot()["lock"]["host"] == "a"  # untouched


async def test_same_host_and_pid_reacquires() -> None:
    gh = FakeGH()
    first = await take(gh)
    second = await take(gh, now=T0 + 5)
    assert second.token != first.token
    assert gh.snapshot()["lock"]["token"] == second.token
    assert len(gh.comments_list) == 1


async def test_other_pid_on_the_same_host_is_a_different_holder() -> None:
    gh = FakeGH()
    await take(gh, host="a", pid=1)
    with pytest.raises(LockHeld):
        await take(gh, host="a", pid=2, now=T0 + 1)


async def test_a_lock_is_live_until_stale_after_has_passed() -> None:
    gh = FakeGH()
    await take(gh, host="a", pid=1)
    with pytest.raises(LockHeld):
        await take(gh, host="b", pid=2, now=T0 + 899)
    lock = await take(gh, host="b", pid=2, now=T0 + 900)  # heartbeat is stale_after old: taken over
    assert gh.snapshot()["lock"]["host"] == "b" and gh.snapshot()["lock"]["token"] == lock.token


async def test_stale_after_is_a_parameter() -> None:
    gh = FakeGH()
    await take(gh, host="a", pid=1)
    await take(gh, host="b", pid=2, now=T0 + 31, stale_after=30)
    assert gh.snapshot()["lock"]["host"] == "b"


async def test_a_heartbeat_keeps_the_lock_alive() -> None:
    gh = FakeGH()
    lock = await take(gh, host="a", pid=1)
    await heartbeat(lock, iso(T0 + 800))
    with pytest.raises(LockHeld):
        await take(gh, host="b", pid=2, now=T0 + 1000)  # 200s after the heartbeat
    assert gh.snapshot()["lock"]["heartbeat"] == iso(T0 + 800)
    assert gh.snapshot()["lock"]["since"] == NOW


async def test_unreadable_heartbeat_counts_as_stale() -> None:
    gh = FakeGH()
    gh.seed({"lock": {"host": "a", "pid": 1, "token": "x", "since": "?", "heartbeat": "garbage"}})
    await take(gh, host="b", pid=2)
    assert gh.snapshot()["lock"]["host"] == "b"


async def test_force_takes_a_live_lock_over() -> None:
    gh = FakeGH()
    old = await take(gh, host="a", pid=1)
    new = await take(gh, host="b", pid=2, now=T0 + 1, force=True)
    assert gh.snapshot()["lock"]["host"] == "b"
    with pytest.raises(LockHeld):  # the old holder notices at its next heartbeat
        await heartbeat(old, iso(T0 + 60))
    await heartbeat(new, iso(T0 + 60))


async def test_release_removes_only_the_lock() -> None:
    gh = FakeGH()
    gh.seed({"campaign": {"name": "m1"}, "n": 3})
    lock = await take(gh)
    await release(lock)
    assert gh.snapshot() == {"campaign": {"name": "m1"}, "n": 3}
    await take(gh, host="b", pid=2, now=T0 + 1)  # free again


async def test_release_leaves_a_lock_somebody_else_took() -> None:
    gh = FakeGH()
    old = await take(gh, host="a", pid=1)
    await take(gh, host="b", pid=2, now=T0 + 1, force=True)
    await release(old)
    assert gh.snapshot()["lock"]["host"] == "b"


async def test_heartbeat_preserves_other_keys_and_the_holder() -> None:
    gh = FakeGH()
    gh.seed({"campaign": 1})
    lock = await take(gh)
    await heartbeat(lock, iso(T0 + 60))
    snap = gh.snapshot()
    assert snap["campaign"] == 1
    assert snap["lock"] == {
        "host": "a",
        "pid": 1,
        "token": lock.token,
        "since": NOW,
        "heartbeat": iso(T0 + 60),
    }


async def test_the_token_is_verified_after_the_write() -> None:
    """A competing writer that lands between our write and our read-back wins; we must notice."""

    def steal(gh: FakeGH) -> None:
        if gh.writes == 1:  # right after our own write
            snap = gh.snapshot()
            snap["lock"] = {"host": "b", "pid": 2, "token": "theirs", "since": NOW, "heartbeat": NOW}
            gh.comments_list[0]["body"] = render_ledger(snap, "stolen")

    gh = FakeGH(after_write=steal)
    with pytest.raises(LockHeld) as info:
        await take(gh)
    assert info.value.holder["token"] == "theirs"


async def test_iso_format() -> None:
    assert iso(0) == "1970-01-01T00:00:00Z"
