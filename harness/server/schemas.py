"""Response models of the read-only REST API. They are the API contract: keep them stable and documented."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignCounts(Schema):
    ready: int = Field(description="Open issues with no open blocker and no harness label.")
    running: int = Field(description="Issues an agent is working on (label harness:running).")
    needs_you: int = Field(description="Issues waiting for a human decision (label harness:needs-you).")
    review: int = Field(description="Issues in review (label harness:review).")
    blocked: int = Field(description="Issues with an open blocker or the harness:blocked label.")
    done: int = Field(description="Closed issues or issues labelled harness:done.")
    failed: int = Field(description="Issues labelled harness:failed.")


class CampaignSummary(Schema):
    id: str = Field(description="Campaign id: the repository full name, owner/repo.")
    repo: str = Field(description="Repository full name, owner/repo.")
    control_issue: int | None = Field(description="Number of the repo's control issue, if configured.")
    title: str = Field(description="Display title; the repository full name in M2.")
    counts: CampaignCounts = Field(description="Issue counts per derived status, control issue excluded.")


class GraphNode(Schema):
    id: int = Field(description="Issue number.")
    title: str = Field(description="Issue title from the local cache.")
    status: str = Field(
        description="One of ready, running, needs-you, review, blocked, done, failed (derived from labels)."
    )
    agent: str = Field(description="Agent from an agent:<x> label, else the repo's default agent.")
    playbook: str = Field(description="Playbook from a playbook:<y> label, else the repo's default playbook.")
    run_id: str | None = Field(description="Id of the issue's latest run when it is not finished, else null.")
    url: str = Field(description="GitHub URL of the issue.")


class GraphEdge(Schema):
    source: int = Field(description="Blocking issue number.")
    target: int = Field(description="Blocked issue number.")


class CampaignGraph(Schema):
    nodes: list[GraphNode] = Field(description="Campaign issues sorted by number.")
    edges: list[GraphEdge] = Field(description="blocked_by links as blocker -> blocked, sorted.")


class Card(Schema):
    repo: str = Field(description="Repository full name.")
    issue: int = Field(description="Issue number.")
    title: str = Field(description="Issue title from the local cache.")
    agent: str = Field(description="Agent from an agent:<x> label, else the repo's default agent.")
    playbook: str = Field(description="Playbook from a playbook:<y> label, else the repo's default playbook.")
    run_id: str | None = Field(description="Id of the issue's latest run when it is not finished, else null.")
    pr: int | None = Field(description="Pull request number recorded by the issue's latest run, if any.")
    blocked_by: list[int] = Field(description="All blocker issue numbers, open or closed.")
    updated_at: str = Field(description="When the cached issue last changed (ISO 8601).")


class BoardColumns(Schema):
    ready: list[Card] = Field(description="Cards that can start.")
    running: list[Card] = Field(description="Cards an agent is working on.")
    needs_you: list[Card] = Field(description="Cards waiting for a human.")
    review: list[Card] = Field(description="Cards in review.")
    blocked: list[Card] = Field(description="Cards waiting on a blocker.")
    done: list[Card] = Field(description="Finished cards.")
    failed: list[Card] = Field(description="Failed cards.")


class Board(Schema):
    columns: BoardColumns = Field(description="Cards per column, newest update first.")


class InboxCheck(Schema):
    name: str = Field(description="Check node id, or the name of a failed CI check.")
    state: str = Field(description="Outcome or status of the check, or the conclusion of a failed CI check.")


class InboxPr(Schema):
    number: int = Field(description="Pull request number.")
    url: str = Field(description="Pull request URL.")
    checks: list[InboxCheck] = Field(description="Latest result of each check node, then failed CI checks.")


class InboxFinding(Schema):
    severity: str = Field(description="Reviewer severity: blocker, major or minor.")
    text: str = Field(description="'<file>: <problem>' or just the problem.")


class InboxItem(Schema):
    id: str = Field(description="Step id of the waiting step.")
    kind: str = Field(
        description=(
            "Open enum, unknown values must pass through. plan_approval, merge_approval, gate (other gates), "
            "question (an agent asked for input), escalation, or the node kind of any other waiting node."
        )
    )
    run_id: str = Field(description="Run the step belongs to.")
    step_id: str = Field(description="Id of the waiting step.")
    repo: str = Field(description="Repository full name.")
    issue: int = Field(description="Issue number.")
    title: str = Field(description="Issue title from the local cache, or '#<number>'.")
    summary: str = Field(description="Reason the step gives for needing a human; may be empty.")
    pr: InboxPr | None = Field(description="Pull request of the run, if it has one.")
    findings: list[InboxFinding] = Field(description="Findings of the run's latest review step.")
    agent: str | None = Field(description="Agent the run uses, if known.")
    created_at: str = Field(description="When the step started waiting (ISO 8601).")
    gate_id: str | None = Field(description="Gate row id for gate steps, to approve or reject it.")


class RunSummary(Schema):
    id: str = Field(description="Run id.")
    repo: str = Field(description="Repository full name.")
    issue: int = Field(description="Issue number.")
    title: str = Field(description="Issue title from the local cache, or '#<number>'.")
    playbook_id: str = Field(description="Id of the playbook the run follows.")
    status: str = Field(description="running, needs-you, paused, done, failed or cancelled.")
    agent: str | None = Field(description="Agent the run uses, if known.")
    pr: int | None = Field(description="Pull request number, if the run has one.")
    current_node: str | None = Field(description="Node of the latest step that is not done, else null.")
    created_at: str = Field(description="When the run started (ISO 8601).")
    updated_at: str = Field(description="When the run last changed (ISO 8601).")


class StepOut(Schema):
    id: str = Field(description="Step id.")
    node: str = Field(description="Playbook node id.")
    visit: int = Field(description="Which visit of the node this step is (1-based).")
    status: str = Field(description="pending, running, waiting, done or failed.")
    outcome: str | None = Field(description="Port the step routed through, once finished.")
    ref: dict[str, Any] | None = Field(description="Executor reference, such as the agent session.")
    started_at: str | None = Field(description="When the step started (ISO 8601).")
    finished_at: str | None = Field(description="When the step finished (ISO 8601).")


class AgentConfigOut(Schema):
    name: str = Field(description="Agent name.")
    enabled: bool = Field(description="Always true for enabled agents.")


class AgentsEnabled(Schema):
    agents: list[AgentConfigOut] = Field(description="Enabled agents, sorted by name.")


class RunDetail(Schema):
    run: RunSummary = Field(description="The run.")
    playbook: dict[str, Any] | None = Field(
        description="Playbook as stored at run start, else the built-in one, else null."
    )
    steps: list[StepOut] = Field(description="Steps ordered by start time, visit and id.")
    visit_counts: dict[str, int] = Field(description="Highest visit number per node.")


class ActivityItem(Schema):
    seq: int = Field(description="Event id; pass the last one as 'after' to get the next page.")
    ts: str | None = Field(description="When the agent reported the item (ISO 8601).")
    step_id: str | None = Field(description="Step the activity belongs to.")
    kind: str = Field(description="message, tool, state or note.")
    text: str = Field(description="Redacted activity text.")


class ActivityPage(Schema):
    items: list[ActivityItem] = Field(description="Activity items after the requested seq, oldest first.")
    next: int = Field(description="Last seq returned, or the requested 'after' when there are no items.")
