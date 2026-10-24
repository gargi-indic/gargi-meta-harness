"""Turn an approved ``ProposedPlan`` into GitHub state: an epic, its sub-issues, ``blocked_by`` links and labels.

GitHub is the state store, so ``materialize`` can be run again from nothing but the plan and what is on GitHub
(the local store only mirrors). Every issue it creates ends with ``<!-- harness:planned run=<run_key> key=<key> -->``
(``harness.github.planned``); before creating anything it looks the planned issues up by that marker, so an issue
that was created but never recorded (a crash between the two) is found again and not created twice. Linking a
sub-issue or a dependency reads what is already linked first, so every link exists exactly once. A complete run
that is repeated makes no write call at all.

Every content write goes through the outbox (``create_issue``, ``add_sub_issue``, ``add_blocked_by``,
``ensure_label``, ``edit_issue_body`` and ``add_comment`` ops), so the throttle and the coalescing keys apply.
This module only reads from the GitHub client. It queues the ops and then ``Outbox.settle`` s them, which drains
through ``drain_once`` and waits when the per-minute window is full; the number and id of a created issue come
from ``Outbox.result``.

``run_key`` names one materialization of one plan. Callers pass it: the same value for the same plan, a new one
for a re-plan. ``plan_run_key`` derives a stable one from the plan's content, which is what ``harness plan
create`` uses by default. The key is written into the marker, so it is limited to ``A-Za-z0-9._:-``.
"""

import dataclasses
import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from harness.adapters.base import Capabilities
from harness.github.client import GitHub
from harness.github.control_table import ControlRow, Status, parse_table
from harness.github.labels import HARNESS_LABELS
from harness.github.ledger import render_event
from harness.github.outbox import Outbox
from harness.github.planned import EPIC_KEY, check_run_key, index_planned, neutralise, planned_marker
from harness.planner.proposal import (
    DEFAULT_MAX_ISSUES,
    ProposalIssue,
    ProposedIssue,
    ProposedPlan,
    validate_proposal,
)
from harness.store.db import Store

EPIC_LABEL = "epic"
_EPIC_STYLE = ("3e4b9e", "Epic created from a plan")
_AGENT_STYLE = ("5319e7", "Agent that works on the issue")
_PLAYBOOK_STYLE = ("c5def5", "Playbook that runs the issue")
_OTHER_STYLE = ("ededed", "")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


class MaterializeError(RuntimeError):
    """The plan was refused, or an outbox op did not go through (the run can simply be repeated)."""

    def __init__(self, message: str, problems: list[ProposalIssue] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


@dataclass(frozen=True)
class MaterializeContext:
    """What ``materialize`` works with.

    ``agents`` and ``playbooks`` are the names that exist; ``validate_proposal`` checks membership against them.
    Left as None, the names the plan itself uses are accepted and only its structure is checked (duplicate
    keys, unknown dependencies, cycles, sizes). ``max_issues`` is the plan size limit.
    """

    repo: str
    gh: GitHub
    outbox: Outbox
    store: Store
    control_issue: int | None
    run_key: str
    label_prefix: str = "harness"
    agents: Collection[str] | None = None
    playbooks: Collection[str] | None = None
    max_issues: int = DEFAULT_MAX_ISSUES


class MaterializeResult(BaseModel):
    """``issues`` maps proposal key to issue number. With ``dry_run`` it holds only issues that already exist
    and ``epic_number`` is 0 when there is no epic yet; ``created`` and ``links_created`` then say what would be
    done, and ``labels_ensured`` the labels that would be created."""

    epic_number: int
    issues: dict[str, int]
    created: list[str]
    reused: list[str]
    links_created: int
    labels_ensured: list[str]


def plan_run_key(plan: ProposedPlan) -> str:
    """A run key that depends on the plan's content only: ``plan-`` and 12 hex digits of its sha256."""
    digest = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
    return f"plan-{digest[:12]}"


def topological_order(plan: ProposedPlan) -> list[str]:
    """Issue keys with every blocker before the issues it blocks; plan order breaks ties. Raises on a cycle."""
    deps = {issue.key: list(dict.fromkeys(issue.blocked_by)) for issue in plan.issues}
    placed: set[str] = set()
    order: list[str] = []
    while len(order) < len(deps):
        batch = [k for k, d in deps.items() if k not in placed and all(b in placed for b in d)]
        if not batch:
            raise MaterializeError("the plan has a dependency cycle")
        order.extend(batch)
        placed.update(batch)
    return order


def _drop_section(body: str, title: str) -> str:
    """``body`` without the markdown section titled ``title`` (up to the next heading of the same or a higher level)."""
    kept: list[str] = []
    level: int | None = None
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        found = None if in_fence else _HEADING.match(line)
        if level is not None:
            if found and len(found.group(1)) <= level:
                level = None
            else:
                continue
        if found and found.group(2).strip().lower() == title:
            level = len(found.group(1))
            continue
        kept.append(line)
    return "\n".join(kept)


def _clean_path(path: str) -> str:
    clean = path.strip(" \t`")
    if not clean or "," in clean or "\n" in clean or "\r" in clean:
        raise MaterializeError(f"path hint {path!r} cannot be written to a 'paths:' line")
    return clean


def render_issue_body(issue: ProposedIssue, run_key: str) -> str:
    """The body GitHub gets: the proposed text, the acceptance criteria as ``- [ ]`` boxes, ``paths:``, the marker.

    The ``acceptance`` list is the source of the criteria, so a section of that name in the proposed text is
    replaced. The ``paths:`` line is what ``harness.engine.dispatcher.parse_path_hints`` reads.
    """
    parts = [_drop_section(neutralise(issue.body), "acceptance criteria").rstrip()]
    boxes = [f"- [ ] {' '.join(item.split())}" for item in issue.acceptance if item.strip()]
    parts.append("## Acceptance criteria\n\n" + "\n".join(boxes))
    if issue.paths:
        parts.append("paths: " + ", ".join(_clean_path(p) for p in issue.paths))
    parts.append(planned_marker(run_key, issue.key))
    return "\n\n".join(p for p in parts if p)


def render_epic_body(plan: ProposedPlan, run_key: str) -> str:
    return f"{neutralise(plan.epic_body).rstrip()}\n\n{planned_marker(run_key, EPIC_KEY)}"


def _issue_labels(issue: ProposedIssue, prefix: str) -> list[str]:
    state = "blocked" if issue.blocked_by else "ready"
    labels = [f"{prefix}:planned", f"{prefix}:{state}", f"agent:{issue.agent}", f"playbook:{issue.playbook}"]
    return list(dict.fromkeys([*labels, *issue.labels]))


def _label_styles(plan: ProposedPlan, prefix: str) -> dict[str, tuple[str, str]]:
    """Every label the plan needs, with (colour, description), in a stable order."""
    styles: dict[str, tuple[str, str]] = {EPIC_LABEL: _EPIC_STYLE}
    for suffix in ("planned", "ready", "blocked"):
        styles[f"{prefix}:{suffix}"] = HARNESS_LABELS.get(f"harness:{suffix}", _OTHER_STYLE)
    for issue in plan.issues:
        styles.setdefault(f"agent:{issue.agent}", _AGENT_STYLE)
        styles.setdefault(f"playbook:{issue.playbook}", _PLAYBOOK_STYLE)
        for label in issue.labels:
            styles.setdefault(label, _OTHER_STYLE)
    return styles


def _validate(plan: ProposedPlan, ctx: MaterializeContext) -> None:
    agents = set(ctx.agents) if ctx.agents is not None else {i.agent for i in plan.issues}
    playbooks = set(ctx.playbooks) if ctx.playbooks is not None else {i.playbook for i in plan.issues}
    everything = Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=True)
    problems = validate_proposal(
        plan,
        agents=dict.fromkeys(agents, everything),
        playbooks=playbooks,
        max_issues=ctx.max_issues,
    )
    if problems:
        shown = "; ".join(f"[{p.code}]{' ' + p.key + ':' if p.key else ''} {p.message}" for p in problems[:5])
        more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
        raise MaterializeError(f"the plan is not valid: {shown}{more}", problems)


@dataclass(frozen=True)
class _Link:
    """One link still to make: ``blocker`` is None for "attach as a sub-issue of the epic"."""

    key: str
    blocker: str | None = None


async def _missing_links(
    ctx: MaterializeContext,
    plan: ProposedPlan,
    order: list[str],
    ids: dict[str, int],
    numbers: dict[str, int],
    fresh: set[str],
) -> list[_Link]:
    """The links that GitHub does not have yet.

    A ``fresh`` issue is one that this run creates (or, in a dry run, would create): it has no links and no id yet.
    """
    by_key = {issue.key: issue for issue in plan.issues}
    attached: set[int] = set()
    if EPIC_KEY not in fresh:
        attached = {sub["id"] for sub in await ctx.gh.sub_issues(ctx.repo, numbers[EPIC_KEY])}
    links: list[_Link] = []
    for key in order:
        if key in fresh or ids[key] not in attached:
            links.append(_Link(key))
        blockers = list(dict.fromkeys(by_key[key].blocked_by))
        if not blockers:
            continue
        have: set[int] = set()
        if key not in fresh:
            have = {dep["id"] for dep in await ctx.gh.blocked_by(ctx.repo, numbers[key])}
        links.extend(_Link(key, b) for b in blockers if key in fresh or b in fresh or ids[b] not in have)
    return links


def _enqueue(ctx: MaterializeContext, op: Any, payload: dict[str, Any], coalesce_key: str) -> int:
    return ctx.outbox.enqueue(op, {"repo": ctx.repo, **payload}, coalesce_key=coalesce_key)


async def materialize(
    plan: ProposedPlan, ctx: MaterializeContext, *, dry_run: bool = False
) -> MaterializeResult:
    """Create the epic, the sub-issues, their links and labels, and the control-table rows; idempotent.

    With ``dry_run`` it only reads: no write call and no outbox op. Raises ``MaterializeError`` for an invalid
    plan or a failed op; the run can then be repeated and continues where it stopped.
    """
    check_run_key(ctx.run_key)
    _validate(plan, ctx)
    order = topological_order(plan)
    by_key = {issue.key: issue for issue in plan.issues}
    prefix = ctx.label_prefix
    for issue in plan.issues:
        render_issue_body(issue, ctx.run_key)  # refuse unusable path hints before anything is written

    existing = index_planned(
        await ctx.gh.issues(ctx.repo, state="all", labels=f"{prefix}:planned"), ctx.run_key
    )
    have_labels = {str(label["name"]).lower() for label in await ctx.gh.labels(ctx.repo)}
    styles = _label_styles(plan, prefix)
    missing_labels = [name for name in styles if name.lower() not in have_labels]

    numbers = {k: int(v["number"]) for k, v in existing.items()}
    ids = {k: int(v["id"]) for k, v in existing.items()}
    keys = [EPIC_KEY, *order]
    to_create = [k for k in keys if k not in existing]
    created, reused = list(to_create), [k for k in keys if k in existing]
    fresh = set(to_create)

    if dry_run:
        links = await _missing_links(ctx, plan, order, ids, numbers, fresh)
        return MaterializeResult(
            epic_number=numbers.get(EPIC_KEY, 0),
            issues={k: n for k, n in numbers.items() if k != EPIC_KEY},
            created=[k for k in created if k != EPIC_KEY],
            reused=[k for k in reused if k != EPIC_KEY],
            links_created=len(links),
            labels_ensured=missing_labels,
        )

    label_ops = [
        _enqueue(
            ctx,
            "ensure_label",
            {"name": name, "color": styles[name][0], "description": styles[name][1]},
            f"planned-label:{ctx.repo}:{name}",
        )
        for name in missing_labels
    ]
    create_ops: dict[str, int] = {}
    for key in to_create:
        if key == EPIC_KEY:
            title, body = plan.epic_title, render_epic_body(plan, ctx.run_key)
            labels = [EPIC_LABEL, f"{prefix}:planned"]
        else:
            issue = by_key[key]
            title, body = issue.title, render_issue_body(issue, ctx.run_key)
            labels = _issue_labels(issue, prefix)
        create_ops[key] = _enqueue(
            ctx,
            "create_issue",
            {
                "title": title,
                "body": body,
                "labels": labels,
                "marker": planned_marker(ctx.run_key, key),
                "dedupe_label": f"{prefix}:planned",
            },
            f"planned-issue:{ctx.repo}:{ctx.run_key}:{key}",
        )
    await ctx.outbox.settle([*label_ops, *create_ops.values()])

    await _record_created(ctx, create_ops, numbers, ids, fresh, created, reused)

    links = await _missing_links(ctx, plan, order, ids, numbers, fresh)
    epic = numbers[EPIC_KEY]
    link_ops: list[int] = []
    for link in links:
        if link.blocker is None:
            link_ops.append(
                _enqueue(
                    ctx,
                    "add_sub_issue",
                    {"issue": epic, "sub_issue_id": ids[link.key]},
                    f"planned-sub:{ctx.repo}:{epic}:{ids[link.key]}",
                )
            )
        else:
            link_ops.append(
                _enqueue(
                    ctx,
                    "add_blocked_by",
                    {"issue": numbers[link.key], "blocker_id": ids[link.blocker]},
                    f"planned-dep:{ctx.repo}:{numbers[link.key]}:{ids[link.blocker]}",
                )
            )
    await ctx.outbox.settle(link_ops)
    links_made = sum(1 for op_id in link_ops if (ctx.outbox.result(op_id) or {}).get("added"))

    if ctx.control_issue is not None:
        await _record_in_control_issue(ctx, plan, order, numbers, epic)

    return MaterializeResult(
        epic_number=epic,
        issues={k: numbers[k] for k in order},
        created=[k for k in created if k != EPIC_KEY],
        reused=[k for k in reused if k != EPIC_KEY],
        links_created=links_made,
        labels_ensured=missing_labels,
    )


async def _record_created(
    ctx: MaterializeContext,
    create_ops: dict[str, int],
    numbers: dict[str, int],
    ids: dict[str, int],
    fresh: set[str],
    created: list[str],
    reused: list[str],
) -> None:
    """Fill ``numbers`` and ``ids`` for the issues just created, from the ops' results or else from GitHub."""
    lost: list[str] = []
    for key, op_id in create_ops.items():
        result = ctx.outbox.result(op_id)
        if result is None:
            lost.append(key)
            continue
        numbers[key], ids[key] = int(result["number"]), int(result["id"])
        if result.get("existing"):
            # Found by the op itself (a send that was repeated after a crash): it may already be linked.
            fresh.discard(key)
            created.remove(key)
            reused.append(key)
    if lost:
        found = index_planned(
            await ctx.gh.issues(ctx.repo, state="all", labels=f"{ctx.label_prefix}:planned"), ctx.run_key
        )
        for key in lost:
            if key not in found:
                raise MaterializeError(f"issue {key!r} was not found on GitHub after it was created")
            numbers[key], ids[key] = int(found[key]["number"]), int(found[key]["id"])
            fresh.discard(key)
            created.remove(key)
            reused.append(key)


async def _record_in_control_issue(
    ctx: MaterializeContext, plan: ProposedPlan, order: list[str], numbers: dict[str, int], epic: int
) -> None:
    """One control-table row per sub-issue and one event comment, each only if the control issue lacks it."""
    control = ctx.control_issue
    assert control is not None
    by_key = {issue.key: issue for issue in plan.issues}
    body = (await ctx.gh.issue(ctx.repo, control)).get("body") or ""
    try:
        rows = {row.issue for row in parse_table(body)}
    except ValueError as exc:
        raise MaterializeError(f"the control issue #{control} has a malformed table: {exc}") from exc
    ops: list[int] = []
    for key in order:
        if numbers[key] in rows:
            continue
        issue = by_key[key]
        status: Status = "blocked" if issue.blocked_by else "ready"
        row = ControlRow(issue=numbers[key], title=issue.title, status=status, agent=issue.agent)
        ops.append(
            _enqueue(
                ctx,
                "edit_issue_body",
                {"issue": control, "row": dataclasses.asdict(row)},
                f"planned-row:{ctx.repo}#{control}:{numbers[key]}",
            )
        )
    announced = f"· #{epic} planned.**"
    if not any(announced in (c.get("body") or "") for c in await ctx.gh.comments(ctx.repo, control)):
        bullets = [f"`{key}` -> #{numbers[key]}" for key in order]
        text = render_event(ctx.store.now()[:10], epic, "planned", bullets)
        ops.append(
            _enqueue(
                ctx,
                "add_comment",
                {"issue": control, "body": text},
                f"planned-event:{ctx.repo}:{control}:{epic}",
            )
        )
    await ctx.outbox.settle(ops)
