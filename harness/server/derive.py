"""Turn store rows into API response models. The only reads are the store lookups in ``load_campaign``."""

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from harness.config import RepoConfig
from harness.dag import blocked
from harness.engine.model import TERMINAL_STATUSES
from harness.ops.costs import UsageRepo
from harness.playbook.load import builtin_playbooks
from harness.secrets import redact
from harness.server.schemas import (
    ActivityItem,
    CampaignCounts,
    CampaignGraph,
    CampaignSummary,
    Card,
    GraphEdge,
    GraphNode,
    InboxCheck,
    InboxFinding,
    InboxItem,
    InboxPr,
    RunCostOut,
    RunSummary,
    StepOut,
)
from harness.store.db import Store
from harness.store.queries import latest_runs_by_issue
from harness.store.repo import IssueRow, RunRow, StepRow, get_repo, list_issues

STATUSES = ("ready", "running", "needs-you", "review", "blocked", "done", "failed")

_LABEL_STATUS = (
    ("harness:needs-you", "needs-you"),
    ("harness:review", "review"),
    ("harness:running", "running"),
    ("harness:blocked", "blocked"),
)

_ACTIVITY_KINDS = {
    "message": "message",
    "agent": "message",
    "user": "message",
    "tool": "tool",
    "command": "tool",
    "bash": "tool",
    "state": "state",
    "status": "state",
    "plan": "state",
    "progress": "state",
    "end": "state",
}


def status_of(issue: IssueRow, open_blockers: list[int], labels: list[str]) -> str:
    """Derived issue status; the first matching rule wins."""
    if "harness:failed" in labels:
        return "failed"
    if issue.state == "closed":
        return "done"
    if "harness:done" in labels:
        return "done"
    for label, status in _LABEL_STATUS:
        if label in labels:
            return status
    if open_blockers:
        return "blocked"
    return "ready"


def label_value(labels: list[str], prefix: str) -> str | None:
    for label in labels:
        if label.startswith(prefix) and len(label) > len(prefix):
            return label[len(prefix) :]
    return None


def repo_config_of(stored: dict[str, Any] | None) -> RepoConfig:
    """The repo config cached by sync; a missing or invalid one means the defaults."""
    if not stored:
        return RepoConfig()
    try:
        return RepoConfig.model_validate(stored)
    except ValidationError:
        return RepoConfig()


def ordered_steps(steps: list[StepRow]) -> list[StepRow]:
    """Steps by (started_at, visit, id); a step that has not started yet sorts last."""
    return sorted(steps, key=lambda s: (s.started_at is None, s.started_at or "", s.visit, s.id))


def pr_number(run: RunRow | None) -> int | None:
    if run is None:
        return None
    pr = run.context.get("pr")
    number = pr.get("number") if isinstance(pr, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def playbook_dict(run: RunRow) -> dict[str, Any] | None:
    """The playbook stored in the run context, else the built-in one with the run's playbook id, else None."""
    stored = run.context.get("playbook")
    if isinstance(stored, dict):
        return stored
    builtin = builtin_playbooks().get(run.playbook_id)
    return builtin.model_dump(mode="json") if builtin is not None else None


# ---- campaigns, graph and board -------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignView:
    repo: str
    config: RepoConfig
    issues: list[IssueRow]
    statuses: dict[int, str]
    runs: dict[int, RunRow]

    def agent_of(self, issue: IssueRow) -> str:
        return label_value(issue.labels, "agent:") or self.config.default_agent

    def playbook_of(self, issue: IssueRow) -> str:
        return label_value(issue.labels, "playbook:") or self.config.default_playbook

    def active_run_id(self, issue: IssueRow) -> str | None:
        run = self.runs.get(issue.number)
        return run.id if run is not None and run.status not in TERMINAL_STATUSES else None


def load_campaign(store: Store, repo: str) -> CampaignView:
    stored = get_repo(store, repo)
    config = repo_config_of(stored["config"] if stored else None)
    every_issue = list_issues(store, repo)
    open_blockers = blocked(every_issue)
    issues = [i for i in every_issue if i.number != config.control_issue]
    statuses = {i.number: status_of(i, open_blockers.get(i.number, []), i.labels) for i in issues}
    return CampaignView(repo, config, issues, statuses, latest_runs_by_issue(store, repo))


def campaign_summary(view: CampaignView) -> CampaignSummary:
    counts = {status.replace("-", "_"): 0 for status in STATUSES}
    for status in view.statuses.values():
        counts[status.replace("-", "_")] += 1
    return CampaignSummary(
        id=view.repo,
        repo=view.repo,
        control_issue=view.config.control_issue,
        title=view.repo,
        counts=CampaignCounts(**counts),
    )


def campaign_graph(view: CampaignView) -> CampaignGraph:
    numbers = {i.number for i in view.issues}
    nodes = [
        GraphNode(
            id=i.number,
            title=i.title,
            status=view.statuses[i.number],
            agent=view.agent_of(i),
            playbook=view.playbook_of(i),
            run_id=view.active_run_id(i),
            url=f"https://github.com/{view.repo}/issues/{i.number}",
        )
        for i in sorted(view.issues, key=lambda i: i.number)
    ]
    edges = sorted(
        {(b, i.number) for i in view.issues for b in i.blocked_by if b in numbers and i.number in numbers}
    )
    return CampaignGraph(nodes=nodes, edges=[GraphEdge(source=s, target=t) for s, t in edges])


def board_cards(view: CampaignView) -> dict[str, list[Card]]:
    """Cards per column key (``needs_you`` for ``needs-you``), newest update first, then issue number."""
    columns: dict[str, list[Card]] = {status.replace("-", "_"): [] for status in STATUSES}
    newest_first = sorted(
        sorted(view.issues, key=lambda i: i.number), key=lambda i: i.updated_at, reverse=True
    )
    for issue in newest_first:
        columns[view.statuses[issue.number].replace("-", "_")].append(
            Card(
                repo=view.repo,
                issue=issue.number,
                title=issue.title,
                agent=view.agent_of(issue),
                playbook=view.playbook_of(issue),
                run_id=view.active_run_id(issue),
                pr=pr_number(view.runs.get(issue.number)),
                blocked_by=list(issue.blocked_by),
                updated_at=issue.updated_at,
            )
        )
    return columns


# ---- runs ------------------------------------------------------------------------------------------------


def run_summary(run: RunRow, steps: list[StepRow], title: str | None) -> RunSummary:
    pending = [s for s in ordered_steps(steps) if s.status != "done"]
    agent = run.context.get("agent")
    return RunSummary(
        id=run.id,
        repo=run.repo,
        issue=run.issue,
        title=title or f"#{run.issue}",
        playbook_id=run.playbook_id,
        status=run.status,
        agent=agent if isinstance(agent, str) else None,
        pr=pr_number(run),
        current_node=pending[-1].node_id if pending else None,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def run_cost_out(store: Store, run_id: str) -> RunCostOut:
    """What the run spent, from the local usage mirror; ``usd`` is None (n/a) when nothing was reported."""
    cost = UsageRepo(store).run_cost(run_id)
    return RunCostOut(usd=cost.usd if cost.recorded else None, estimated=cost.estimated, by_node=cost.by_node)


def step_out(step: StepRow) -> StepOut:
    return StepOut(
        id=step.id,
        node=step.node_id,
        visit=step.visit,
        status=step.status,
        outcome=step.outcome,
        ref=step.ref,
        started_at=step.started_at,
        finished_at=step.ended_at,
    )


def visit_counts(steps: list[StepRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in steps:
        counts[step.node_id] = max(counts.get(step.node_id, 0), step.visit)
    return counts


def activity_item(row: dict[str, Any]) -> ActivityItem:
    data = row["data"] if isinstance(row.get("data"), dict) else {}
    ts = data.get("ts") or row.get("ts")
    step = data.get("step")
    return ActivityItem(
        seq=row["id"],
        ts=str(ts) if ts is not None else None,
        step_id=str(step) if step is not None else None,
        kind=_ACTIVITY_KINDS.get(str(data.get("kind")), "note"),
        text=redact(str(data.get("text") or "")),
    )


# ---- inbox -----------------------------------------------------------------------------------------------


def needs_human(step: StepRow) -> bool:
    return step.status == "waiting" and bool(step.output and step.output.get("needs_you"))


def _latest(steps: list[StepRow], node_ids: set[str]) -> StepRow | None:
    mine = [s for s in steps if s.node_id in node_ids]
    return max(mine, key=lambda s: (s.visit, s.started_at or "", s.id)) if mine else None


def _nodes_of_kind(playbook: dict[str, Any] | None, kind: str) -> set[str]:
    nodes = (playbook or {}).get("nodes")
    if not isinstance(nodes, dict):
        return set()
    return {nid for nid, node in nodes.items() if isinstance(node, dict) and node.get("kind") == kind}


def inbox_kind(step: StepRow, steps: list[StepRow], playbook: dict[str, Any] | None) -> str:
    nodes = (playbook or {}).get("nodes")
    node = nodes.get(step.node_id) if isinstance(nodes, dict) else None
    if not isinstance(node, dict):
        return "escalation"
    kind = str(node.get("kind"))
    if kind == "gate":
        return {"plan": "plan_approval", "merge": "merge_approval"}.get(str(node.get("gate")), "gate")
    if kind == "escalate":
        others = [s for s in steps if s.status == "done" and s.id != step.id]
        previous = max(others, key=lambda s: (s.ended_at or "", s.id), default=None)
        return "question" if previous is not None and previous.outcome == "needs_input" else "escalation"
    return kind


def inbox_pr(run: RunRow, steps: list[StepRow], playbook: dict[str, Any] | None) -> InboxPr | None:
    number = pr_number(run)
    if number is None:
        return None
    pr = run.context["pr"]
    url = pr.get("url") or f"https://github.com/{run.repo}/pull/{number}"
    nodes = (playbook or {}).get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
    check_nodes = {nid: n for nid, n in nodes.items() if isinstance(n, dict) and n.get("kind") == "check"}
    latest = {nid: _latest(steps, {nid}) for nid in check_nodes}
    checks = [
        InboxCheck(name=nid, state=step.outcome or step.status)
        for nid, step in latest.items()
        if step is not None
    ]
    for nid, node in check_nodes.items():
        step = latest[nid]
        failed = step.output.get("failed") if step is not None and step.output else None
        if (
            node.get("check") == "ci"
            and step is not None
            and step.outcome == "fail"
            and isinstance(failed, list)
        ):
            checks.extend(
                InboxCheck(name=str(f["name"]), state=str(f.get("conclusion") or "unknown"))
                for f in failed
                if isinstance(f, dict) and f.get("name")
            )
    return InboxPr(number=number, url=str(url), checks=checks)


def inbox_findings(steps: list[StepRow], playbook: dict[str, Any] | None) -> list[InboxFinding]:
    reviews = [s for s in ordered_steps(steps) if s.node_id in _nodes_of_kind(playbook, "review")]
    if not reviews:
        return []
    raw = (reviews[-1].output or {}).get("findings")
    findings: list[InboxFinding] = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        problem = str(f.get("problem") or "")
        file = f.get("file")
        findings.append(
            InboxFinding(
                severity=str(f.get("severity") or ""), text=f"{file}: {problem}" if file else problem
            )
        )
    return findings


def inbox_item(run: RunRow, step: StepRow, steps: list[StepRow], title: str | None) -> InboxItem:
    playbook = playbook_dict(run)
    output = step.output or {}
    agent = run.context.get("agent")
    gate_id = output.get("gate_id")
    return InboxItem(
        id=step.id,
        kind=inbox_kind(step, steps, playbook),
        run_id=run.id,
        step_id=step.id,
        repo=run.repo,
        issue=run.issue,
        title=title or f"#{run.issue}",
        summary=str(output.get("reason") or ""),
        pr=inbox_pr(run, steps, playbook),
        findings=inbox_findings(steps, playbook),
        agent=agent if isinstance(agent, str) else None,
        created_at=step.started_at or run.updated_at,
        gate_id=str(gate_id) if gate_id else None,
    )
