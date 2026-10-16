"""Review executor (M1.9): prompts for plan/pr/question targets, verdict routing, the PR comment, crash safety."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import Response

from harness.adapters.base import AdapterRegistry
from harness.config import RepoConfig
from harness.engine import Done, ExecContext
from harness.engine.executors.review import ReviewExecutor
from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.llm.backend import BackendError, FakeBackend
from harness.playbook.models import ReviewNode
from harness.playbook.templating import Context, IssueCtx, RunCtx
from harness.store.db import Store
from harness.store.repo import RunRow, StepRow, insert_run, insert_step, steps_for_run, upsert_issue
from tests.engine.helpers import REPO, FakeClock, RecordingOutbox, make_issue, make_playbook

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)

API = "https://api.github.com"
PR = 7
ISSUE = 3
HEAD = "a" * 40
SECRET = "ghp_supersecretvalue99"
PULL_URL = f"{API}/repos/{REPO}/pulls/{PR}"
FILES_URL = f"{PULL_URL}/files"

APPROVE = {"verdict": "approve", "findings": [], "correction": "", "summary": "looks good"}
CHANGES = {
    "verdict": "changes",
    "findings": [
        {
            "severity": "blocker",
            "file": "a.py",
            "problem": "uses | pipes\nand lines",
            "required_change": "fix a.py",
        },
        {"severity": "minor", "file": None, "problem": "naming", "required_change": "rename"},
    ],
    "correction": "1. Edit a.py.\n2. Add tests/test_a.py.\n3. Do not add b.py.",
    "summary": "two things to fix",
}
ESCALATE = {"verdict": "escalate", "findings": [], "correction": "", "summary": "needs a human"}

PLAYBOOK = make_playbook(
    {
        "plan": {"kind": "agent", "session": "main", "action": "plan", "agent": "jules"},
        "ci": {"kind": "check", "check": "ci"},
        "rev": {"kind": "review", "target": "pr", "checklist": ["scope"]},
    },
    {"plan.plan_ready": "ci", "ci.pass": "rev", "ci.fail": "@failed", "rev.approve": "@done"},
)


@pytest.fixture
async def gh() -> AsyncIterator[GitHub]:
    async def no_sleep(_: float) -> None:
        return None

    client = GitHub("test-token", write_spacing=0.0, sleep=no_sleep)
    client._max_retries = 0
    yield client
    await client.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock):
    s = Store(":memory:", clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    s.migrate()
    upsert_issue(
        s, make_issue(ISSUE, title="Add slugify").model_copy(update={"body": f"Scope: slugify() {SECRET}"})
    )
    insert_run(s, make_run())
    yield s
    s.close()


@pytest.fixture
def outbox(store: Store, gh: GitHub, clock: FakeClock) -> RecordingOutbox:
    async def no_sleep(_: float) -> None:
        return None

    return RecordingOutbox(store, gh, clock=clock, sleep=no_sleep)


def make_run(context: dict[str, Any] | None = None) -> RunRow:
    return RunRow(
        id="run1",
        repo=REPO,
        issue=ISSUE,
        playbook_id="mini",
        playbook_sha=None,
        status="running",
        context={"pr": {"number": PR, "url": "u"}} if context is None else context,
        ledger_comment_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def make_ctx(
    gh: GitHub | None,
    outbox: Outbox | None,
    store: Store,
    clock: FakeClock,
    *,
    context: dict[str, Any] | None = None,
    nodes: dict[str, dict[str, Any]] | None = None,
    prev: dict[str, Any] | None = None,
) -> ExecContext:
    return ExecContext(
        store=store,
        outbox=outbox,
        gh=gh,
        adapters=AdapterRegistry(),
        repo_config=RepoConfig(),
        playbook=PLAYBOOK,
        run=make_run(context),
        template=Context(
            issue=IssueCtx(number=ISSUE, title="Add slugify", agent="jules"),
            run=RunCtx(id="run1"),
            nodes=nodes or {},
            prev=prev,
        ),
        clock=clock,
    )


def make_step(
    store: Store, node_id: str = "rev", visit: int = 1, *, output: dict[str, Any] | None = None
) -> StepRow:
    step = StepRow(
        id=f"run1/{node_id}/{visit}",
        run_id="run1",
        node_id=node_id,
        visit=visit,
        status="running",
        outcome=None,
        ref=None,
        output=output,
        poll_after=None,
        started_at="2026-01-01T00:00:00Z",
        ended_at=None,
    )
    insert_step(store, step)
    return step


def add_done_step(store: Store, node_id: str, visit: int, ended_at: str, output: dict[str, Any]) -> None:
    insert_step(
        store,
        StepRow(
            id=f"run1/{node_id}/{visit}",
            run_id="run1",
            node_id=node_id,
            visit=visit,
            status="done",
            outcome="plan_ready",
            ref=None,
            output=output,
            poll_after=None,
            started_at="2026-01-01T00:00:00Z",
            ended_at=ended_at,
        ),
    )


def node(target: str, checklist: list[str] | None = None) -> ReviewNode:
    return ReviewNode(target=target, checklist=["scope"] if checklist is None else checklist)  # type: ignore[arg-type]


def checklist_url(name: str) -> str:
    return f"{API}/repos/{REPO}/contents/.harness/checklists/{name}.md"


def mock_checklist_404(router, *names: str) -> None:
    for name in names:
        router.get(checklist_url(name)).mock(return_value=Response(404, json={"message": "Not Found"}))


def pull_json(sha: str = HEAD) -> dict[str, Any]:
    return {"number": PR, "state": "open", "head": {"sha": sha, "ref": "harness/3"}}


def pr_file(name: str, patch: str | None, status: str = "modified") -> dict[str, Any]:
    return {"filename": name, "status": status, "additions": 1, "deletions": 0, "patch": patch}


def mock_pr(router, files: list[dict[str, Any]], sha: str = HEAD):
    pull = router.get(PULL_URL).mock(return_value=Response(200, json=pull_json(sha)))
    listing = router.get(FILES_URL).mock(return_value=Response(200, json=files))
    return pull, listing


def comments(store: Store) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM outbox WHERE op = 'add_comment' ORDER BY id")
    return [{**r, "payload": json.loads(r["payload_json"])} for r in rows]


def events(store: Store, kind: str) -> list[dict[str, Any]]:
    return [
        {**r, "data": json.loads(r["data_json"])}
        for r in store.query("SELECT * FROM events WHERE kind = ?", (kind,))
    ]


# target: plan ------------------------------------------------------------------------------------------------


async def test_plan_prompt_has_issue_plan_and_every_checklist(respx_mock, gh, outbox, store, clock):
    mock_checklist_404(respx_mock, "scope", "acceptance")
    backend = FakeBackend([APPROVE])
    ctx = make_ctx(gh, outbox, store, clock, prev={"plan": "1. write slugify.py"})

    result = await ReviewExecutor(backend).advance(
        make_step(store, "plan_rev"), node("plan", ["scope", "acceptance"]), ctx
    )

    assert result == Done("approve", APPROVE)
    prompt = backend.calls[0]["prompt"]
    assert "Add slugify" in prompt and "Scope: slugify()" in prompt
    assert "1. write slugify.py" in prompt
    assert "Is every item under the issue's Scope covered?" in prompt  # built-in scope
    assert "Is every Acceptance criterion satisfied" in prompt  # built-in acceptance
    assert "`.harness/`" in prompt  # protected paths
    assert "untrusted data" in prompt
    assert backend.calls[0]["system"].lstrip().startswith("You are the reviewer")
    assert backend.calls[0]["workdir"] is None
    assert comments(store) == []  # no PR comment for a plan review


async def test_plan_falls_back_to_the_latest_done_plan(respx_mock, gh, outbox, store, clock):
    add_done_step(store, "plan", 1, "2026-01-01T00:01:00Z", {"plan": "OLD PLAN"})
    add_done_step(store, "plan", 2, "2026-01-01T00:05:00Z", {"plan": "NEW PLAN"})
    add_done_step(store, "other", 1, "2026-01-01T00:09:00Z", {"plan": ""})  # empty plans do not count
    backend = FakeBackend([APPROVE])

    await ReviewExecutor(backend).advance(
        make_step(store, "plan_rev"), node("plan", []), make_ctx(None, None, store, clock)
    )

    assert "NEW PLAN" in backend.calls[0]["prompt"] and "OLD PLAN" not in backend.calls[0]["prompt"]


async def test_plan_review_without_a_plan_escalates_without_calling_the_model(store, clock):
    backend = FakeBackend([])

    result = await ReviewExecutor(backend).advance(
        make_step(store, "plan_rev"), node("plan", []), make_ctx(None, None, store, clock)
    )

    assert result.outcome == "escalate" and result.output["summary"] == "no plan to review"
    assert backend.calls == []


# checklists ---------------------------------------------------------------------------------------------------


async def test_repo_checklist_overrides_the_builtin(respx_mock, gh, outbox, store, clock):
    respx_mock.get(checklist_url("scope")).mock(return_value=Response(200, text="- REPO RULE: no viper\n"))
    mock_checklist_404(respx_mock, "plan-match")
    backend = FakeBackend([APPROVE])

    await ReviewExecutor(backend).advance(
        make_step(store, "plan_rev"),
        node("plan", ["scope", "plan-match"]),
        make_ctx(gh, outbox, store, clock, prev={"plan": "p"}),
    )

    prompt = backend.calls[0]["prompt"]
    assert "- REPO RULE: no viper" in prompt
    assert "Is every item under the issue's Scope covered?" not in prompt
    assert "Does the diff do what the approved plan says" in prompt  # built-in plan-match, no repo version


async def test_missing_checklist_is_skipped_and_logged(respx_mock, gh, outbox, store, clock):
    mock_checklist_404(respx_mock, "ghost")
    backend = FakeBackend([APPROVE])

    result = await ReviewExecutor(backend).advance(
        make_step(store, "plan_rev"),
        node("plan", ["ghost", "../etc/passwd"]),
        make_ctx(gh, outbox, store, clock, prev={"plan": "p"}),
    )

    assert result.outcome == "approve"
    logged = events(store, "checklist_missing")
    assert [e["data"] for e in logged] == [{"name": "ghost"}, {"name": "../etc/passwd"}]
    assert logged[0]["repo"] == REPO and logged[0]["issue"] == ISSUE and logged[0]["run_id"] == "run1"
    assert "(no checklists configured)" in backend.calls[0]["prompt"]


# target: pr ---------------------------------------------------------------------------------------------------


async def test_pr_prompt_has_plan_patches_ci_checklists_and_head_is_read_first(
    respx_mock, gh, outbox, store, clock
):
    add_done_step(store, "plan", 1, "2026-01-01T00:01:00Z", {"plan": "THE APPROVED PLAN"})
    mock_checklist_404(respx_mock, "scope")
    pull, listing = mock_pr(
        respx_mock, [pr_file("a.py", "+alpha = 1"), pr_file("b.py", "+beta = 2", "added")]
    )
    backend = FakeBackend([APPROVE])
    ctx = make_ctx(gh, outbox, store, clock, nodes={"ci": {"conclusion": "success", "checks": ["pytest"]}})

    result = await ReviewExecutor(backend).advance(make_step(store), node("pr"), ctx)

    prompt = backend.calls[0]["prompt"]
    for needle in (
        "THE APPROVED PLAN",
        "+alpha = 1",
        "+beta = 2",
        "a.py",
        "b.py (added",
        '"conclusion": "success"',
        "Scope covered",
    ):
        assert needle in prompt, needle
    assert result.outcome == "approve"
    assert result.output == {**APPROVE, "head_sha": HEAD}
    assert respx_mock.calls[0].request.url.path.endswith(f"/pulls/{PR}")  # head sha read before the files
    assert respx_mock.calls[1].request.url.path.endswith(f"/pulls/{PR}/files")
    assert pull.call_count == 1 and listing.call_count == 1


async def test_pr_prompt_without_plan_or_ci(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", None, "renamed")])
    backend = FakeBackend([APPROVE])

    await ReviewExecutor(backend).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    prompt = backend.calls[0]["prompt"]
    assert "No approved plan recorded" in prompt and "No CI result recorded" in prompt
    assert "no textual patch" in prompt


async def test_diff_is_truncated_per_file_and_in_total(respx_mock, gh, outbox, store, clock):
    files = [
        pr_file("big.py", "B" * 500),
        pr_file("small.py", "S" * 50),
        pr_file("mid.py", "M" * 150),
        pr_file("late.py", "L" * 10),
    ]
    mock_pr(respx_mock, files)
    backend = FakeBackend([APPROVE])

    await ReviewExecutor(backend, diff_budget=300, per_file_budget=200).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    prompt = backend.calls[0]["prompt"]
    marker = "\n[... truncated]"
    assert "B" * 200 + marker in prompt and "B" * 201 not in prompt
    assert "S" * 50 in prompt  # 216 + 50 fits in 300
    # mid.py would bring the total to 366: it and everything after it are listed by name only.
    assert "M" * 20 not in prompt
    assert "L" * 10 not in prompt
    assert "Diff truncated" in prompt
    assert "- mid.py" in prompt and "- late.py" in prompt
    assert "- big.py" not in prompt and "- small.py" not in prompt


async def test_diff_total_stays_within_budget(respx_mock, gh, outbox, store, clock):
    files = [pr_file(f"f{i}.py", "\u00a7" * 90) for i in range(6)]
    mock_pr(respx_mock, files)
    backend = FakeBackend([APPROVE])

    await ReviewExecutor(backend, diff_budget=300, per_file_budget=100).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    prompt = backend.calls[0]["prompt"]
    assert prompt.count("\u00a7") == 270  # three 90-char patches fit in 300; the fourth does not
    for omitted in ("f3.py", "f4.py", "f5.py"):
        assert f"- {omitted}" in prompt
    assert "- f2.py" not in prompt


async def test_secrets_never_reach_the_prompt_or_the_comment(
    respx_mock, gh, outbox, store, clock, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    add_done_step(store, "plan", 1, "2026-01-01T00:01:00Z", {"plan": f"use {SECRET} here"})
    mock_pr(respx_mock, [pr_file(f"{SECRET}.py", f"+TOKEN = '{SECRET}'")])
    leaky = {**CHANGES, "summary": f"found {SECRET}", "correction": f"1. remove {SECRET}"}
    backend = FakeBackend([leaky])
    ctx = make_ctx(gh, outbox, store, clock, nodes={"ci": {"log": f"token {SECRET}"}})

    result = await ReviewExecutor(backend, per_file_budget=200).advance(make_step(store), node("pr", []), ctx)

    assert SECRET not in backend.calls[0]["prompt"] and "[redacted]" in backend.calls[0]["prompt"]
    # The issue body in the store holds the secret too (see the store fixture) and is redacted in the prompt.
    assert "Scope: slugify()" in backend.calls[0]["prompt"]
    body = comments(store)[0]["payload"]["body"]
    assert SECRET not in body and "[redacted]" in body
    assert result.outcome == "changes"


async def test_pr_review_without_a_pr_escalates(gh, outbox, store, clock):
    backend = FakeBackend([])

    result = await ReviewExecutor(backend).advance(
        make_step(store), node("pr"), make_ctx(gh, outbox, store, clock, context={})
    )

    assert result.outcome == "escalate" and result.output["summary"] == "no PR in run context"
    assert backend.calls == []


# verdict routing ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("response", [APPROVE, CHANGES, ESCALATE], ids=["approve", "changes", "escalate"])
async def test_verdicts_route_to_matching_outcomes(respx_mock, gh, outbox, store, clock, response):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])

    result = await ReviewExecutor(FakeBackend([response])).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    assert result.outcome == response["verdict"]
    assert result.output == {**response, "head_sha": HEAD}
    assert result.output["findings"] == response["findings"]


async def test_the_result_is_stored_on_the_step(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    step = make_step(store)

    result = await ReviewExecutor(FakeBackend([CHANGES])).advance(
        step, node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    stored = steps_for_run(store, "run1")[0]
    assert stored.output == result.output


async def test_invalid_json_once_is_retried(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    backend = FakeBackend(["not json", CHANGES])

    result = await ReviewExecutor(backend).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    assert result.outcome == "changes" and len(backend.calls) == 2
    assert "Your previous reply was invalid" in backend.calls[1]["prompt"]


async def test_invalid_twice_escalates_and_changes_needs_a_correction(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    empty_correction = {**CHANGES, "correction": ""}
    backend = FakeBackend([empty_correction, BackendError("sdk down")])

    result = await ReviewExecutor(backend).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    assert result.outcome == "escalate" and len(backend.calls) == 2
    assert result.output["summary"].startswith("reviewer returned invalid output: ")
    assert result.output["head_sha"] == HEAD and result.output["correction"] == ""


# PR comment -----------------------------------------------------------------------------------------------------


async def test_pr_comment_is_enqueued_once_with_findings_and_correction(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    step = make_step(store, visit=2)

    await ReviewExecutor(FakeBackend([CHANGES])).advance(
        step, node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    assert len(outbox.enqueued) == 1
    op, payload, key = outbox.enqueued[0]
    assert op == "add_comment" and key == "review:run1/rev/2"
    assert payload["repo"] == REPO and payload["issue"] == PR  # the PR, not the issue
    body = payload["body"]
    assert body.startswith("**Harness review: changes**\n\ntwo things to fix\n")
    assert body.endswith("\n\n<!-- harness-review:run1/rev/2 -->")
    assert "| Severity | File | Problem | Required change |" in body
    assert "| blocker | `a.py` | uses \\| pipes and lines | fix a.py |" in body
    assert "| minor |  | naming | rename |" in body
    assert "### Required changes\n\n1. Edit a.py.\n2. Add tests/test_a.py.\n3. Do not add b.py." in body


async def test_approve_comment_has_no_table_or_required_changes(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])

    await ReviewExecutor(FakeBackend([APPROVE])).advance(
        make_step(store), node("pr", []), make_ctx(gh, outbox, store, clock)
    )

    assert comments(store)[0]["payload"]["body"] == (
        "**Harness review: approve**\n\nlooks good\n\n<!-- harness-review:run1/rev/1 -->"
    )


async def test_no_outbox_means_no_comment_and_no_failure(respx_mock, gh, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])

    result = await ReviewExecutor(FakeBackend([APPROVE])).advance(
        make_step(store), node("pr", []), make_ctx(gh, None, store, clock)
    )

    assert result.outcome == "approve"


async def test_readvance_after_a_crash_does_not_ask_the_model_again(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    first_backend = FakeBackend([CHANGES])
    ex = ReviewExecutor(first_backend)
    ctx = make_ctx(gh, outbox, store, clock)
    first = await ex.advance(make_step(store), node("pr", []), ctx)

    # The runner crashed before finishing the step; it is re-advanced from the stored row.
    reloaded = steps_for_run(store, "run1")[0]
    second_backend = FakeBackend([])
    second = await ReviewExecutor(second_backend).advance(reloaded, node("pr", []), ctx)

    assert second == first
    assert second_backend.calls == [] and len(first_backend.calls) == 1
    assert len(comments(store)) == 1 and len(outbox.enqueued) == 1


async def test_readvance_repairs_a_comment_lost_between_store_and_enqueue(
    respx_mock, gh, outbox, store, clock
):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    ctx = make_ctx(gh, outbox, store, clock)
    first = await ReviewExecutor(FakeBackend([CHANGES])).advance(make_step(store), node("pr", []), ctx)
    store.execute("DELETE FROM outbox")  # crash happened after update_step and before enqueue
    reloaded = steps_for_run(store, "run1")[0]
    backend = FakeBackend([])

    second = await ReviewExecutor(backend).advance(reloaded, node("pr", []), ctx)

    assert second == first and backend.calls == []
    assert len(comments(store)) == 1


async def test_readvance_after_the_comment_was_sent_does_not_repost(respx_mock, gh, outbox, store, clock):
    mock_pr(respx_mock, [pr_file("a.py", "+x")])
    post = respx_mock.post(f"{API}/repos/{REPO}/issues/{PR}/comments").mock(
        return_value=Response(201, json={"id": 1})
    )
    ctx = make_ctx(gh, outbox, store, clock)
    await ReviewExecutor(FakeBackend([APPROVE])).advance(make_step(store), node("pr", []), ctx)
    assert await outbox.drain_once() == 1 and post.call_count == 1

    await ReviewExecutor(FakeBackend([])).advance(steps_for_run(store, "run1")[0], node("pr", []), ctx)

    assert await outbox.drain_once() == 0 and post.call_count == 1


# target: question ---------------------------------------------------------------------------------------------------


async def test_question_prompt_and_answer(respx_mock, gh, outbox, store, clock):
    add_done_step(store, "plan", 1, "2026-01-01T00:01:00Z", {"plan": "1. parse config"})
    mock_checklist_404(respx_mock, "decisions")
    answer = {**APPROVE, "correction": "Use the stdlib; do not add viper.", "summary": "answered"}
    backend = FakeBackend([answer])
    ctx = make_ctx(gh, outbox, store, clock, prev={"question": "Should I add viper?"})

    result = await ReviewExecutor(backend).advance(
        make_step(store, "q"), node("question", ["decisions"]), ctx
    )

    prompt = backend.calls[0]["prompt"]
    assert "Should I add viper?" in prompt and "1. parse config" in prompt and "Add slugify" in prompt
    assert "Does the work follow the decisions already recorded" in prompt
    assert result == Done("approve", answer)
    assert comments(store) == [] and "head_sha" not in result.output


async def test_question_review_without_a_question_escalates(store, clock):
    backend = FakeBackend([])

    result = await ReviewExecutor(backend).advance(
        make_step(store, "q"), node("question", []), make_ctx(None, None, store, clock)
    )

    assert result.outcome == "escalate" and backend.calls == []
