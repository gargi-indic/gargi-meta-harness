import json

import httpx
import pytest

from harness.github.client import GitHub
from harness.github.control_table import TABLE_END, TABLE_START, ControlRow
from harness.github.ledger import LEDGER_MARKER
from harness.github.outbox import Outbox
from harness.store.db import Store


class FakeClock:
    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


@pytest.fixture
def store(tmp_path) -> Store:
    db = Store(":memory:")
    db.migrate()
    return db


@pytest.fixture
def gh(respx_mock) -> GitHub:
    # use respx_mock to ensure the mock is active before the client is instantiated
    g = GitHub("token")
    g._max_retries = 0
    return g


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def outbox(store: Store, gh: GitHub, clock: FakeClock) -> Outbox:
    return Outbox(store, gh, clock=clock)


def test_enqueue_validates_payload(outbox: Outbox):
    with pytest.raises(ValueError, match="Invalid payload"):
        outbox.enqueue("add_comment", {"repo": "user/repo", "issue": 1})  # missing body
    with pytest.raises(ValueError, match="Unknown op"):
        outbox.enqueue("unknown", {})


@pytest.mark.asyncio
async def test_coalescing(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)

    id1 = outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "first"}, coalesce_key="key1")
    id2 = outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "second"}, coalesce_key="key1")

    assert id1 == id2

    rows = store.query("SELECT * FROM outbox")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["body"] == "second"

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 100})

    sent = await outbox.drain_once()
    assert sent == 1

    rows = store.query("SELECT * FROM outbox")
    assert rows[0]["status"] == "sent"
    assert rows[0]["coalesce_key"] is None

    # Enqueue again with the same key
    id3 = outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "third"}, coalesce_key="key1")
    assert id3 != id1

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 101})
    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_upsert_ledger_find_existing(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "new ledger"})

    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(
        200,
        json=[
            {"id": 10, "body": "Normal comment"},
            {"id": 11, "body": f"{LEDGER_MARKER}\nold ledger\n<!-- harness:state v1 z:eJwB= -->"},
        ],
    )
    respx_mock.patch("https://api.github.com/repos/a/b/issues/comments/11").respond(200, json={"id": 11})

    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_upsert_ledger_create_new(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "new ledger"})

    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(200, json=[])
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 12})

    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_edit_issue_body(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock)
    row = ControlRow(issue=10, title="T", status="ready")
    outbox.enqueue("edit_issue_body", {"repo": "a/b", "issue": 1, "row": row.__dict__})

    old_body = f"Human intro\n{TABLE_START}\n| Issue |\n| --- |\n{TABLE_END}\nHuman outro"
    respx_mock.get("https://api.github.com/repos/a/b/issues/1").respond(200, json={"body": old_body})

    def match_patch(request):
        body = json.loads(request.content)
        text = body["body"]
        assert "Human intro" in text
        assert "Human outro" in text
        assert "READY" in text
        assert str(row.issue) in text
        return httpx.Response(200, json={})

    respx_mock.patch("https://api.github.com/repos/a/b/issues/1").mock(side_effect=match_patch)

    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_backoff_and_fail(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    # ensure it doesn't backoff internally
    gh._max_retries = 0

    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "failing"})

    # First attempt: fails
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(500)
    sent = await outbox.drain_once()
    assert sent == 0

    rows = store.query("SELECT * FROM outbox")
    assert rows[0]["status"] == "pending"
    assert rows[0]["attempts"] == 1
    assert "500" in rows[0]["last_error"]

    # Try draining immediately - should skip due to not_before
    sent = await outbox.drain_once()
    assert sent == 0
    rows = store.query("SELECT * FROM outbox")
    assert rows[0]["attempts"] == 1

    # We'll fail it up to attempt 5 by mocking now() and sending it again
    for _ in range(4):
        store.execute("UPDATE outbox SET not_before = NULL")
        respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(500)
        sent = await outbox.drain_once()
        assert sent == 0

    rows = store.query("SELECT * FROM outbox")
    assert rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == 5

    events = store.query("SELECT * FROM events")
    assert len(events) == 1
    assert events[0]["kind"] == "outbox_failed"


@pytest.mark.asyncio
async def test_rate_limit(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    outbox = Outbox(store, gh, clock=clock, per_minute=2)

    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "m1"})
    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 2, "body": "m2"})
    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 3, "body": "m3"})

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 1})
    respx_mock.post("https://api.github.com/repos/a/b/issues/2/comments").respond(201, json={"id": 2})

    sent = await outbox.drain_once()
    assert sent == 2  # limit hit

    rows = store.query("SELECT * FROM outbox WHERE status = 'pending'")
    assert len(rows) == 1

    # Fast forward clock
    clock.time += 60.0
    respx_mock.post("https://api.github.com/repos/a/b/issues/3/comments").respond(201, json={"id": 3})

    sent = await outbox.drain_once()
    assert sent == 1


@pytest.mark.asyncio
async def test_restart(respx_mock, store: Store, clock: FakeClock):
    respx_mock.assert_all_called = True
    respx_mock.assert_all_mocked = True

    gh1 = GitHub("token")
    gh1._max_retries = 0
    outbox1 = Outbox(store, gh1, clock=clock)
    outbox1.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "survives"})

    gh2 = GitHub("token")
    gh2._max_retries = 0
    outbox2 = Outbox(store, gh2, clock=clock)
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 1})

    sent = await outbox2.drain_once()
    assert sent == 1
