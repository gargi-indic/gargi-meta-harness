"""POST /api/inbox/.../approve|reject|reroute, POST /api/runs/.../pause|resume|cancel|message|nodes/.../retry,
GET /api/control-requests/{id} (M2.3)."""

import json
from urllib.parse import quote

import pytest

from harness.adapters.base import AdapterRegistry
from harness.config import AgentConfig, RepoConfig
from harness.engine import ExecutorRegistry, Runner
from harness.engine.control import apply_pending
from harness.engine.executors.base import ExecContext
from harness.engine.executors.gate import GateExecutor
from harness.github.commands import decide
from harness.playbook.models import GateNode, Playbook
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.repo import get_gate, get_run, steps_for_run, update_run, update_step, upsert_repo
from tests.fixtures.server_seed import REPO, seed_store, ts
from tests.server.conftest import run_path
from tests.server.helpers import make_store


def item_path(step_id: str) -> str:
    return quote(step_id, safe="")


def control_rows(store) -> list[dict]:
    return [
        {**r, "payload": json.loads(r["payload_json"])}
        for r in store.query("SELECT * FROM control_requests ORDER BY id")
    ]


def events(store, kind: str) -> list[dict]:
    return [
        {**r, "data": json.loads(r["data_json"])}
        for r in store.query("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))
    ]


def snapshot(store) -> tuple:
    return (
        store.query("SELECT * FROM gates ORDER BY id"),
        store.query("SELECT * FROM steps ORDER BY id"),
        store.query("SELECT COUNT(*) AS n FROM events"),
        store.query("SELECT COUNT(*) AS n FROM control_requests"),
    )


# --- approve: gate items ---------------------------------------------------------------------------------


async def test_approve_a_gate_item_is_store_only(client, store, seed) -> None:
    response = await client.post(
        f"/api/inbox/{item_path(seed.plan_gate_step)}/approve", json={"note": "lgtm"}
    )
    assert response.status_code == 200
    assert response.json() == {"gate_id": seed.plan_gate_id, "status": "approved"}
    gate = get_gate(store, seed.plan_gate_id)
    assert (gate.status, gate.note, gate.decided_by) == ("approved", "lgtm", "web")
    (event,) = events(store, "gate_decided")
    assert event["data"] == {
        "gate_id": seed.plan_gate_id,
        "status": "approved",
        "note": "lgtm",
        "decided_by": "web",
    }
    assert (event["run_id"], event["repo"], event["issue"]) == (seed.run_a, REPO, 5)
    assert control_rows(store) == []


async def test_approve_without_a_body(client, store, seed) -> None:
    response = await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/approve")
    assert response.status_code == 200
    assert get_gate(store, seed.plan_gate_id).note is None


async def test_approve_matches_the_cli_decision(client, store, seed) -> None:
    other = make_store()
    other_seed = seed_store(other)
    decide(other, other_seed.plan_gate_id, "approved", "lgtm", "cli")
    outbox_before = store.query("SELECT COUNT(*) AS n FROM outbox")

    response = await client.post(
        f"/api/inbox/{item_path(seed.plan_gate_step)}/approve", json={"note": "lgtm"}
    )
    assert response.status_code == 200

    def gates(s):
        return [
            {k: v for k, v in row.items() if k not in ("decided_by", "decided_at")}
            for row in s.query("SELECT * FROM gates ORDER BY id")
        ]

    def decided_events(s):
        return [
            {k: v for k, v in e["data"].items() if k != "decided_by"} | {"run_id": e["run_id"]}
            for e in events(s, "gate_decided")
        ]

    assert gates(store) == gates(other)
    assert decided_events(store) == decided_events(other) and len(decided_events(store)) == 1
    assert store.query("SELECT COUNT(*) AS n FROM outbox") == other.query("SELECT COUNT(*) AS n FROM outbox")
    assert store.query("SELECT COUNT(*) AS n FROM outbox") == outbox_before == [{"n": 0}]


async def test_double_approve_is_409_and_changes_nothing(client, store, seed) -> None:
    url = f"/api/inbox/{item_path(seed.plan_gate_step)}/approve"
    assert (await client.post(url, json={"note": "first"})).status_code == 200
    before = snapshot(store)
    second = await client.post(url, json={"note": "second"})
    assert (second.status_code, second.json()) == (409, {"detail": "already resolved"})
    assert snapshot(store) == before
    assert len(events(store, "gate_decided")) == 1
    assert get_gate(store, seed.plan_gate_id).note == "first"


async def test_a_decided_gate_cannot_be_rejected_either(client, store, seed) -> None:
    await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/approve")
    response = await client.post(
        f"/api/inbox/{item_path(seed.plan_gate_step)}/reject", json={"message": "too late"}
    )
    assert (response.status_code, response.json()) == (409, {"detail": "already resolved"})


# --- reject ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [{"message": ""}, {"message": "   \n"}, {}, {"message": 5}, {"msg": "x"}])
async def test_reject_needs_a_real_message(client, store, seed, body) -> None:
    before = snapshot(store)
    response = await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/reject", json=body)
    assert response.status_code == 422
    assert snapshot(store) == before


async def test_reject_without_a_body_is_422(client, seed) -> None:
    assert (await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/reject")).status_code == 422


async def test_reject_passes_the_message_to_the_next_step_as_correction(client, store, seed) -> None:
    response = await client.post(
        f"/api/inbox/{item_path(seed.plan_gate_step)}/reject", json={"message": "  split the plan  "}
    )
    assert response.status_code == 200
    assert response.json() == {"gate_id": seed.plan_gate_id, "status": "rejected"}
    gate = get_gate(store, seed.plan_gate_id)
    assert (gate.status, gate.note, gate.decided_by) == ("rejected", "split the plan", "web")

    run = get_run(store, seed.run_a)
    playbook = Playbook.model_validate({**run.context["playbook"], "gates": {"plan": "human"}})
    step = next(s for s in steps_for_run(store, run.id) if s.id == seed.plan_gate_step)
    ctx = ExecContext(
        store=store,
        outbox=None,
        gh=None,
        adapters=AdapterRegistry(),
        repo_config=RepoConfig(),
        playbook=playbook,
        run=run,
        template=Context(issue=IssueCtx(number=5, title="t", agent="jules"), run=RunCtx(id=run.id)),
        clock=lambda: 0.0,
    )
    result = await GateExecutor().advance(step, GateNode(gate="plan"), ctx)
    assert result.outcome == "rejected"
    assert result.output["correction"] == "split the plan"


async def test_reject_applies_to_gate_items_only(client, store, seed) -> None:
    before = snapshot(store)
    response = await client.post(f"/api/inbox/{item_path(seed.escalate_step)}/reject", json={"message": "no"})
    assert (response.status_code, response.json()) == (422, {"detail": "reject applies to gate items"})
    assert snapshot(store) == before


# --- approve: escalation items ---------------------------------------------------------------------------


async def test_approve_an_escalation_queues_a_resume_at_the_previous_node(client, store, seed) -> None:
    response = await client.post(f"/api/inbox/{item_path(seed.escalate_step)}/approve")
    assert response.status_code == 202
    (row,) = control_rows(store)
    assert response.json() == {"request_id": row["id"], "request_ids": [row["id"]]}
    assert (row["run_id"], row["kind"], row["payload"], row["handled_at"]) == (
        seed.run_b,
        "resume",
        {"to": "fix"},
        None,
    )
    assert row["step_id"] == seed.escalate_step
    assert events(store, "gate_decided") == []
    (event,) = events(store, "control_requested")
    assert event["data"] == {"request_id": row["id"], "kind": "resume"}


async def test_approve_an_escalation_with_a_note_sends_it_first(client, store, seed) -> None:
    response = await client.post(
        f"/api/inbox/{item_path(seed.escalate_step)}/approve", json={"note": "use JSON", "to": "implement"}
    )
    assert response.status_code == 202
    first, second = control_rows(store)
    assert [(r["kind"], r["payload"], r["step_id"]) for r in (first, second)] == [
        ("send_message", {"text": "use JSON"}, None),
        ("resume", {"to": "implement"}, seed.escalate_step),
    ]
    assert response.json() == {"request_id": second["id"], "request_ids": [first["id"], second["id"]]}


async def test_approve_an_escalation_validates_the_node(client, store, seed) -> None:
    url = f"/api/inbox/{item_path(seed.escalate_step)}/approve"
    assert (await client.post(url, json={"to": "nope"})).status_code == 422
    for step in steps_for_run(store, seed.run_b):
        if step.status == "done":
            update_step(store, step.id, status="failed")
    response = await client.post(url)
    assert (response.status_code, response.json()) == (422, {"detail": "no node to resume at"})
    assert control_rows(store) == []


# --- item resolution -------------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["approve", "reject", "reroute"])
async def test_unknown_and_unlisted_items_are_404(client, store, seed, verb) -> None:
    body = {"message": "x"} if verb == "reject" else {"agent": "jules"} if verb == "reroute" else None
    for item in ("acme/widgets#99@1/nope/1", f"{seed.run_a}/plan_gate/9", "x"):
        response = await client.post(f"/api/inbox/{item_path(item)}/{verb}", json=body)
        assert (response.status_code, response.json()) == (404, {"detail": "unknown inbox item"})
    update_step(store, seed.plan_gate_step, output={"reason": "polling"})  # waiting, but not listed
    response = await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/{verb}", json=body)
    assert response.status_code == 404
    update_step(store, seed.plan_gate_step, output={"needs_you": True, "gate_id": seed.plan_gate_id})
    update_run(store, seed.run_a, status="cancelled")
    response = await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/{verb}", json=body)
    assert (response.status_code, response.json()) == (404, {"detail": "unknown inbox item"})


async def test_a_step_that_is_no_longer_waiting_is_409(client, store, seed) -> None:
    update_step(store, seed.escalate_step, status="done", outcome="resolved")
    response = await client.post(f"/api/inbox/{item_path(seed.escalate_step)}/approve")
    assert (response.status_code, response.json()) == (409, {"detail": "already resolved"})
    done_step = f"{seed.run_a}/plan_review/2"
    response = await client.post(f"/api/inbox/{item_path(done_step)}/approve")
    assert response.status_code == 409


async def test_a_gate_decided_while_the_step_still_waits_is_409(client, store, seed) -> None:
    decide(store, seed.plan_gate_id, "approved", None, "cli")
    assert (
        next(s for s in steps_for_run(store, seed.run_a) if s.id == seed.plan_gate_step).status == "waiting"
    )
    response = await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/approve")
    assert (response.status_code, response.json()) == (409, {"detail": "already resolved"})
    assert len(events(store, "gate_decided")) == 1


# --- reroute ---------------------------------------------------------------------------------------------


def set_agents(store) -> None:
    config = RepoConfig(
        control_issue=1,
        agents={"jules": AgentConfig(), "claude": AgentConfig(), "codex": AgentConfig(enabled=False)},
    )
    upsert_repo(store, REPO, "main", config.model_dump(), synced_at=ts(0))


async def test_reroute_to_an_unknown_or_disabled_agent_is_422(client, store, seed) -> None:
    set_agents(store)
    url = f"/api/inbox/{item_path(seed.escalate_step)}/reroute"
    for agent in ("ghost", "codex"):
        response = await client.post(url, json={"agent": agent})
        assert (response.status_code, response.json()) == (422, {"detail": "unknown or disabled agent"})
    assert control_rows(store) == []


async def test_reroute_to_an_enabled_agent_queues_a_request(client, store, seed) -> None:
    set_agents(store)
    response = await client.post(
        f"/api/inbox/{item_path(seed.escalate_step)}/reroute", json={"agent": "claude"}
    )
    assert response.status_code == 202
    (row,) = control_rows(store)
    assert response.json() == {"request_id": row["id"]}
    assert (row["run_id"], row["step_id"], row["kind"], row["payload"]) == (
        seed.run_b,
        seed.escalate_step,
        "reroute",
        {"agent": "claude"},
    )


async def test_reroute_defaults_to_the_default_config_when_the_repo_is_not_synced(
    client, store, seed
) -> None:
    store.execute("DELETE FROM repos")
    ok = await client.post(f"/api/inbox/{item_path(seed.escalate_step)}/reroute", json={"agent": "jules"})
    bad = await client.post(f"/api/inbox/{item_path(seed.escalate_step)}/reroute", json={"agent": "claude"})
    assert (ok.status_code, bad.status_code) == (202, 422)


# --- run controls -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "body", "kind", "payload"),
    [
        ("pause", None, "pause", {}),
        ("resume", None, "resume", {}),
        ("resume", {"to": "implement"}, "resume", {"to": "implement"}),
        ("cancel", None, "cancel", {}),
        ("nodes/implement/retry", None, "retry_node", {"node": "implement"}),
        ("message", {"text": "  please hurry  "}, "send_message", {"text": "please hurry"}),
    ],
)
async def test_run_controls_queue_a_request(client, store, seed, suffix, body, kind, payload) -> None:
    response = await client.post(f"/api/runs/{run_path(seed.run_b)}/{suffix}", json=body)
    assert response.status_code == 202
    (row,) = control_rows(store)
    assert response.json() == {"request_id": row["id"]}
    assert (row["run_id"], row["kind"], row["payload"], row["step_id"], row["handled_at"]) == (
        seed.run_b,
        kind,
        payload,
        None,
        None,
    )
    assert [e["data"] for e in events(store, "control_requested")] == [
        {"request_id": row["id"], "kind": kind}
    ]


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        ("pause", None),
        ("resume", None),
        ("cancel", None),
        ("nodes/implement/retry", None),
        ("message", {"text": "hi"}),
    ],
)
async def test_unknown_run_is_404(client, store, suffix, body) -> None:
    response = await client.post(f"/api/runs/{run_path('acme/widgets#99@1')}/{suffix}", json=body)
    assert (response.status_code, response.json()) == (404, {"detail": "unknown run"})
    assert control_rows(store) == []


@pytest.mark.parametrize(
    ("suffix", "body"),
    [("pause", None), ("resume", None), ("cancel", None), ("message", {"text": "hi"})],
)
@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
async def test_terminal_runs_are_409(client, store, seed, suffix, body, status) -> None:
    update_run(store, seed.run_b, status=status)
    response = await client.post(f"/api/runs/{run_path(seed.run_b)}/{suffix}", json=body)
    assert (response.status_code, response.json()) == (409, {"detail": f"run is {status}"})
    assert control_rows(store) == []


async def test_retry_is_allowed_on_a_failed_run_but_not_a_done_or_cancelled_one(client, store, seed) -> None:
    url = f"/api/runs/{run_path(seed.run_b)}/nodes/fix/retry"
    update_run(store, seed.run_b, status="failed")
    assert (await client.post(url)).status_code == 202
    for status in ("done", "cancelled"):
        update_run(store, seed.run_b, status=status)
        response = await client.post(url)
        assert (response.status_code, response.json()) == (409, {"detail": f"run is {status}"})
    assert len(control_rows(store)) == 1


async def test_bad_nodes_are_422(client, store, seed) -> None:
    run = run_path(seed.run_b)
    response = await client.post(f"/api/runs/{run}/nodes/nope/retry")
    assert (response.status_code, response.json()) == (422, {"detail": "unknown node"})
    response = await client.post(f"/api/runs/{run}/resume", json={"to": "nope"})
    assert (response.status_code, response.json()) == (422, {"detail": "unknown node"})
    assert control_rows(store) == []


async def test_the_context_playbook_decides_which_nodes_exist(client, store, seed) -> None:
    run = get_run(store, seed.run_b)
    playbook = run.context["playbook"]
    playbook["nodes"]["custom"] = playbook["nodes"]["fix"]
    update_run(store, seed.run_b, context={**run.context, "playbook": playbook})
    assert (await client.post(f"/api/runs/{run_path(seed.run_b)}/nodes/custom/retry")).status_code == 202


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "  \n "}, {}, {"text": "x" * 20001}, {"text": 1}])
async def test_message_needs_non_empty_text(client, store, seed, body) -> None:
    response = await client.post(f"/api/runs/{run_path(seed.run_b)}/message", json=body)
    assert response.status_code == 422
    assert control_rows(store) == []


async def test_bad_json_and_extra_fields_are_422_not_500(client, store, seed) -> None:
    run = run_path(seed.run_b)
    raw = {"content": b"{not json", "headers": {"content-type": "application/json"}}
    for url in (
        f"/api/runs/{run}/message",
        f"/api/runs/{run}/resume",
        f"/api/inbox/{item_path(seed.plan_gate_step)}/approve",
        f"/api/inbox/{item_path(seed.plan_gate_step)}/reject",
        f"/api/inbox/{item_path(seed.escalate_step)}/reroute",
    ):
        assert (await client.post(url, **raw)).status_code == 422, url
    assert (await client.post(f"/api/runs/{run}/message", json={"text": "x", "extra": 1})).status_code == 422
    assert (await client.post(f"/api/runs/{run}/resume", json={"to": "fix", "x": 1})).status_code == 422
    assert (
        await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/approve", json=[1])
    ).status_code == 422
    assert control_rows(store) == []
    assert events(store, "gate_decided") == []


async def test_ids_with_slash_hash_and_at_work_encoded(client, store, seed) -> None:
    assert "/" in seed.run_b and "#" in seed.run_b and "@" in seed.run_b
    assert (await client.post(f"/api/runs/{run_path(seed.run_b)}/pause")).status_code == 202
    assert (await client.post(f"/api/runs/{seed.run_b.replace('#', '%23')}/pause")).status_code == 202
    assert (await client.post(f"/api/inbox/{item_path(seed.plan_gate_step)}/approve")).status_code == 200


# --- routes do not shadow the read-only API -------------------------------------------------------------


async def test_the_read_routes_still_work(client, seed) -> None:
    detail = await client.get(f"/api/runs/{run_path(seed.run_a)}")
    assert detail.status_code == 200 and detail.json()["run"]["id"] == seed.run_a
    activity = await client.get(f"/api/runs/{run_path(seed.run_a)}/activity")
    assert activity.status_code == 200 and len(activity.json()["items"]) == 5
    assert (await client.get("/api/inbox")).status_code == 200


async def test_get_on_an_action_url_does_not_run_the_action(client, store, seed) -> None:
    response = await client.get(f"/api/runs/{run_path(seed.run_b)}/pause")
    assert response.status_code == 404
    assert control_rows(store) == []


# --- GET /api/control-requests/{id} ---------------------------------------------------------------------


async def test_control_request_is_pending_then_done_or_error(client, store, seed) -> None:
    ok = (await client.post(f"/api/runs/{run_path(seed.run_b)}/pause")).json()["request_id"]
    bad = (await client.post(f"/api/runs/{run_path(seed.run_b)}/message", json={"text": "hi"})).json()[
        "request_id"
    ]
    pending = (await client.get(f"/api/control-requests/{ok}")).json()
    assert pending == {
        "id": ok,
        "run_id": seed.run_b,
        "step_id": None,
        "kind": "pause",
        "status": "pending",
        "result": None,
        "created_at": pending["created_at"],
        "handled_at": None,
    }

    runner = Runner(store, None, ExecutorRegistry(), AdapterRegistry())
    assert await apply_pending(runner, store) == 2

    done = (await client.get(f"/api/control-requests/{ok}")).json()
    assert (done["status"], done["result"]) == ("done", {"ok": True})
    assert done["handled_at"] is not None
    failed = (await client.get(f"/api/control-requests/{bad}")).json()
    assert (failed["status"], failed["result"]) == (
        "error",
        {"ok": False, "error": "no agent session to message"},
    )
    assert get_run(store, seed.run_b).status == "paused"


async def test_unknown_control_request_is_404(client) -> None:
    response = await client.get("/api/control-requests/999")
    assert (response.status_code, response.json()) == (404, {"detail": "unknown control request"})
    assert (await client.get("/api/control-requests/abc")).status_code == 404


# --- auth ------------------------------------------------------------------------------------------------


async def test_actions_need_the_cookie(anon, store, seed) -> None:
    run = run_path(seed.run_b)
    urls = [
        f"/api/inbox/{item_path(seed.plan_gate_step)}/approve",
        f"/api/inbox/{item_path(seed.plan_gate_step)}/reject",
        f"/api/inbox/{item_path(seed.escalate_step)}/reroute",
        f"/api/runs/{run}/pause",
        f"/api/runs/{run}/resume",
        f"/api/runs/{run}/cancel",
        f"/api/runs/{run}/message",
        f"/api/runs/{run}/nodes/fix/retry",
    ]
    before = snapshot(store)
    for url in urls:
        assert (await anon.post(url, json={})).status_code == 401, url
    assert (await anon.get("/api/control-requests/1")).status_code == 401
    assert snapshot(store) == before
