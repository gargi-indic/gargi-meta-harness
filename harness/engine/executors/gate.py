"""Gate executor: auto, auto_if (falls back to human) and human gates.

A human gate inserts one pending ``gates`` row (id ``<step id>/gate``, never twice) and waits. It posts
nothing and enqueues nothing itself: the runner turns ``needs_you`` into the label and the one needs-you
comment. The decision arrives through ``harness.github.commands.decide`` (CLI or slash command); the next
poll of the waiting step reads it and returns ``approved`` or ``rejected``.
"""

from typing import Any

from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.playbook.models import AutoIf, GateNode
from harness.playbook.templating import evaluate
from harness.store.repo import GateRow, StepRow, get_gate, insert_gate

POLL_AFTER = 15.0


class GateExecutor:
    async def advance(self, step: StepRow, node: GateNode, ctx: ExecContext) -> Waiting | Done:
        if node.gate not in ctx.playbook.gates:
            return Done("failed", {"error": f"unknown gate {node.gate}"})
        mode = ctx.playbook.gates[node.gate]
        if mode == "auto":
            return Done("approved", {"note": None, "decided_by": "auto"})
        if isinstance(mode, AutoIf):
            if evaluate(mode.auto_if, ctx.template, path=f"gates.{node.gate}.auto_if"):
                return Done("approved", {"note": None, "decided_by": "auto_if"})
        return self._human(step, node, ctx)

    def _human(self, step: StepRow, node: GateNode, ctx: ExecContext) -> Waiting | Done:
        store = ctx.store
        gate_id = f"{step.id}/gate"
        with store.tx():
            gate = get_gate(store, gate_id)
            if gate is None:
                gate = GateRow(
                    id=gate_id,
                    run_id=ctx.run.id,
                    step_id=step.id,
                    name=node.gate,
                    status="pending",
                    note=None,
                    decided_by=None,
                    created_at=store.now(),
                    decided_at=None,
                )
                insert_gate(store, gate)

        if gate.status in ("approved", "rejected"):
            output: dict[str, Any] = {"note": gate.note, "decided_by": gate.decided_by}
            if gate.status == "rejected":
                output["correction"] = gate.note or f"Gate '{node.gate}' was rejected"
            return Done(gate.status, output)
        if gate.status != "pending":
            return Done("failed", {"error": f"gate {gate_id} has unexpected status {gate.status!r}"})

        pr = ctx.run.context.get("pr")
        url = pr.get("url") if isinstance(pr, dict) else None
        links = [url] if isinstance(url, str) and url else []
        reason = (
            f"Gate '{node.gate}' needs a decision: comment `/harness approve [note]` or "
            f"`/harness reject <reason>`, or run `harness approve {gate_id}` / `harness reject {gate_id} --note <reason>`"
        )
        return Waiting(
            ref={"gate_id": gate_id},
            poll_after=POLL_AFTER,
            output={"needs_you": True, "reason": reason, "links": links, "gate_id": gate_id},
        )
