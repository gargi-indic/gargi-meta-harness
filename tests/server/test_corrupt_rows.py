"""Corrupt or odd stored JSON must not turn read endpoints into 500s (M2.2 review round 1)."""

from harness.store.queries import activity_for_run, latest_runs_by_issue
from harness.store.repo import get_run, update_run, update_step
from tests.server.conftest import run_path


def test_activity_query_survives_corrupt_data(store, seed) -> None:
    for bad in ("{bad", "[]", "", "3"):
        store.execute(
            "INSERT INTO events (ts, repo, issue, run_id, kind, data_json) VALUES ('t', ?, 5, ?, 'agent_activity', ?)",
            (seed.repo, seed.run_a, bad),
        )
    rows = activity_for_run(store, seed.run_a, after=seed.activity_ids[-1], limit=10)
    assert [r["data"] for r in rows] == [{}, {}, {}, {}]


async def test_activity_endpoint_survives_corrupt_data(client, store, seed) -> None:
    store.execute(
        "INSERT INTO events (ts, repo, issue, run_id, kind, data_json) VALUES ('t', ?, 5, ?, 'agent_activity', '{bad')",
        (seed.repo, seed.run_b),
    )
    response = await client.get(f"/api/runs/{run_path(seed.run_b)}/activity")
    assert response.status_code == 200
    assert [i["kind"] for i in response.json()["items"]] == ["note"]


async def test_corrupt_run_context_does_not_break_campaign_endpoints(client, store, seed) -> None:
    for bad in ("{bad", "[]"):
        store.execute("UPDATE runs SET context_json = ? WHERE id = ?", (bad, seed.run_a))
        assert latest_runs_by_issue(store, seed.repo)[5].context == {}
        for path in ("/api/campaigns", "/api/campaigns/acme/widgets/graph", "/api/board"):
            assert (await client.get(path)).status_code == 200, (bad, path)


def test_latest_run_per_issue_is_selected_in_sql(store, seed) -> None:
    from harness.store.repo import RunRow, insert_run

    older = get_run(store, seed.run_a)
    assert older is not None
    newer = older.model_copy(
        update={"id": "acme/widgets#5@2", "created_at": "2026-02-01T00:00:00Z", "status": "running"}
    )
    insert_run(store, newer)
    # Same created_at as the newer one but a lower id must lose.
    insert_run(store, newer.model_copy(update={"id": "acme/widgets#5@1b"}))
    latest = latest_runs_by_issue(store, seed.repo)
    assert latest[5].id == "acme/widgets#5@2"
    assert latest[6].id == seed.run_b
    assert set(latest) == {5, 6}
    assert isinstance(latest[5], RunRow)
    assert latest_runs_by_issue(store, "nope/nope") == {}


async def test_huge_after_is_422_not_500(client, seed) -> None:
    url = f"/api/runs/{run_path(seed.run_a)}/activity"
    assert (await client.get(url, params={"after": 99999999999999999999})).status_code == 422
    assert (await client.get(url, params={"after": 2**63 - 1})).status_code == 200


async def test_list_shaped_playbook_nodes_do_not_break_the_inbox(client, store, seed) -> None:
    run = get_run(store, seed.run_b)
    assert run is not None
    update_run(store, seed.run_b, context={**run.context, "playbook": {"nodes": ["ci", "fix"]}})
    response = await client.get("/api/inbox")
    assert response.status_code == 200
    item = next(i for i in response.json() if i["run_id"] == seed.run_b)
    assert item["kind"] == "escalation"
    assert item["pr"]["checks"] == []
    assert item["findings"] == []


async def test_failed_ci_entries_without_a_name_are_skipped(client, store, seed) -> None:
    update_step(
        store,
        f"{seed.run_b}/ci/1",
        output={
            "failed": [{"conclusion": "failure"}, {"name": "", "conclusion": "x"}, {"name": "lint"}, "junk"]
        },
    )
    item = next(i for i in (await client.get("/api/inbox")).json() if i["run_id"] == seed.run_b)
    assert item["pr"]["checks"] == [{"name": "ci", "state": "fail"}, {"name": "lint", "state": "unknown"}]
