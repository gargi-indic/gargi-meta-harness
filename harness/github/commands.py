"""Slash commands in issue comments (``/harness approve`` ...) and the shared gate decision API.

Comment bodies are untrusted input: only ``command_users`` (or the repo owner when that list is empty) can act,
every reply is redacted, and the last-seen comment id is persisted in the same transaction as the command's
effects, so a restart never applies a command twice.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from harness.config import RepoConfig
from harness.engine.runner import Runner
from harness.github.client import GitHub
from harness.github.ledger import render_event
from harness.github.outbox import Outbox
from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import (
    GateRow,
    RunRow,
    append_event,
    decide_gate,
    get_gate,
    get_meta,
    get_run,
    list_runs,
    set_meta,
    steps_for_run,
)

Verb = Literal["approve", "reject", "retry", "pause", "resume", "cancel"]
VERBS: tuple[str, ...] = ("approve", "reject", "retry", "pause", "resume", "cancel")
MAX_NOTE_CHARS = 2000
MAX_REPLY_ERROR_CHARS = 500
PREFIX = "/harness "
ACTIVE_RUN_STATUSES = ("running", "needs-you", "paused")
_NODE_RE = re.compile(r"[a-z][a-z0-9_]*")
_AGENT_RE = re.compile(r"[A-Za-z0-9_.-]+")
_ISSUE_REF_RE = re.compile(r"#?(\d+)")
_RETRY_OUTCOMES = frozenset({"failed", "fail", "escalate", "escalated", "timeout"})


@dataclass
class Command:
    verb: Verb
    note: str | None = None
    node: str | None = None
    agent: str | None = None
    to: str | None = None


# ---- parsing -------------------------------------------------------------------------------------------


def _first_line(body: str) -> str | None:
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return None


def parse_command(body: str) -> Command | None:
    """Parse the first non-empty line of a comment; None when it is not a well-formed ``/harness`` command."""
    if not isinstance(body, str):
        return None
    line = _first_line(body)
    if line is None or not line.startswith(PREFIX):
        return None
    parts = line[len(PREFIX) :].strip().split(None, 1)
    if not parts:
        return None
    verb = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if verb == "approve":
        return Command("approve", note=arg[:MAX_NOTE_CHARS] or None)
    if verb == "reject":
        return Command("reject", note=arg[:MAX_NOTE_CHARS]) if arg else None
    if verb in ("pause", "cancel"):
        return Command(verb) if not arg else None
    if verb == "resume":
        if not arg:
            return Command("resume")
        tokens = arg.split()
        if len(tokens) == 2 and tokens[0] == "--to" and _NODE_RE.fullmatch(tokens[1]):
            return Command("resume", to=tokens[1])
        return None
    if verb == "retry":
        node: str | None = None
        agent: str | None = None
        for token in arg.split():
            if token.startswith("agent="):
                value = token[len("agent=") :]
                if agent is not None or not _AGENT_RE.fullmatch(value):
                    return None
                agent = value
            elif node is None and _NODE_RE.fullmatch(token):
                node = token
            else:
                return None
        return Command("retry", node=node, agent=agent)
    return None


# ---- decision API (shared by the CLI and the slash commands) -------------------------------------------


def decide(
    store: Store,
    gate_id: str,
    status: Literal["approved", "rejected"],
    note: str | None,
    decided_by: str | None,
) -> GateRow:
    """Record a decision and a ``gate_decided`` event. KeyError: unknown gate; ValueError: not pending."""
    with store.tx():
        before = get_gate(store, gate_id)
        decide_gate(store, gate_id, status, note, decided_by)
        run = get_run(store, before.run_id) if before is not None else None
        append_event(
            store,
            kind="gate_decided",
            data={"gate_id": gate_id, "status": status, "note": note, "decided_by": decided_by},
            repo=run.repo if run else None,
            issue=run.issue if run else None,
            run_id=before.run_id if before else None,
        )
    gate = get_gate(store, gate_id)
    assert gate is not None
    return gate


def resolve_gate(store: Store, repo: str | None, ref: str) -> GateRow:
    """``ref`` is a gate id, or an issue number (``42`` / ``#42``): the single pending gate of its active run."""
    ref = ref.strip()
    issue_match = _ISSUE_REF_RE.fullmatch(ref)
    if issue_match is None:
        gate = get_gate(store, ref)
        if gate is None:
            raise ValueError(f"no gate {ref!r}")
        return gate
    if not repo:
        raise ValueError("an issue reference needs a repo")
    issue = int(issue_match.group(1))
    found: list[GateRow] = []
    for run in list_runs(store):
        if run.repo != repo or run.issue != issue or run.status not in ACTIVE_RUN_STATUSES:
            continue
        waiting = {s.id for s in steps_for_run(store, run.id) if s.status == "waiting"}
        for row in store.query("SELECT id FROM gates WHERE run_id = ? AND status = 'pending'", (run.id,)):
            gate = get_gate(store, row["id"])
            if gate is not None and gate.step_id in waiting:
                found.append(gate)
    if not found:
        raise ValueError(f"no pending gate on {repo}#{issue}")
    if len(found) > 1:
        ids = ", ".join(g.id for g in found)
        raise ValueError(f"{len(found)} pending gates on {repo}#{issue}: {ids}")
    return found[0]


def default_retry_node(store: Store, run_id: str) -> str:
    """The node of the run's latest failed or escalated step."""
    for step in reversed(steps_for_run(store, run_id)):
        if step.status == "failed" or step.outcome in _RETRY_OUTCOMES:
            return step.node_id
    raise ValueError(f"run {run_id} has no failed or escalated node to retry")


# ---- polling comments ----------------------------------------------------------------------------------


def _error_text(exc: Exception) -> str:
    text = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    text = " ".join(redact(text).split())
    if len(text) > MAX_REPLY_ERROR_CHARS:
        text = text[: MAX_REPLY_ERROR_CHARS - 3] + "..."
    return text or type(exc).__name__


def _ack(cmd: Command, login: str) -> str:
    if cmd.verb == "approve":
        return f"Approved by @{login}" + (f": {cmd.note}" if cmd.note else "")
    if cmd.verb == "reject":
        return f"Rejected by @{login}: {cmd.note}"
    if cmd.verb == "pause":
        return f"Paused by @{login}"
    if cmd.verb == "cancel":
        return f"Cancelled by @{login}"
    if cmd.verb == "resume":
        return f"Resumed by @{login}" + (f" at `{cmd.to}`" if cmd.to else "")
    return f"Retry requested by @{login}" + (f" for `{cmd.node}`" if cmd.node else "")


def _apply_sync(
    store: Store, runner: Runner, repo: str, issue: int, run_id: str, cmd: Command, login: str
) -> None:
    if cmd.verb in ("approve", "reject"):
        gate = resolve_gate(store, repo, str(issue))
        decide(store, gate.id, "approved" if cmd.verb == "approve" else "rejected", cmd.note, login)
    elif cmd.verb == "pause":
        runner.pause(run_id)
    elif cmd.verb == "resume":
        runner.resume(run_id, to=cmd.to)
    elif cmd.verb == "retry":
        # agent= is parsed and recorded in the command event; acting on it is M3.
        runner.retry_node(run_id, cmd.node or default_retry_node(store, run_id))


_COMMAND_ERRORS = (KeyError, ValueError, LookupError, RuntimeError)


def _record(
    store: Store,
    outbox: Outbox,
    run: RunRow,
    comment_id: int,
    login: str,
    cmd: Command,
    error: str | None,
) -> None:
    """Event, reply and last-seen marker for one command. Call inside the transaction that applied it."""
    data: dict[str, Any] = {
        "comment_id": comment_id,
        "login": login,
        "verb": cmd.verb,
        "ok": error is None,
        "error": error,
    }
    if cmd.agent:
        data["agent"] = cmd.agent
    append_event(store, kind="command", data=data, repo=run.repo, issue=run.issue, run_id=run.id)
    if error is None:
        event, bullet = "command applied", _ack(cmd, login)
    else:
        event, bullet = "command failed", error
    _reply(outbox, store, run.repo, run.issue, comment_id, event, bullet)
    set_meta(store, _meta_key(run.repo, run.issue), str(comment_id))


def _meta_key(repo: str, issue: int) -> str:
    return f"commands:{repo}#{issue}"


def _reply(
    outbox: Outbox, store: Store, repo: str, issue: int, comment_id: int, event: str, bullet: str
) -> None:
    body = render_event(store.now()[:10], issue, event, [redact(bullet)])
    outbox.enqueue(
        "add_comment",
        {"repo": repo, "issue": issue, "body": body},
        coalesce_key=f"cmd:{comment_id}",
    )


async def _process(
    store: Store, runner: Runner, outbox: Outbox, run: RunRow, comment_id: int, login: str, cmd: Command
) -> bool:
    """Apply one allowed command. Returns True when it succeeded."""
    if cmd.verb == "cancel":
        # Async, and it commits in its own transaction; cancelling twice is a no-op, so a crash here is safe.
        error: str | None = None
        try:
            await runner.cancel(run.id)
        except _COMMAND_ERRORS as exc:
            error = _error_text(exc)
        with store.tx():
            _record(store, outbox, run, comment_id, login, cmd, error)
        return error is None
    # Effects, event, reply and last-seen id commit together, so a crash never re-applies the command.
    with store.tx():
        error = None
        try:
            with store.tx():  # savepoint: a failed command leaves no half-applied state
                _apply_sync(store, runner, run.repo, run.issue, run.id, cmd, login)
        except _COMMAND_ERRORS as exc:
            error = _error_text(exc)
        _record(store, outbox, run, comment_id, login, cmd, error)
    return error is None


async def poll_commands(
    gh: GitHub,
    store: Store,
    repo: str,
    runner: Runner,
    *,
    repo_config: RepoConfig,
    outbox: Outbox,
) -> int:
    """Apply new ``/harness`` comments on the repo's active runs. Returns the number of commands applied."""
    allowed = {u.lower() for u in repo_config.command_users} or {repo.split("/")[0].lower()}
    runs: dict[int, RunRow] = {}
    for candidate in list_runs(store):  # newest first: keep one run per issue
        if candidate.repo == repo and candidate.status in ACTIVE_RUN_STATUSES:
            runs.setdefault(candidate.issue, candidate)

    applied = 0
    for issue in sorted(runs):
        run = runs[issue]
        baseline = run.created_at
        key = _meta_key(repo, issue)
        last_seen = int(get_meta(store, key) or 0)
        comments: list[dict[str, Any]] = []
        async for comment in gh.paginate(f"repos/{repo}/issues/{issue}/comments", since=baseline):
            cid = comment.get("id")
            if not isinstance(cid, int) or cid <= last_seen:
                continue
            if (comment.get("created_at") or "") < baseline:
                continue
            comments.append(comment)
        comments.sort(key=lambda c: c["id"])

        for comment in comments:
            cid = comment["id"]
            cmd = parse_command(comment.get("body") or "")
            if cmd is None:
                set_meta(store, key, str(cid))
                continue
            login = str((comment.get("user") or {}).get("login") or "")
            if not login or login.lower() not in allowed:
                with store.tx():
                    who = f"@{login}" if login else "this user"
                    _reply(
                        outbox,
                        store,
                        repo,
                        issue,
                        cid,
                        "command ignored",
                        f"{who} is not allowed to run harness commands",
                    )
                    append_event(
                        store,
                        kind="command",
                        data={
                            "comment_id": cid,
                            "login": login,
                            "verb": cmd.verb,
                            "ok": False,
                            "error": "not allowed",
                        },
                        repo=repo,
                        issue=issue,
                        run_id=run.id,
                    )
                    set_meta(store, key, str(cid))
                continue
            if await _process(store, runner, outbox, run, cid, login, cmd):
                applied += 1
    return applied
