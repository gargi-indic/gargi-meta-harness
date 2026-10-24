"""A stateful stand-in for the GitHub REST calls that the plan loader, the plan gate and the outbox make.

It is installed on a respx router, so the real ``GitHub`` client (ETag cache, pagination, retries) runs against it.
Labels added through the API create ``labeled`` events by ``world.actor``, like GitHub does.
"""

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

REPO = "o/r"
CONTROL = 1
PLAN_PATH = "docs/PLAN.md"
PLAN_TEXT = "# The plan\n\nIntro.\n\n## Goals\n\n```\n# not a heading\n```\n\n## Steps\n"
T = "2026-03-01T10:00:00Z"
T_LATER = "2026-03-01T12:00:00Z"
T_LATEST = "2026-03-01T14:00:00Z"


@dataclass
class PlanFile:
    text: str
    sha: str
    commit_sha: str
    commit_date: str
    etag: str = '"v1"'


@dataclass
class World:
    router: respx.MockRouter
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    files: dict[str, PlanFile] = field(default_factory=dict)
    # Contents API bodies served as they are (to model a lying size or non-text content), by path.
    raw_contents: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    commit_pulls: dict[str, list[int]] = field(default_factory=dict)
    actor: str = "o"
    user: str = "o"
    now: str = T
    calls: list[tuple[str, str]] = field(default_factory=list)
    _ids: int = 1000

    # ---- test-side setup ----------------------------------------------------------------------------

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def put_file(
        self, path: str = PLAN_PATH, text: str = PLAN_TEXT, *, version: int = 1, date: str = T
    ) -> None:
        self.files[path] = PlanFile(
            text=text,
            sha=f"blob{version:03d}".ljust(40, "0"),
            commit_sha=f"commit{version:03d}".ljust(40, "0"),
            commit_date=date,
            etag=f'"v{version}"',
        )

    def put_issue(self, number: int, title: str, body: str) -> None:
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open"}

    def merge_pr(self, number: int, path: str = PLAN_PATH, by: str = "o") -> None:
        file = self.files[path]
        self.pulls[number] = {
            "number": number,
            "merged_at": file.commit_date,
            "merged_by": {"login": by},
            "merge_commit_sha": file.commit_sha,
        }
        self.commit_pulls.setdefault(file.commit_sha, []).append(number)

    def label_event(self, actor: str, *, label: str = "harness:plan-approved", at: str | None = None) -> int:
        event = {
            "id": self.next_id(),
            "event": "labeled",
            "actor": {"login": actor},
            "label": {"name": label},
            "created_at": at or self.now,
        }
        self.events.append(event)
        if label == "harness:plan-approved":
            self.labels.add(label)
        return int(event["id"])

    def unlabel_event(self, actor: str, label: str = "harness:plan-approved") -> None:
        self.events.append(
            {
                "id": self.next_id(),
                "event": "unlabeled",
                "actor": {"login": actor},
                "label": {"name": label},
                "created_at": self.now,
            }
        )
        self.labels.discard(label)

    def ledger(self, issue: int = CONTROL) -> dict[str, Any] | None:
        from harness.github.ledger import find_ledger, parse_ledger

        comment = find_ledger(self.comments.get(issue, []))
        return parse_ledger(comment["body"]) if comment else None

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p.endswith(suffix))

    def bodies(self, issue: int = CONTROL) -> list[str]:
        return [c["body"] for c in self.comments.get(issue, [])]

    # ---- routes ---------------------------------------------------------------------------------------

    def install(self) -> "World":
        r = self.router
        base = rf"^/repos/{REPO}"

        def route(method: str, pattern: str, handler: Any) -> None:
            def wrapped(request: httpx.Request, **groups: str) -> httpx.Response:
                self.calls.append((request.method, request.url.path))
                return handler(request, **groups)

            r.route(method=method, path__regex=pattern).mock(side_effect=wrapped)

        route("GET", base + r"/contents/(?P<path>.+)$", self._get_contents)
        route("GET", base + r"/commits$", self._get_commits)
        route("GET", base + r"/commits/(?P<sha>[^/]+)/pulls$", self._get_commit_pulls)
        route("GET", base + r"/pulls/(?P<number>\d+)$", self._get_pull)
        route("GET", base + r"/issues/(?P<number>\d+)/comments$", self._list_comments)
        route("POST", base + r"/issues/(?P<number>\d+)/comments$", self._add_comment)
        route("PATCH", base + r"/issues/comments/(?P<comment_id>\d+)$", self._edit_comment)
        route("GET", base + r"/issues/(?P<number>\d+)/events$", self._list_events)
        route("POST", base + r"/issues/(?P<number>\d+)/labels$", self._add_labels)
        route("DELETE", base + r"/issues/(?P<number>\d+)/labels/(?P<name>.+)$", self._remove_label)
        route("POST", base + r"/labels$", lambda request: httpx.Response(201, json={}))
        route("GET", base + r"/issues/(?P<number>\d+)$", self._get_issue)
        route(
            "GET", rf"^/repos/{REPO}$", lambda request: httpx.Response(200, json={"default_branch": "main"})
        )
        route("GET", base + r"/issues$", lambda request: httpx.Response(200, json=[]))
        route("GET", r"^/user$", lambda request: httpx.Response(200, json={"login": self.user}))
        return self

    def _get_contents(self, request: httpx.Request, path: str) -> httpx.Response:
        if path in self.raw_contents:
            return httpx.Response(200, json=self.raw_contents[path])
        file = self.files.get(path)
        if file is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.headers.get("if-none-match") == file.etag:
            return httpx.Response(304, headers={"etag": file.etag})
        raw = file.text.encode()
        if "raw" in request.headers.get("accept", ""):
            return httpx.Response(200, text=file.text)
        body = {
            "type": "file",
            "path": path,
            "sha": file.sha,
            "size": len(raw),
            "encoding": "base64",
            "content": base64.encodebytes(raw).decode(),
        }
        return httpx.Response(200, json=body, headers={"etag": file.etag})

    def _get_commits(self, request: httpx.Request) -> httpx.Response:
        file = self.files.get(request.url.params.get("path", ""))
        if file is None:
            return httpx.Response(200, json=[])
        commit = {"sha": file.commit_sha, "commit": {"committer": {"date": file.commit_date}}}
        return httpx.Response(200, json=[commit])

    def _get_commit_pulls(self, request: httpx.Request, sha: str) -> httpx.Response:
        return httpx.Response(200, json=[self.pulls[n] for n in self.commit_pulls.get(sha, [])])

    def _get_pull(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self.pulls[int(number)])

    def _get_issue(self, request: httpx.Request, number: str) -> httpx.Response:
        issue = self.issues.get(int(number))
        if issue is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=issue)

    def _list_comments(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self.comments.get(int(number), []))

    def _add_comment(self, request: httpx.Request, number: str) -> httpx.Response:
        comment = {"id": self.next_id(), "body": json.loads(request.content)["body"], "created_at": self.now}
        self.comments.setdefault(int(number), []).append(comment)
        return httpx.Response(201, json=comment)

    def _edit_comment(self, request: httpx.Request, comment_id: str) -> httpx.Response:
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == int(comment_id):
                    comment["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})

    def _list_events(self, request: httpx.Request, number: str) -> httpx.Response:
        return httpx.Response(200, json=self.events)

    def _add_labels(self, request: httpx.Request, number: str) -> httpx.Response:
        for name in json.loads(request.content)["labels"]:
            if name not in self.labels:
                self.labels.add(name)
                self.events.append(
                    {
                        "id": self.next_id(),
                        "event": "labeled",
                        "actor": {"login": self.actor},
                        "label": {"name": name},
                        "created_at": self.now,
                    }
                )
        return httpx.Response(200, json=[{"name": n} for n in sorted(self.labels)])

    def _remove_label(self, request: httpx.Request, number: str, name: str) -> httpx.Response:
        self.labels.discard(re.sub("%3A", ":", name, flags=re.I))
        return httpx.Response(200, json=[])
