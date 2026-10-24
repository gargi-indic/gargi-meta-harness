"""The tournament playbook (M3.9) end to end: two agents race, a judge picks, the winner is checked and merged.

The built-in ``tournament.yaml`` runs on the real executors (agent, join select, check, review, gate, merge,
escalate) over scripted ``FakeAdapter`` instances registered as ``claude`` and ``codex``, a ``FakeBackend`` as
judge and reviewer, and a stateful GitHub stand-in behind respx. The outbox is drained between ticks so the
merge really reaches the (fake) GitHub API.
"""

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from harness.adapters.base import AgentSnapshot, AgentState, PrRef, SessionRef
from harness.engine import SimulatedCrash
from harness.engine.executors import default_registry
from harness.engine.model import TERMINAL_STATUSES
from harness.github.commands import decide
from harness.llm.backend import FakeBackend
from harness.playbook.load import builtin_playbooks
from harness.store.repo import pending_gates
from tests.engine.helpers import REPO, CountingAdapter, make_issue

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)

RUN_ID = f"{REPO}#7@1"
JOIN_STEP = f"{RUN_ID}/pick/1"
MARKER = f"<!-- harness:select run={RUN_ID} step={JOIN_STEP} -->"
CLAUDE_PR, CODEX_PR = 101, 102
RATIONALE = "codex has tests and the smaller diff"
JUDGE_CODEX = {
    "winner": "b",
    "ranking": ["b", "a"],
    "rationale": RATIONALE,
    "per_candidate": {"a": ["no tests"], "b": ["tests present"]},
}
JUDGE_CLAUDE = {**JUDGE_CODEX, "winner": "a", "ranking": ["a", "b"], "rationale": "claude is cleaner"}
APPROVE = {"verdict": "approve", "findings": [], "correction": "", "summary": "matches the issue"}
BOTH_AND_REVIEW = [JUDGE_CODEX, APPROVE]
MERGE_SHA = "c0ffee1" + "0" * 33


class GitHubWorld:
    """Stateful GitHub behind respx: pulls (head sha moves on every fix), files, CI per sha, comments, merge."""

    def __init__(self, respx_mock) -> None:
        self.head = {CLAUDE_PR: "claude-1", CODEX_PR: "codex-1"}
        self.state = {CLAUDE_PR: "open", CODEX_PR: "open"}
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.deleted: list[str] = []
        self.comments: dict[int, list[str]] = {}
        self.issue_state = "open"
        self.passing: Callable[[str], bool] = lambda sha: True
        self.requests: list[tuple[str, str]] = []
        respx_mock.route().mock(side_effect=self._handle)

    def _pull(self, n: int) -> dict[str, Any]:
        merged = n in self.merged
        return {
            "number": n,
            "state": "closed" if merged else self.state[n],
            "merged": merged,
            "mergeable": True,
            "mergeable_state": "clean",
            "merge_commit_sha": MERGE_SHA if merged else None,
            "title": f"PR {n}",
            "additions": 40 if n == CLAUDE_PR else 10,
            "deletions": 5 if n == CLAUDE_PR else 2,
            "head": {"sha": self.head[n], "ref": f"agent-{n}", "repo": {"full_name": REPO}},
        }

    def count(self, method: str, pattern: str) -> int:
        return sum(1 for m, p in self.requests if m == method and re.fullmatch(pattern, p))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else None
        base = f"/repos/{REPO}"
        if path == base:
            return httpx.Response(200, json={"full_name": REPO, "default_branch": "main"})
        if path.startswith(f"{base}/contents/"):
            return httpx.Response(404, json={"message": "Not Found"})
        if m := re.fullmatch(rf"{base}/pulls/(\d+)", path):
            n = int(m[1])
            if method == "PATCH":
                assert body == {"state": "closed"}
                self.state[n] = "closed"
                self.closed.append(n)
            return httpx.Response(200, json=self._pull(n))
        if m := re.fullmatch(rf"{base}/pulls/(\d+)/merge", path):
            self.merged.append(int(m[1]))
            return httpx.Response(200, json={"merged": True, "sha": MERGE_SHA})
        if m := re.fullmatch(rf"{base}/pulls/(\d+)/files", path):
            n = int(m[1])
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "slugify.py",
                        "status": "added",
                        "additions": 6,
                        "deletions": 0,
                        "patch": f"@@ +1 @@\n+# change from PR {n}",
                    }
                ],
            )
        if m := re.fullmatch(rf"{base}/commits/(.+)/check-runs", path):
            conclusion = "success" if self.passing(m[1]) else "failure"
            run = {
                "name": "ci",
                "status": "completed",
                "conclusion": conclusion,
                "html_url": "https://ci.invalid",
            }
            return httpx.Response(200, json={"total_count": 1, "check_runs": [run]})
        if re.fullmatch(rf"{base}/commits/.+/status", path):
            return httpx.Response(200, json={"state": "pending", "statuses": []})
        if m := re.fullmatch(rf"{base}/issues/(\d+)/comments", path):
            n = int(m[1])
            if method == "POST":
                self.comments.setdefault(n, []).append(body["body"])
                return httpx.Response(201, json={"id": 1000 + len(self.comments[n])})
            return httpx.Response(
                200, json=[{"id": i, "body": b} for i, b in enumerate(self.comments.get(n, []))]
            )
        if m := re.fullmatch(rf"{base}/issues/(\d+)", path):
            if method == "PATCH" and body and "state" in body:
                self.issue_state = body["state"]
            return httpx.Response(
                200,
                json={
                    "number": int(m[1]),
                    "state": self.issue_state,
                    "body": "",
                    "title": "Fix it",
                    "labels": [],
                },
            )
        if method == "DELETE" and (m := re.fullmatch(rf"{base}/git/refs/heads/(.+)", path)):
            self.deleted.append(m[1])
            return httpx.Response(204)
        if method == "POST" and re.fullmatch(rf"{base}/(issues/\d+/)?labels", path):
            return httpx.Response(201, json=[])
        if method == "DELETE" and re.fullmatch(rf"{base}/issues/\d+/labels/.+", path):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unmocked GitHub call: {method} {path}")


def _pr(number: int, sha: str) -> PrRef:
    return PrRef(number=number, url=f"https://example.invalid/pull/{number}", head_sha=sha)


class RaceAdapter(CountingAdapter):
    """A scripted agent whose ``send`` (a fix round) moves the PR head on the fake GitHub."""

    def __init__(self, name: str, world: GitHubWorld, number: int, rounds: int = 1, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.world, self.number, self.round, self.rounds = world, number, 0, rounds

    async def send(self, ref: SessionRef, text: str) -> None:
        await super().send(ref, text)
        self.round = min(self.round + 1, self.rounds)
        self.world.head[self.number] = _head(self.name, self.round)


def _head(name: str, round: int) -> str:
    return f"{name}-{round + 1}"


def _script(name: str, number: int, *, fail: bool = False, rounds: int = 1) -> list[list[AgentSnapshot]]:
    """One implement phase, then one phase per fix round (each ends on a new head sha)."""
    first = (
        AgentSnapshot(state=AgentState.FAILED, error="boom")
        if fail
        else AgentSnapshot(state=AgentState.PR_READY, pr=_pr(number, _head(name, 0)))
    )
    phases = [[AgentSnapshot(state=AgentState.WORKING), first]]
    for i in range(1, rounds + 1):
        phases.append(
            [
                AgentSnapshot(state=AgentState.WORKING),
                AgentSnapshot(state=AgentState.PR_READY, pr=_pr(number, _head(name, i))),
            ]
        )
    return phases


class Cup:
    """One tournament: both adapters, the GitHub world, the judge/reviewer backend and a runner factory."""

    def __init__(
        self,
        env,
        respx_mock,
        responses: list[dict | str | Exception],
        *,
        claude_fails: bool = False,
        codex_fails: bool = False,
        rounds: int = 1,
    ) -> None:
        self.env = env
        self.world = GitHubWorld(respx_mock)
        self.backend = FakeBackend(list(responses))
        self.claude = RaceAdapter(
            "claude",
            self.world,
            CLAUDE_PR,
            rounds,
            script={f"{RUN_ID}/a": _script("claude", CLAUDE_PR, fail=claude_fails, rounds=rounds)},
        )
        self.codex = RaceAdapter(
            "codex",
            self.world,
            CODEX_PR,
            rounds,
            script={f"{RUN_ID}/b": _script("codex", CODEX_PR, fail=codex_fails, rounds=rounds)},
        )
        self.world.head[CLAUDE_PR] = _head("claude", 0)
        self.world.head[CODEX_PR] = _head("codex", 0)
        env.adapters.register(self.claude)
        env.adapters.register(self.codex)
        self.playbook = builtin_playbooks()["tournament"]

    def runner(self):
        return self.env.runner(executors=default_registry(review_backend=self.backend))

    def start(self):
        runner = self.runner()
        runner.start_run(REPO, make_issue(), self.playbook, agent="claude")
        return runner

    async def advance(
        self, runner, *, until: str | None = None, approve_merge: bool = True, limit: int = 120
    ):
        """Tick and drain the outbox until the run is terminal (or in ``until``); approve the merge gate."""
        env = self.env
        for _ in range(limit):
            await runner.tick()
            await env.outbox.drain_once()
            run = env.run(RUN_ID)
            if run.status in TERMINAL_STATUSES or run.status == until:
                return run
            if approve_merge:
                for gate in pending_gates(env.store):
                    decide(env.store, gate.id, "approved", None, "test")
            env.clock.advance(60)
        raise AssertionError(f"run not finished after {limit} ticks: {env.run(RUN_ID).status}")

    def step(self, node: str, visit: int = 1):
        return next(s for s in self.env.steps(RUN_ID) if s.node_id == node and s.visit == visit)

    def visits(self, node: str) -> int:
        return sum(1 for s in self.env.steps(RUN_ID) if s.node_id == node)


@pytest.fixture
def cup(env, respx_mock):
    return Cup(env, respx_mock, BOTH_AND_REVIEW)


def test_the_playbook_is_builtin_and_forks_both_agents():
    pb = builtin_playbooks()["tournament"]
    assert pb.nodes["fork"].branches == ["a", "b"]
    assert (pb.nodes["a"].agent, pb.nodes["b"].agent) == ("claude", "codex")
    assert pb.nodes["pick"].mode == "select"


async def test_full_path_both_prs_judge_picks_loser_closed_winner_checked_and_merged(cup, env):
    runner = cup.start()
    run = await cup.advance(runner)
    world = cup.world

    assert run.status == "done"
    done = {s.node_id: s.outcome for s in env.steps(RUN_ID) if s.status == "done"}
    assert done == {
        "fork": "forked",
        "a": "pr_ready",
        "b": "pr_ready",
        "pick": "done",
        "ci": "pass",
        "scope": "pass",
        "pr_review": "approve",
        "merge_gate": "approved",
        "merge": "merged",
    }
    # The winner is recorded in the join step, the run context and the PR the later nodes work on.
    pick = cup.step("pick")
    assert pick.output["winner"] == "b" and pick.output["winner_pr"] == CODEX_PR
    assert pick.output["losers"] == [{"node": "a", "pr": CLAUDE_PR, "branch": f"agent-{CLAUDE_PR}"}]
    assert run.context["nodes"]["pick"]["winner"] == "b" and run.context["pr"]["number"] == CODEX_PR
    assert cup.step("ci").output["head_sha"] == "codex-1"
    # The loser was commented on (with the select marker) and closed; the winner got the rationale.
    assert world.closed == [CLAUDE_PR] and world.state[CODEX_PR] == "open"
    assert world.comments[CLAUDE_PR] == [
        f"Not selected in tournament for #7 (winner: PR #{CODEX_PR}): {RATIONALE}\n\n{MARKER}"
    ]
    selected, review = world.comments[CODEX_PR]  # then the PR review comment of the winner
    assert selected == f"Selected in tournament for #7: {RATIONALE}\n\n{MARKER}"
    assert review.startswith("**Harness review: approve**")
    # Only the winner is merged, exactly once; the loser's branch is deleted.
    assert world.merged == [CODEX_PR]
    assert f"agent-{CLAUDE_PR}" in world.deleted
    # One judge call (with both diffs) and one PR review; each agent got exactly one session.
    assert len(cup.backend.calls) == 2
    judge_prompt = cup.backend.calls[0]["prompt"]
    assert "change from PR 101" in judge_prompt and "change from PR 102" in judge_prompt
    assert "smallest-diff" in judge_prompt
    assert cup.claude.start_invocations == cup.codex.start_invocations == 1
    # The ledger on the issue is updated with the run's steps through the outbox.
    ledger = env.outbox.of("upsert_ledger")[-1]["body"]
    assert "| PR | #102 |" in ledger and '"winner":"b"' in ledger and '"winner_pr":102' in ledger
    assert env.events("outbox_failed") == []


async def test_the_merge_gate_waits_for_a_human(cup, env):
    runner = cup.start()
    run = await cup.advance(runner, until="needs-you", approve_merge=False)
    assert run.status == "needs-you"
    assert [g.name for g in pending_gates(env.store)] == ["merge"]
    assert cup.world.merged == []
    assert cup.world.closed == [CLAUDE_PR]  # the tournament is already decided


async def test_one_branch_fails_and_the_other_wins_without_a_judge_call(env, respx_mock):
    cup = Cup(env, respx_mock, [APPROVE], claude_fails=True)
    run = await cup.advance(cup.start())
    assert run.status == "done"
    assert cup.step("a").outcome == "failed"
    pick = cup.step("pick")
    assert pick.outcome == "done" and pick.output["winner"] == "b" and pick.output["failed"] == ["a"]
    assert len(cup.backend.calls) == 1  # only the PR review: a lone candidate wins without the judge
    assert cup.world.merged == [CODEX_PR] and cup.world.closed == []


async def test_the_other_agent_can_win_too(env, respx_mock):
    cup = Cup(env, respx_mock, [JUDGE_CLAUDE, APPROVE])
    run = await cup.advance(cup.start())
    assert run.status == "done"
    assert cup.world.merged == [CLAUDE_PR] and cup.world.closed == [CODEX_PR]


async def test_a_branch_that_asks_for_input_is_dropped_by_the_join(env, respx_mock):
    cup = Cup(env, respx_mock, [APPROVE])
    cup.claude.script[f"{RUN_ID}/a"] = [
        [AgentSnapshot(state=AgentState.NEEDS_INPUT, question="Which approach?")]
    ]
    run = await cup.advance(cup.start())
    assert run.status == "done"
    assert cup.step("a").outcome == "needs_input"
    assert cup.step("pick").output["winner"] == "b"


async def test_both_branches_fail_and_the_run_escalates(env, respx_mock):
    cup = Cup(env, respx_mock, [], claude_fails=True, codex_fails=True)
    run = await cup.advance(cup.start(), until="needs-you")
    assert run.status == "needs-you"
    pick = cup.step("pick")
    assert pick.outcome == "failed" and sorted(pick.output["failed"]) == ["a", "b"]
    assert cup.step("escalate").status == "waiting"
    assert cup.visits("ci") == 0
    assert cup.world.merged == [] and cup.world.closed == []
    assert cup.backend.calls == []


async def test_fix_continues_the_winners_own_session_not_the_losers(env, respx_mock):
    cup = Cup(env, respx_mock, BOTH_AND_REVIEW)
    cup.world.passing = lambda sha: sha != "codex-1"  # the winner's first head fails CI
    run = await cup.advance(cup.start())
    world = cup.world

    assert run.status == "done"
    assert [cup.step("ci", v).outcome for v in (1, 2)] == ["fail", "pass"]
    fix = cup.step("fix")
    assert fix.outcome == "pr_ready" and fix.ref["agent"] == "codex"
    codex_session = cup.step("b").ref["id"]
    assert fix.ref["id"] == codex_session
    sends = [c for name, c in cup.codex.calls if name == "send"]
    assert [c["session"] for c in sends] == [codex_session]
    assert "CI failed on codex-1" in sends[0]["text"]  # the correction of the failed check
    assert cup.claude.count("send") == 0
    # No new session was started for the fix, and the merge is of the fixed head.
    assert cup.claude.start_invocations == cup.codex.start_invocations == 1
    assert cup.step("ci", 2).output["head_sha"] == "codex-2"
    assert world.merged == [CODEX_PR]
    assert (
        run.context["sessions"]["b"]["agent"] == "codex"
        and "{{ pick.winner }}" not in run.context["sessions"]
    )


async def test_fix_continues_the_session_of_claude_when_claude_wins(env, respx_mock):
    cup = Cup(env, respx_mock, [JUDGE_CLAUDE, APPROVE])
    cup.world.passing = lambda sha: sha != "claude-1"
    run = await cup.advance(cup.start())
    assert run.status == "done"
    assert cup.step("fix").ref["agent"] == "claude" and cup.claude.count("send") == 1
    assert cup.codex.count("send") == 0


async def test_the_fix_loop_stops_at_max_visits_and_escalates(env, respx_mock):
    cup = Cup(env, respx_mock, BOTH_AND_REVIEW, rounds=3)
    cup.world.passing = lambda sha: False  # CI never goes green
    run = await cup.advance(cup.start(), until="needs-you")
    assert run.status == "needs-you"
    max_visits = cup.playbook.nodes["fix"].max_visits
    assert max_visits == 3 and cup.visits("fix") == 3
    exhausted = [e["data"] for e in env.events("exhausted")]
    assert exhausted == [{"node": "fix", "max_visits": 3}]
    assert cup.step("escalate").status == "waiting"
    assert cup.codex.count("send") == 3 and cup.world.merged == []
    assert cup.codex.start_invocations == 1


@pytest.mark.parametrize("warmup", range(0, 8, 2))
@pytest.mark.parametrize(
    "point", ["before_advance", "after_advance", "after_step_update", "after_route", "after_mirror"]
)
async def test_a_crash_mid_tournament_leaves_no_duplicate_sessions_or_merges(env, respx_mock, point, warmup):
    cup = Cup(env, respx_mock, BOTH_AND_REVIEW)
    first = cup.start()
    for _ in range(warmup):
        await first.tick()
        env.clock.advance(60)
    first.crash_after = point
    with pytest.raises(SimulatedCrash):
        await cup.advance(first)

    run = await cup.advance(cup.runner())  # the restarted process: same store, adapters and GitHub
    assert run.status == "done"
    assert cup.claude.start_invocations == cup.codex.start_invocations == 1
    assert cup.claude.count("start") == cup.codex.count("start") == 1
    nodes = [s.node_id for s in env.steps(RUN_ID)]
    assert nodes.count("pick") == 1 and nodes.count("fork") == 1
    assert cup.world.merged == [CODEX_PR]
    assert cup.world.closed == [CLAUDE_PR]
    assert len(cup.world.comments[CLAUDE_PR]) == 1
    assert cup.world.count("PUT", rf"/repos/{REPO}/pulls/\d+/merge") == 1


@pytest.mark.parametrize("point", ["select_after_choose", "select_after_loser_comment"])
async def test_a_crash_inside_the_selection_reuses_the_stored_choice(env, respx_mock, point):
    cup = Cup(env, respx_mock, BOTH_AND_REVIEW)
    first = cup.start()
    first.crash_after = point
    with pytest.raises(SimulatedCrash):
        await cup.advance(first)
    assert len(cup.backend.calls) == 1

    run = await cup.advance(cup.runner())
    assert run.status == "done"
    assert len(cup.backend.calls) == 2  # the judge once, the PR review once
    assert cup.world.comments[CLAUDE_PR] == [
        f"Not selected in tournament for #7 (winner: PR #{CODEX_PR}): {RATIONALE}\n\n{MARKER}"
    ]
    assert cup.world.closed == [CLAUDE_PR] and cup.world.merged == [CODEX_PR]
    assert cup.claude.start_invocations == cup.codex.start_invocations == 1
