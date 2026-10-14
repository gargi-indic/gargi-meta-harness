import json

import httpx
import pytest

from harness.github.client import GitHub
from harness.github.control_table import TABLE_END, TABLE_START, ControlRow
from harness.github.ledger import LEDGER_MARKER
from harness.github.outbox import Outbox
from harness.store.db import Store

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.respx(assert_all_called=True, assert_all_mocked=True),
]


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
    async def fake_sleep(duration):
        pass

    g = GitHub("test-token", write_spacing=0.0, sleep=fake_sleep)
    g._max_retries = 0
    return g


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def outbox(store: Store, gh: GitHub, clock: FakeClock) -> Outbox:
    return Outbox(store, gh, clock=clock)


async def test_lost_update(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "first"}, coalesce_key="key1")

    # In the middle of drain_once, simulate another enqueue with the same key
    async def match_post(request):
        outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "second"}, coalesce_key="key1")
        return httpx.Response(201, json={"id": 100})

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").mock(side_effect=match_post)

    sent = await outbox.drain_once()
    assert sent == 0  # Should not count as sent, since row was updated underneath

    rows = store.query("SELECT * FROM outbox")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert json.loads(rows[0]["payload_json"])["body"] == "second"
    assert rows[0]["attempts"] == 0

    # Drain again, should send the second payload
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 101})
    sent = await outbox.drain_once()
    assert sent == 1
    rows = store.query("SELECT * FROM outbox")
    assert rows[0]["status"] == "sent"


async def test_enqueue_validates_payload(outbox: Outbox):
    with pytest.raises(ValueError, match="Invalid payload"):
        outbox.enqueue("add_comment", {"repo": "user/repo", "issue": 1})  # missing body
    with pytest.raises(ValueError, match="Unknown op"):
        outbox.enqueue("unknown", {})


async def test_coalescing(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
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


async def test_upsert_ledger_find_existing(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
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


async def test_upsert_ledger_create_new(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "new ledger"})

    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(200, json=[])
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 12})

    sent = await outbox.drain_once()
    assert sent == 1


async def test_edit_issue_body(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
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


async def test_backoff_and_fail(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
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

    # Verify the back-off timings. We don't advance the clock but inspect the DB
    for i in range(4):
        expected_delay = min((2 ** (i + 1)) * 5, 600)
        delay_dt = store.clock() + __import__("datetime").timedelta(seconds=expected_delay)
        expected_not_before = delay_dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = store.query("SELECT * FROM outbox")
        assert rows[0]["not_before"] == expected_not_before

        # force not_before to past to run next attempt
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

    # Assert token redaction
    assert "token" not in events[0]["data_json"]


async def test_rate_limit(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
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


async def test_restart(respx_mock, store: Store, clock: FakeClock):
    async def fake_sleep(duration):
        pass

    gh1 = GitHub("test-token", write_spacing=0.0, sleep=fake_sleep)
    gh1._max_retries = 0
    outbox1 = Outbox(store, gh1, clock=clock)
    outbox1.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "survives"})

    gh2 = GitHub("test-token", write_spacing=0.0, sleep=fake_sleep)
    gh2._max_retries = 0
    outbox2 = Outbox(store, gh2, clock=clock)
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 1})

    sent = await outbox2.drain_once()
    assert sent == 1


async def test_three_coalesced_upsert_ledgers(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "first"}, coalesce_key="ledger_1")
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "second"}, coalesce_key="ledger_1")
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "third"}, coalesce_key="ledger_1")

    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(
        200, json=[{"id": 11, "body": f"{LEDGER_MARKER}\nold ledger\n<!-- harness:state v1 z:eJwB= -->"}]
    )

    def match_patch(request):
        body = json.loads(request.content)
        assert body["body"] == "third"
        return httpx.Response(200, json={"id": 11})

    patch_mock = respx_mock.patch("https://api.github.com/repos/a/b/issues/comments/11").mock(
        side_effect=match_patch
    )

    sent = await outbox.drain_once()
    assert sent == 1
    assert patch_mock.call_count == 1


async def test_upsert_ledger_create_then_edit(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    called_ids = []

    async def callback(repo, issue, comment_id):
        called_ids.append(comment_id)

    outbox = Outbox(store, gh, clock=clock, on_ledger_comment=callback)

    # First drain: creates the comment
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "new ledger"})
    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(200, json=[])
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 100})

    sent = await outbox.drain_once()
    assert sent == 1
    assert called_ids == [100]

    # Second drain: updates the comment
    outbox.enqueue("upsert_ledger", {"repo": "a/b", "issue": 1, "body": "updated ledger"})
    respx_mock.get("https://api.github.com/repos/a/b/issues/1/comments").respond(
        200, json=[{"id": 100, "body": f"{LEDGER_MARKER}\nold ledger\n<!-- harness:state v1 z:eJwB= -->"}]
    )
    respx_mock.patch("https://api.github.com/repos/a/b/issues/comments/100").respond(200, json={"id": 100})

    sent = await outbox.drain_once()
    assert sent == 1
    assert called_ids == [100, 100]


async def test_restart_file_db(respx_mock, tmp_path, gh: GitHub, clock: FakeClock):
    db_path = tmp_path / "test.db"
    store1 = Store(db_path)
    store1.migrate()

    outbox1 = Outbox(store1, gh, clock=clock)
    outbox1.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "persisted"})
    store1.close()

    store2 = Store(db_path)
    store2.migrate()
    outbox2 = Outbox(store2, gh, clock=clock)
    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").respond(201, json={"id": 1})

    sent = await outbox2.drain_once()
    assert sent == 1
    store2.close()


async def test_drain_order_by_id(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    outbox = Outbox(store, gh, clock=clock)

    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "first"})
    outbox.enqueue("add_comment", {"repo": "a/b", "issue": 2, "body": "second"})

    order = []

    def match_post1(request):
        order.append(1)
        return httpx.Response(201, json={"id": 1})

    def match_post2(request):
        order.append(2)
        return httpx.Response(201, json={"id": 2})

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/comments").mock(side_effect=match_post1)
    respx_mock.post("https://api.github.com/repos/a/b/issues/2/comments").mock(side_effect=match_post2)

    sent = await outbox.drain_once()
    assert sent == 2
    assert order == [1, 2]


async def test_set_labels_add_and_remove(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("set_labels", {"repo": "a/b", "issue": 1, "add": ["label1"], "remove": ["label2"]})

    # 7 labels ensured
    from harness.github.labels import HARNESS_LABELS

    for name in HARNESS_LABELS:
        respx_mock.post("https://api.github.com/repos/a/b/labels").respond(201, json={"name": name})

    respx_mock.post("https://api.github.com/repos/a/b/issues/1/labels").respond(201, json=[])
    respx_mock.delete("https://api.github.com/repos/a/b/issues/1/labels/label2").respond(200)

    sent = await outbox.drain_once()
    assert sent == 1


async def test_close_issue(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    outbox = Outbox(store, gh, clock=clock)
    outbox.enqueue("close_issue", {"repo": "a/b", "issue": 1})

    def match_patch(request):
        body = json.loads(request.content)
        assert body["state"] == "closed"
        return httpx.Response(200, json={})

    respx_mock.patch("https://api.github.com/repos/a/b/issues/1").mock(side_effect=match_patch)
    sent = await outbox.drain_once()
    assert sent == 1


async def test_run_with_fake_sleep(respx_mock, store: Store, gh: GitHub, clock: FakeClock):
    sleep_count = 0
    import asyncio

    stop_event = asyncio.Event()

    async def fake_sleep(duration):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            stop_event.set()

    outbox = Outbox(store, gh, clock=clock, sleep=fake_sleep)
    await outbox.run(stop_event, idle_sleep=2.0)
    assert sleep_count == 2
