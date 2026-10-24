"""Demo mode for the planner screen: an in-memory GitHub REST stand-in, a fixed decomposer and the demo plan.

The real :class:`GitHub` client, outbox, proposal store and ``materialize`` run against :class:`DemoPlannerHub`,
which answers the REST calls they make from memory (contents, issues, comments, labels, sub-issues and
dependencies). Nothing here touches the network. The only stand-ins are the model (:class:`DemoBackend` always
answers with the same five-issue proposal) and the GitHub transport.
"""

import base64
import hashlib
import json
import re
from typing import Any

import httpx

from harness.adapters.base import Capabilities
from harness.github.ledger import find_ledger, render_ledger
from harness.server.demo_seed import REPO

DEMO_CONTROL = 1
DEMO_PLAN_PATH = "docs/PLAN.md"
DEMO_PLAN_TEXT = """# Widget platform

Ship the widget platform in small, reviewable steps.

## Model

Add the widget model and its storage.

## API

- CRUD endpoints for widgets
- Search over widget names

## Docs

Document the API and add a changelog entry.
"""

# The proposal the demo decomposer returns for any plan: five issues, the first three chained.
DEMO_PROPOSAL: dict[str, Any] = {
    "epic_title": "Widget platform",
    "epic_body": "Ship the widget platform in small steps.",
    "notes": "Demo proposal: the same five issues for any plan.",
    "issues": [
        {
            "key": "model",
            "title": "Add the widget model",
            "body": "## Context\nWidgets need a model.\n\n## Scope\nA widget table and a dataclass.\n\n"
            "## Acceptance criteria\n- A widget can be stored and loaded",
            "acceptance": ["A widget can be stored and loaded"],
            "blocked_by": [],
            "agent": "jules",
            "playbook": "plan-build-review",
            "paths": ["widgets/model.py"],
        },
        {
            "key": "crud",
            "title": "Add widget CRUD endpoints",
            "body": "## Context\nClients manage widgets over HTTP.\n\n## Scope\nCreate, read, update, delete.\n\n"
            "## Acceptance criteria\n- All four endpoints answer",
            "acceptance": ["All four endpoints answer"],
            "blocked_by": ["model"],
            "agent": "jules",
            "playbook": "plan-build-review",
            "paths": ["widgets/api.py"],
        },
        {
            "key": "search",
            "title": "Add widget search",
            "body": "## Context\nUsers look widgets up by name.\n\n## Scope\nA search endpoint.\n\n"
            "## Acceptance criteria\n- Search finds a widget by name prefix",
            "acceptance": ["Search finds a widget by name prefix"],
            "blocked_by": ["crud"],
            "agent": "claude",
            "playbook": "plan-build-review",
            "paths": [],
        },
        {
            "key": "export",
            "title": "Add widget export",
            "body": "## Context\nOperators export widgets.\n\n## Scope\nA CSV export.\n\n"
            "## Acceptance criteria\n- Export lists every widget",
            "acceptance": ["Export lists every widget"],
            "blocked_by": ["model"],
            "agent": "claude",
            "playbook": "plan-build-review",
            "paths": [],
        },
        {
            "key": "docs",
            "title": "Document the widget API",
            "body": "## Context\nThe API has no docs.\n\n## Scope\nA page and a changelog entry.\n\n"
            "## Acceptance criteria\n- Every endpoint is documented",
            "acceptance": ["Every endpoint is documented"],
            "blocked_by": [],
            "agent": "jules",
            "playbook": "plan-build-review",
            "paths": ["docs/widgets.md"],
        },
    ],
}

DEMO_AGENT_CAPS = {
    "jules": Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=False),
    "claude": Capabilities(plan_approval=True, messaging=True, opens_pr=False, local_workspace=True),
    "codex": Capabilities(plan_approval=True, messaging=True, opens_pr=False, local_workspace=True),
}


class DemoBackend:
    """The decomposer's model: always the same proposal."""

    async def review(self, system: str, prompt: str, schema: dict, workdir: Any) -> dict:
        return json.loads(json.dumps(DEMO_PROPOSAL))


def _blob_sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()  # noqa: S324 - a stand-in for a git blob sha, not security


class DemoPlannerHub(httpx.AsyncBaseTransport):
    """GitHub's REST API for one repo, from memory: what the planner service reads and writes."""

    def __init__(self, repo: str = REPO) -> None:
        self.repo = repo
        self.files: dict[str, str] = {DEMO_PLAN_PATH: DEMO_PLAN_TEXT}
        self.issues: dict[int, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.sub_issues: dict[int, list[int]] = {}
        self.blocked_by: dict[int, list[int]] = {}
        self._next_issue = 1
        self._next_comment = 9000
        self._add_issue("Campaign control", "Control issue.\n\nIntro text.\n\nOutro text.\n", [])
        self._approve_plan()
        self._next_issue = 8  # the seeded campaign uses #1-#7
        self._routes: list[tuple[str, re.Pattern[str], Any]] = []
        base = rf"^/repos/{re.escape(repo)}"
        for method, pattern, handler in (
            ("GET", r"/contents/(?P<path>.+)$", self._get_contents),
            ("GET", r"/issues$", self._list_issues),
            ("POST", r"/issues$", self._create_issue),
            ("GET", r"/issues/(?P<number>\d+)$", self._get_issue),
            ("PATCH", r"/issues/(?P<number>\d+)$", self._patch_issue),
            ("GET", r"/issues/(?P<number>\d+)/comments$", self._list_comments),
            ("POST", r"/issues/(?P<number>\d+)/comments$", self._post_comment),
            ("PATCH", r"/issues/comments/(?P<comment_id>\d+)$", self._edit_comment),
            ("GET", r"/labels$", self._list_labels),
            ("POST", r"/labels$", self._create_label),
            ("POST", r"/issues/(?P<number>\d+)/labels$", self._add_labels),
            ("GET", r"/issues/(?P<number>\d+)/sub_issues$", self._list_sub_issues),
            ("POST", r"/issues/(?P<number>\d+)/sub_issues$", self._add_sub_issue),
            ("GET", r"/issues/(?P<number>\d+)/dependencies/blocked_by$", self._list_blocked_by),
            ("POST", r"/issues/(?P<number>\d+)/dependencies/blocked_by$", self._add_blocked_by),
        ):
            self._routes.append((method, re.compile(base + pattern), handler))

    # ---- state -------------------------------------------------------------------------------------------

    def plan_sha(self, path: str = DEMO_PLAN_PATH) -> str:
        return _blob_sha(self.files[path])

    def _add_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        number = self._next_issue
        self._next_issue += 1
        issue = {
            "number": number,
            "id": 70_000 + number * 3,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": n} for n in labels],
        }
        self.issues[number] = issue
        return issue

    def _add_comment(self, number: int, body: str) -> dict[str, Any]:
        self._next_comment += 1
        comment = {"id": self._next_comment, "body": body, "created_at": "2026-01-01T00:00:00Z"}
        self.comments.setdefault(number, []).append(comment)
        return comment

    def _approve_plan(self) -> None:
        """The plan is approved from the start (the ledger record ``harness plan approve`` writes)."""
        record = {
            "kind": "file",
            "path": DEMO_PLAN_PATH,
            "issue": None,
            "ref": None,
            "plan_sha": self.plan_sha(),
            "title": "Widget platform",
            "approved_by": "acme",
            "via": "cli",
            "approved_at": "2026-01-01T00:00:00Z",
            "label_cursor": 0,
        }
        body = render_ledger({"plan": record}, "Harness control ledger.")
        ledger = find_ledger(self.comments.get(DEMO_CONTROL, []))
        if ledger is None:
            self._add_comment(DEMO_CONTROL, body)
        else:
            ledger["body"] = body

    def created(self) -> list[dict[str, Any]]:
        """The issues made through the API, oldest first (the control issue is not among them)."""
        return [
            {"number": n, "title": i["title"], "labels": [lab["name"] for lab in i["labels"]]}
            for n, i in sorted(self.issues.items())
            if n != DEMO_CONTROL
        ]

    # ---- transport ---------------------------------------------------------------------------------------

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for method, pattern, handler in self._routes:
            match = pattern.match(path)
            if match and method == request.method:
                return handler(request, **match.groupdict())
        return httpx.Response(404, json={"message": "Not Found"})

    # ---- handlers ----------------------------------------------------------------------------------------

    def _get_contents(self, request: httpx.Request, path: str) -> httpx.Response:
        if path not in self.files:
            return httpx.Response(404, json={"message": "Not Found"})
        text = self.files[path]
        if "raw" in request.headers.get("accept", ""):
            return httpx.Response(200, text=text)
        raw = text.encode()
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": path,
                "sha": self.plan_sha(path),
                "size": len(raw),
                "encoding": "base64",
                "content": base64.encodebytes(raw).decode(),
            },
        )

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
        issue = self.issues.get(int(number))
        if issue is None:
            return httpx.Response(404, json={"message": "Not Found"})
        issue.update(json.loads(request.content))
        return httpx.Response(200, json=issue)

    def _list_comments(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self.comments.get(int(number), []))

    def _post_comment(self, request: httpx.Request, number: str) -> httpx.Response:
        comment = self._add_comment(int(number), json.loads(request.content)["body"])
        return httpx.Response(201, json=comment)

    def _edit_comment(self, request: httpx.Request, comment_id: str) -> httpx.Response:
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == int(comment_id):
                    comment["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})

    def _list_labels(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=list(self.labels.values()))

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if data["name"].lower() in {n.lower() for n in self.labels}:
            return httpx.Response(422, json={"message": "Validation Failed", "errors": ["already_exists"]})
        self.labels[data["name"]] = data
        return httpx.Response(201, json=data)

    def _add_labels(self, request: httpx.Request, number: str) -> httpx.Response:
        issue = self.issues.get(int(number))
        if issue is None:
            return httpx.Response(404, json={"message": "Not Found"})
        for name in json.loads(request.content).get("labels", []):
            if name not in [lab["name"] for lab in issue["labels"]]:
                issue["labels"].append({"name": name})
        return httpx.Response(200, json=issue["labels"])

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
