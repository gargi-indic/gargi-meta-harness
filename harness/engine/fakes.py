"""Scripted executors for the editor's dry run (M2.8).

A :class:`ScriptedExecutor` stands in for every real executor: it never touches the network, an agent or the
clock, it just answers each visit of each node with the outcome a scenario names, or with the node's primary
port when the scenario says nothing. That gives per-port control for every node kind, which driving
``FakeAdapter`` could not do (its states only cover agent ports).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from harness.engine.executors.base import Done, ExecContext, ExecutorRegistry
from harness.playbook.models import GateNode, Node, Playbook
from harness.store.repo import StepRow


class TraceEntry(BaseModel):
    node: str = Field(description="Node id")
    visit: int = Field(description="1-based visit number of the node")
    outcome: str | None = Field(
        description="Port taken; null for the escalate step the dry run stops at without advancing it"
    )


class ScriptedExecutor:
    def __init__(
        self,
        outcomes: Mapping[str, Sequence[str]],
        trace: list[TraceEntry],
        gates: list[dict[str, Any]],
    ) -> None:
        self.outcomes = outcomes
        self.trace = trace
        self.gates = gates

    async def advance(self, step: StepRow, node: Node, ctx: ExecContext) -> Done:
        scripted = self.outcomes.get(step.node_id, ())
        index = step.visit - 1
        if 0 <= index < len(scripted):
            outcome = scripted[index]
        else:
            outcome = node.ports()[0]
            if isinstance(node, GateNode):
                self.gates.append({"node": step.node_id, "visit": step.visit, "gate": node.gate})
        self.trace.append(TraceEntry(node=step.node_id, visit=step.visit, outcome=outcome))
        return Done(outcome, {})


def scripted_registry(
    playbook: Playbook,
    outcomes: Mapping[str, Sequence[str]],
    trace: list[TraceEntry],
    gates: list[dict[str, Any]],
) -> ExecutorRegistry:
    """A registry with one :class:`ScriptedExecutor` per node kind in the playbook.

    ``join`` and ``parallel`` are left to the Runner, which owns their semantics.
    """
    registry = ExecutorRegistry()
    executor = ScriptedExecutor(outcomes, trace, gates)
    for kind in sorted({node.kind for node in playbook.nodes.values()} - {"join", "parallel"}):
        registry.register(kind, executor)
    return registry
