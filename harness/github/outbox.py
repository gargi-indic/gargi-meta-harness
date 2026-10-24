import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from harness.github.client import GitHub
from harness.github.control_table import ControlRow, upsert_row
from harness.github.labels import ensure_harness_labels
from harness.github.ledger import find_ledger, ledger_summary, parse_ledger, render_ledger
from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import append_event, set_meta

OpType = Literal[
    "add_comment",
    "edit_comment",
    "upsert_ledger",
    "merge_ledger",
    "edit_issue_body",
    "set_labels",
    "merge_pull",
    "delete_branch",
    "close_issue",
    "create_pull",
    "tick_boxes",
]


class OutboxPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str


class AddCommentPayload(OutboxPayload):
    issue: int
    body: str


class EditCommentPayload(OutboxPayload):
    comment_id: int
    body: str


class UpsertLedgerPayload(OutboxPayload):
    issue: int
    body: str


class MergeLedgerPayload(OutboxPayload):
    issue: int
    key: str
    value: dict[str, Any] | None


class EditIssueBodyPayload(OutboxPayload):
    issue: int
    row: ControlRow


class SetLabelsPayload(OutboxPayload):
    issue: int
    add: list[str]
    remove: list[str]


class MergePullPayload(OutboxPayload):
    number: int
    method: Literal["merge", "squash", "rebase"]
    sha: str | None = None


class DeleteBranchPayload(OutboxPayload):
    branch: str


class CloseIssuePayload(OutboxPayload):
    issue: int


class CreatePullPayload(OutboxPayload):
    title: str
    head: str
    base: str
    body: str


class TickBoxesPayload(OutboxPayload):
    issue: int


MERGED_LEDGER_SUMMARY = "Harness control ledger."


def ledger_mirror_key(repo: str, issue: int, key: str) -> str:
    """The ``meta`` row that mirrors one top-level key of ``issue``'s ledger snapshot (JSON, or absent)."""
    return f"ledger:{repo}#{issue}:{key}"


_UNCHECKED_BOX = re.compile(r"(?m)^(\s*[-*+] )\[ \]")


def tick_boxes(body: str) -> str:
    """Tick every unchecked task box at the start of a line; the rest of the body is left byte-for-byte."""
    return _UNCHECKED_BOX.sub(r"\1[x]", body)


_PAYLOAD_MODELS: dict[OpType, type[BaseModel]] = {
    "add_comment": AddCommentPayload,
    "edit_comment": EditCommentPayload,
    "upsert_ledger": UpsertLedgerPayload,
    "merge_ledger": MergeLedgerPayload,
    "edit_issue_body": EditIssueBodyPayload,
    "set_labels": SetLabelsPayload,
    "merge_pull": MergePullPayload,
    "delete_branch": DeleteBranchPayload,
    "close_issue": CloseIssuePayload,
    "create_pull": CreatePullPayload,
    "tick_boxes": TickBoxesPayload,
}


class Outbox:
    def __init__(
        self,
        store: Store,
        gh: GitHub,
        *,
        per_minute: int = 40,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_ledger_comment: Callable[[str, int, int], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.gh = gh
        self.per_minute = per_minute
        self.clock = clock
        self.sleep = sleep
        self.on_ledger_comment = on_ledger_comment

        self._send_times: deque[float] = deque()
        self._ensured_labels_repos: set[str] = set()

    def enqueue(self, op: OpType, payload: dict[str, Any], coalesce_key: str | None = None) -> int:
        if op not in _PAYLOAD_MODELS:
            raise ValueError(f"Unknown op {op}")

        try:
            model = _PAYLOAD_MODELS[op](**payload)
        except ValidationError as e:
            raise ValueError(f"Invalid payload for {op}: {e}") from e

        repo = model.repo
        payload_json = model.model_dump_json()

        with self.store.tx():
            if coalesce_key is not None:
                existing = self.store.query(
                    "SELECT id FROM outbox WHERE coalesce_key = ? AND status = 'pending'", (coalesce_key,)
                )
                if existing:
                    row_id = existing[0]["id"]
                    self.store.execute(
                        """
                        UPDATE outbox
                        SET payload_json = ?, attempts = 0, last_error = NULL, not_before = NULL
                        WHERE id = ?
                        """,
                        (payload_json, row_id),
                    )
                    return row_id

            now = self.store.now()
            cursor = self.store.execute(
                """
                INSERT INTO outbox (repo, op, coalesce_key, payload_json, status, attempts, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?)
                """,
                (repo, op, coalesce_key, payload_json, now),
            )
            return cursor.lastrowid

    def _can_send(self) -> bool:
        now = self.clock()
        while self._send_times and now - self._send_times[0] >= 60:
            self._send_times.popleft()
        return len(self._send_times) < self.per_minute

    def _record_send(self) -> None:
        self._send_times.append(self.clock())

    async def drain_once(self) -> int:
        now = self.store.now()
        rows = self.store.query(
            """
            SELECT id, op, payload_json, coalesce_key, attempts
            FROM outbox
            WHERE status = 'pending' AND (not_before IS NULL OR not_before <= ?)
            ORDER BY id ASC
            """,
            (now,),
        )

        sent_count = 0
        for row in rows:
            if not self._can_send():
                break

            op = cast(OpType, row["op"])
            payload = json.loads(row["payload_json"])
            row_id = row["id"]
            attempts = row["attempts"]

            payload_json = row["payload_json"]

            self._record_send()  # Count attempts in the sliding window

            try:
                await self._process_op(op, payload)

                with self.store.tx():
                    cursor = self.store.execute(
                        """
                        UPDATE outbox
                        SET status = 'sent', sent_at = ?, coalesce_key = NULL
                        WHERE id = ? AND payload_json = ?
                        """,
                        (self.store.now(), row_id, payload_json),
                    )
                    if cursor.rowcount > 0:
                        sent_count += 1
            except Exception as e:
                attempts += 1
                last_error = f"{type(e).__name__}: {e}"
                last_error = redact(last_error)

                if attempts >= 5:
                    with self.store.tx():
                        cursor = self.store.execute(
                            """
                            UPDATE outbox
                            SET status = 'failed', last_error = ?, coalesce_key = NULL, attempts = ?
                            WHERE id = ? AND payload_json = ?
                            """,
                            (last_error, attempts, row_id, payload_json),
                        )
                        if cursor.rowcount > 0:
                            repo = payload.get("repo", "")
                            issue = payload.get("issue", None)
                            data = {"outbox_id": row_id, "op": op, "error": last_error}
                            if issue is None:
                                number = payload.get("number")
                                if number is not None:
                                    data["number"] = number
                            append_event(
                                self.store,
                                repo=repo,
                                issue=issue,
                                kind="outbox_failed",
                                data=data,
                            )
                else:
                    delay = min((2**attempts) * 5, 600)
                    not_before_dt = self.store.clock() + timedelta(seconds=delay)
                    not_before = not_before_dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

                    with self.store.tx():
                        self.store.execute(
                            """
                            UPDATE outbox
                            SET attempts = ?, last_error = ?, not_before = ?
                            WHERE id = ? AND payload_json = ?
                            """,
                            (attempts, last_error, not_before, row_id, payload_json),
                        )

        return sent_count

    async def _process_op(self, op: OpType, payload: dict[str, Any]) -> None:
        repo = payload["repo"]
        if op == "add_comment":
            await self.gh.add_comment(repo, payload["issue"], payload["body"])
        elif op == "edit_comment":
            await self.gh.edit_comment(repo, payload["comment_id"], payload["body"])
        elif op == "upsert_ledger":
            issue = payload["issue"]
            body = payload["body"]
            comments = await self.gh.comments(repo, issue)
            ledger = find_ledger(comments)
            if ledger is not None:
                comment_id = ledger["id"]
                await self.gh.edit_comment(repo, comment_id, body)
            else:
                resp = await self.gh.add_comment(repo, issue, body)
                comment_id = resp["id"]
            if self.on_ledger_comment is not None:
                await self.on_ledger_comment(repo, issue, comment_id)
        elif op == "merge_ledger":
            await self._merge_ledger(repo, payload["issue"], payload["key"], payload["value"])
        elif op == "edit_issue_body":
            issue = payload["issue"]
            row_dict = payload["row"]
            row = ControlRow(**row_dict)
            issue_data = await self.gh.issue(repo, issue)
            current_body = issue_data.get("body") or ""
            new_body = upsert_row(current_body, row)
            await self.gh.update_issue(repo, issue, body=new_body)
        elif op == "set_labels":
            issue = payload["issue"]
            if repo not in self._ensured_labels_repos:
                await ensure_harness_labels(self.gh, repo)
                self._ensured_labels_repos.add(repo)

            if payload["add"]:
                await self.gh.add_labels(repo, issue, payload["add"])
            for label in payload["remove"]:
                await self.gh.remove_label(repo, issue, label)
        elif op == "merge_pull":
            kwargs = {}
            if payload.get("sha") is not None:
                kwargs["sha"] = payload["sha"]
            await self.gh.merge_pull(repo, payload["number"], payload["method"], **kwargs)
        elif op == "delete_branch":
            await self.gh.delete_branch(repo, payload["branch"])
        elif op == "close_issue":
            await self.gh.update_issue(repo, payload["issue"], state="closed")
        elif op == "create_pull":
            await self.gh.create_pull(
                repo, payload["title"], payload["head"], payload["base"], body=payload["body"]
            )
        elif op == "tick_boxes":
            issue = payload["issue"]
            issue_data = await self.gh.issue(repo, issue)
            current_body = issue_data.get("body") or ""
            new_body = tick_boxes(current_body)
            if new_body != current_body:
                await self.gh.update_issue(repo, issue, body=new_body)

    async def _merge_ledger(self, repo: str, issue: int, key: str, value: dict[str, Any] | None) -> None:
        """Set (or, for None, drop) one top-level key of the issue's ledger snapshot, keeping every other key.

        The snapshot is read when the op is sent, not when it was queued, so a lock or a run snapshot written in
        between is preserved. The local mirror of the key is written only after GitHub has the new ledger.
        """
        ledger = find_ledger(await self.gh.comments(repo, issue))
        snapshot = (parse_ledger(ledger.get("body") or "") if ledger else None) or {}
        if value is None:
            snapshot.pop(key, None)
        else:
            snapshot[key] = value
        summary = (ledger_summary(ledger.get("body") or "") if ledger else "") or MERGED_LEDGER_SUMMARY
        body = render_ledger(snapshot, summary)
        if ledger is not None:
            comment_id = ledger["id"]
            await self.gh.edit_comment(repo, comment_id, body)
        else:
            comment_id = (await self.gh.add_comment(repo, issue, body))["id"]
        set_meta(self.store, ledger_mirror_key(repo, issue, key), json.dumps(value, sort_keys=True))
        if self.on_ledger_comment is not None:
            await self.on_ledger_comment(repo, issue, comment_id)

    async def run(self, stop: asyncio.Event, idle_sleep: float = 2.0) -> None:
        while not stop.is_set():
            await self.drain_once()
            if not stop.is_set():
                await self.sleep(idle_sleep)
