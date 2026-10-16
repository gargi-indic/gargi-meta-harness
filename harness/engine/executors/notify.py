from datetime import UTC, datetime

from harness.engine.executors.base import Done, ExecContext, Executor, Waiting
from harness.github.ledger import render_event
from harness.playbook.models import Node, NotifyNode
from harness.playbook.templating import render


class NotifyExecutor(Executor):
    async def advance(self, step, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, NotifyNode)

        msg = render(node.message, ctx.template, path=f"nodes.{step.node_id}.message")

        if ctx.outbox:
            now_str = datetime.now(UTC).isoformat()
            comment_body = render_event(now_str, ctx.run.issue, "note", [msg])
            ctx.outbox.add_comment(
                ctx.run.repo,
                ctx.run.issue,
                comment_body,
                coalesce_key=f"notify:{step.id}",
            )

        return Done("sent", {"message": msg})
