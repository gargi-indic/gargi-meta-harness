"""GET /api/inbox (M2.2)."""

from harness.store.repo import update_run, update_step


async def test_lists_the_waiting_gate_and_the_question(client, seed) -> None:
    response = await client.get("/api/inbox")
    assert response.status_code == 200
    items = response.json()
    assert [(i["kind"], i["run_id"]) for i in items] == [
        ("plan_approval", seed.run_a),
        ("question", seed.run_b),
    ]
    gate, question = items
    assert gate == {
        "id": seed.plan_gate_step,
        "kind": "plan_approval",
        "run_id": seed.run_a,
        "step_id": seed.plan_gate_step,
        "repo": "acme/widgets",
        "issue": 5,
        "title": "Widget export",
        "summary": "Approve the plan for #5",
        "pr": None,
        "findings": [],
        "agent": "jules",
        "created_at": "2026-01-01T00:15:00Z",
        "gate_id": seed.plan_gate_id,
    }
    assert question["id"] == seed.escalate_step
    assert question["summary"] == "Escalated after fix: which format?"
    assert question["gate_id"] is None
    assert question["created_at"] == "2026-01-01T00:25:00Z"


async def test_question_carries_the_pr_checks_and_findings(client) -> None:
    question = (await client.get("/api/inbox")).json()[1]
    assert question["pr"] == {
        "number": 42,
        "url": "https://github.com/acme/widgets/pull/42",
        "checks": [
            {"name": "ci", "state": "fail"},
            {"name": "build", "state": "failure"},
            {"name": "lint", "state": "cancelled"},
        ],
    }
    assert question["findings"] == [
        {"severity": "major", "text": "widgets/importer.py: No size limit"},
        {"severity": "minor", "text": "Missing docstring"},
    ]


async def test_a_pr_url_in_the_context_wins(client, store, seed) -> None:
    from harness.store.repo import get_run

    run = get_run(store, seed.run_b)
    assert run is not None
    update_run(
        store, seed.run_b, context={**run.context, "pr": {"number": 42, "url": "https://example.test/42"}}
    )
    assert (await client.get("/api/inbox")).json()[1]["pr"]["url"] == "https://example.test/42"


async def test_an_escalation_without_a_needs_input_step(client, store, seed) -> None:
    update_step(store, f"{seed.run_b}/fix/1", outcome="pr_ready")
    assert (await client.get("/api/inbox")).json()[1]["kind"] == "escalation"


async def test_resolved_items_disappear(client, store, seed) -> None:
    update_step(store, seed.plan_gate_step, status="done", outcome="approved")
    items = (await client.get("/api/inbox")).json()
    assert [i["run_id"] for i in items] == [seed.run_b]
    update_step(store, seed.escalate_step, status="done", outcome="resolved")
    assert (await client.get("/api/inbox")).json() == []


async def test_waiting_steps_without_needs_you_are_not_listed(client, store, seed) -> None:
    update_step(store, seed.plan_gate_step, output={"reason": "polling"})
    assert [i["run_id"] for i in (await client.get("/api/inbox")).json()] == [seed.run_b]


async def test_terminal_runs_are_not_listed(client, store, seed) -> None:
    update_run(store, seed.run_a, status="cancelled")
    assert [i["run_id"] for i in (await client.get("/api/inbox")).json()] == [seed.run_b]


async def test_other_gates_and_node_kinds_pass_through(client, store, seed) -> None:
    from harness.store.repo import get_run

    run = get_run(store, seed.run_a)
    assert run is not None
    playbook = run.context["playbook"]
    playbook["nodes"]["plan_gate"]["gate"] = "deploy"
    update_run(store, seed.run_a, context={**run.context, "playbook": playbook})
    assert (await client.get("/api/inbox")).json()[0]["kind"] == "gate"

    playbook["nodes"]["plan_gate"] = {"kind": "handoff"}
    update_run(store, seed.run_a, context={**run.context, "playbook": playbook})
    assert (await client.get("/api/inbox")).json()[0]["kind"] == "handoff"

    playbook["nodes"].pop("plan_gate")
    update_run(store, seed.run_a, context={**run.context, "playbook": playbook})
    assert (await client.get("/api/inbox")).json()[0]["kind"] == "escalation"


async def test_requires_the_cookie(anon) -> None:
    assert (await anon.get("/api/inbox")).status_code == 401
