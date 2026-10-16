"""Executor for the escalate node."""

from harness.engine.executors.base import Done, ExecContext, Executor, Waiting
from harness.playbook.models import EscalateNode, Node
from harness.secrets import redact
from harness.store.repo import steps_for_run

_REASON_KEYS = ("question", "error", "correction", "note", "summary")


class EscalateExecutor(Executor):
    async def advance(self, step, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, EscalateNode)

        prev = ctx.template.prev
        detail = None
        if prev:
            for key in _REASON_KEYS:
                if prev.get(key):
                    detail = str(prev[key])
                    break

        done_steps = sorted(
            (s for s in steps_for_run(ctx.store, ctx.run.id) if s.status == "done" and s.id != step.id),
            key=lambda s: s.ended_at or "",
        )
        routed_from = [s for s in done_steps if prev is not None and s.output == prev]
        candidates = routed_from or done_steps
        prev_node = candidates[-1].node_id if candidates else "an earlier step"

        reason = f"Escalated after {prev_node}"
        if detail:
            reason += f":\n\n{redact(detail)[:500]}"

        pr = ctx.run.context.get("pr")
        links = [pr["url"]] if isinstance(pr, dict) and pr.get("url") else []

        return Waiting(
            ref=None, poll_after=3600, output={"needs_you": True, "reason": reason, "links": links}
        )
