"""Outbox ops for issue creation (create_issue, add_sub_issue, add_blocked_by, ensure_label) and Outbox.settle."""

import json

import httpx
import pytest

from harness.github.client import GitHub
from harness.github.outbox import Outbox, OutboxOpFailed, op_result_key
from harness.github.planned import planned_marker
from harness.store.db import Store
from harness.store.repo import get_meta

API = "https://api.github.com/repos/a/b"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.respx(assert_all_called=False, assert_all_mocked=True),
]


class Clock:
    def __init__(self) -> None:
        self.time = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.time += seconds


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.migrate()
    return s


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def gh(respx_mock, clock: Clock):
    client = GitHub("t", write_spacing=0.0, sleep=clock.sleep, clock=clock, max_retries=0)
    yield client
    await client.aclose()


def outbox_for(store: Store, gh: GitHub, clock: Clock, **kw) -> Outbox:
    return Outbox(store, gh, clock=clock, sleep=clock.sleep, **kw)


async def test_create_issue_posts_and_records_the_number_and_id(respx_mock, store, gh, clock) -> None:
    outbox = outbox_for(store, gh, clock)
    op = outbox.enqueue(
        "create_issue", {"repo": "a/b", "title": "T", "body": "B", "labels": ["x", "y"]}, coalesce_key="k"
    )
    route = respx_mock.post(f"{API}/issues").respond(201, json={"number": 7, "id": 77})

    assert outbox.result(op) is None
    await outbox.settle([op])

    assert json.loads(route.calls[0].request.content) == {"title": "T", "body": "B", "labels": ["x", "y"]}
    assert outbox.result(op) == {"number": 7, "id": 77, "existing": False}
    assert json.loads(get_meta(store, op_result_key(op)) or "") == outbox.result(op)


async def test_create_issue_finds_an_issue_with_the_marker_instead_of_creating_one(
    respx_mock, store, gh, clock
) -> None:
    marker = planned_marker("r1", "a")
    listing = respx_mock.get(f"{API}/issues").respond(
        200,
        json=[
            {"number": 3, "id": 33, "body": f"other\n\n{planned_marker('r1', 'b')}"},
            {"number": 4, "id": 44, "body": f"text\n\n{marker}"},
        ],
    )
    create = respx_mock.post(f"{API}/issues").respond(201, json={"number": 9, "id": 99})
    outbox = outbox_for(store, gh, clock)
    op = outbox.enqueue(
        "create_issue",
        {
            "repo": "a/b",
            "title": "T",
            "body": f"text\n\n{marker}",
            "marker": marker,
            "dedupe_label": "harness:planned",
        },
    )
    await outbox.settle([op])

    assert not create.called
    params = dict(listing.calls[0].request.url.params)
    assert params["labels"] == "harness:planned" and params["state"] == "all"
    assert outbox.result(op) == {"number": 4, "id": 44, "existing": True}


async def test_create_issue_creates_when_the_marker_is_not_there(respx_mock, store, gh, clock) -> None:
    marker = planned_marker("r1", "a")
    respx_mock.get(f"{API}/issues").respond(
        200, json=[{"number": 3, "id": 33, "body": planned_marker("r1", "b")}]
    )
    create = respx_mock.post(f"{API}/issues").respond(201, json={"number": 9, "id": 99})
    outbox = outbox_for(store, gh, clock)
    op = outbox.enqueue(
        "create_issue",
        {"repo": "a/b", "title": "T", "body": marker, "marker": marker, "dedupe_label": "harness:planned"},
    )
    await outbox.settle([op])
    assert create.call_count == 1 and outbox.result(op) == {"number": 9, "id": 99, "existing": False}


async def test_add_sub_issue_and_add_blocked_by_skip_what_is_already_linked(
    respx_mock, store, gh, clock
) -> None:
    respx_mock.get(f"{API}/issues/1/sub_issues").respond(200, json=[{"id": 50}])
    respx_mock.get(f"{API}/issues/2/dependencies/blocked_by").respond(200, json=[{"id": 60}])
    sub = respx_mock.post(f"{API}/issues/1/sub_issues").respond(201, json={})
    dep = respx_mock.post(f"{API}/issues/2/dependencies/blocked_by").respond(201, json={})
    outbox = outbox_for(store, gh, clock)
    ops = [
        outbox.enqueue("add_sub_issue", {"repo": "a/b", "issue": 1, "sub_issue_id": 50}),
        outbox.enqueue("add_sub_issue", {"repo": "a/b", "issue": 1, "sub_issue_id": 51}),
        outbox.enqueue("add_blocked_by", {"repo": "a/b", "issue": 2, "blocker_id": 60}),
        outbox.enqueue("add_blocked_by", {"repo": "a/b", "issue": 2, "blocker_id": 61}),
    ]
    await outbox.settle(ops)

    assert [json.loads(c.request.content) for c in sub.calls] == [{"sub_issue_id": 51}]
    assert [json.loads(c.request.content) for c in dep.calls] == [{"issue_id": 61}]
    assert [outbox.result(op) for op in ops] == [
        {"added": False},
        {"added": True},
        {"added": False},
        {"added": True},
    ]


async def test_ensure_label_ignores_an_existing_label(respx_mock, store, gh, clock) -> None:
    route = respx_mock.post(f"{API}/labels").mock(
        side_effect=[httpx.Response(201, json={}), httpx.Response(422, json={"message": "exists"})]
    )
    outbox = outbox_for(store, gh, clock)
    first = outbox.enqueue(
        "ensure_label", {"repo": "a/b", "name": "n1", "color": "ffffff", "description": "d"}
    )
    second = outbox.enqueue("ensure_label", {"repo": "a/b", "name": "n2"})
    await outbox.settle([first, second])
    assert json.loads(route.calls[0].request.content) == {"name": "n1", "color": "ffffff", "description": "d"}
    assert json.loads(route.calls[1].request.content) == {"name": "n2", "description": ""}
    assert outbox.result(first) == {"created": True} and outbox.result(second) == {"created": False}


async def test_new_ops_reject_unknown_payload_fields(store, gh, clock) -> None:
    outbox = outbox_for(store, gh, clock)
    for op, payload in {
        "create_issue": {"repo": "a/b", "title": "T", "body": "B", "assignee": "x"},
        "add_sub_issue": {"repo": "a/b", "issue": 1},
        "add_blocked_by": {"repo": "a/b", "issue": 1, "blocker": 2},
        "ensure_label": {"repo": "a/b"},
    }.items():
        with pytest.raises(ValueError, match="Invalid payload"):
            outbox.enqueue(op, payload)  # type: ignore[arg-type]


async def test_an_op_without_a_result_has_none(respx_mock, store, gh, clock) -> None:
    respx_mock.post(f"{API}/issues/1/comments").respond(201, json={"id": 1})
    outbox = outbox_for(store, gh, clock)
    op = outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "x"})
    await outbox.settle([op])
    assert outbox.result(op) is None


async def test_settle_waits_for_the_window_instead_of_bypassing_it(respx_mock, store, gh, clock) -> None:
    sent_at: list[float] = []

    def comment(request: httpx.Request) -> httpx.Response:
        sent_at.append(clock.time)
        return httpx.Response(201, json={"id": 1})

    respx_mock.post(f"{API}/issues/1/comments").mock(side_effect=comment)
    outbox = outbox_for(store, gh, clock, per_minute=2)
    ops = [outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": f"m{n}"}) for n in range(5)]

    await outbox.settle(ops)

    assert len(sent_at) == 5
    for start in sent_at:
        assert sum(1 for t in sent_at if start <= t < start + 60) <= 2
    assert sent_at[2] - sent_at[0] >= 60 and sent_at[4] - sent_at[0] >= 120
    assert len(clock.slept) == 2 and all(0 < s <= 61 for s in clock.slept)


async def test_settle_raises_at_the_first_failure_and_leaves_the_op_queued(
    respx_mock, store, gh, clock
) -> None:
    respx_mock.post(f"{API}/issues/1/comments").respond(500, json={"message": "boom"})
    outbox = outbox_for(store, gh, clock)
    op = outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "x"}, coalesce_key="c")

    with pytest.raises(OutboxOpFailed, match="add_comment"):
        await outbox.settle([op])
    assert clock.slept == []  # it did not sit out the retry backoff
    row = store.query("SELECT status, attempts FROM outbox WHERE id = ?", (op,))[0]
    assert (row["status"], row["attempts"]) == ("pending", 1)

    again = outbox.enqueue("add_comment", {"repo": "a/b", "issue": 1, "body": "x"}, coalesce_key="c")
    assert again == op  # the same key resets the row
    respx_mock.post(f"{API}/issues/1/comments").respond(201, json={"id": 1})
    await outbox.settle([again])
    assert store.query("SELECT status FROM outbox WHERE id = ?", (op,))[0]["status"] == "sent"


async def test_settle_with_nothing_to_wait_for_or_an_unknown_op(store, gh, clock) -> None:
    outbox = outbox_for(store, gh, clock)
    await outbox.settle([])
    with pytest.raises(OutboxOpFailed, match="does not exist"):
        await outbox.settle([123])
