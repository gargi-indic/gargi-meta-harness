"""Executor for the branch node."""

from harness.engine.executors.base import Done, ExecContext, Executor, Waiting
from harness.playbook.models import BranchNode, Node
from harness.playbook.templating import evaluate


class BranchExecutor(Executor):
    async def advance(self, step, node: Node, ctx: ExecContext) -> Waiting | Done:
        assert isinstance(node, BranchNode)

        for i, case in enumerate(node.cases):
            is_match = evaluate(case.when, ctx.template, path=f"nodes.{step.node_id}.cases[{i}].when")
            if is_match:
                return Done(case.port, {"port": case.port})

        return Done(node.default, {"port": node.default})
