"""Executor for the notify node."""

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
            now_iso = datetime.fromtimestamp(ctx.clock(), UTC).strftime("%Y-%m-%d")
            comment_body = render_event(now_iso, ctx.run.issue, "note", [msg])
            ctx.outbox.enqueue(
                "add_comment",
                {"repo": ctx.run.repo, "issue": ctx.run.issue, "body": comment_body},
                coalesce_key=f"notify:{step.id}",
            )

        return Done("sent", {"message": msg})
