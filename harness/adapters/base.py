"""The one interface every coding agent sits behind, plus the normalized states and value types.

Real adapters (Jules, Claude, Codex, Copilot, Antigravity) implement :class:`AgentAdapter`; the engine only
ever sees the types defined here. :class:`AdapterRegistry` maps adapter names to instances and exposes
their capability names in the form the playbook validator expects.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class AgentState(StrEnum):
    """Normalized state of an agent session, whatever the underlying agent calls it."""

    STARTING = "STARTING"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    WORKING = "WORKING"
    NEEDS_INPUT = "NEEDS_INPUT"
    PR_READY = "PR_READY"
    FAILED = "FAILED"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capabilities(_Model):
    plan_approval: bool
    messaging: bool
    opens_pr: bool
    local_workspace: bool
    max_concurrency: int = 1

    def names(self) -> set[str]:
        """Capability names as the playbook validator checks them.

        ``action: plan`` needs ``"plan"`` and ``action: revise`` needs ``"message"``.
        """
        names: set[str] = set()
        if self.plan_approval:
            names.add("plan")
        if self.messaging:
            names.add("message")
        if self.opens_pr:
            names.add("pr")
        if self.local_workspace:
            names.add("local")
        return names


class StartRequest(_Model):
    repo: str
    base_branch: str
    branch: str
    issue: int
    title: str
    brief: str
    mode: Literal["plan_first", "direct"]
    idempotency_key: str
    workdir: Path | None = None


class SessionRef(_Model):
    agent: str
    id: str
    url: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class PrRef(_Model):
    number: int
    url: str
    head_sha: str


class ActivityItem(_Model):
    ts: str
    kind: str
    text: str


class Usage(_Model):
    """What a session or a model call has spent. Adapters that cannot tell report no ``Usage`` at all (``n/a``)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float = 0.0
    # True when the cost was computed from a price table instead of reported by the provider.
    estimated: bool = False
    model: str | None = None


class AgentSnapshot(_Model):
    state: AgentState
    plan: str | None = None
    question: str | None = None
    pr: PrRef | None = None
    # Only the items that are new since the previous poll.
    activity: list[ActivityItem] = Field(default_factory=list)
    error: str | None = None
    # Seconds the engine should wait before polling again.
    poll_after: float = 60.0
    # Cumulative usage of the session so far; None when the adapter cannot report it.
    usage: Usage | None = None


@runtime_checkable
class AgentAdapter(Protocol):
    name: str
    caps: Capabilities

    async def start(self, req: StartRequest) -> SessionRef: ...

    async def find(self, idempotency_key: str) -> SessionRef | None: ...

    async def poll(self, ref: SessionRef) -> AgentSnapshot: ...

    async def approve_plan(self, ref: SessionRef, note: str | None = None) -> None: ...

    async def send(self, ref: SessionRef, text: str) -> None: ...

    async def cancel(self, ref: SessionRef) -> None: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"adapter already registered: {adapter.name!r}")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> AgentAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            known = ", ".join(sorted(self._adapters)) or "(none)"
            raise KeyError(f"unknown adapter {name!r}; known adapters: {known}") from None

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def caps_by_name(self) -> dict[str, set[str]]:
        """Adapter name -> capability names, the shape of the playbook validator's ``agent_caps``."""
        return {name: adapter.caps.names() for name, adapter in self._adapters.items()}
