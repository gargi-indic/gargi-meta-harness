"""Join ``select`` (M3.8): judge over candidate PRs, close the losers, continue on the winner, resume after a crash."""

import json
import re
from typing import Any

import httpx
import pytest

from harness.adapters.base import AgentSnapshot, AgentState, PrRef
from harness.config import MergeConfig, RepoConfig
from harness.engine import Done, ExecutorRegistry, SimulatedCrash
from harness.llm.backend import FakeBackend
from tests.engine.helpers import (
    REPO,
    CountingAdapter,
    Scripted,
    StubAgentExecutor,
    drive,
    make_issue,
    make_playbook,
)

pytestmark = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)

RUN_ID = f"{REPO}#7@1"
JOIN_STEP = f"{RUN_ID}/join/1"
MARKER = f"<!-- harness:select run={RUN_ID} step={JOIN_STEP} -->"
RATIONALE = "b has tests and the smaller diff"
JUDGE_B = {
    "winner": "b",
    "ranking": ["b", "a"],
    "rationale": RATIONALE,
    "per_candidate": {"a": ["no tests"], "b": ["tests present"]},
}
LOSER_BODY = f"Not selected in tournament for #7 (winner: PR #102): {RATIONALE}\n\n{MARKER}"
WINNER_BODY = f"Selected in tournament for #7: {RATIONALE}\n\n{MARKER}"


class GitHubFake:
    """Stateful GitHub API behind respx: pulls, files, CI, comments, close and branch deletion."""

    def __init__(self, respx_mock, prs: dict[int, dict[str, Any]]) -> None:
        self.prs = prs
        self.state = {n: "open" for n in prs}
        self.comments: dict[int, list[str]] = {n: [] for n in prs}
        self.closed: list[int] = []
        self.deleted: list[str] = []
        self.requests: list[tuple[str, str]] = []
        respx_mock.route().mock(side_effect=self._handle)

    def _pull(self, n: int) -> dict[str, Any]:
        pr = self.prs[n]
        return {
            "number": n,
            "state": self.state[n],
            "title": f"PR {n}",
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "head": {"sha": f"sha{n}", "ref": f"agent-{n}", "repo": {"full_name": REPO}},
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        base = f"/repos/{REPO}"
        if m := re.fullmatch(rf"{base}/pulls/(\d+)", path):
            n = int(m[1])
            if method == "PATCH":
                assert json.loads(request.content) == {"state": "closed"}
                self.state[n] = "closed"
                self.closed.append(n)
            return httpx.Response(200, json=self._pull(n))
        if m := re.fullmatch(rf"{base}/pulls/(\d+)/files", path):
            pr = self.prs[int(m[1])]
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "a.py",
                        "status": "added",
                        "additions": pr["additions"],
                        "deletions": pr["deletions"],
                        "patch": f"+ change from PR {m[1]}",
                    }
                ],
            )
        if m := re.fullmatch(rf"{base}/commits/sha(\d+)/check-runs", path):
            conclusion = "success" if self.prs[int(m[1])]["ci"] else "failure"
            return httpx.Response(
                200, json={"check_runs": [{"name": "ci", "status": "completed", "conclusion": conclusion}]}
            )
        if re.fullmatch(rf"{base}/commits/sha\d+/status", path):
            return httpx.Response(200, json={"state": "pending", "statuses": []})
        if m := re.fullmatch(rf"{base}/issues/(\d+)/comments", path):
            n = int(m[1])
            if method == "POST":
                self.comments[n].append(json.loads(request.content)["body"])
                return httpx.Response(201, json={"id": 1})
            return httpx.Response(200, json=[{"body": b} for b in self.comments[n]])
        if method == "DELETE" and (m := re.fullmatch(rf"{base}/git/refs/heads/(.+)", path)):
            self.deleted.append(m[1])
            return httpx.Response(204)
        raise AssertionError(f"unmocked GitHub call: {method} {path}")


def _prs(**overrides: dict[str, Any]) -> dict[int, dict[str, Any]]:
    prs = {
        101: {"additions": 40, "deletions": 5, "ci": True},
        102: {"additions": 10, "deletions": 2, "ci": True},
    }
    for key, value in overrides.items():
        prs[int(key.removeprefix("n"))].update(value)
    return prs


def _agent(session: str) -> dict:
    return {"kind": "agent", "session": session, "action": "implement", "agent": "fake"}


def _pr(number: int) -> PrRef:
    return PrRef(number=number, url=f"https://example.invalid/pull/{number}", head_sha=f"sha{number}")


def _phases(number: int | None) -> list[list[AgentSnapshot]]:
    end = (
        AgentSnapshot(state=AgentState.FAILED, error="boom")
        if number is None
        else AgentSnapshot(state=AgentState.PR_READY, pr=_pr(number))
    )
    return [
        [AgentSnapshot(state=AgentState.PLAN_READY, plan="p")],
        [AgentSnapshot(state=AgentState.WORKING), end],
    ]


def _playbook(rubric: list[str] | None = None):
    join: dict[str, Any] = {"kind": "join", "mode": "select"}
    if rubric:
        join["rubric"] = rubric
    return make_playbook(
        {
            "fork": {"kind": "parallel", "branches": ["a", "b"]},
            "a": _agent("sa"),
            "b": _agent("sb"),
            "join": join,
            "ci": {"kind": "check", "check": "command", "command": "true"},
        },
        {
            "a.pr_ready": "join",
            "a.failed": "join",
            "b.pr_ready": "join",
            "b.failed": "join",
            "join.done": "ci",
            "join.failed": "@failed",
            "ci.pass": "@done",
            "ci.fail": "@failed",
        },
    )


class Probe:
    """Stands in for the ``ci`` node: records the PR the run context carries when it runs."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def __call__(self, step: Any, ctx: Any) -> Done:
        self.seen.append(ctx.run.context["pr"])
        return Done("pass")


def _setup(env, backend: FakeBackend, a: int | None = 101, b: int | None = 102, probe: Probe | None = None):
    adapter = CountingAdapter(script={f"{RUN_ID}/sa": _phases(a), f"{RUN_ID}/sb": _phases(b)})
    env.adapters.register(adapter)
    return adapter, _runner(env, backend, probe or Probe())


def _runner(env, backend: FakeBackend, probe: Probe, **kwargs: Any):
    registry = ExecutorRegistry(select_backend=backend)
    registry.register("agent", StubAgentExecutor())
    registry.register("check", Scripted({"ci": [probe]}))
    return env.runner(executors=registry, **kwargs)


async def test_judge_picks_the_winner_closes_the_loser_and_the_path_continues_on_the_winner(env, respx_mock):
    gh = GitHubFake(respx_mock, _prs())
    backend = FakeBackend([JUDGE_B])
    probe = Probe()
    _, runner = _setup(env, backend, probe=probe)
    runner.start_run(REPO, make_issue(), _playbook(["prefers tests"]), agent="fake")
    run = await drive(runner, env, RUN_ID)

    assert run.status == "done"
    join = next(s for s in env.steps(RUN_ID) if s.node_id == "join")
    assert join.outcome == "done"
    assert join.output["winner"] == "b" and join.output["winner_pr"] == 102
    assert join.output["losers"] == [{"node": "a", "pr": 101, "branch": "agent-101"}]
    assert join.output["pr"]["number"] == 102
    # The following node operates on the winner's PR via the unchanged run-context key.
    assert [p["number"] for p in probe.seen] == [102] and run.context["pr"]["number"] == 102
    # The loser is commented on (exact payload) and closed; the winner gets the rationale and stays open.
    assert gh.comments[101] == [LOSER_BODY]
    assert gh.comments[102] == [WINNER_BODY]
    assert gh.closed == [101] and gh.state[102] == "open"
    assert gh.deleted == ["agent-101"]
    # One judge call, with the rubric and both diffs in the prompt.
    assert len(backend.calls) == 1
    assert "prefers tests" in backend.calls[0]["prompt"]
    assert (
        "change from PR 101" in backend.calls[0]["prompt"]
        and "change from PR 102" in backend.calls[0]["prompt"]
    )


async def test_loser_branch_is_kept_when_delete_branch_is_off(env, respx_mock):
    gh = GitHubFake(respx_mock, _prs())
    adapter = CountingAdapter(script={f"{RUN_ID}/sa": _phases(101), f"{RUN_ID}/sb": _phases(102)})
    env.adapters.register(adapter)
    runner = _runner(
        env,
        FakeBackend([JUDGE_B]),
        Probe(),
        repo_configs={REPO: RepoConfig(merge=MergeConfig(delete_branch=False))},
    )
    runner.start_run(REPO, make_issue(), _playbook(), agent="fake")
    assert (await drive(runner, env, RUN_ID)).status == "done"
    assert gh.closed == [101] and gh.deleted == []


async def test_invalid_winner_falls_back_to_passing_ci_then_smaller_diff(env, respx_mock):
    gh = GitHubFake(respx_mock, _prs(n102={"ci": False}))
    backend = FakeBackend([{"winner": "nobody", "rationale": "?"}])
    _, runner = _setup(env, backend)
    runner.start_run(REPO, make_issue(), _playbook(), agent="fake")
    run = await drive(runner, env, RUN_ID)
    join = next(s for s in env.steps(RUN_ID) if s.node_id == "join")
    assert run.status == "done" and join.output["winner"] == "a"  # a has passing CI, b does not
    assert gh.closed == [102]
    assert "fallback" in gh.comments[102][0]


async def test_one_candidate_wins_without_a_model_call_and_a_failed_branch_is_excluded(env, respx_mock):
    gh = GitHubFake(respx_mock, _prs())
    backend = FakeBackend([])
    _, runner = _setup(env, backend, a=None, b=102)
    runner.start_run(REPO, make_issue(), _playbook(), agent="fake")
    run = await drive(runner, env, RUN_ID)
    join = next(s for s in env.steps(RUN_ID) if s.node_id == "join")
    assert run.status == "done"
    assert join.output["winner"] == "b" and join.output["losers"] == [] and join.output["failed"] == ["a"]
    assert backend.calls == []
    assert gh.closed == [] and all(not c for c in gh.comments.values())
    assert run.context["pr"]["number"] == 102


async def test_no_candidate_fails_the_join(env, respx_mock):
    gh = GitHubFake(respx_mock, _prs())
    backend = FakeBackend([])
    _, runner = _setup(env, backend, a=None, b=None)
    runner.start_run(REPO, make_issue(), _playbook(), agent="fake")
    run = await drive(runner, env, RUN_ID)
    join = next(s for s in env.steps(RUN_ID) if s.node_id == "join")
    assert run.status == "failed" and join.outcome == "failed"
    assert sorted(join.output["failed"]) == ["a", "b"]
    assert backend.calls == [] and gh.requests == []


async def test_several_candidates_without_a_backend_fail_the_step_with_a_clear_error(env, respx_mock):
    GitHubFake(respx_mock, _prs())
    adapter = CountingAdapter(script={f"{RUN_ID}/sa": _phases(101), f"{RUN_ID}/sb": _phases(102)})
    env.adapters.register(adapter)
    registry = ExecutorRegistry()  # no select backend
    registry.register("agent", StubAgentExecutor())
    registry.register("check", Scripted({"ci": [Probe()]}))
    runner = env.runner(executors=registry)
    runner.start_run(REPO, make_issue(), _playbook(), agent="fake")
    run = await drive(runner, env, RUN_ID)
    assert run.status == "failed"
    assert "selector backend" in env.events("executor_error")[0]["data"]["error"]


@pytest.mark.parametrize("point", ["select_after_choose", "select_after_loser_comment"])
async def test_crash_after_selection_resumes_without_a_second_judge_call_comment_or_close(
    env, respx_mock, point
):
    gh = GitHubFake(respx_mock, _prs())
    backend = FakeBackend([JUDGE_B])
    probe = Probe()
    adapter, first = _setup(env, backend, probe=probe)
    first.start_run(REPO, make_issue(), _playbook(), agent="fake")
    first.crash_after = point
    with pytest.raises(SimulatedCrash):
        await drive(first, env, RUN_ID)
    assert len(backend.calls) == 1

    second = _runner(env, backend, probe)  # the restarted process: same store, same adapter, same GitHub
    run = await drive(second, env, RUN_ID)

    assert run.status == "done"
    assert len(backend.calls) == 1  # the stored selection was reused
    assert gh.comments[101] == [LOSER_BODY]  # exactly one comment on the loser
    assert gh.comments[102] == [WINNER_BODY]
    assert gh.closed == [101]  # exactly one close
    assert [p["number"] for p in probe.seen] == [102]
    assert adapter.start_invocations == 2
