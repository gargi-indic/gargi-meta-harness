"""Where a proposal lives: harness-owned comments on the control issue (GitHub), mirrored in the ``proposals`` table.

GitHub is the state store. A proposal is saved as one or more comments on the control issue, each starting with::

    <!-- harness:proposal run=<run_id> sha=<plan_sha> part=<k>/<n> -->

The rest of a comment is a short note for humans and the data in a fenced block inside ``<details>``. The data is
a JSON envelope ``{"v", "run_id", "plan_sha", "status", "created_at", "proposal"}`` (plus ``"rev"`` and ``"meta"``
when they are not the defaults 1 and ``{}``) in compact form, with ``<``,
``>`` and the backtick written as JSON ``\\u`` escapes (so neither a comment opener nor a code fence can occur in
it), cut into parts of at most ``MAX_COMMENT_CHARS`` characters; the parts joined in order give the JSON text.
A part is edited in place when the proposal is saved again (matched by its marker, never duplicated); parts that a
shorter text no longer needs are retired (``part=<k>/0``) because the outbox has no delete op. A proposal whose
parts are not all present and consistent is treated as absent.

Writes go through the outbox (``add_comment`` / ``edit_comment``) and are awaited with ``Outbox.settle``; the local
``proposals`` row and the small summary under the ``proposal`` key of the control issue's ledger (never the proposal
itself) are written only after GitHub has the comments. ``get_proposal`` answers from the mirror first and refills it
from the comments when it is empty; ``harness sync`` rebuilds the whole mirror (``restore_proposal_mirror``).

``rev`` is a counter that the caller bumps with every change of the draft, and ``meta`` a small JSON object of the
caller's own (the planner API keeps the plan source, job errors and results there). The ``proposals`` table has no
column for either, so the mirror keeps them in the ``meta`` key-value table under ``proposal:ext:<run>:<sha>``;
callers that never set them (the decompose executor) see ``rev`` 1 and ``meta`` ``{}`` and write the same text as
before.

Like the ledger, the comments are trusted as they stand: the control issue of a private repo is writable by
collaborators only. Nothing here verifies who wrote a comment.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from harness.github.client import GitHub
from harness.github.outbox import Outbox
from harness.planner.proposal import ProposedPlan
from harness.store.db import Store
from harness.store.repo import (
    ProposalRow,
    delete_proposal_rows_of_repo,
    get_meta,
    get_proposal_row,
    save_proposal_row,
    set_meta,
)

LEDGER_KEY = "proposal"
# The Planner API's run ids are ``<repo>#planner-<token>``; its drafts that passed validation have these statuses.
PLANNER_RUN_INFIX = "#planner-"
DRAFT_STATUSES = ("ready", "creating", "created")
ENVELOPE_VERSION = 1
# GitHub rejects comments of more than 65,536 characters; stay well below.
MAX_COMMENT_CHARS = 60_000
_OVERHEAD = 1_000  # the marker, the note and the wrapper around one part of the data
PART_CHARS = MAX_COMMENT_CHARS - _OVERHEAD

_MARKER = re.compile(
    r"\A\s*<!-- harness:proposal run=(?P<run>[^\s>]+) sha=(?P<sha>[A-Za-z0-9]+) part=(?P<k>\d+)/(?P<n>\d+) -->"
)
_DATA = re.compile(r"```json\n(?P<data>[^`\n]*)\n```")
_STATUS = re.compile(r"^[a-z][a-z_-]{0,31}$")
_SAFE_RUN = re.compile(r"^[^\s<>`]+$")


@dataclass(frozen=True)
class StoredProposal:
    run_id: str
    plan_sha: str
    status: str
    created_at: str
    proposal: ProposedPlan
    rev: int = 1
    meta: dict[str, Any] = field(default_factory=dict)


# ---- the comment format --------------------------------------------------------------------------------


def _marker(run_id: str, plan_sha: str, k: int, n: int) -> str:
    return f"<!-- harness:proposal run={run_id} sha={plan_sha} part={k}/{n} -->"


def _envelope_text(stored: StoredProposal) -> str:
    envelope = {
        "v": ENVELOPE_VERSION,
        "run_id": stored.run_id,
        "plan_sha": stored.plan_sha,
        "status": stored.status,
        "created_at": stored.created_at,
        "proposal": stored.proposal.model_dump(mode="json"),
    }
    if stored.rev != 1:
        envelope["rev"] = stored.rev
    if stored.meta:
        envelope["meta"] = stored.meta
    text = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("`", "\\u0060")


def _render_parts(stored: StoredProposal) -> list[str]:
    text = _envelope_text(stored)
    chunks = [text[i : i + PART_CHARS] for i in range(0, len(text), PART_CHARS)] or [""]
    total = len(chunks)
    count = len(stored.proposal.issues)
    parts = []
    for k, chunk in enumerate(chunks, 1):
        note = (
            f"**Harness proposal** for plan `{stored.plan_sha[:12]}`: {count} issue{'s' if count != 1 else ''}, "
            f"status `{stored.status}` (part {k} of {total}). The harness reads this comment: do not edit it."
        )
        parts.append(
            f"{_marker(stored.run_id, stored.plan_sha, k, total)}\n{note}\n\n"
            f"<details><summary>Proposal data (part {k} of {total})</summary>\n\n"
            f"```json\n{chunk}\n```\n\n</details>"
        )
    return parts


def _retired_body(run_id: str, plan_sha: str, k: int) -> str:
    return (
        f"{_marker(run_id, plan_sha, k, 0)}\n"
        "**Harness proposal**: this part is no longer used (the proposal became shorter)."
    )


@dataclass(frozen=True)
class _Part:
    run_id: str
    plan_sha: str
    k: int
    n: int
    comment: dict[str, Any]


def _parts_in(comments: list[dict[str, Any]]) -> list[_Part]:
    found = []
    for comment in comments:
        match = _MARKER.match(comment.get("body") or "")
        if match is None:
            continue
        found.append(_Part(match["run"], match["sha"], int(match["k"]), int(match["n"]), comment))
    return found


def _by_part(parts: list[_Part], run_id: str, plan_sha: str) -> dict[int, _Part]:
    """The parts of one proposal by number (retired ones included); the oldest comment wins a duplicate."""
    chosen: dict[int, _Part] = {}
    for part in sorted(parts, key=lambda p: int(p.comment.get("id") or 0)):
        if part.run_id == run_id and part.plan_sha == plan_sha:
            chosen.setdefault(part.k, part)
    return chosen


def _assemble(by_k: dict[int, _Part]) -> StoredProposal | None:
    live = {k: p for k, p in by_k.items() if p.n > 0}
    totals = {p.n for p in live.values()}
    if len(totals) != 1:
        return None
    total = totals.pop()
    if sorted(live) != list(range(1, total + 1)):
        return None
    chunks = []
    for k in range(1, total + 1):
        data = _DATA.search(live[k].comment.get("body") or "")
        if data is None:
            return None
        chunks.append(data["data"])
    first = live[1]
    try:
        envelope = json.loads("".join(chunks))
        if (
            not isinstance(envelope, dict)
            or envelope.get("v") != ENVELOPE_VERSION
            or envelope.get("run_id") != first.run_id
            or envelope.get("plan_sha") != first.plan_sha
            or not isinstance(envelope.get("status"), str)
            or not isinstance(envelope.get("created_at"), str)
        ):
            return None
        proposal = ProposedPlan.model_validate(envelope.get("proposal"))
        rev, meta = envelope.get("rev", 1), envelope.get("meta", {})
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1 or not isinstance(meta, dict):
            return None
    except (ValueError, ValidationError):
        return None
    return StoredProposal(
        first.run_id, first.plan_sha, envelope["status"], envelope["created_at"], proposal, rev, meta
    )


def read_proposals(comments: list[dict[str, Any]]) -> list[StoredProposal]:
    """Every complete proposal in ``comments``, oldest first (by the id of the comment of its first part)."""
    parts = _parts_in(comments)
    found: list[tuple[int, StoredProposal]] = []
    for run_id, plan_sha in dict.fromkeys((p.run_id, p.plan_sha) for p in parts):
        by_k = _by_part(parts, run_id, plan_sha)
        stored = _assemble(by_k)
        if stored is not None:
            first = min(int(p.comment.get("id") or 0) for p in by_k.values() if p.n > 0)
            found.append((first, stored))
    return [stored for _, stored in sorted(found, key=lambda item: item[0])]


# ---- the mirror ------------------------------------------------------------------------------------------


def _ext_key(run_id: str, plan_sha: str) -> str:
    return f"proposal:ext:{run_id}:{plan_sha}"


def _ext_of(store: Store, run_id: str, plan_sha: str) -> tuple[int, dict[str, Any]]:
    """The mirrored ``(rev, meta)``; the defaults when there are none or the text cannot be read."""
    raw = get_meta(store, _ext_key(run_id, plan_sha))
    try:
        data = json.loads(raw) if raw else {}
        rev, meta = data.get("rev", 1), data.get("meta", {})
        if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1 and isinstance(meta, dict):
            return rev, meta
    except (ValueError, AttributeError):
        pass
    return 1, {}


def _from_row(store: Store, row: ProposalRow) -> StoredProposal | None:
    try:
        proposal = ProposedPlan.model_validate_json(row.json_text)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return None
    rev, meta = _ext_of(store, row.run_id, row.plan_sha)
    return StoredProposal(row.run_id, row.plan_sha, row.status, row.created_at, proposal, rev, meta)


def _mirror(store: Store, stored: StoredProposal) -> None:
    key = _ext_key(stored.run_id, stored.plan_sha)
    with store.tx():
        save_proposal_row(
            store,
            run_id=stored.run_id,
            plan_sha=stored.plan_sha,
            json_text=stored.proposal.model_dump_json(),
            status=stored.status,
            created_at=stored.created_at,
        )
        if stored.rev != 1 or stored.meta:
            set_meta(store, key, json.dumps({"rev": stored.rev, "meta": stored.meta}, sort_keys=True))
        else:
            store.execute("DELETE FROM meta WHERE key = ?", (key,))


# ---- public API ------------------------------------------------------------------------------------------


def _check(run_id: str, plan_sha: str, status: str) -> None:
    if not _SAFE_RUN.match(run_id):
        raise ValueError(f"run id {run_id!r} cannot be used in a proposal marker")
    if not re.fullmatch(r"[A-Za-z0-9]{4,128}", plan_sha):
        raise ValueError(f"plan sha {plan_sha!r} must be 4-128 letters or digits")
    if not _STATUS.match(status):
        raise ValueError(f"proposal status {status!r} must match {_STATUS.pattern}")


async def save_proposal(
    store: Store,
    outbox: Outbox,
    *,
    repo: str,
    control_issue: int,
    run_id: str,
    plan_sha: str,
    proposal: ProposedPlan,
    status: str = "proposed",
    rev: int = 1,
    meta: dict[str, Any] | None = None,
) -> StoredProposal:
    """Write the proposal to the control issue (GitHub first), then to the ledger summary and the mirror.

    Saving the same ``(run_id, plan_sha)`` again edits the comments that are there; one that already has the right
    text is not touched. Raises ``OutboxOpFailed`` when GitHub did not take a write (nothing is mirrored then, the
    queued writes stay in the outbox and go out when it drains again). ``rev`` and ``meta`` are stored with the
    proposal (see the module text); ``meta`` must be JSON.
    """
    _check(run_id, plan_sha, status)
    gh = outbox.gh
    parts = _parts_in(await gh.comments(repo, control_issue))
    existing = _by_part(parts, run_id, plan_sha)
    before = _assemble(existing)
    row = get_proposal_row(store, run_id, plan_sha)
    created_at = (before.created_at if before else None) or (row.created_at if row else None) or store.now()
    stored = StoredProposal(run_id, plan_sha, status, created_at, proposal, rev, dict(meta or {}))

    bodies = _render_parts(stored)
    prefix = f"proposal:{repo}#{control_issue}:{run_id}:{plan_sha}"
    op_ids: list[int] = []
    with store.tx():
        for k, body in enumerate(bodies, 1):
            current = existing.get(k)
            if current is None:
                payload: dict[str, Any] = {"repo": repo, "issue": control_issue, "body": body}
                op_ids.append(outbox.enqueue("add_comment", payload, coalesce_key=f"{prefix}:add:{k}"))
            elif (current.comment.get("body") or "") != body:
                edit = {"repo": repo, "comment_id": int(current.comment["id"]), "body": body}
                op_ids.append(outbox.enqueue("edit_comment", edit, coalesce_key=f"{prefix}:edit:{k}"))
        for k, current in existing.items():
            if k > len(bodies) and current.n > 0:
                retired = {
                    "repo": repo,
                    "comment_id": int(current.comment["id"]),
                    "body": _retired_body(run_id, plan_sha, k),
                }
                op_ids.append(outbox.enqueue("edit_comment", retired, coalesce_key=f"{prefix}:edit:{k}"))
    await outbox.settle(op_ids)

    ids = [
        int(p.comment["id"])
        for p in sorted(
            _by_part(_parts_in(await gh.comments(repo, control_issue)), run_id, plan_sha).values(),
            key=lambda p: p.k,
        )
        if p.n > 0
    ]
    summary = {
        "run_id": run_id,
        "plan_sha": plan_sha,
        "status": status,
        "issues": len(proposal.issues),
        "comment_ids": ids,
        "updated_at": store.now(),
    }
    ledger_op = outbox.enqueue(
        "merge_ledger",
        {"repo": repo, "issue": control_issue, "key": LEDGER_KEY, "value": summary},
        coalesce_key=f"proposal-ledger:{repo}#{control_issue}",
    )
    await outbox.settle([ledger_op])
    _mirror(store, stored)
    return stored


async def get_proposal(
    store: Store,
    gh: GitHub,
    *,
    repo: str,
    control_issue: int,
    run_id: str,
    plan_sha: str | None = None,
) -> StoredProposal | None:
    """The proposal of ``run_id`` for ``plan_sha`` (the newest one without ``plan_sha``): the mirror, else GitHub.

    A proposal found on GitHub refills the mirror. None when neither has it.
    """
    row = get_proposal_row(store, run_id, plan_sha)
    stored = _from_row(store, row) if row is not None else None
    if stored is not None:
        return stored
    found = [
        s
        for s in read_proposals(await gh.comments(repo, control_issue))
        if s.run_id == run_id and (plan_sha is None or s.plan_sha == plan_sha)
    ]
    if not found:
        return None
    stored = found[-1]
    _mirror(store, stored)
    return stored


async def find_planner_draft(
    gh: GitHub, *, repo: str, control_issue: int, plan_sha: str
) -> StoredProposal | None:
    """The draft that the Planner API keeps for ``plan_sha``, from any of its runs, read from GitHub.

    The Planner API stores a draft under its own run id (``<repo>#planner-<token>``) in this same store. A draft
    that passed validation (``ready``, or already being / done being created) is what the user chose to build;
    a placeholder or invalid one is not. When there are several, the most edited one wins (``rev``), then the
    newest. None when there is none.
    """
    prefix = f"{repo}{PLANNER_RUN_INFIX}"
    found = [
        s
        for s in read_proposals(await gh.comments(repo, control_issue))
        if s.run_id.startswith(prefix) and s.plan_sha == plan_sha and s.status in DRAFT_STATUSES
    ]
    if not found:
        return None
    return max(enumerate(found), key=lambda item: (item[1].rev, item[0]))[1]


async def update_proposal(
    store: Store,
    outbox: Outbox,
    *,
    repo: str,
    control_issue: int,
    run_id: str,
    plan_sha: str,
    proposal: ProposedPlan | None = None,
    status: str | None = None,
    rev: int | None = None,
    meta: dict[str, Any] | None = None,
) -> StoredProposal:
    """Change the proposal, its status, ``rev`` and/or ``meta`` (replaced as a whole), GitHub first like ``save_proposal``.

    Raises ``KeyError`` when there is no proposal for ``(run_id, plan_sha)``.
    """
    current = await get_proposal(
        store, outbox.gh, repo=repo, control_issue=control_issue, run_id=run_id, plan_sha=plan_sha
    )
    if current is None:
        raise KeyError(f"no proposal for run {run_id} and plan {plan_sha}")
    return await save_proposal(
        store,
        outbox,
        repo=repo,
        control_issue=control_issue,
        run_id=run_id,
        plan_sha=plan_sha,
        proposal=proposal if proposal is not None else current.proposal,
        status=status if status is not None else current.status,
        rev=rev if rev is not None else current.rev,
        meta=meta if meta is not None else current.meta,
    )


async def restore_proposal_mirror(gh: GitHub, store: Store, repo: str, control_issue: int) -> int:
    """Rebuild the mirror of ``repo`` from the proposal comments (``harness sync``); returns the proposals found."""
    prefix = f"{repo}#"
    found = [s for s in read_proposals(await gh.comments(repo, control_issue)) if s.run_id.startswith(prefix)]
    with store.tx():
        delete_proposal_rows_of_repo(store, repo)
        ext_prefix = f"proposal:ext:{repo}#"
        store.execute("DELETE FROM meta WHERE substr(key, 1, ?) = ?", (len(ext_prefix), ext_prefix))
        for stored in found:
            _mirror(store, stored)
    return len(found)
