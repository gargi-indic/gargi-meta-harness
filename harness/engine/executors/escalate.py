"""Executor for the escalate node."""

from harness.engine.executors.base import Done, ExecContext, Executor, Waiting
from harness.playbook.models import EscalateNode, Node
from harness.secrets import redact


class EscalateExecutor(Executor):
    async def advance(self, step, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, EscalateNode)

        reason_content = None
        if ctx.template.prev:
            for k in ["question", "error", "correction", "note", "summary"]:
                if k in ctx.template.prev:
                    val = ctx.template.prev[k]
                    if val:
                        reason_content = str(val)
                        break

        # Determine the node_id that routed here
        if ctx.run.context.get("cursor"):
            # find previous step whose output matches prev or just grab the latest done
            pass

        # Let's see if we can find the node_id from steps
        prev_node = "an earlier step"
        from harness.store.repo import steps_for_run

        steps = steps_for_run(ctx.store, ctx.run.id)
        done_steps = [s for s in steps if s.status == "done" and s.id != step.id]
        if done_steps:
            done_steps.sort(key=lambda s: s.ended_at or "", reverse=True)
            prev_node = done_steps[0].node_id

        reason = f"Escalated after {prev_node}"
        if reason_content:
            reason += f":\n\n{redact(reason_content)[:500]}"

        links = []
        if ctx.run.context.get("pr") and "url" in ctx.run.context["pr"]:
            links.append(ctx.run.context["pr"]["url"])

        return Waiting(
            ref=None, poll_after=3600, output={"needs_you": True, "reason": reason, "links": links}
        )
