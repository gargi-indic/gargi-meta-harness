"""Shared builders for the planner API tests (M4.5): a stateful GitHub stand-in and a service/app rig.

``Hub`` answers the REST calls that the plan loader, the plan gate, the proposal store and ``materialize`` make,
on a respx router, so the real ``GitHub`` client, outbox and proposal store run against it. It models what the
tests rely on: comments that can be edited, labels that must exist, ids that differ from numbers, 422 for a
duplicate link, and writes that can be made to fail.
"""

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from harness.adapters.base import Capabilities
from harness.github.client import GitHub
from harness.github.ledger import find_ledger, parse_ledger, render_ledger
from harness.llm.backend import FakeBackend
from harness.planner.proposal import ProposedPlan
from harness.server import create_app
from harness.server.planner_service import PlannerService
from harness.store.db import Store
from harness.store.repo import upsert_repo
from tests.server.helpers import AUTH, PORT, TOKEN, make_client, make_config, make_store

REPO = "acme/repo0"
CONTROL = 1
PLAN_PATH = "docs/PLAN.md"
PLAN_TEXT = "# The plan\n\nBuild a slugifier.\n\n## Steps\n\n- one\n"
CONTROL_BODY = "Control issue.\n\nIntro text.\n\nOutro text.\n"
JULES = Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=False)
CODEX = Capabilities(plan_approval=False, messaging=False, opens_pr=False, local_workspace=True)


async def no_sleep(_: float) -> None:
    return None


@dataclass
class Hub:
    router: respx.MockRouter
    files: dict[str, tuple[str, int]] = field(default_factory=dict)  # path -> (text, version)
    repo_playbooks: dict[str, str] = field(default_factory=dict)  # file name -> YAML
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    sub_issues: dict[int, list[int]] = field(default_factory=dict)
    blocked_by: dict[int, list[int]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    down: set[tuple[str, str]] = field(default_factory=set)  # (method, path suffix) answered with a 500
    _next_issue: int = 1
    _next_comment: int = 5000

    def __post_init__(self) -> None:
        self._add_issue("Control", CONTROL_BODY, [])

    # ---- test-side helpers -------------------------------------------------------------------------------

    def _add_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        number = self._next_issue
        self._next_issue += 1
        issue = {
            "number": number,
            "id": 7000 + number * 3,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": n} for n in labels],
        }
        self.issues[number] = issue
        return issue

    def put_plan(self, text: str = PLAN_TEXT, *, path: str = PLAN_PATH, version: int = 1) -> str:
        self.files[path] = (text, version)
        return self.plan_sha(path)

    def plan_sha(self, path: str = PLAN_PATH) -> str:
        return f"blob{self.files[path][1]:03d}".ljust(40, "0")

    def approve_plan(self, path: str = PLAN_PATH, by: str = "acme", number: int = CONTROL) -> None:
        """Write the plan approval record into an issue's ledger, as ``harness plan approve`` does."""
        record = {
            "kind": "file",
            "path": path,
            "issue": None,
            "ref": None,
            "plan_sha": self.plan_sha(path),
            "title": "The plan",
            "approved_by": by,
            "via": "cli",
            "approved_at": "2026-10-20T10:00:00Z",
            "label_cursor": 0,
        }
        body = render_ledger({"plan": record}, "Harness control ledger.")
        ledger = find_ledger(self.comments.get(number, []))
        if ledger is None:
            self.add_comment(number, body)
        else:
            ledger["body"] = body

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        self._next_comment += 1
        comment = {"id": self._next_comment, "body": body, "created_at": "2026-10-20T10:00:00Z"}
        self.comments.setdefault(number, []).append(comment)
        return comment

    def ledger(self, number: int = CONTROL) -> dict[str, Any] | None:
        comment = find_ledger(self.comments.get(number, []))
        return parse_ledger(comment["body"]) if comment else None

    def proposal_comments(self) -> list[dict[str, Any]]:
        return [
            c
            for c in self.comments.get(CONTROL, [])
            if c["body"].lstrip().startswith("<!-- harness:proposal")
        ]

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p.endswith(suffix))

    def created(self) -> list[dict[str, Any]]:
        """The issues made through the API, oldest first (the control issue is not among them)."""
        return [i for n, i in sorted(self.issues.items()) if n != CONTROL]

    # ---- routes ------------------------------------------------------------------------------------------

    def install(self) -> "Hub":
        base = rf"^/repos/{REPO}"

        def route(method: str, pattern: str, handler: Any) -> None:
            def wrapped(request: httpx.Request, **groups: str) -> httpx.Response:
                path = request.url.path
                self.calls.append((request.method, path))
                if request.method != "GET" and any(
                    request.method == m and path.endswith(suffix) for m, suffix in self.down
                ):
                    return httpx.Response(500, json={"message": "boom"})
                return handler(request, **groups)

            self.router.route(method=method, path__regex=pattern).mock(side_effect=wrapped)

        route("GET", base + r"/contents/(?P<path>.+)$", self._get_contents)
        route("GET", base + r"/commits$", lambda request: httpx.Response(200, json=[]))
        route(
            "GET",
            base + r"/issues/(?P<number>\d+)/events$",
            lambda request, number: httpx.Response(200, json=[]),
        )
        route("GET", base + r"/issues$", self._list_issues)
        route("POST", base + r"/issues$", self._create_issue)
        route("GET", base + r"/issues/(?P<number>\d+)$", self._get_issue)
        route("PATCH", base + r"/issues/(?P<number>\d+)$", self._patch_issue)
        route("GET", base + r"/issues/(?P<number>\d+)/comments$", self._list_comments)
        route("POST", base + r"/issues/(?P<number>\d+)/comments$", self._add_comment)
        route("PATCH", base + r"/issues/comments/(?P<comment_id>\d+)$", self._edit_comment)
        route("GET", base + r"/labels$", lambda request: httpx.Response(200, json=list(self.labels.values())))
        route("POST", base + r"/labels$", self._create_label)
        route("GET", base + r"/issues/(?P<number>\d+)/sub_issues$", self._list_sub_issues)
        route("POST", base + r"/issues/(?P<number>\d+)/sub_issues$", self._add_sub_issue)
        route("GET", base + r"/issues/(?P<number>\d+)/dependencies/blocked_by$", self._list_blocked_by)
        route("POST", base + r"/issues/(?P<number>\d+)/dependencies/blocked_by$", self._add_blocked_by)
        return self

    def _get_contents(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == ".harness/playbooks":
            if not self.repo_playbooks:
                return httpx.Response(404, json={"message": "Not Found"})
            entries = [
                {"type": "file", "path": f".harness/playbooks/{n}"} for n in sorted(self.repo_playbooks)
            ]
            return httpx.Response(200, json=entries)
        if path.startswith(".harness/playbooks/"):
            text = self.repo_playbooks.get(path.rsplit("/", 1)[1])
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, text=text)
        if path not in self.files:
            return httpx.Response(404, json={"message": "Not Found"})
        text = self.files[path][0]
        if "raw" in request.headers.get("accept", ""):
            return httpx.Response(200, text=text)
        raw = text.encode()
        body = {
            "type": "file",
            "path": path,
            "sha": self.plan_sha(path),
            "size": len(raw),
            "encoding": "base64",
            "content": base64.encodebytes(raw).decode(),
        }
        return httpx.Response(200, json=body)

    def _list_issues(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        wanted = [x for x in params.get("labels", "").split(",") if x]
        state = params.get("state", "open")
        found = [
            i
            for i in self.issues.values()
            if all(w in [lab["name"] for lab in i["labels"]] for w in wanted)
            and (state == "all" or i["state"] == state)
        ]
        return httpx.Response(200, json=found)

    def _create_issue(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        known = {n.lower() for n in self.labels}
        unknown = [n for n in data.get("labels", []) if n.lower() not in known]
        if unknown:
            return httpx.Response(422, json={"message": f"labels do not exist: {unknown}"})
        issue = self._add_issue(data["title"], data.get("body", ""), data.get("labels", []))
        return httpx.Response(201, json=issue)

    def _get_issue(self, request: httpx.Request, number: str) -> httpx.Response:
        issue = self.issues.get(int(number))
        if issue is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=issue)

    def _patch_issue(self, request: httpx.Request, number: str) -> httpx.Response:
        issue = self.issues[int(number)]
        issue.update(json.loads(request.content))
        return httpx.Response(200, json=issue)

    def _list_comments(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self.comments.get(int(number), []))

    def _add_comment(self, request: httpx.Request, number: str) -> httpx.Response:
        comment = self.add_comment(int(number), json.loads(request.content)["body"])
        return httpx.Response(201, json=comment)

    def _edit_comment(self, request: httpx.Request, comment_id: str) -> httpx.Response:
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == int(comment_id):
                    comment["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if data["name"].lower() in {n.lower() for n in self.labels}:
            return httpx.Response(422, json={"message": "Validation Failed", "errors": ["already_exists"]})
        self.labels[data["name"]] = data
        return httpx.Response(201, json=data)

    def _by_id(self, ids: list[int]) -> list[dict[str, Any]]:
        return [i for i in self.issues.values() if i["id"] in ids]

    def _list_sub_issues(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self._by_id(self.sub_issues.get(int(number), [])))

    def _add_sub_issue(self, request: httpx.Request, number: str) -> httpx.Response:
        child = json.loads(request.content)["sub_issue_id"]
        if child in self.sub_issues.setdefault(int(number), []) or not self._by_id([child]):
            return httpx.Response(422, json={"message": "duplicate or unknown sub-issue"})
        self.sub_issues[int(number)].append(child)
        return httpx.Response(201, json=self.issues[int(number)])

    def _list_blocked_by(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self._by_id(self.blocked_by.get(int(number), [])))

    def _add_blocked_by(self, request: httpx.Request, number: str) -> httpx.Response:
        blocker = json.loads(request.content)["issue_id"]
        if blocker in self.blocked_by.setdefault(int(number), []) or not self._by_id([blocker]):
            return httpx.Response(422, json={"message": "duplicate or unknown dependency"})
        self.blocked_by[int(number)].append(blocker)
        return httpx.Response(201, json=self.issues[int(number)])


# ---- proposals ------------------------------------------------------------------------------------------


def good_issue(key: str, *deps: str, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "key": key,
        "title": f"Do {key}",
        "body": f"## Context\nWhy {key}.\n\n## Scope\nWhat {key}.\n\n## Acceptance criteria\n- {key} works",
        "acceptance": [f"{key} works"],
        "blocked_by": list(deps),
        "agent": "jules",
        "playbook": "plan-build-review",
    }
    data.update(over)
    return data


def good_plan(*keys: str, **over: Any) -> dict[str, Any]:
    """A valid proposal as the decomposer's JSON: each key blocked by the one before it."""
    issues = [good_issue(k, *keys[n - 1 : n]) for n, k in enumerate(keys)]
    return {"epic_title": "The epic", "epic_body": "Epic body.", "issues": issues, "notes": "", **over}


def proposed(*keys: str) -> ProposedPlan:
    return ProposedPlan.model_validate(good_plan(*keys))


# ---- the rig --------------------------------------------------------------------------------------------


class GatedBackend(FakeBackend):
    """A fake model that answers only after ``release()``, so a test can look at the running job."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.gate = asyncio.Event()

    def release(self) -> None:
        self.gate.set()

    async def review(self, system: str, prompt: str, schema: dict, workdir: Any) -> dict:
        await self.gate.wait()
        return await super().review(system, prompt, schema, workdir)


@dataclass
class Rig:
    hub: Hub
    store: Store
    gh: GitHub
    service: PlannerService
    app: Any
    client: httpx.AsyncClient
    backend: Any

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        return await self.client.post(path, **kw)

    async def decompose(self, **body: Any) -> httpx.Response:
        return await self.client.post(
            "/api/planner/decompose", json={"repo": REPO, "path": PLAN_PATH, **body}
        )

    async def settle(self) -> None:
        await self.service.idle()

    async def drafted(self, *, gate: bool = False) -> str:
        """Decompose and wait for the draft; returns the proposal id."""
        response = await self.decompose()
        assert response.status_code == 202, response.text
        if gate:
            self.backend.release()
        await self.settle()
        return response.json()["proposal_id"]

    async def proposal(self, proposal_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/planner/proposals/{proposal_id}")
        assert response.status_code == 200, response.text
        return response.json()

    async def put(self, proposal_id: str, plan: dict[str, Any], **headers: str) -> httpx.Response:
        return await self.client.put(f"/api/planner/proposals/{proposal_id}", json=plan, headers=headers)

    async def validate(self, proposal_id: str) -> dict[str, Any]:
        response = await self.client.post(f"/api/planner/proposals/{proposal_id}/validate")
        assert response.status_code == 200, response.text
        return response.json()

    async def approve(self, proposal_id: str, **body: Any) -> httpx.Response:
        return await self.client.post(f"/api/planner/proposals/{proposal_id}/approve", json=body)

    async def job(self, job_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/planner/jobs/{job_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def events(self) -> list[dict[str, Any]]:
        rows = self.store.query(
            "SELECT kind, data_json FROM events WHERE kind = 'planner_updated' ORDER BY id"
        )
        return [json.loads(r["data_json"]) for r in rows]

    async def aclose(self) -> None:
        await self.service.aclose()
        await self.client.aclose()
        await self.gh.aclose()


def make_rig(
    router: respx.MockRouter,
    *,
    hub: Hub | None = None,
    store: Store | None = None,
    responses: list[Any] | None = None,
    gated: bool = False,
    control_issue: int | None = CONTROL,
    agent_caps: dict[str, Capabilities] | None = None,
    **service_kwargs: Any,
) -> Rig:
    if hub is None:
        hub = Hub(router).install()
        hub.put_plan()
    store = store or make_store()
    upsert_repo(
        store,
        REPO,
        "main",
        {"control_issue": control_issue, "agents": {"jules": {}, "codex": {"enabled": False}}},
    )
    gh = GitHub("t", write_spacing=0.0, sleep=no_sleep)
    backend: Any = (GatedBackend if gated else FakeBackend)(
        responses if responses is not None else [good_plan("a", "b")]
    )
    service = PlannerService(
        store=store,
        gh=gh,
        config=make_config(),
        backend=backend,
        agent_caps=agent_caps if agent_caps is not None else {"jules": JULES, "codex": CODEX},
        sleep=no_sleep,
        playbook_ttl_s=0.0,
        **service_kwargs,
    )
    app = create_app(
        store=store, config=make_config(), token=TOKEN, port=PORT, tail_events=False, gh=gh, planner=service
    )
    client = make_client(app, headers=AUTH)
    return Rig(hub, store, gh, service, app, client, backend)
