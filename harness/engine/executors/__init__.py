"""Executor protocol, results and registry."""

from harness.engine.executors.base import Done, ExecContext, Executor, ExecutorRegistry, Waiting
from harness.llm.backend import ReviewBackend

__all__ = ["Done", "ExecContext", "Executor", "ExecutorRegistry", "Waiting", "default_registry"]


def default_registry(*, review_backend: ReviewBackend) -> ExecutorRegistry:
    """The executors of every node kind the built-in playbooks use.

    ``join`` is registered by the Runner and ``parallel`` is handled by the Runner itself; ``decompose`` is
    M4 and stays unregistered.
    """
    # Imported here so that importing the package for the protocol alone stays cheap and cycle-free.
    from harness.engine.executors.agent import AgentExecutor
    from harness.engine.executors.branch import BranchExecutor
    from harness.engine.executors.check import CheckExecutor
    from harness.engine.executors.escalate import EscalateExecutor
    from harness.engine.executors.gate import GateExecutor
    from harness.engine.executors.merge import MergeExecutor
    from harness.engine.executors.notify import NotifyExecutor
    from harness.engine.executors.review import ReviewExecutor

    registry = ExecutorRegistry()
    registry.register("agent", AgentExecutor())
    registry.register("review", ReviewExecutor(review_backend))
    registry.register("check", CheckExecutor())
    registry.register("gate", GateExecutor())
    registry.register("merge", MergeExecutor())
    registry.register("escalate", EscalateExecutor())
    registry.register("notify", NotifyExecutor())
    registry.register("branch", BranchExecutor())
    return registry
