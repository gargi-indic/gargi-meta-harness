"""A stateful respx stand-in for the GitHub REST calls that issue creation makes (M4.4).

It models what the real API enforces and the tests rely on: label filters and ETags on issue lists, labels that
must exist before an issue uses them, ids that differ from numbers, and 422 for a duplicate label, sub-issue or
dependency, so a test fails when a link is made twice. ``crash_after`` makes the n-th matching write take effect
and then raise ``Crash`` instead of answering, which is a process that dies before it sees the response.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

REPO = "o/r"
CONTROL = 1
CONTROL_BODY = "Control issue.\n\nIntro text.\n\nOutro text.\n"
BASE = rf"^/repos/{REPO}"


class Crash(BaseException):
    """The process died. A BaseException, so that nothing in the harness catches it as an error."""


@dataclass
class Write:
    method: str
    path: str
    body: Any
    at: float


@dataclass
class PlannerWorld:
    router: respx.MockRouter
    now: Any = lambda: 0.0  # noqa: E731 - the clock that stamps writes
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # repo files by path (the contents API)
    sub_issues: dict[int, list[int]] = field(default_factory=dict)
    blocked_by: dict[int, list[int]] = field(default_factory=dict)
    writes: list[Write] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)
    crash_at: tuple[str, str, int] | None = None  # (method, path suffix, n-th match)
    down: set[tuple[str, str]] = field(default_factory=set)  # (method, path suffix) answered with a 500
    _crash_seen: int = 0
    _next: int = 1

    def __post_init__(self) -> None:
        self._add_issue("Control", CONTROL_BODY, [])

    # ---- test-side helpers ---------------------------------------------------------------------------

    def _add_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        number = self._next
        self._next += 1
        issue = {
            "number": number,
            "id": 7000 + number * 3,  # never equal to the number
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": n} for n in labels],
        }
        self.issues[number] = issue
        return issue

    def crash_after(self, method: str, suffix: str, nth: int) -> None:
        self.crash_at = (method, suffix, nth)
        self._crash_seen = 0

    def writes_to(self, method: str, suffix: str) -> list[Write]:
        return [w for w in self.writes if w.method == method and w.path.endswith(suffix)]

    def created(self) -> list[dict[str, Any]]:
        """The issues made through the API, oldest first (the control issue is not among them)."""
        return [i for n, i in sorted(self.issues.items()) if n != CONTROL]

    def by_title(self, title: str) -> dict[str, Any]:
        return next(i for i in self.issues.values() if i["title"] == title)

    # ---- routes --------------------------------------------------------------------------------------

    def install(self) -> "PlannerWorld":
        def route(method: str, pattern: str, handler: Any) -> None:
            def wrapped(request: httpx.Request, **groups: str) -> httpx.Response:
                path = request.url.path
                body = json.loads(request.content) if request.content else None
                if method == "GET":
                    self.reads.append((method, path))
                    return handler(request, **groups)
                self.writes.append(Write(method, path, body, self.now()))
                if any(method == m and path.endswith(suffix) for m, suffix in self.down):
                    return httpx.Response(500, json={"message": "boom"})
                response = handler(request, **groups)
                if self.crash_at and self.crash_at[0] == method and path.endswith(self.crash_at[1]):
                    self._crash_seen += 1
                    if self._crash_seen == self.crash_at[2]:
                        raise Crash(f"{method} {path}")
                return response

            self.router.route(method=method, path__regex=pattern).mock(side_effect=wrapped)

        route("GET", BASE + r"/contents/(?P<path>.+)$", self._get_contents)
        route("GET", BASE + r"/issues$", self._list_issues)
        route("POST", BASE + r"/issues$", self._create_issue)
        route("GET", BASE + r"/issues/(?P<number>\d+)$", self._get_issue)
        route("PATCH", BASE + r"/issues/(?P<number>\d+)$", self._patch_issue)
        route("GET", BASE + r"/issues/(?P<number>\d+)/comments$", self._list_comments)
        route("POST", BASE + r"/issues/(?P<number>\d+)/comments$", self._add_comment)
        route("GET", BASE + r"/labels$", lambda request: self._etagged(request, list(self.labels.values())))
        route("POST", BASE + r"/labels$", self._create_label)
        route("GET", BASE + r"/issues/(?P<number>\d+)/sub_issues$", self._list_sub_issues)
        route("POST", BASE + r"/issues/(?P<number>\d+)/sub_issues$", self._add_sub_issue)
        route("GET", BASE + r"/issues/(?P<number>\d+)/dependencies/blocked_by$", self._list_blocked_by)
        route("POST", BASE + r"/issues/(?P<number>\d+)/dependencies/blocked_by$", self._add_blocked_by)
        return self

    def _get_contents(self, request: httpx.Request, path: str) -> httpx.Response:
        if path not in self.files:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, text=self.files[path])

    def _etagged(self, request: httpx.Request, body: Any) -> httpx.Response:
        etag = '"' + hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, json=body, headers={"etag": etag})

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
        return self._etagged(request, found)

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
        comment = {
            "id": 9000 + sum(map(len, self.comments.values())),
            "body": json.loads(request.content)["body"],
        }
        self.comments.setdefault(int(number), []).append(comment)
        return httpx.Response(201, json=comment)

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if data["name"].lower() in {n.lower() for n in self.labels}:
            return httpx.Response(422, json={"message": "Validation Failed", "errors": ["already_exists"]})
        self.labels[data["name"]] = data
        return httpx.Response(201, json=data)

    def _by_id(self, ids: list[int]) -> list[dict[str, Any]]:
        return [i for i in self.issues.values() if i["id"] in ids]

    def _list_sub_issues(self, request: httpx.Request, number: str) -> httpx.Response:
        return self._etagged(request, self._by_id(self.sub_issues.get(int(number), [])))

    def _add_sub_issue(self, request: httpx.Request, number: str) -> httpx.Response:
        child = json.loads(request.content)["sub_issue_id"]
        if child in self.sub_issues.setdefault(int(number), []) or not self._by_id([child]):
            return httpx.Response(422, json={"message": "duplicate or unknown sub-issue"})
        self.sub_issues[int(number)].append(child)
        return httpx.Response(201, json=self.issues[int(number)])

    def _list_blocked_by(self, request: httpx.Request, number: str) -> httpx.Response:
        return self._etagged(request, self._by_id(self.blocked_by.get(int(number), [])))

    def _add_blocked_by(self, request: httpx.Request, number: str) -> httpx.Response:
        blocker = json.loads(request.content)["issue_id"]
        if blocker in self.blocked_by.setdefault(int(number), []) or not self._by_id([blocker]):
            return httpx.Response(422, json={"message": "duplicate or unknown dependency"})
        self.blocked_by[int(number)].append(blocker)
        return httpx.Response(201, json=self.issues[int(number)])
