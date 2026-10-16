"""GET /api/board (M2.2)."""

from harness.store.repo import get_issue, upsert_issue


def issues(column: list[dict]) -> list[int]:
    return [card["issue"] for card in column]


async def test_columns_hold_the_right_issues(client) -> None:
    response = await client.get("/api/board")
    assert response.status_code == 200
    columns = response.json()["columns"]
    assert set(columns) == {"ready", "running", "needs_you", "review", "blocked", "done", "failed"}
    assert issues(columns["ready"]) == [7, 3]  # newest update first
    assert issues(columns["running"]) == [5]
    assert issues(columns["needs_you"]) == [6]
    assert issues(columns["review"]) == []
    assert issues(columns["blocked"]) == [4]
    assert issues(columns["done"]) == [2]
    assert issues(columns["failed"]) == []


async def test_card_fields(client, seed) -> None:
    columns = (await client.get("/api/board", params={"campaign": "acme/widgets"})).json()["columns"]
    assert columns["needs_you"][0] == {
        "repo": "acme/widgets",
        "issue": 6,
        "title": "Widget import",
        "agent": "jules",
        "playbook": "plan-build-review",
        "run_id": seed.run_b,
        "pr": 42,
        "blocked_by": [],
        "updated_at": "2026-01-01T00:06:00Z",
    }
    assert columns["running"][0]["pr"] is None
    four = columns["blocked"][0]
    assert four["blocked_by"] == [3]
    assert four["run_id"] is None


async def test_blocked_by_lists_closed_blockers_too(client) -> None:
    columns = (await client.get("/api/board")).json()["columns"]
    three = next(c for c in columns["ready"] if c["issue"] == 3)
    assert three["blocked_by"] == [2]


async def test_cards_with_the_same_update_time_sort_by_issue(client, store) -> None:
    for number in (3, 7):
        issue = get_issue(store, "acme/widgets", number)
        assert issue is not None
        upsert_issue(store, issue.model_copy(update={"updated_at": "2026-02-01T00:00:00Z"}))
    columns = (await client.get("/api/board")).json()["columns"]
    assert issues(columns["ready"]) == [3, 7]


async def test_unknown_campaign_is_404(client) -> None:
    response = await client.get("/api/board", params={"campaign": "nope/nope"})
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown campaign"}


async def test_requires_the_cookie(anon) -> None:
    assert (await anon.get("/api/board")).status_code == 401
