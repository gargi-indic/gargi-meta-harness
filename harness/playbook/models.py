"""Playbook models.

Correction convention: every outcome that can route into a revise node puts
a human-readable `correction: str` in its output. That covers review changes,
check fail and gate rejected. Revise messages then use `{{ prev.correction }}`.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Duration = Annotated[str, Field(pattern=r"^\d+[smh]$")]


def duration_seconds(duration: str) -> int:
    val = int(duration[:-1])
    unit = duration[-1]
    if unit == "s":
        return val
    elif unit == "m":
        return val * 60
    elif unit == "h":
        return val * 3600
    raise ValueError(f"Invalid duration unit in {duration}")


class AutoIf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_if: str


GateMode = Literal["human", "auto"] | AutoIf


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_usd: float | None = None
    timeout: Duration | None = None


class NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    max_visits: int | None = Field(default=None, ge=1)

    def _add_exhausted(self, ports: tuple[str, ...]) -> tuple[str, ...]:
        if self.max_visits is not None:
            return ports + ("exhausted",)
        return ports


class AgentNode(NodeBase):
    kind: Literal["agent"] = "agent"
    session: str
    action: Literal["plan", "implement", "revise"]
    agent: str = "{{ issue.agent }}"
    expect: Literal["plan", "pr"] | None = None
    message: str | None = None
    prompt: str | None = None
    timeout: Duration | None = None

    @model_validator(mode="after")
    def _check_expect(self) -> "AgentNode":
        if self.action == "revise" and self.expect is None:
            raise ValueError("revise actions require an 'expect' value")
        if self.action != "revise" and self.expect is not None:
            raise ValueError(f"expect is forbidden on {self.action} actions")
        return self

    def ports(self) -> tuple[str, ...]:
        if self.action == "plan":
            p = ("plan_ready", "pr_ready", "needs_input", "failed", "timeout")
        elif self.action == "implement":
            p = ("pr_ready", "needs_input", "failed", "timeout")
        elif self.action == "revise":
            p = (f"{self.expect}_ready", "needs_input", "failed", "timeout")
        else:
            p = ()
        return self._add_exhausted(p)


class ReviewNode(NodeBase):
    kind: Literal["review"] = "review"
    target: Literal["plan", "pr", "question"]
    checklist: list[str]

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("approve", "changes", "escalate"))


class CheckNode(NodeBase):
    kind: Literal["check"] = "check"
    check: Literal["ci", "scope", "pinned_versions", "command"]
    command: str | None = None

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("pass", "fail"))


class GateNode(NodeBase):
    kind: Literal["gate"] = "gate"
    gate: str
    # ``campaign``: the gate waits for the campaign's plan approval (gate name ``plan``), not a per-issue decision.
    scope: Literal["issue", "campaign"] = "issue"

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("approved", "rejected"))


class BranchCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    when: str
    port: str


class BranchNode(NodeBase):
    kind: Literal["branch"] = "branch"
    cases: list[BranchCase]
    # Port taken when no case matches. Without it, no match ends the node with outcome ``failed``.
    default: str | None = None

    def ports(self) -> tuple[str, ...]:
        ports = [c.port for c in self.cases]
        ports.append(self.default if self.default is not None else "failed")
        return self._add_exhausted(tuple(dict.fromkeys(ports)))


class ParallelNode(NodeBase):
    kind: Literal["parallel"] = "parallel"
    branches: list[str]

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(())


class JoinNode(NodeBase):
    kind: Literal["join"] = "join"
    mode: Literal["all", "any", "select"]
    inputs: list[str] | None = None
    # Judging criteria for ``mode: select``; the selector prompt lists them after its built-in rubric.
    rubric: list[str] | None = None

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("done", "failed"))


class DecomposeNode(NodeBase):
    kind: Literal["decompose"] = "decompose"

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("created", "failed"))


class MergeNode(NodeBase):
    kind: Literal["merge"] = "merge"
    method: Literal["squash", "merge", "rebase"] | None = None

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("merged", "failed"))


class EscalateNode(NodeBase):
    kind: Literal["escalate"] = "escalate"

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("resolved",))


class NotifyNode(NodeBase):
    kind: Literal["notify"] = "notify"
    message: str

    def ports(self) -> tuple[str, ...]:
        return self._add_exhausted(("sent",))


Node = Annotated[
    AgentNode
    | ReviewNode
    | CheckNode
    | GateNode
    | BranchNode
    | ParallelNode
    | JoinNode
    | DecomposeNode
    | MergeNode
    | EscalateNode
    | NotifyNode,
    Field(discriminator="kind"),
]


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiVersion: Literal["harness/v1"]
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    name: str | None = None
    description: str | None = None
    gates: dict[str, GateMode] = Field(default_factory=dict)
    limits: Limits | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    nodes: dict[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")], Node]
    edges: dict[str, str]
