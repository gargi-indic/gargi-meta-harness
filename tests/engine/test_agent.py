"""Unit tests for the agent, escalate and notify executors against FakeAdapter."""

import json
from typing import Any

import pytest

from harness.adapters.base import (
    ActivityItem,
    AdapterRegistry,
    AgentSnapshot,
    AgentState,
    Capabilities,
    PrRef,
    SessionRef,
    StartRequest,
)
from harness.adapters.fake import FakeAdapter
from harness.config import RepoConfig
from harness.engine.executors.agent import AgentExecutor
from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.engine.executors.escalate import EscalateExecutor
from harness.engine.executors.notify import NotifyExecutor
from harness.github.client import GitHub, GitHubError
from harness.github.outbox import Outbox
from harness.playbook.models import AgentNode, EscalateNode, NotifyNode, Playbook
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.db import Store
from harness.store.repo import StepRow, get_run, steps_for_run, update_step

REPO = "owner/repo"
KEY = "run-1/session"


def pr(sha: str) -> PrRef:
    return PrRef(number=101, url="https://example.invalid/pull/101", head_sha=sha)


def script(*phases: list[AgentSnapshot]) -> FakeAdapter:
    return FakeAdapter(script={"*": list(phases)})


class RecordingAdapter(FakeAdapter):
    """FakeAdapter that keeps the StartRequests it receives and can fail one approve/send call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[StartRequest] = []
        self.fail_next: set[str] = set()
        self.attempts: dict[str, int] = {}

    async def start(self, req: StartRequest) -> SessionRef:
        self.requests.append(req)
        return await super().start(req)

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None:
        self._maybe_fail("approve_plan")
        await super().approve_plan(ref, note)

    async def send(self, ref: SessionRef, text: str) -> None:
        self._maybe_fail("send")
        await super().send(ref, text)

    def _maybe_fail(self, name: str) -> None:
        self.attempts[name] = self.attempts.get(name, 0) + 1
        if name in self.fail_next:
            self.fail_next.discard(name)
            raise RuntimeError(f"{name} crashed")

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)


class FakeGitHub:
    def __init__(self, files: dict[str, str], default_branch: str = "develop") -> None:
        self.files = files
        self.default_branch = default_branch
        self.repo_calls = 0
        self.file_refs: list[str] = []

    async def repo(self, repo: str) -> dict[str, Any]:
        self.repo_calls += 1
        return {"default_branch": self.default_branch}

    async def file_text(self, repo: str, path: str, ref: str | None = None) -> str:
        self.file_refs.append(str(ref))
        if path not in self.files:
            raise GitHubError(404, "GET", path, "not found")
        return self.files[path]


class Harness:
    """A store with one run and one step, plus helpers that persist step refs like the runner does."""

    def __init__(self, adapter: FakeAdapter, issue: int = 1, **ctx_kwargs: Any) -> None:
        self.adapter = adapter
        self.store = Store(":memory:")
        self.store.migrate()
        with self.store.tx():
            self.store.execute(
                "INSERT INTO issues (repo, number, id, title, body, state, updated_at) "
                "VALUES (?, 1, 100, 'Cached title', 'Cached body', 'open', '2023')",
                (REPO,),
            )
            self.store.execute(
                "INSERT INTO runs (id, repo, issue, playbook_id, status, context_json, created_at, updated_at) "
                "VALUES ('run-1', ?, ?, 'playbook', 'running', '{}', '2023', '2023')",
                (REPO, issue),
            )
            self.store.execute(
                "INSERT INTO steps (id, run_id, node_id, visit, status) VALUES ('s1', 'run-1', 'agent_1', 1, 'pending')"
            )
        adapters = AdapterRegistry()
        adapters.register(adapter)
        self.now = 1000.0
        self.ctx = ExecContext(
            store=self.store,
            outbox=ctx_kwargs.pop("outbox", None),
            gh=ctx_kwargs.pop("gh", None),
            adapters=adapters,
            repo_config=ctx_kwargs.pop("repo_config", RepoConfig()),
            playbook=Playbook(apiVersion="harness/v1", id="playbook", nodes={}, edges={}),
            run=get_run(self.store, "run-1"),
            template=ctx_kwargs.pop(
                "template",
                Context(
                    issue=IssueCtx(number=issue, title="Template title", body="Template body", agent="fake"),
                    run=RunCtx(id="run-1"),
                ),
            ),
            clock=lambda: self.now,
        )

    def step(self) -> StepRow:
        return steps_for_run(self.store, "run-1")[0]

    def events(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM events WHERE kind = 'agent_activity' ORDER BY id")
        return [{**r, "data": json.loads(r["data_json"])} for r in rows]

    async def advance(self, node: Any, executor: Any = None) -> Waiting | Done:
        """Advance the stored step with a (fresh) executor; persist a Waiting ref like the runner."""
        result = await (executor or AgentExecutor()).advance(self.step(), node, self.ctx)
        if isinstance(result, Waiting) and result.ref is not None:
            update_step(self.store, "s1", ref=result.ref)
        return result

    async def start_session(self, mode: str = "direct") -> SessionRef:
        return await self.adapter.start(
            StartRequest(
                repo=REPO,
                base_branch="main",
                branch="harness/1-x",
                issue=1,
                title="x",
                brief="",
                mode=mode,  # type: ignore[arg-type]
                idempotency_key=KEY,
            )
        )


def node(action: str, **kwargs: Any) -> AgentNode:
    return AgentNode(kind="agent", action=action, agent="fake", session="session", **kwargs)


# plan


@pytest.mark.parametrize(("plan_approval", "mode"), [(True, "plan_first"), (False, "direct")])
async def test_plan_start_mode_follows_capabilities(plan_approval, mode):
    caps = Capabilities(
        plan_approval=plan_approval, messaging=True, opens_pr=True, local_workspace=True, max_concurrency=1
    )
    adapter = RecordingAdapter(caps=caps)
    h = Harness(adapter)

    result = await h.advance(node("plan"))

    assert isinstance(result, Waiting)
    assert [r.mode for r in adapter.requests] == [mode]
    assert adapter.requests[0].idempotency_key == KEY
    assert result.ref["id"] == "fake-1" and result.ref["agent"] == "fake"
    assert "session" not in result.ref


async def test_plan_ready_returns_plan():
    h = Harness(script([AgentSnapshot(state=AgentState.PLAN_READY, plan="the plan")]))

    result = await h.advance(node("plan"))

    assert result == Done("plan_ready", {"plan": "the plan", "session": "fake-1"})


async def test_plan_pr_ready_returns_pr_dump():
    h = Harness(script([AgentSnapshot(state=AgentState.PR_READY, pr=pr("a1"))]))

    result = await h.advance(node("plan"))

    assert result == Done("pr_ready", {"pr": pr("a1").model_dump()})


async def test_plan_waits_with_snapshot_poll_after():
    h = Harness(script([AgentSnapshot(state=AgentState.PLANNING, poll_after=7)]))

    result = await h.advance(node("plan"))

    assert isinstance(result, Waiting) and result.poll_after == 7


async def test_plan_does_not_restart_existing_session():
    adapter = RecordingAdapter()
    h = Harness(adapter)
    session = await h.start_session()
    adapter.requests.clear()

    result = await h.advance(node("plan"))

    assert isinstance(result, Waiting)
    assert result.ref["id"] == session.id
    assert adapter.requests == []
    assert adapter.count("start") == 1

    await h.advance(node("plan"))
    assert adapter.requests == []


# implement


async def test_implement_without_session_starts_direct_and_never_approves():
    adapter = RecordingAdapter(
        script={"*": [[AgentSnapshot(state=AgentState.WORKING), AgentSnapshot(state=AgentState.WORKING)]]}
    )
    h = Harness(adapter)

    first = await h.advance(node("implement"))
    second = await h.advance(node("implement"))

    assert isinstance(first, Waiting) and isinstance(second, Waiting)
    assert [r.mode for r in adapter.requests] == ["direct"]
    assert adapter.count("approve_plan") == 0


async def test_implement_approves_plan_once():
    adapter = RecordingAdapter(
        script={
            "*": [
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="p")],
                [
                    AgentSnapshot(state=AgentState.WORKING),
                    AgentSnapshot(state=AgentState.PR_READY, pr=pr("a1")),
                ],
            ]
        }
    )
    h = Harness(adapter)
    await h.start_session()

    first = await h.advance(node("implement"))
    assert isinstance(first, Waiting) and first.poll_after == 0
    assert first.ref["approved"] is True
    assert h.step().ref["approved"] is True

    assert isinstance(await h.advance(node("implement")), Waiting)
    done = await h.advance(node("implement"))

    assert done == Done("pr_ready", {"pr": pr("a1").model_dump()})
    assert adapter.count("approve_plan") == 1


async def test_implement_does_not_approve_again_after_crash():
    adapter = RecordingAdapter(
        script={
            "*": [
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="p")],
                [AgentSnapshot(state=AgentState.WORKING)],
            ]
        }
    )
    h = Harness(adapter)
    await h.start_session()
    adapter.fail_next.add("approve_plan")

    with pytest.raises(RuntimeError, match="approve_plan crashed"):
        await h.advance(node("implement"))
    assert h.step().ref["approved"] is True

    result = await h.advance(node("implement"), executor=AgentExecutor())

    assert isinstance(result, Waiting)
    assert adapter.attempts["approve_plan"] == 1
    assert adapter.count("approve_plan") == 0


async def test_implement_planning_waits_without_setting_flag_then_approves():
    adapter = RecordingAdapter(
        script={
            "*": [
                [
                    AgentSnapshot(state=AgentState.STARTING),
                    AgentSnapshot(state=AgentState.PLANNING),
                    AgentSnapshot(state=AgentState.PLAN_READY, plan="p"),
                ],
                [AgentSnapshot(state=AgentState.WORKING)],
            ]
        }
    )
    h = Harness(adapter)
    await h.start_session()

    for _ in range(2):
        result = await h.advance(node("implement"))
        assert isinstance(result, Waiting)
        assert "approved" not in result.ref
        assert adapter.count("approve_plan") == 0

    result = await h.advance(node("implement"))

    assert isinstance(result, Waiting) and result.ref["approved"] is True
    assert adapter.count("approve_plan") == 1


@pytest.mark.parametrize("state", [AgentState.WORKING, AgentState.PR_READY])
async def test_implement_past_planning_sets_flag_without_approving(state):
    adapter = RecordingAdapter(script={"*": [[AgentSnapshot(state=state, pr=pr("a1"))]]})
    h = Harness(adapter)
    await h.start_session()

    await h.advance(node("implement"))

    assert h.step().ref["approved"] is True
    assert adapter.count("approve_plan") == 0


# revise


async def test_revise_without_session_fails():
    h = Harness(FakeAdapter.happy_path())

    result = await h.advance(node("revise", expect="plan", message="hello"))

    assert result == Done("failed", {"error": "no session for slot session"})


async def test_revise_empty_message_fails():
    adapter = RecordingAdapter()
    h = Harness(adapter)
    await h.start_session()

    result = await h.advance(node("revise", expect="plan", message="   "))

    assert result == Done("failed", {"error": "empty revise message"})
    assert adapter.count("send") == 0


async def test_revise_message_is_rendered_from_prev():
    adapter = RecordingAdapter(script={"*": [[AgentSnapshot(state=AgentState.PLAN_READY, plan="v1")]]})
    template = Context(
        issue=IssueCtx(number=1, title="T", agent="fake"),
        run=RunCtx(id="run-1"),
        prev={"correction": "tighten it"},
    )
    h = Harness(adapter, template=template)
    await h.start_session()

    await h.advance(node("revise", expect="plan", message="{{ prev.correction }}"))

    assert [c for c in adapter.calls if c[0] == "send"] == [
        ("send", {"session": "fake-1", "text": "tighten it"})
    ]


async def test_revise_plan_waits_for_changed_plan():
    adapter = RecordingAdapter(
        script={
            "*": [
                [AgentSnapshot(state=AgentState.PLAN_READY, plan="v1")],
                [
                    AgentSnapshot(state=AgentState.PLAN_READY, plan="v1"),
                    AgentSnapshot(state=AgentState.PLAN_READY, plan="v2"),
                ],
            ]
        }
    )
    h = Harness(adapter)
    await h.start_session()
    revise = node("revise", expect="plan", message="fix")

    sent = await h.advance(revise)
    assert isinstance(sent, Waiting) and sent.poll_after == 0
    assert sent.ref["baseline_plan"] == "v1" and sent.ref["sent"] is True

    assert isinstance(await h.advance(revise), Waiting)
    done = await h.advance(revise)

    assert done == Done("plan_ready", {"plan": "v2", "session": "fake-1"})
    assert adapter.count("send") == 1


async def test_revise_pr_waits_while_head_sha_equals_baseline():
    adapter = RecordingAdapter(
        script={
            "*": [
                [AgentSnapshot(state=AgentState.PR_READY, pr=pr("a1"))],
                [
                    AgentSnapshot(state=AgentState.PR_READY, pr=pr("a1")),
                    AgentSnapshot(state=AgentState.WORKING),
                    AgentSnapshot(state=AgentState.PR_READY, pr=pr("b2")),
                ],
            ]
        }
    )
    h = Harness(adapter)
    await h.start_session()
    revise = node("revise", expect="pr", message="fix")

    sent = await h.advance(revise)
    assert sent.ref["baseline_sha"] == "a1"

    assert isinstance(await h.advance(revise), Waiting)
    assert isinstance(await h.advance(revise), Waiting)
    done = await h.advance(revise)

    assert done == Done("pr_ready", {"pr": pr("b2").model_dump()})
    assert adapter.count("send") == 1


async def test_revise_does_not_send_again_after_crash():
    adapter = RecordingAdapter(script={"*": [[AgentSnapshot(state=AgentState.PR_READY, pr=pr("a1"))]]})
    h = Harness(adapter)
    await h.start_session()
    adapter.fail_next.add("send")
    revise = node("revise", expect="pr", message="fix")

    with pytest.raises(RuntimeError, match="send crashed"):
        await h.advance(revise)
    assert h.step().ref["sent"] is True

    result = await h.advance(revise, executor=AgentExecutor())

    assert isinstance(result, Waiting)
    assert adapter.attempts["send"] == 1
    assert adapter.count("send") == 0


# polling, activity, terminal states


async def test_activity_lands_in_events_redacted_and_capped_not_in_outbox(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue123")
    items = [
        ActivityItem(ts="t1", kind="message", text="used ghp_supersecretvalue123 here"),
        ActivityItem(ts="t2", kind="command", text="x" * 5000),
    ]
    adapter = script(
        [
            AgentSnapshot(state=AgentState.PLANNING, activity=items),
            AgentSnapshot(state=AgentState.PLANNING),
        ]
    )
    h = Harness(adapter)
    h.ctx.outbox = Outbox(h.store, GitHub(None))

    await h.advance(node("plan"))
    await h.advance(node("plan"))

    events = h.events()
    assert [e["data"]["ts"] for e in events] == ["t1", "t2"]
    assert [e["data"]["kind"] for e in events] == ["message", "command"]
    assert all(e["data"]["step"] == "s1" and e["run_id"] == "run-1" for e in events)
    assert events[0]["data"]["text"] == "used [redacted] here"
    assert len(events[1]["data"]["text"]) == 2000
    assert h.store.query("SELECT * FROM outbox") == []


async def test_activity_recorded_once_per_poll_on_implement_and_revise_paths():
    def snap(ts: str, state: AgentState = AgentState.WORKING) -> AgentSnapshot:
        return AgentSnapshot(
            state=state, plan="p", activity=[ActivityItem(ts=ts, kind="message", text=ts)], pr=pr("a1")
        )

    adapter = script([snap("i1", AgentState.PLAN_READY)], [snap("i2")], [snap("r1"), snap("r2"), snap("r3")])
    h = Harness(adapter)
    await h.start_session()

    await h.advance(node("implement"))
    await h.advance(node("implement"))
    assert [e["data"]["ts"] for e in h.events()] == ["i1", "i2"]

    await adapter.approve_plan(await adapter.find(KEY))
    revise = node("revise", expect="pr", message="fix")
    await h.advance(revise)
    await h.advance(revise)

    assert [e["data"]["ts"] for e in h.events()] == ["i1", "i2", "r1", "r2"]


async def test_timeout():
    h = Harness(script([AgentSnapshot(state=AgentState.PLANNING)]))
    plan = node("plan", timeout="1h")

    first = await h.advance(plan)
    assert isinstance(first, Waiting) and first.ref["since"] == 1000.0

    h.now = 1000.0 + 3600
    assert isinstance(await h.advance(plan), Waiting)

    h.now = 1000.0 + 3601
    result = await h.advance(plan)

    assert isinstance(result, Done) and result.outcome == "timeout"
    assert result.output["error"] == "timed out after 1h"
    assert "correction" in result.output


async def test_needs_input_returns_question():
    h = Harness(FakeAdapter.asks_question("what do?"))
    await h.advance(node("plan"))

    result = await h.advance(node("plan"))

    assert result == Done("needs_input", {"question": "what do?", "session": "fake-1"})


async def test_failed_returns_redacted_error(monkeypatch):
    monkeypatch.setenv("JULES_API_KEY", "jules-secret-value")
    h = Harness(FakeAdapter.fails("boom jules-secret-value"))
    await h.advance(node("plan"))

    result = await h.advance(node("plan"))

    assert result == Done("failed", {"error": "boom [redacted]"})


# starting a session: brief inputs


async def test_uncached_issue_falls_back_to_template_issue():
    adapter = RecordingAdapter()
    h = Harness(adapter, issue=99)

    await h.advance(node("plan"))

    req = adapter.requests[0]
    assert req.issue == 99 and req.title == "Template title"
    assert req.branch == "harness/99-template-title"
    assert "# Task: #99 Template title" in req.brief
    assert "Template body" in req.brief


async def test_cached_issue_context_files_and_base_branch_from_github():
    gh = FakeGitHub({"docs/a.md": "alpha text"})
    adapter = RecordingAdapter()
    config = RepoConfig(context_files=["docs/a.md", "docs/", "missing.md"])
    h = Harness(adapter, gh=gh, repo_config=config)

    await h.advance(node("plan", prompt="Be careful."))
    await h.advance(node("plan"))

    req = adapter.requests[0]
    assert req.base_branch == "develop"
    assert req.title == "Cached title" and req.branch == "harness/1-cached-title"
    assert "Cached body" in req.brief
    assert "### docs/a.md\n\nalpha text" in req.brief
    assert "missing.md" not in req.brief and "### docs/\n" not in req.brief
    assert "## Playbook instructions\n\nBe careful." in req.brief
    assert gh.repo_calls == 1
    assert set(gh.file_refs) == {"develop"}


async def test_configured_base_branch_skips_github_lookup():
    gh = FakeGitHub({})
    adapter = RecordingAdapter()
    h = Harness(adapter, gh=gh, repo_config=RepoConfig(base_branch="release"))

    await h.advance(node("plan"))

    assert adapter.requests[0].base_branch == "release"
    assert gh.repo_calls == 0


# escalate


def add_done_step(h: Harness, step_id: str, node_id: str, output: dict[str, Any], ended_at: str) -> None:
    with h.store.tx():
        h.store.execute(
            "INSERT INTO steps (id, run_id, node_id, visit, status, output_json, started_at, ended_at) "
            "VALUES (?, 'run-1', ?, 1, 'done', ?, ?, ?)",
            (step_id, node_id, json.dumps(output), ended_at, ended_at),
        )


async def test_escalate_names_the_step_that_routed_here():
    h = Harness(FakeAdapter())
    add_done_step(h, "run-1/review/1", "review", {"error": "bad"}, "2")
    add_done_step(h, "run-1/check/1", "check", {"summary": "later but unrelated"}, "3")
    h.ctx.template = h.ctx.template.model_copy(update={"prev": {"error": "bad"}})
    h.ctx.run.context["pr"] = {"url": "https://example.invalid/pull/5"}

    result = await EscalateExecutor().advance(h.step(), EscalateNode(kind="escalate"), h.ctx)

    assert isinstance(result, Waiting) and result.ref is None and result.poll_after == 3600
    assert result.output == {
        "needs_you": True,
        "reason": "Escalated after review:\n\nbad",
        "links": ["https://example.invalid/pull/5"],
    }


async def test_escalate_falls_back_to_latest_done_step_then_placeholder(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue123")
    h = Harness(FakeAdapter())
    h.ctx.template = h.ctx.template.model_copy(
        update={"prev": {"question": 42, "note": "ghp_supersecretvalue123"}}
    )

    alone = await EscalateExecutor().advance(h.step(), EscalateNode(kind="escalate"), h.ctx)
    assert alone.output["reason"] == "Escalated after an earlier step:\n\n42"
    assert alone.output["links"] == []

    add_done_step(h, "run-1/old/1", "old", {}, "1")
    add_done_step(h, "run-1/newer/1", "newer", {}, "2")
    h.ctx.template = h.ctx.template.model_copy(
        update={"prev": {"note": "ghp_supersecretvalue123 " + "n" * 600}}
    )
    result = await EscalateExecutor().advance(h.step(), EscalateNode(kind="escalate"), h.ctx)

    reason = result.output["reason"]
    assert reason.startswith("Escalated after newer:\n\n[redacted] n")
    assert len(reason.split("\n\n", 1)[1]) == 500


# notify


async def test_notify_enqueues_one_comment_dated_from_the_clock():
    h = Harness(FakeAdapter())
    h.ctx.outbox = Outbox(h.store, GitHub(None))
    h.now = 1_800_000_000.0
    notify = NotifyNode(kind="notify", message="test message")

    first = await NotifyExecutor().advance(h.step(), notify, h.ctx)
    await NotifyExecutor().advance(h.step(), notify, h.ctx)

    assert first == Done("sent", {"message": "test message"})
    rows = h.store.query("SELECT * FROM outbox")
    assert len(rows) == 1
    assert rows[0]["op"] == "add_comment"
    assert rows[0]["coalesce_key"] == "notify:s1"
    assert json.loads(rows[0]["payload_json"]) == {
        "repo": REPO,
        "issue": 1,
        "body": "**2027-01-15 · #1 note.**\n- test message",
    }
