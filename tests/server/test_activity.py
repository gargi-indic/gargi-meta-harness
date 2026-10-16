"""GET /api/runs/{id}/activity (M2.2)."""

from harness.store.repo import append_event
from tests.server.conftest import run_path


def url(run_id: str) -> str:
    return f"/api/runs/{run_path(run_id)}/activity"


async def test_pages_through_all_items_without_gaps_or_duplicates(client, seed) -> None:
    seen: list[int] = []
    after = 0
    pages = 0
    while True:
        response = await client.get(url(seed.run_a), params={"after": after, "limit": 2})
        assert response.status_code == 200
        page = response.json()
        if not page["items"]:
            assert page["next"] == after
            break
        seen.extend(item["seq"] for item in page["items"])
        assert page["next"] == page["items"][-1]["seq"]
        after = page["next"]
        pages += 1
    assert pages == 3
    assert seen == list(seed.activity_ids)


async def test_item_fields_and_kind_mapping(client, seed) -> None:
    page = (await client.get(url(seed.run_a))).json()
    assert page["next"] == seed.activity_ids[-1]
    assert [i["kind"] for i in page["items"]] == ["state", "message", "tool", "state", "note"]
    assert page["items"][0] == {
        "seq": seed.activity_ids[0],
        "ts": "2026-01-01T00:30:00Z",
        "step_id": f"{seed.run_a}/plan/1",
        "kind": "state",
        "text": "Planning started",
    }


async def test_only_this_runs_activity_is_returned(client, store, seed) -> None:
    append_event(
        store,
        kind="agent_activity",
        data={"step": "s", "ts": "t", "kind": "message", "text": "b"},
        run_id=seed.run_b,
    )
    append_event(store, kind="step_done", data={"text": "not activity"}, run_id=seed.run_a)
    assert len((await client.get(url(seed.run_a))).json()["items"]) == 5
    (only,) = (await client.get(url(seed.run_b))).json()["items"]
    assert only["text"] == "b"


async def test_every_documented_kind_maps(client, store, seed) -> None:
    mapping = {
        "message": "message",
        "agent": "message",
        "user": "message",
        "tool": "tool",
        "command": "tool",
        "bash": "tool",
        "state": "state",
        "status": "state",
        "plan": "state",
        "progress": "state",
        "end": "state",
        "other": "note",
    }
    for kind in mapping:
        append_event(
            store,
            kind="agent_activity",
            data={"step": "s", "ts": "t", "kind": kind, "text": kind},
            run_id=seed.run_b,
        )
    items = (await client.get(url(seed.run_b), params={"limit": 500})).json()["items"]
    assert {i["text"]: i["kind"] for i in items} == mapping


async def test_text_is_redacted_again_on_read(client, store, seed, monkeypatch) -> None:
    secret = "sk-test-0123456789abcdef"
    monkeypatch.setenv("JULES_API_KEY", secret)
    append_event(
        store,
        kind="agent_activity",
        data={"step": "s", "ts": "t", "kind": "message", "text": f"key {secret}"},
        run_id=seed.run_b,
    )
    (item,) = (await client.get(url(seed.run_b))).json()["items"]
    assert secret not in item["text"]
    assert item["text"] == "key [redacted]"


async def test_rows_missing_fields_still_render(client, store, seed) -> None:
    append_event(store, kind="agent_activity", data={}, run_id=seed.run_b)
    (item,) = (await client.get(url(seed.run_b))).json()["items"]
    assert (item["kind"], item["text"], item["step_id"]) == ("note", "", None)
    assert item["ts"]  # falls back to the row's timestamp


async def test_limit_bounds_are_422(client, seed) -> None:
    assert (await client.get(url(seed.run_a), params={"limit": 0})).status_code == 422
    assert (await client.get(url(seed.run_a), params={"limit": 501})).status_code == 422
    assert (await client.get(url(seed.run_a), params={"after": -1})).status_code == 422
    assert (await client.get(url(seed.run_a), params={"limit": 500})).status_code == 200


async def test_unknown_run_is_404(client) -> None:
    response = await client.get(url("nope"))
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown run"}


async def test_requires_the_cookie(anon, seed) -> None:
    assert (await anon.get(url(seed.run_a))).status_code == 401
