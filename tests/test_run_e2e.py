"""The M1.11 acceptance test: ``harness run`` drives the built-in plan-build-review playbook to ``merged``.

GitHub is a stateful respx stand-in (comments, labels, issue bodies and the pull request change as the
harness writes to them), the agent is ``FakeAdapter.happy_path()`` (PR #101) and the reviewer is a
``FakeBackend`` that approves. The CLI is driven through typer's ``CliRunner`` with the app factory
(``harness.cli.build_app``) replaced by one that injects those fakes and a fake clock, so no test sleeps.
"""

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from typer.testing import CliRunner

from harness import config as config_module
from harness.adapters.base import AdapterRegistry
from harness.adapters.fake import FakeAdapter
from harness.app import App, build_app
from harness.cli import app as cli_app
from harness.config import GlobalConfig, RepoEntry, save_global
from harness.github.client import GitHub
from harness.github.control_table import parse_table
from harness.github.ledger import is_ledger, parse_ledger
from harness.llm.backend import FakeBackend
from harness.store.db import Store
from harness.store.repo import get_run, steps_for_run
from tests.engine.helpers import FakeClock

pytestmark = pytest.mark.respx(assert_all_called=True, assert_all_mocked=True)
# Tests that stop early (errors, --max-ticks) cannot reach every route; they still fail on any unmocked request.
PARTIAL = pytest.mark.respx(assert_all_called=False, assert_all_mocked=True)

REPO = "o/r"
ISSUE = 7
CONTROL = 1
PR = 101
HEAD = "a1"
BRANCH = "harness/7-add-slugify"
MERGE_SHA = "c0ffee1" + "0" * 33
RUN_ID = f"{REPO}#{ISSUE}@1"
GATE_ID = f"{RUN_ID}/merge_gate/1/gate"
API = "/repos/o/r"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

ISSUE_BODY = """## Scope
Add `slugify()` to `slugify.py`.

## Acceptance criteria
- [ ] `slugify("Hello World")` returns `hello-world`
- [ ] tests cover punctuation and empty input

## Out of scope
- unicode transliteration
"""
CONFIG_YAML = "control_issue: 1\ndefault_agent: fake\n"
APPROVE = {"verdict": "approve", "findings": [], "correction": "", "summary": "matches the issue"}


class World:
    """GitHub as the harness sees it, plus a log of every request."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {
            CONTROL: self._issue(CONTROL, "Control", "# Control\n"),
            ISSUE: self._issue(ISSUE, "Add slugify()", ISSUE_BODY),
        }
        self.comments: dict[int, list[dict[str, Any]]] = {}
        # files under .harness/ that the repo ships, by path relative to .harness/ ("playbooks/x.yaml")
        self.repo_files: dict[str, str] = {}
        self.merged = False
        self.next_comment = 500
        self.log: list[tuple[str, str, Any]] = []

    @staticmethod
    def _issue(number: int, title: str, body: str) -> dict[str, Any]:
        return {
            "id": 9000 + number,
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }

    def issue_json(self, number: int) -> dict[str, Any]:
        issue = dict(self.issues[number])
        issue["labels"] = [{"name": n} for n in self.issues[number]["labels"]]
        return issue

    def pull_json(self) -> dict[str, Any]:
        return {
            "number": PR,
            "state": "closed" if self.merged else "open",
            "merged": self.merged,
            "mergeable": True,
            "mergeable_state": "clean",
            "merge_commit_sha": MERGE_SHA if self.merged else None,
            "head": {"sha": HEAD, "ref": BRANCH, "repo": {"full_name": REPO}},
        }

    # ---- assertions helpers ----------------------------------------------------------------------------

    def requests(self, method: str, path: str | re.Pattern[str]) -> list[Any]:
        """The bodies of the logged requests of ``method`` whose path matches."""
        found = []
        for m, p, body in self.log:
            hit = path.fullmatch(p) if isinstance(path, re.Pattern) else p == path
            if m == method and hit:
                found.append(body)
        return found

    def event_titles(self, number: int = ISSUE) -> list[str]:
        titles = []
        for comment in self.comments.get(number, []):
            if is_ledger(comment["body"]):
                continue
            match = re.match(r"\*\*\S+ · #\d+ (.*)\.\*\*", comment["body"])
            titles.append(match.group(1) if match else comment["body"])
        return titles

    def ledgers(self, number: int = ISSUE) -> list[dict[str, Any]]:
        return [c for c in self.comments.get(number, []) if is_ledger(c["body"])]


def mount(router: Any, world: World) -> None:
    """Register every GitHub endpoint the run touches on the respx router."""

    def route(method: str, regex: str) -> Callable[[Callable[..., httpx.Response]], None]:
        def register(handler: Callable[..., httpx.Response]) -> None:
            def side_effect(request: httpx.Request, **groups: str) -> httpx.Response:
                body = json.loads(request.content) if request.content else None
                world.log.append((method, unquote(request.url.path), body))
                return handler(body, **groups)

            router.route(method=method, path__regex=regex).mock(side_effect=side_effect)

        return register

    def ok(data: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=data)

    def not_found() -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    @route("GET", rf"^{API}$")
    def _repo(body: Any) -> httpx.Response:
        return ok({"full_name": REPO, "default_branch": "main"})

    @route("GET", rf"^{API}/contents/\.harness/config\.yaml$")
    def _config(body: Any) -> httpx.Response:
        return httpx.Response(200, text=CONFIG_YAML)

    @route("GET", rf"^{API}/contents/\.harness/(?P<path>(playbooks|checklists)/.+)$")
    def _repo_files(body: Any, path: str, **kw: str) -> httpx.Response:
        if path in world.repo_files:
            return httpx.Response(200, text=world.repo_files[path])
        return not_found()  # by default the repo ships nothing here: the built-ins are used

    @route("GET", rf"^{API}/issues$")
    def _issues(body: Any) -> httpx.Response:
        return ok([world.issue_json(n) for n in sorted(world.issues)])

    @route("GET", rf"^{API}/issues/(?P<n>\d+)/parent$")
    def _parent(body: Any, n: str) -> httpx.Response:
        return not_found()

    @route("GET", rf"^{API}/issues/(?P<n>\d+)/dependencies/blocked_by$")
    def _blocked(body: Any, n: str) -> httpx.Response:
        return ok([])

    @route("GET", rf"^{API}/issues/(?P<n>\d+)$")
    def _get_issue(body: Any, n: str) -> httpx.Response:
        return ok(world.issue_json(int(n)))

    @route("PATCH", rf"^{API}/issues/(?P<n>\d+)$")
    def _patch_issue(body: Any, n: str) -> httpx.Response:
        world.issues[int(n)].update(body)
        return ok(world.issue_json(int(n)))

    @route("GET", rf"^{API}/issues/(?P<n>\d+)/comments$")
    def _comments(body: Any, n: str) -> httpx.Response:
        return ok(world.comments.get(int(n), []))

    @route("POST", rf"^{API}/issues/(?P<n>\d+)/comments$")
    def _add_comment(body: Any, n: str) -> httpx.Response:
        world.next_comment += 1
        stamp = f"2026-01-01T00:{world.next_comment // 60 % 60:02d}:{world.next_comment % 60:02d}Z"
        comment = {
            "id": world.next_comment,
            "body": body["body"],
            "created_at": stamp,
            "user": {"login": "o"},
        }
        world.comments.setdefault(int(n), []).append(comment)
        return ok(comment, 201)

    @route("PATCH", rf"^{API}/issues/comments/(?P<cid>\d+)$")
    def _edit_comment(body: Any, cid: str) -> httpx.Response:
        for comments in world.comments.values():
            for comment in comments:
                if comment["id"] == int(cid):
                    comment["body"] = body["body"]
                    return ok(comment)
        return not_found()

    @route("POST", rf"^{API}/labels$")
    def _create_label(body: Any) -> httpx.Response:
        return ok({"name": body["name"]}, 201)

    @route("POST", rf"^{API}/issues/(?P<n>\d+)/labels$")
    def _add_labels(body: Any, n: str) -> httpx.Response:
        names = world.issues[int(n)]["labels"]
        names.extend(name for name in body["labels"] if name not in names)
        return ok([{"name": name} for name in names])

    @route("DELETE", rf"^{API}/issues/(?P<n>\d+)/labels/(?P<name>.+)$")
    def _remove_label(body: Any, n: str, name: str) -> httpx.Response:
        names = world.issues[int(n)]["labels"]
        if unquote(name) not in names:
            return not_found()
        names.remove(unquote(name))
        return httpx.Response(200, json=[])

    @route("GET", rf"^{API}/pulls/{PR}$")
    def _pull(body: Any) -> httpx.Response:
        return ok(world.pull_json())

    @route("GET", rf"^{API}/pulls/{PR}/files$")
    def _files(body: Any) -> httpx.Response:
        return ok(
            [
                {
                    "filename": "slugify.py",
                    "status": "added",
                    "additions": 6,
                    "deletions": 0,
                    "patch": "@@ +1 @@\n+def slugify(s): ...",
                },
                {
                    "filename": "tests/test_slugify.py",
                    "status": "added",
                    "additions": 9,
                    "deletions": 0,
                    "patch": "@@ +1 @@\n+def test(): ...",
                },
            ]
        )

    @route("PUT", rf"^{API}/pulls/{PR}/merge$")
    def _merge(body: Any) -> httpx.Response:
        world.merged = True
        return ok({"merged": True, "sha": MERGE_SHA})

    @route("GET", rf"^{API}/commits/{HEAD}/check-runs$")
    def _check_runs(body: Any) -> httpx.Response:
        run = {
            "name": "ci",
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://ci.invalid/1",
        }
        return ok({"total_count": 1, "check_runs": [run]})

    @route("GET", rf"^{API}/commits/{HEAD}/status$")
    def _status(body: Any) -> httpx.Response:
        return ok({"state": "success", "statuses": []})

    @route("DELETE", rf"^{API}/git/refs/heads/(?P<ref>.+)$")
    def _delete_branch(body: Any, ref: str) -> httpx.Response:
        return httpx.Response(204)


class Harness:
    """Everything a test needs: the fakes the CLI is wired with, and helpers to invoke it."""

    def __init__(self, tmp_path: Path, world: World) -> None:
        self.tmp_path = tmp_path
        self.world = world
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        self.adapter = FakeAdapter.happy_path()
        self.backend = FakeBackend([APPROVE, APPROVE])  # the plan review and the PR review
        self.apps: list[App] = []
        self.cli = CliRunner()
        self.cfg = GlobalConfig(data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name=REPO)])

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        await asyncio.sleep(0)

    def factory(self, cfg: GlobalConfig, **kwargs: Any) -> App:
        """Stands in for ``build_app``: a fresh GitHub client per command (each runs its own event loop)."""
        adapters = AdapterRegistry()
        adapters.register(self.adapter)
        application = build_app(
            cfg,
            gh=GitHub("test-token", write_spacing=0.0, sleep=self.sleep),
            adapters=adapters,
            review_backend=self.backend,
            clock=self.clock,
            sleep=self.sleep,
        )
        self.apps.append(application)
        return application

    def invoke(self, *args: str) -> tuple[int, str]:
        result = self.cli.invoke(cli_app, list(args))
        return result.exit_code, ANSI.sub("", result.output)

    def store(self) -> Store:
        store = Store(self.cfg.db_path)
        store.migrate()
        return store


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respx_mock: Any) -> Iterator[Harness]:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    for name in ("JULES_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    world = World()
    mount(respx_mock, world)
    harness = Harness(tmp_path, world)
    save_global(harness.cfg)
    monkeypatch.setattr("harness.cli.build_app", harness.factory)
    yield harness


def test_run_to_needs_you_then_approve_then_merged(h: Harness) -> None:
    world = h.world

    # ---- harness run: plan -> review -> implement -> CI -> scope -> PR review -> needs-you at the merge gate
    code, out = h.invoke("run", REPO, str(ISSUE))
    assert code == 0, out
    assert f"started {RUN_ID} (plan-build-review, agent fake)" in out
    assert "run_started" in out and "needs_you" in out
    assert "needs you at merge_gate: Gate 'merge' needs a decision" in out
    assert f"gate: {GATE_ID}" in out
    assert out.rstrip().endswith(f"run {RUN_ID}: needs-you")
    assert "test-token" not in out
    assert max(h.sleeps) <= 5.0  # the loop never sleeps longer than five seconds

    store = h.store()
    run = get_run(store, RUN_ID)
    assert run is not None and run.status == "needs-you" and run.context["pr"]["number"] == PR
    done = [(s.node_id, s.outcome) for s in steps_for_run(store, RUN_ID) if s.status == "done"]
    # (steps of one second sort by id, so compare without regard to order)
    assert sorted(done) == [
        ("ci", "pass"),
        ("implement", "pr_ready"),
        ("plan", "plan_ready"),
        ("plan_gate", "approved"),
        ("plan_review", "approve"),
        ("pr_review", "approve"),
        ("scope", "pass"),
    ]
    store.close()

    # GitHub so far: one ledger comment, the needs-you label, a control-table row, the PR review comment.
    assert len(world.ledgers()) == 1
    assert world.issues[ISSUE]["labels"] == ["harness:needs-you"]
    assert world.event_titles() == [
        "run started",
        "plan ready",
        "review approve",
        "PR ready",
        "review approve",
        "needs you",
    ]
    (row,) = parse_table(world.issues[CONTROL]["body"])
    assert (row.issue, row.status, row.agent, row.run_id, row.pr) == (
        ISSUE,
        "needs-you",
        "fake",
        RUN_ID,
        "#101",
    )
    assert not world.requests("PUT", f"{API}/pulls/{PR}/merge")  # nothing merged yet

    # ---- harness runs / show see it
    code, out = h.invoke("runs")
    assert code == 0 and RUN_ID in out and "needs-you" in out and "merge_gate" in out and "#101" in out
    code, out = h.invoke("runs", "--status", "done")
    assert code == 0 and RUN_ID not in out
    code, out = h.invoke("show", RUN_ID)
    assert code == 0
    assert (
        "status: needs-you" in out and "plan_review" in out and "approve" in out and "events (last 50)" in out
    )
    assert "needs_you" in out

    # ---- harness approve <issue>, then harness run continues the same run to the end
    code, out = h.invoke("approve", str(ISSUE), "--repo", REPO, "--note", "ship it")
    assert code == 0, out
    code, out = h.invoke("run", REPO, str(ISSUE))
    assert code == 0, out
    assert f"continuing {RUN_ID}" in out and "started" not in out
    assert "gate_decided" not in out  # events from before this invocation are not replayed
    assert out.rstrip().endswith(f"run {RUN_ID}: done")

    store = h.store()
    run = get_run(store, RUN_ID)
    assert run is not None and run.status == "done"
    finished = {s.node_id: s.outcome for s in steps_for_run(store, RUN_ID) if s.status == "done"}
    assert finished["merge_gate"] == "approved" and finished["merge"] == "merged"
    assert len(store.query("SELECT id FROM runs")) == 1  # continued, not restarted
    store.close()

    # ---- the GitHub writes
    # the ledger comment was created once and edited afterwards, never duplicated
    (ledger,) = world.ledgers()
    posts = [b for b in world.requests("POST", f"{API}/issues/{ISSUE}/comments") if is_ledger(b["body"])]
    edits = world.requests("PATCH", re.compile(rf"{API}/issues/comments/\d+"))
    assert len(posts) == 1 and len(edits) >= 3
    snapshot = parse_ledger(ledger["body"])
    assert snapshot is not None and snapshot["run"]["status"] == "done" and snapshot["pr"]["number"] == PR

    # labels: running at the start, needs-you at the gate, running again, done at the end
    adds = [b["labels"] for b in world.requests("POST", f"{API}/issues/{ISSUE}/labels")]
    assert adds == [["harness:running"], ["harness:needs-you"], ["harness:running"], ["harness:done"]]
    assert world.issues[ISSUE]["labels"] == ["harness:done"]

    # one event comment per meaningful event, none for plain polls
    assert world.event_titles() == [
        "run started",
        "plan ready",
        "review approve",
        "PR ready",
        "review approve",
        "needs you",
        "merged",
        "done",
    ]

    # the PR review verdict was posted on the pull request, once
    pr_comments = world.comments[PR]
    assert len(pr_comments) == 1 and "Harness review: approve" in pr_comments[0]["body"]

    # merge with the reviewed sha, then branch delete, ticked boxes and the closed issue
    assert world.requests("PUT", f"{API}/pulls/{PR}/merge") == [{"merge_method": "squash", "sha": HEAD}]
    assert world.requests("DELETE", f"{API}/git/refs/heads/{BRANCH}") == [None]
    body_patches = [b["body"] for b in world.requests("PATCH", f"{API}/issues/{ISSUE}") if "body" in b]
    assert body_patches and "- [ ]" not in body_patches[-1] and body_patches[-1].count("- [x]") == 2
    assert "## Out of scope\n- unicode transliteration" in body_patches[-1]  # untouched
    assert {"state": "closed"} in world.requests("PATCH", f"{API}/issues/{ISSUE}")
    assert world.issues[ISSUE]["state"] == "closed"

    # the control table says done, with the PR
    (row,) = parse_table(world.issues[CONTROL]["body"])
    assert (row.status, row.pr) == ("done", "#101")

    # the fakes saw exactly one session and one plan approval, and both reviews
    assert [c[0] for c in h.adapter.calls].count("start") == 1
    assert [c[0] for c in h.adapter.calls].count("approve_plan") == 1
    assert len(h.backend.calls) == 2

    # a finished issue is not started again
    code, out = h.invoke("run", REPO, str(ISSUE))
    assert code == 1 and "issue #7 is closed" in out


@PARTIAL
def test_run_without_an_available_agent_fails_before_starting(h: Harness) -> None:
    code, out = h.invoke("run", REPO, str(ISSUE), "--agent", "jules")
    assert code == 1
    assert "agent 'jules' is not available" in out and "known: fake" in out
    assert "Traceback" not in out
    store = h.store()
    assert store.query("SELECT id FROM runs") == []
    store.close()
    assert not h.world.requests("POST", f"{API}/issues/{ISSUE}/comments")  # nothing was written to GitHub


@PARTIAL
def test_run_with_an_unknown_playbook_fails_with_one_line(h: Harness) -> None:
    code, out = h.invoke("run", REPO, str(ISSUE), "--playbook", "no-such-playbook")
    assert code == 1
    assert "unknown playbook 'no-such-playbook'" in out
    assert len([line for line in out.splitlines() if line.startswith("error:")]) == 1


@PARTIAL
def test_run_for_an_unknown_issue_fails(h: Harness) -> None:
    code, out = h.invoke("run", REPO, "99")
    assert code == 1 and "issue #99 not found" in out


@PARTIAL
def test_the_playbook_and_agent_come_from_the_issue_labels(h: Harness) -> None:
    h.world.issues[ISSUE]["labels"] = ["playbook:no-such-playbook", "agent:nobody"]
    code, out = h.invoke("run", REPO, str(ISSUE))
    assert code == 1 and "unknown playbook 'no-such-playbook'" in out  # the label won over the repo default
    h.world.issues[ISSUE]["labels"] = ["agent:nobody"]
    code, out = h.invoke("run", REPO, str(ISSUE))
    assert code == 1 and "agent 'nobody' is not available" in out  # and over default_agent: fake
    code, out = h.invoke("run", REPO, str(ISSUE), "--agent", "fake", "--max-ticks", "1")
    assert (
        code == 0 and f"started {RUN_ID} (plan-build-review, agent fake)" in out
    )  # the flag won over the label


@PARTIAL
def test_max_ticks_stops_a_running_run_which_the_next_run_continues(h: Harness) -> None:
    code, out = h.invoke("run", REPO, str(ISSUE), "--max-ticks", "2")
    assert code == 0, out
    assert "stopped after 2 ticks" in out and out.rstrip().endswith(f"run {RUN_ID}: running")
    code, out = h.invoke("run", REPO, str(ISSUE), "--max-ticks", "1")
    assert code == 0 and f"continuing {RUN_ID}" in out


FAILING_PLAYBOOK = """\
apiVersion: harness/v1
id: boom
nodes:
  c: {kind: check, check: command, command: "true"}
edges:
  c.pass: "@done"
  c.fail: "@failed"
"""


@PARTIAL
def test_a_repo_playbook_is_used_and_a_failed_run_exits_1(h: Harness) -> None:
    h.world.repo_files["playbooks/boom.yaml"] = FAILING_PLAYBOOK
    code, out = h.invoke("run", REPO, str(ISSUE), "--playbook", "boom")
    assert code == 1, out
    assert f"started {RUN_ID} (boom, agent fake)" in out
    assert "run_failed" in out and out.rstrip().endswith(f"run {RUN_ID}: failed")
    assert h.world.issues[ISSUE]["labels"] == ["harness:failed"]
    assert "failed" in h.world.event_titles()
    code, out = h.invoke("runs", "--status", "failed")
    assert code == 0 and RUN_ID in out and "boom" in out


@PARTIAL
def test_an_invalid_repo_playbook_is_one_error_line(h: Harness) -> None:
    h.world.repo_files["playbooks/bad.yaml"] = (
        "apiVersion: harness/v1\nid: bad\nnodes:\n  c: {kind: check, check: ci}\nedges:\n  c.pass: nowhere\n"
    )
    h.world.repo_files["playbooks/empty.yaml"] = "apiVersion: harness/v1\nid: empty\nnodes: {}\nedges: {}\n"
    h.world.repo_files["playbooks/decomposing.yaml"] = (
        "apiVersion: harness/v1\nid: decomposing\nnodes:\n  d: {kind: decompose}\nedges:\n  '*.created': '@done'\n  '*.failed': '@failed'\n"
    )
    code, out = h.invoke("run", REPO, str(ISSUE), "--playbook", "bad")
    assert code == 1 and out.count("error:") == 1 and "playbook bad is invalid" in out and "bad_target" in out
    code, out = h.invoke("run", REPO, str(ISSUE), "--playbook", "empty")
    assert code == 1 and "playbook empty has no nodes" in out
    code, out = h.invoke("run", REPO, str(ISSUE), "--playbook", "decomposing")
    assert code == 1 and "without an executor: decompose" in out
    code, out = h.invoke("run", REPO, str(ISSUE), "--playbook", "../etc/passwd")
    assert code == 1 and "invalid playbook id" in out


@PARTIAL
def test_an_escalated_agent_failure_waits_for_a_human_and_a_cancelled_run_is_terminal(h: Harness) -> None:
    h.adapter = FakeAdapter.fails("agent exploded")
    h.backend = FakeBackend([])
    code, out = h.invoke("run", REPO, str(ISSUE))
    # the playbook escalates a failed agent, so the run waits for a human instead of failing
    assert code == 0 and "needs you at escalate" in out and out.rstrip().endswith(f"run {RUN_ID}: needs-you")
    code, out = h.invoke("cancel", RUN_ID)
    assert code == 0 and f"cancelled: {RUN_ID}" in out
    code, out = h.invoke("run", REPO, str(ISSUE), "--max-ticks", "1")
    assert code == 0 and "started o/r#7@2" in out  # a cancelled run is terminal: a new run starts


@PARTIAL
def test_runs_and_show_for_unknown_things(h: Harness) -> None:
    code, out = h.invoke("run", REPO, str(ISSUE), "--max-ticks", "1")
    assert code == 0
    code, out = h.invoke("show", "o/r#99@1")
    assert code == 1 and "run o/r#99@1 not found" in out
    code, out = h.invoke("runs", "--repo", "x/y")
    assert code == 0 and RUN_ID not in out
    code, out = h.invoke("runs", "--repo", REPO, "--status", "running")
    assert code == 0 and RUN_ID in out


def test_a_slash_command_approval_under_the_daemon_finishes_the_run(h: Harness) -> None:
    """The second procedure of the runbook: ``harness run`` stops at the gate, the daemon takes the comment."""
    code, out = h.invoke("run", REPO, str(ISSUE))
    assert code == 0 and out.rstrip().endswith(f"run {RUN_ID}: needs-you")
    # A later comment by the repo owner (an owner may command when command_users is empty).
    h.world.comments[ISSUE].append(
        {
            "id": 900,
            "body": "/harness approve ship it",
            "created_at": "2099-01-01T00:00:00Z",
            "user": {"login": "o"},
        }
    )

    application = h.factory(h.cfg)

    async def drive_daemon() -> None:
        from harness.daemon import run_daemon

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_daemon(
                application,
                stop,
                host="test-host",
                pid=1,
                sync_every=3600,
                commands_every=0.005,
                heartbeat_every=3600,
                idle=0.005,
            )
        )
        try:
            for _ in range(600):
                h.clock.advance(20)  # the fake clock only moves when we say so
                if h.world.event_titles()[-1:] == ["done"]:
                    break
                await asyncio.sleep(0.005)
        finally:
            stop.set()
            await task

    asyncio.run(drive_daemon())

    world = h.world
    assert world.event_titles()[-3:] == ["command applied", "merged", "done"]
    assert world.requests("PUT", f"{API}/pulls/{PR}/merge") == [{"merge_method": "squash", "sha": HEAD}]
    assert world.issues[ISSUE]["state"] == "closed" and world.issues[ISSUE]["labels"] == ["harness:done"]
    assert "Approved by @o: ship it" in world.comments[ISSUE][-3]["body"]
    # the daemon held the run lock on the control issue while it ran, and gave it back
    (lock_ledger,) = world.ledgers(CONTROL)
    assert "lock" not in (parse_ledger(lock_ledger["body"]) or {})
    assert len(world.requests("POST", f"{API}/issues/{CONTROL}/comments")) == 1
    store = h.store()
    run = get_run(store, RUN_ID)
    assert run is not None and run.status == "done"
    assert [r["kind"] for r in store.query("SELECT kind FROM events WHERE kind LIKE 'daemon%'")] == [
        "daemon_started",
        "daemon_stopped",
    ]
    store.close()
