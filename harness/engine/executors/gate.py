"""Gate executor: auto, auto_if (falls back to human) and human gates.

A human gate inserts one pending ``gates`` row (id ``<step id>/gate``, never twice) and waits. It posts
nothing and enqueues nothing itself: the runner turns ``needs_you`` into the label and the one needs-you
comment. The decision arrives through ``harness.github.commands.decide`` (CLI or slash command); the next
poll of the waiting step reads it and returns ``approved`` or ``rejected``.

A gate with ``scope: campaign`` (the gate named ``plan``) does not use the gates table: it waits for the approval
of the campaign's plan document, read through ``harness.planner.plan_gate.check_plan_approval``. The plan source
comes from the run inputs (or the playbook's ``inputs``): ``plan_path`` or ``plan_issue``, and optionally ``plan_ref``.
Unlike the per-issue gate it asks on the repo's control issue (one comment and the needs-you label per plan sha),
because the approval belongs to the campaign, not to the run's issue.
"""

from typing import Any

from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.github.client import GitHubError
from harness.planner.plan_doc import load_plan_doc
from harness.planner.plan_gate import (
    PlanApprovalStatus,
    check_plan_approval,
    plan_gate_outcome,
    record_plan_approval,
    request_plan_approval,
)
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
        if node.scope == "campaign":
            return await self._plan(node, ctx)
        return self._human(step, node, ctx)

    async def _plan(self, node: GateNode, ctx: ExecContext) -> Waiting | Done:
        control_issue = ctx.repo_config.control_issue
        if ctx.gh is None or ctx.outbox is None or control_issue is None:
            return Done(
                "failed", {"error": "a campaign plan gate needs GitHub, an outbox and a control_issue"}
            )
        inputs = {**ctx.playbook.inputs, **(ctx.run.context.get("inputs") or {})}
        issue = inputs.get("plan_issue")
        try:
            plan = await load_plan_doc(
                ctx.gh,
                ctx.run.repo,
                path=inputs.get("plan_path"),
                issue=int(issue) if issue is not None else None,
                ref=inputs.get("plan_ref"),
            )
        except (ValueError, GitHubError) as exc:  # PlanDocError is a ValueError
            return Done("failed", {"error": f"plan gate: {exc}"})
        approval = await check_plan_approval(
            ctx.gh,
            repo=ctx.run.repo,
            control_issue=control_issue,
            plan=plan,
            owner_allowlist=ctx.repo_config.command_users,
            store=ctx.store,
        )
        if approval.fresh and approval.status is PlanApprovalStatus.APPROVED:
            # GitHub first: queue the ledger record and wait; the next poll sees it mirrored.
            record_plan_approval(
                ctx.store,
                ctx.outbox,
                repo=ctx.run.repo,
                control_issue=control_issue,
                plan=plan,
                by=approval.approved_by or "",
                via=approval.via or "label",
                label_cursor=approval.label_cursor,
            )
            return Waiting(ref={"plan_sha": plan.sha}, poll_after=POLL_AFTER)
        outcome = plan_gate_outcome(approval)
        if outcome is not None:
            output: dict[str, Any] = {
                "plan_sha": approval.approved_sha,
                "decided_by": approval.approved_by,
                "via": approval.via,
            }
            if outcome == "rejected":
                output["correction"] = approval.note or f"Gate '{node.gate}' was rejected"
            return Done(outcome, output)
        await request_plan_approval(
            ctx.gh, ctx.outbox, repo=ctx.run.repo, control_issue=control_issue, plan=plan
        )
        stale = approval.status is PlanApprovalStatus.STALE
        reason = (
            f"The plan {plan.source} changed after it was approved: approve it again"
            if stale
            else f"The plan {plan.source} needs approval"
        )
        reason += f": add the label `harness:plan-approved` to #{control_issue} or run `harness plan approve`"
        if approval.note:
            reason += f" ({approval.note})"
        return Waiting(
            ref={"plan_sha": plan.sha},
            poll_after=POLL_AFTER,
            output={
                "needs_you": True,
                "reason": reason,
                "links": [plan.url],
                "plan_sha": plan.sha,
                "plan_status": approval.status.value,
            },
        )

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
