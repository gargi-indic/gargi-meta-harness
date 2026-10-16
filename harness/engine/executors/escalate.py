from harness.engine.executors.base import Done, ExecContext, Executor, Waiting
from harness.playbook.models import EscalateNode, Node
from harness.secrets import redact


class EscalateExecutor(Executor):
    async def advance(self, step, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, EscalateNode)

        # Actually from context, we just do it manually or via template prev
        reason_prefix = "Escalated after an earlier step"
        if ctx.run.context.get("prev_for"):
            pass  # we can check if there's a specific prev node.

        # Or let's just get it from template prev
        reason_content = None
        if ctx.template.prev:
            for k in ["question", "error", "correction", "note", "summary"]:
                if k in ctx.template.prev:
                    val = ctx.template.prev[k]
                    if val:
                        reason_content = val
                        break

        reason = reason_prefix
        if reason_content:
            reason += f":\n\n{redact(reason_content)[:500]}"

        links = []
        if ctx.run.context.get("pr") and "url" in ctx.run.context["pr"]:
            links.append(ctx.run.context["pr"]["url"])

        return Waiting(
            ref=None, poll_after=3600, output={"needs_you": True, "reason": reason, "links": links}
        )
