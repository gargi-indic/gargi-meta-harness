"""Slash commands (M1.6): parser, decision API and poll_commands. GitHub is mocked with respx."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import Response

from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig
from harness.engine import Done, ExecutorRegistry, Runner
from harness.engine.executors.gate import GateExecutor
from harness.github.client import GitHub
from harness.github.commands import (
    Command,
    decide,
    parse_command,
    poll_commands,
    resolve_gate,
)
from harness.github.outbox import Outbox
from harness.store.db import Store
from harness.store.repo import GateRow, get_gate, get_meta, get_run, insert_gate, steps_for_run
from tests.engine.helpers import REPO, FakeClock, RecordingOutbox, Scripted, make_issue, make_playbook

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

ISSUE = 7
COMMENTS_URL = f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments"
LATER = "2099-01-01T00:00:00Z"
EARLIER = "2000-01-01T00:00:00Z"
GATE_ID = f"{REPO}#{ISSUE}@1/g/1/gate"

PLAYBOOK = make_playbook(
    {"a": {"kind": "check", "check": "command", "command": "x"}, "g": {"kind": "gate", "gate": "g"}},
    {"a.fail": "g", "a.pass": "@done", "g.approved": "@done", "g.rejected": "@failed"},
    id="cmds",
).model_copy(update={"gates": {"g": "human"}})


# ---- parser --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/harness approve", Command("approve")),
        ("/harness approve looks good to me", Command("approve", note="looks good to me")),
        ("  /harness approve   spaced  ", Command("approve", note="spaced")),
        ("\n\n/harness approve first line\nsecond line", Command("approve", note="first line")),
        ("/harness reject needs tests", Command("reject", note="needs tests")),
        ("/harness retry", Command("retry")),
        ("/harness retry implement", Command("retry", node="implement")),
        ("/harness retry agent=codex", Command("retry", agent="codex")),
        ("/harness retry implement agent=codex", Command("retry", node="implement", agent="codex")),
        ("/harness retry agent=codex implement", Command("retry", node="implement", agent="codex")),
        ("/harness pause", Command("pause")),
        ("/harness cancel", Command("cancel")),
        ("/harness resume", Command("resume")),
        ("/harness resume --to implement", Command("resume", to="implement")),
    ],
)
def test_parse_valid(body: str, expected: Command) -> None:
    assert parse_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n  ",
        "hello",
        "harness approve",
        "/Harness approve",
        "/HARNESS approve",
        "/harness",
        "/harness ",
        "/harnessapprove",
        "/harness frobnicate",
        "/harness reject",
        "/harness reject   ",
        "hello\n/harness approve",
        "> /harness approve",
        "text /harness approve",
        "/harness pause now",
        "/harness cancel everything",
        "/harness resume now",
        "/harness resume --to",
        "/harness resume --to Bad-Node",
        "/harness resume --from x",
        "/harness retry a b",
        "/harness retry agent=",
        "/harness retry agent=a agent=b",
        "/harness retry Bad-Node",
    ],
)
def test_parse_rejected(body: str) -> None:
    assert parse_command(body) is None


def test_parse_note_is_capped() -> None:
    cmd = parse_command("/harness approve " + "x" * 5000)
    assert cmd is not None and cmd.note == "x" * 2000
    cmd = parse_command("/harness reject " + "y" * 5000)
    assert cmd is not None and cmd.note == "y" * 2000


# ---- fixtures ------------------------------------------------------------------------------------------


@dataclass
class Setup:
    store: Store
    clock: FakeClock
    gh: GitHub
    outbox: RecordingOutbox
    run_id: str

    def runner(self) -> Runner:
        registry = ExecutorRegistry()
        registry.register("gate", GateExecutor())
        registry.register("check", Scripted({"a": [Done("fail", {"error": "boom"})]}))
        return Runner(self.store, self.outbox, registry, AdapterRegistry(), gh=self.gh, clock=self.clock)

    def fresh_outbox(self) -> RecordingOutbox:
        return RecordingOutbox(self.store, self.gh, clock=self.clock)

    async def poll(
        self, runner: Runner | None = None, config: RepoConfig | None = None, outbox: Outbox | None = None
    ) -> int:
        return await poll_commands(
            self.gh,
            self.store,
            REPO,
            runner or self.runner(),
            repo_config=config or RepoConfig(),
            outbox=outbox or self.outbox,
        )

    def replies(self) -> list[str]:
        return [p["body"] for p in self.outbox.of("add_comment")]

    def cmd_keys(self) -> list[str]:
        rows = self.store.query("SELECT coalesce_key FROM outbox WHERE coalesce_key LIKE 'cmd:%' ORDER BY id")
        return [r["coalesce_key"] for r in rows]

    def command_events(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT data_json FROM events WHERE kind = 'command' ORDER BY id")
        return [json.loads(r["data_json"]) for r in rows]


@pytest.fixture
async def s() -> AsyncIterator[Setup]:
    clock = FakeClock()
    store = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    store.migrate()

    async def no_sleep(_: float) -> None:
        return None

    gh = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    outbox = RecordingOutbox(store, gh, clock=clock, sleep=no_sleep)
    setup = Setup(store, clock, gh, outbox, "")
    runner = setup.runner()
    setup.run_id = runner.start_run(REPO, make_issue(ISSUE), PLAYBOOK)
    await runner.tick()  # a fails, routes to the gate
    clock.advance(60)
    await runner.tick()  # the gate goes pending
    assert get_run(store, setup.run_id).status == "needs-you"  # type: ignore[union-attr]
    setup.outbox.enqueued.clear()
    yield setup
    await gh.aclose()
    store.close()


def comment(cid: int, body: str, login: str = "o", created: str = LATER) -> dict[str, Any]:
    return {"id": cid, "body": body, "created_at": created, "user": {"login": login}}


def serve(respx_mock, *comments: dict[str, Any], issue: int = ISSUE):
    return respx_mock.get(f"https://api.github.com/repos/{REPO}/issues/{issue}/comments").mock(
        return_value=Response(200, json=list(comments))
    )


# ---- decision API --------------------------------------------------------------------------------------


def test_decide_records_event(s: Setup) -> None:
    gate = decide(s.store, GATE_ID, "approved", "ok", "alice")
    assert (gate.status, gate.note, gate.decided_by) == ("approved", "ok", "alice")
    events = s.store.query("SELECT * FROM events WHERE kind = 'gate_decided'")
    assert len(events) == 1 and events[0]["run_id"] == s.run_id and events[0]["issue"] == ISSUE
    assert json.loads(events[0]["data_json"]) == {
        "gate_id": GATE_ID,
        "status": "approved",
        "note": "ok",
        "decided_by": "alice",
    }


def test_decide_errors_leave_no_event(s: Setup) -> None:
    with pytest.raises(KeyError):
        decide(s.store, "nope", "approved", None, "x")
    decide(s.store, GATE_ID, "rejected", "no", "x")
    with pytest.raises(ValueError, match="not pending"):
        decide(s.store, GATE_ID, "approved", None, "x")
    assert len(s.store.query("SELECT * FROM events WHERE kind = 'gate_decided'")) == 1


@pytest.mark.parametrize("ref", [str(ISSUE), f"#{ISSUE}", f" #{ISSUE} ", GATE_ID])
def test_resolve_gate_by_issue_or_id(s: Setup, ref: str) -> None:
    assert resolve_gate(s.store, REPO, ref).id == GATE_ID


def test_resolve_gate_errors(s: Setup) -> None:
    with pytest.raises(ValueError, match="no pending gate"):
        resolve_gate(s.store, REPO, "99")
    with pytest.raises(ValueError, match="no pending gate"):
        resolve_gate(s.store, "other/repo", str(ISSUE))
    with pytest.raises(ValueError, match="no gate"):
        resolve_gate(s.store, REPO, "missing/gate")
    with pytest.raises(ValueError, match="needs a repo"):
        resolve_gate(s.store, None, str(ISSUE))
    insert_gate(
        s.store,
        GateRow(
            id="second",
            run_id=s.run_id,
            step_id=f"{s.run_id}/g/1",
            name="g",
            status="pending",
            note=None,
            decided_by=None,
            created_at="2027-01-01T00:00:00Z",
            decided_at=None,
        ),
    )
    with pytest.raises(ValueError, match="2 pending gates"):
        resolve_gate(s.store, REPO, str(ISSUE))


def test_resolve_gate_ignores_decided_gates(s: Setup) -> None:
    decide(s.store, GATE_ID, "approved", None, "x")
    with pytest.raises(ValueError, match="no pending gate"):
        resolve_gate(s.store, REPO, str(ISSUE))


# ---- poll_commands -------------------------------------------------------------------------------------


async def test_allowed_approve_decides_and_acks(s: Setup, respx_mock) -> None:
    route = serve(respx_mock, comment(11, "/harness approve ship it", login="O"))
    assert await s.poll() == 1

    gate = get_gate(s.store, GATE_ID)
    assert gate is not None
    assert (gate.status, gate.note, gate.decided_by) == ("approved", "ship it", "O")
    assert route.calls.last.request.url.params["since"] == get_run(s.store, s.run_id).created_at  # type: ignore[union-attr]
    [(op, payload, key)] = s.outbox.enqueued
    assert op == "add_comment" and key == "cmd:11"
    assert payload["repo"] == REPO and payload["issue"] == ISSUE
    assert payload["body"].startswith("**2027-01-15 · #7 command applied.**\n")
    assert "Approved by @O: ship it" in payload["body"]
    assert s.command_events() == [
        {"comment_id": 11, "login": "O", "verb": "approve", "ok": True, "error": None}
    ]
    assert get_meta(s.store, f"commands:{REPO}#{ISSUE}") == "11"


async def test_reject_decides_and_the_runner_routes(s: Setup, respx_mock) -> None:
    serve(respx_mock, comment(12, "/harness reject wrong approach"))
    assert await s.poll() == 1
    gate = get_gate(s.store, GATE_ID)
    assert gate is not None and gate.status == "rejected" and gate.note == "wrong approach"
    assert "Rejected by @o: wrong approach" in s.replies()[0]

    runner = s.runner()
    s.clock.advance(60)
    await runner.tick()
    assert get_run(s.store, s.run_id).status == "failed"  # type: ignore[union-attr]


async def test_non_allowed_user_gets_a_reply_and_nothing_happens(s: Setup, respx_mock) -> None:
    serve(respx_mock, comment(13, "/harness approve", login="mallory"))
    assert await s.poll() == 0

    gate = get_gate(s.store, GATE_ID)
    assert gate is not None and gate.status == "pending"
    [(op, payload, key)] = s.outbox.enqueued
    assert op == "add_comment" and key == "cmd:13"
    assert payload["body"].startswith("**2027-01-15 · #7 command ignored.**\n")
    assert "- @mallory is not allowed to run harness commands" in payload["body"]
    assert s.command_events() == [
        {"comment_id": 13, "login": "mallory", "verb": "approve", "ok": False, "error": "not allowed"}
    ]
    assert get_run(s.store, s.run_id).status == "needs-you"  # type: ignore[union-attr]


async def test_command_users_replace_the_owner_and_compare_case_insensitively(s: Setup, respx_mock) -> None:
    serve(
        respx_mock,
        comment(14, "/harness approve", login="o"),  # the owner is not listed: ignored
        comment(15, "/harness approve fine", login="ALICE"),
    )
    assert await s.poll(config=RepoConfig(command_users=["alice"])) == 1
    gate = get_gate(s.store, GATE_ID)
    assert gate is not None and gate.status == "approved" and gate.decided_by == "ALICE"
    assert "is not allowed" in s.replies()[0] and "Approved by @ALICE" in s.replies()[1]


async def test_non_command_comments_are_skipped_silently(s: Setup, respx_mock) -> None:
    serve(respx_mock, comment(16, "looks good!", login="mallory"), comment(17, "/harness bogus"))
    assert await s.poll() == 0
    assert s.outbox.enqueued == []
    assert s.command_events() == []
    assert get_meta(s.store, f"commands:{REPO}#{ISSUE}") == "17"


async def test_restart_processes_nothing_twice(s: Setup, respx_mock) -> None:
    route = serve(
        respx_mock,
        comment(21, "/harness approve first"),
        comment(22, "/harness pause", login="mallory"),
        comment(23, "random chatter"),
    )
    assert await s.poll() == 1
    assert s.cmd_keys() == ["cmd:21", "cmd:22"]

    # "restart": a new runner and outbox over the same store; GitHub still returns the same comments
    again = await s.poll(runner=s.runner(), outbox=s.fresh_outbox())
    assert again == 0
    assert route.call_count == 2
    assert s.cmd_keys() == [
        "cmd:21",
        "cmd:22",
    ]
    assert len(s.command_events()) == 2
    gate = get_gate(s.store, GATE_ID)
    assert gate is not None and gate.note == "first"


async def test_last_seen_survives_a_crash_in_the_middle_of_a_batch(s: Setup, respx_mock) -> None:
    class Boom(Exception):
        pass

    class CrashingRunner(Runner):
        def resume(self, run_id: str, to: str | None = None) -> None:
            raise Boom

    serve(respx_mock, comment(31, "/harness pause"), comment(32, "/harness resume"))
    crashing = CrashingRunner(s.store, s.outbox, ExecutorRegistry(), AdapterRegistry(), clock=s.clock)
    with pytest.raises(Boom):
        await s.poll(runner=crashing)
    assert get_meta(s.store, f"commands:{REPO}#{ISSUE}") == "31"
    assert get_run(s.store, s.run_id).status == "paused"  # type: ignore[union-attr]

    # the second poll applies only comment 32, so comment 31 is never replied to twice
    serve(respx_mock, comment(31, "/harness pause"), comment(32, "/harness resume"))
    assert await s.poll(outbox=s.fresh_outbox()) == 1
    assert s.cmd_keys() == [
        "cmd:31",
        "cmd:32",
    ]
    assert get_run(s.store, s.run_id).status == "needs-you"  # type: ignore[union-attr]


async def test_comments_from_before_the_run_are_ignored(s: Setup, respx_mock) -> None:
    serve(respx_mock, comment(41, "/harness approve", created=EARLIER))
    assert await s.poll() == 0
    gate = get_gate(s.store, GATE_ID)
    assert gate is not None and gate.status == "pending"
    assert s.outbox.enqueued == []


async def test_pause_resume_cancel(s: Setup, respx_mock) -> None:
    runner = s.runner()
    serve(respx_mock, comment(51, "/harness pause"))
    assert await s.poll(runner=runner) == 1
    assert get_run(s.store, s.run_id).status == "paused"  # type: ignore[union-attr]
    assert "Paused by @o" in s.replies()[-1]

    serve(respx_mock, comment(51, "/harness pause"), comment(52, "/harness resume"))
    assert await s.poll(runner=runner) == 1  # paused runs are polled too
    assert get_run(s.store, s.run_id).status == "needs-you"  # type: ignore[union-attr]
    assert "Resumed by @o" in s.replies()[-1]

    serve(
        respx_mock,
        comment(51, "/harness pause"),
        comment(52, "/harness resume"),
        comment(53, "/harness cancel"),
    )
    assert await s.poll(runner=runner) == 1
    assert get_run(s.store, s.run_id).status == "cancelled"  # type: ignore[union-attr]
    assert "Cancelled by @o" in s.replies()[-1]
    assert [e["verb"] for e in s.command_events()] == ["pause", "resume", "cancel"]
    assert all(e["ok"] for e in s.command_events())


async def test_resume_to_a_node(s: Setup, respx_mock) -> None:
    serve(respx_mock, comment(54, "/harness resume --to a"))
    assert await s.poll() == 1
    run = get_run(s.store, s.run_id)
    assert run is not None and run.status == "running"
    assert run.context["visits"]["a"] == 2
    assert "Resumed by @o at `a`" in s.replies()[-1]


async def test_retry_defaults_to_the_failed_node_and_records_agent(s: Setup, respx_mock) -> None:
    serve(respx_mock, comment(61, "/harness retry agent=codex"))
    assert await s.poll() == 1
    run = get_run(s.store, s.run_id)
    assert run is not None and run.context["visits"]["a"] == 2
    assert any(st.node_id == "a" and st.visit == 2 for st in steps_for_run(s.store, s.run_id))
    assert s.command_events() == [
        {"comment_id": 61, "login": "o", "verb": "retry", "ok": True, "error": None, "agent": "codex"}
    ]
    assert "Retry requested by @o" in s.replies()[-1]


async def test_retry_a_named_node_and_max_visits_error(s: Setup, respx_mock) -> None:
    limited = PLAYBOOK.model_copy(deep=True)
    limited.nodes["a"] = limited.nodes["a"].model_copy(update={"max_visits": 1})
    s.store.execute(
        "UPDATE runs SET context_json = json_set(context_json, '$.playbook', json(?)) WHERE id = ?",
        (limited.model_dump_json(), s.run_id),
    )
    serve(respx_mock, comment(62, "/harness retry a"))
    assert await s.poll() == 0
    assert "command failed" in s.replies()[-1] and "max_visits=1" in s.replies()[-1]
    assert s.command_events()[-1]["ok"] is False


async def test_errors_become_a_reply_and_are_not_retried(s: Setup, respx_mock) -> None:
    decide(s.store, GATE_ID, "approved", None, "cli")  # nothing left to approve
    serve(respx_mock, comment(71, "/harness approve"), comment(72, "/harness resume --to nowhere"))
    assert await s.poll() == 0

    assert "command failed" in s.replies()[0] and "no pending gate on o/r#7" in s.replies()[0]
    assert "command failed" in s.replies()[1] and "unknown node 'nowhere'" in s.replies()[1]
    events = s.command_events()
    assert [e["ok"] for e in events] == [False, False]
    assert events[1]["error"] == "unknown node 'nowhere'"
    assert get_meta(s.store, f"commands:{REPO}#{ISSUE}") == "72"


async def test_error_replies_are_redacted(s: Setup, respx_mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "tokenvalue1234")
    serve(respx_mock, comment(81, "/harness resume --to tokenvalue1234"))
    assert await s.poll() == 0
    [reply] = s.replies()
    assert "tokenvalue1234" not in reply and "[redacted]" in reply
    assert "tokenvalue1234" not in json.dumps(s.command_events())


async def test_failed_command_leaves_no_partial_state(s: Setup, respx_mock) -> None:
    # retry of a node that still has an active step fails after the visit counter logic starts: nothing sticks
    serve(respx_mock, comment(91, "/harness retry g"))
    assert await s.poll() == 0
    run = get_run(s.store, s.run_id)
    assert run is not None and run.context["visits"].get("g") == 1
    assert "still has an active step" in s.replies()[-1]


async def test_runs_that_are_not_active_are_not_polled(s: Setup) -> None:
    await s.runner().cancel(s.run_id)
    # respx would fail the test on any request (assert_all_mocked), and no route is registered
    assert await s.poll() == 0
    assert s.cmd_keys() == []


async def test_other_repos_runs_are_not_polled(s: Setup) -> None:
    s.store.execute("UPDATE runs SET repo = 'x/y' WHERE id = ?", (s.run_id,))
    assert await s.poll() == 0
