"""GET /api/campaigns and /api/campaigns/{owner}/{repo}/graph (M2.2)."""

from harness.store.repo import get_issue, upsert_issue

STATUSES = {2: "done", 3: "ready", 4: "blocked", 5: "running", 6: "needs-you", 7: "ready"}


async def test_lists_one_campaign_per_registered_repo(client) -> None:
    response = await client.get("/api/campaigns")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "acme/widgets",
            "repo": "acme/widgets",
            "control_issue": 1,
            "title": "acme/widgets",
            "counts": {
                "ready": 2,
                "running": 1,
                "needs_you": 1,
                "review": 0,
                "blocked": 1,
                "done": 1,
                "failed": 0,
            },
        }
    ]


async def test_requires_the_cookie(anon) -> None:
    assert (await anon.get("/api/campaigns")).status_code == 401
    assert (await anon.get("/api/campaigns/acme/widgets/graph")).status_code == 401


async def test_counts_are_zero_for_a_repo_that_was_never_synced(client, app, store) -> None:
    app.state.config.repos.append(type(app.state.config.repos[0])(name="acme/empty"))
    data = (await client.get("/api/campaigns")).json()
    assert data[1]["id"] == "acme/empty"
    assert data[1]["control_issue"] is None
    assert set(data[1]["counts"].values()) == {0}


async def test_graph_nodes_edges_and_statuses(client) -> None:
    response = await client.get("/api/campaigns/acme/widgets/graph")
    assert response.status_code == 200
    graph = response.json()
    assert [n["id"] for n in graph["nodes"]] == [2, 3, 4, 5, 6, 7]
    assert {n["id"]: n["status"] for n in graph["nodes"]} == STATUSES
    assert graph["edges"] == [{"source": 2, "target": 3}, {"source": 3, "target": 4}]


async def test_graph_node_fields(client, seed) -> None:
    nodes = {n["id"]: n for n in (await client.get("/api/campaigns/acme/widgets/graph")).json()["nodes"]}
    assert nodes[5] == {
        "id": 5,
        "title": "Widget export",
        "status": "running",
        "agent": "jules",
        "playbook": "plan-build-review",
        "run_id": seed.run_a,
        "url": "https://github.com/acme/widgets/issues/5",
    }
    assert nodes[7]["agent"] == "fake"
    assert nodes[7]["run_id"] is None
    assert nodes[3]["run_id"] is None


async def test_finished_runs_are_not_attached(client, store, seed) -> None:
    from harness.store.repo import update_run

    update_run(store, seed.run_a, status="done")
    nodes = {n["id"]: n for n in (await client.get("/api/campaigns/acme/widgets/graph")).json()["nodes"]}
    assert nodes[5]["run_id"] is None


async def test_closing_a_blocker_unblocks_the_issue(client, store) -> None:
    three = get_issue(store, "acme/widgets", 3)
    assert three is not None
    upsert_issue(store, three.model_copy(update={"state": "closed"}))
    nodes = {
        n["id"]: n["status"] for n in (await client.get("/api/campaigns/acme/widgets/graph")).json()["nodes"]
    }
    assert nodes[3] == "done"
    assert nodes[4] == "ready"


async def test_label_precedence(client, store) -> None:
    seven = get_issue(store, "acme/widgets", 7)
    assert seven is not None
    cases = [
        (["harness:failed", "harness:done"], "open", "failed"),
        (["harness:running"], "closed", "done"),
        (["harness:done", "harness:needs-you"], "open", "done"),
        (["harness:needs-you", "harness:review"], "open", "needs-you"),
        (["harness:review", "harness:running"], "open", "review"),
        (["harness:running", "harness:blocked"], "open", "running"),
        (["harness:blocked"], "open", "blocked"),
        ([], "open", "ready"),
    ]
    for labels, state, expected in cases:
        upsert_issue(store, seven.model_copy(update={"labels": labels, "state": state}))
        nodes = {
            n["id"]: n["status"]
            for n in (await client.get("/api/campaigns/acme/widgets/graph")).json()["nodes"]
        }
        assert nodes[7] == expected, (labels, state)


async def test_unknown_campaign_is_404(client) -> None:
    response = await client.get("/api/campaigns/nope/nope/graph")
    assert response.status_code == 404
    assert response.json() == {"detail": "unknown campaign"}


async def test_unknown_api_paths_stay_json_404(client) -> None:
    response = await client.get("/api/nothing-here")
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
