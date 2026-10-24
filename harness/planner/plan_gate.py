"""The plan gate: is the campaign's plan approved, and by whom?

GitHub is the state store. An approval is a record under the ``plan`` key of the control issue's ledger comment::

    {"kind": "file"|"issue", "path": ..., "issue": ..., "ref": ..., "plan_sha": ..., "title": ...,
     "approved_by": login, "via": "label"|"pr"|"cli", "approved_at": ts, "label_cursor": event id}

The plan text is never copied there, only its hash, so a large plan cannot blow the ledger size cap. The record is
written through the outbox (``merge_ledger``), which also mirrors it into the local store once GitHub has it;
``harness sync`` rebuilds the mirror from the ledger. :func:`check_plan_approval` never writes anything.

Approval signals (all end in the same record, and only the repo owner or an allowlisted login counts):

* the label ``harness:plan-approved`` on the control issue, added after the plan's last change;
* for a file plan, the merge of the pull request that last modified the file;
* ``harness plan approve`` (adds the label and records the approval).

A record whose ``plan_sha`` differs from the plan's current sha is ``STALE``: the gate reopens, nothing that was
already created is cancelled. A label event is used up by the record it produced (``label_cursor``), so after the
plan changes the label has to be added again (remove and re-add). A merged PR that modifies the file later is a new
signal for the new text. Slash command approval (``/harness approve plan``) is not supported: the command poller
only watches issues with active runs and ``approve`` takes a free-form note there.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from harness.github.client import GitHub, GitHubError
from harness.github.commands import allowed_logins
from harness.github.labels import HARNESS_LABELS
from harness.github.ledger import find_ledger, parse_ledger, render_event
from harness.github.outbox import Outbox, ledger_mirror_key
from harness.planner.plan_doc import PlanDoc
from harness.secrets import redact
from harness.store.db import Store
from harness.store.repo import append_event, get_meta, set_meta

APPROVED_LABEL = "harness:plan-approved"
NEEDS_YOU_LABEL = "harness:needs-you"
PLAN_KEY = "plan"
assert APPROVED_LABEL in HARNESS_LABELS and NEEDS_YOU_LABEL in HARNESS_LABELS

PlanVia = Literal["label", "pr", "cli"]


class PlanApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    STALE = "stale"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PlanApproval:
    status: PlanApprovalStatus
    approved_sha: str | None = None
    approved_by: str | None = None
    via: PlanVia | None = None
    note: str | None = None
    # True when an approval signal was just seen that is not in the ledger yet: call record_plan_approval.
    fresh: bool = False
    label_cursor: int = 0


def plan_gate_outcome(approval: PlanApproval) -> Literal["approved", "rejected"] | None:
    """The gate port for an approval, or None while the gate must keep waiting."""
    if approval.status is PlanApprovalStatus.APPROVED:
        return "approved"
    if approval.status is PlanApprovalStatus.REJECTED:
        return "rejected"
    return None


# ---- the recorded approval (ledger on GitHub, mirrored in the store) ------------------------------------


def _valid_record(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and isinstance(value.get("plan_sha"), str) and value["plan_sha"]:
        return value
    return None


def mirrored_plan_record(store: Store, repo: str, control_issue: int) -> dict[str, Any] | None:
    raw = get_meta(store, ledger_mirror_key(repo, control_issue, PLAN_KEY))
    if not raw:
        return None
    try:
        return _valid_record(json.loads(raw))
    except ValueError:
        return None


async def ledger_plan_record(gh: GitHub, repo: str, control_issue: int) -> dict[str, Any] | None:
    """The approval record in the control issue's ledger on GitHub (LedgerCorrupt if the ledger is unreadable)."""
    comment = find_ledger(await gh.comments(repo, control_issue))
    snapshot = parse_ledger(comment.get("body") or "") if comment else None
    return _valid_record((snapshot or {}).get(PLAN_KEY))


async def load_plan_record(
    gh: GitHub, repo: str, control_issue: int, store: Store | None = None
) -> dict[str, Any] | None:
    """The mirror when there is one; otherwise the ledger, which then refills the mirror."""
    if store is not None:
        record = mirrored_plan_record(store, repo, control_issue)
        if record is not None:
            return record
    record = await ledger_plan_record(gh, repo, control_issue)
    if record is not None and store is not None:
        set_meta(store, ledger_mirror_key(repo, control_issue, PLAN_KEY), json.dumps(record, sort_keys=True))
    return record


def _same_source(record: dict[str, Any], plan: PlanDoc) -> bool:
    return (
        record.get("kind") == plan.kind
        and record.get("path") == plan.path
        and record.get("issue") == plan.issue
    )


# ---- approval signals ----------------------------------------------------------------------------------


@dataclass
class _LabelState:
    current: dict[str, Any] | None  # the "labeled" event that put the label on, while it is still on
    max_id: int  # highest "labeled" event id ever seen


async def _label_state(gh: GitHub, repo: str, control_issue: int) -> _LabelState:
    current: dict[str, Any] | None = None
    max_id = 0
    async for event in gh.paginate(f"repos/{repo}/issues/{control_issue}/events"):
        if not isinstance(event, dict) or (event.get("label") or {}).get("name") != APPROVED_LABEL:
            continue
        if event.get("event") == "labeled":
            current = event
            max_id = max(max_id, int(event.get("id") or 0))
        elif event.get("event") == "unlabeled":
            current = None
    return _LabelState(current, max_id)


async def label_cursor(gh: GitHub, repo: str, control_issue: int) -> int:
    """The id of the newest ``harness:plan-approved`` labeled event (0 if there is none)."""
    return (await _label_state(gh, repo, control_issue)).max_id


async def _last_commit(gh: GitHub, repo: str, plan: PlanDoc) -> dict[str, Any] | None:
    params: dict[str, Any] = {"path": plan.path, "per_page": 1}
    if plan.ref:
        params["sha"] = plan.ref
    commits = (await gh.request("GET", f"repos/{repo}/commits", params=params)).body
    return commits[0] if isinstance(commits, list) and commits else None


def _commit_date(commit: dict[str, Any]) -> str:
    return str(((commit.get("commit") or {}).get("committer") or {}).get("date") or "")


async def _merged_pr(gh: GitHub, repo: str, commit: dict[str, Any]) -> dict[str, Any] | None:
    """The merged PR behind the commit that last modified the plan file, with its merger."""
    try:
        pulls = await gh.get(f"repos/{repo}/commits/{commit['sha']}/pulls")
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    merged = (
        [p for p in pulls if isinstance(p, dict) and p.get("merged_at")] if isinstance(pulls, list) else []
    )
    if not merged:
        return None
    chosen = next((p for p in merged if p.get("merge_commit_sha") == commit["sha"]), merged[0])
    return await gh.pull(repo, int(chosen["number"]))


def _login(user: Any) -> str:
    return str((user or {}).get("login") or "")


async def check_plan_approval(
    gh: GitHub,
    *,
    repo: str,
    control_issue: int,
    plan: PlanDoc,
    owner_allowlist: list[str] | tuple[str, ...] = (),
    store: Store | None = None,
) -> PlanApproval:
    """Read-only: the approval state of ``plan``. With ``store``, the mirror answers before GitHub is asked."""
    allowed = allowed_logins(repo, owner_allowlist) | {repo.split("/")[0].lower()}
    record = await load_plan_record(gh, repo, control_issue, store)
    if record is not None and _same_source(record, plan) and record["plan_sha"] == plan.sha:
        return PlanApproval(
            PlanApprovalStatus.APPROVED,
            approved_sha=plan.sha,
            approved_by=record.get("approved_by"),
            via=record.get("via"),
            label_cursor=int(record.get("label_cursor") or 0),
        )

    cursor = int(record.get("label_cursor") or 0) if record else 0
    notes: list[str] = []
    labels = await _label_state(gh, repo, control_issue)
    commit = await _last_commit(gh, repo, plan) if plan.kind == "file" else None

    event = labels.current
    if event is not None and int(event.get("id") or 0) > cursor:
        actor = _login(event.get("actor"))
        if actor.lower() not in allowed:
            notes.append(
                f"label {APPROVED_LABEL} was added by @{actor or '?'}, who is not allowed to approve plans"
            )
        elif commit is not None and _commit_date(commit) > str(event.get("created_at") or ""):
            notes.append(f"the plan changed after label {APPROVED_LABEL} was added: add it again")
        else:
            return PlanApproval(
                PlanApprovalStatus.APPROVED,
                approved_sha=plan.sha,
                approved_by=actor,
                via="label",
                fresh=True,
                label_cursor=int(event["id"]),
            )

    if commit is not None:
        pull = await _merged_pr(gh, repo, commit)
        if pull is not None:
            merger = _login(pull.get("merged_by"))
            if merger.lower() in allowed:
                return PlanApproval(
                    PlanApprovalStatus.APPROVED,
                    approved_sha=plan.sha,
                    approved_by=merger,
                    via="pr",
                    fresh=True,
                    label_cursor=labels.max_id,
                )
            notes.append(
                f"PR #{pull['number']} was merged by @{merger or '?'}, who is not allowed to approve plans"
            )

    note = "; ".join(notes) or None
    if record is not None:
        return PlanApproval(
            PlanApprovalStatus.STALE,
            approved_sha=record["plan_sha"],
            approved_by=record.get("approved_by"),
            via=record.get("via"),
            note=note,
            label_cursor=cursor,
        )
    return PlanApproval(PlanApprovalStatus.PENDING, note=note, label_cursor=cursor)


# ---- writing --------------------------------------------------------------------------------------------


def record_plan_approval(
    store: Store,
    outbox: Outbox,
    *,
    repo: str,
    control_issue: int,
    plan: PlanDoc,
    by: str,
    via: PlanVia,
    label_cursor: int = 0,
) -> dict[str, Any]:
    """Queue the approval record for the control issue's ledger (and drop ``harness:needs-you``).

    GitHub first: nothing is mirrored into the store here. The outbox mirrors the record when the ledger write
    succeeds, and ``harness sync`` restores it from the ledger. Returns the record.
    """
    record: dict[str, Any] = {
        "kind": plan.kind,
        "path": plan.path,
        "issue": plan.issue,
        "ref": plan.ref,
        "plan_sha": plan.sha,
        "title": redact(plan.title)[:200],
        "approved_by": by,
        "via": via,
        "approved_at": store.now(),
        "label_cursor": label_cursor,
    }
    with store.tx():
        outbox.enqueue(
            "merge_ledger",
            {"repo": repo, "issue": control_issue, "key": PLAN_KEY, "value": record},
            coalesce_key=f"plan-approval:{repo}#{control_issue}",
        )
        outbox.enqueue(
            "set_labels",
            {"repo": repo, "issue": control_issue, "add": [], "remove": [NEEDS_YOU_LABEL]},
            coalesce_key=f"plan-approval-labels:{repo}#{control_issue}",
        )
        append_event(
            store,
            kind="plan_approval_recorded",
            data={"plan_sha": plan.sha, "approved_by": by, "via": via, "source": plan.source},
            repo=repo,
            issue=control_issue,
        )
    return record


def _request_marker(sha: str) -> str:
    return f"<!-- harness:plan-request sha={sha} -->"


def _request_meta_key(repo: str, control_issue: int) -> str:
    return f"plan_request:{repo}#{control_issue}"


async def request_plan_approval(
    gh: GitHub, outbox: Outbox, *, repo: str, control_issue: int, plan: PlanDoc
) -> bool:
    """Ask for approval of this version of the plan: one event comment and the needs-you label, once per sha.

    Returns True when it queued the writes, False when this sha was already requested (the store remembers the
    last requested sha; a request comment already on the control issue counts too, so a wiped store does not
    post twice).
    """
    store = outbox.store
    key = _request_meta_key(repo, control_issue)
    if get_meta(store, key) == plan.sha:
        return False
    marker = _request_marker(plan.sha)
    if any(marker in (c.get("body") or "") for c in await gh.comments(repo, control_issue)):
        set_meta(store, key, plan.sha)
        return False
    bullets = [
        f"plan: {plan.source} (`{plan.sha[:12]}`){f' on {plan.ref}' if plan.ref else ''}",
        f"title: {redact(plan.title)}",
        f"approve: add the label `{APPROVED_LABEL}`, or run `harness plan approve`"
        + (", or merge the PR that changes the file" if plan.kind == "file" else ""),
    ]
    body = render_event(store.now()[:10], control_issue, "plan approval requested", bullets) + f"\n\n{marker}"
    with store.tx():
        outbox.enqueue(
            "add_comment",
            {"repo": repo, "issue": control_issue, "body": body},
            coalesce_key=f"plan-request:{repo}#{control_issue}:{plan.sha}",
        )
        outbox.enqueue(
            "set_labels",
            {"repo": repo, "issue": control_issue, "add": [NEEDS_YOU_LABEL], "remove": []},
            coalesce_key=f"plan-request-labels:{repo}#{control_issue}:{plan.sha}",
        )
        set_meta(store, key, plan.sha)
    return True
