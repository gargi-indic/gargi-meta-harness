"""Merge executor: squash-merge the reviewed PR, then finalize the issue.

Every GitHub write goes through the outbox; this executor only reads (``pull``, ``issue``). The runner owns
the ``harness:done`` label and the control-table row, so nothing here enqueues ``set_labels`` or
``edit_issue_body``. Phases live in ``step.ref["phase"]``: none (decide and enqueue the merge), ``merging``
(wait for GitHub to report the PR merged), then finalize.
"""

from datetime import UTC, datetime
from typing import Any

from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.github.ledger import render_event
from harness.playbook.models import CheckNode, MergeNode, ReviewNode
from harness.store.repo import StepRow

_POLL_MERGEABLE = 10.0
_POLL_MERGING = 15.0


class MergeExecutor:
    async def advance(self, step: StepRow, node: MergeNode, ctx: ExecContext) -> Waiting | Done:
        pr = ctx.run.context.get("pr")
        number = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(number, int):
            return Done("failed", {"note": "no PR in run context"})
        if ctx.gh is None or ctx.outbox is None:
            raise RuntimeError("merge executors need a GitHub client and an outbox")

        if step.ref and step.ref.get("phase") == "merging":
            return await self._merging(step, ctx, number)
        return await self._first(node, ctx, number)

    # A. decide ---------------------------------------------------------------------------------------------

    async def _first(self, node: MergeNode, ctx: ExecContext, number: int) -> Waiting | Done:
        assert ctx.gh is not None and ctx.outbox is not None
        repo = ctx.run.repo
        pull = await ctx.gh.pull(repo, number)

        # Idempotency guard: a crash after the merge must never lead to a second merge call.
        if pull.get("merged"):
            return await self._finalize(ctx, number, pull)
        if pull.get("state") == "closed":
            return Done("failed", {"note": "PR closed without merge"})

        head = str(pull["head"]["sha"])
        reviewed = _reviewed_shas(ctx)
        if not reviewed:
            return Done("failed", {"note": "no reviewed head sha"})
        if any(sha != head for sha in reviewed):
            return Done(
                "failed", {"note": "head changed since review", "head_sha": head, "reviewed": reviewed}
            )

        mergeable = pull.get("mergeable")
        if mergeable is None:
            return Waiting(ref=None, poll_after=_POLL_MERGEABLE)
        if mergeable is False:
            return Done("failed", {"note": f"not mergeable: {pull.get('mergeable_state')}", "head_sha": head})

        outbox_id = ctx.outbox.enqueue(
            "merge_pull",
            {
                "repo": repo,
                "number": number,
                "method": node.method or ctx.repo_config.merge.method,
                "sha": head,
            },
            coalesce_key=f"merge:{repo}#{number}",
        )
        return Waiting(
            ref={"phase": "merging", "outbox_id": outbox_id, "sha": head}, poll_after=_POLL_MERGING
        )

    # B. wait for the merge ---------------------------------------------------------------------------------

    async def _merging(self, step: StepRow, ctx: ExecContext, number: int) -> Waiting | Done:
        assert ctx.gh is not None and step.ref is not None
        pull = await ctx.gh.pull(ctx.run.repo, number)
        if pull.get("merged"):
            return await self._finalize(ctx, number, pull)

        rows = ctx.store.query(
            "SELECT status, last_error FROM outbox WHERE id = ?", (step.ref.get("outbox_id"),)
        )
        if rows and rows[0]["status"] == "failed":
            return Done("failed", {"note": f"merge failed: {rows[0]['last_error']}"})
        return Waiting(ref=step.ref, poll_after=_POLL_MERGING)

    # C. finalize -------------------------------------------------------------------------------------------

    async def _finalize(self, ctx: ExecContext, number: int, pull: dict[str, Any]) -> Done:
        assert ctx.gh is not None and ctx.outbox is not None
        repo, issue = ctx.run.repo, ctx.run.issue
        head = pull["head"]
        head_repo = (head.get("repo") or {}).get("full_name")

        # Only after the merge was observed, and never for a fork's branch.
        if ctx.repo_config.merge.delete_branch and head_repo == repo and head.get("ref"):
            branch = str(head["ref"])
            ctx.outbox.enqueue(
                "delete_branch",
                {"repo": repo, "branch": branch},
                coalesce_key=f"delete_branch:{repo}:{branch}",
            )

        ctx.outbox.enqueue("tick_boxes", {"repo": repo, "issue": issue}, coalesce_key=f"tick:{repo}#{issue}")

        issue_data = await ctx.gh.issue(repo, issue)
        if issue_data.get("state") != "closed":
            ctx.outbox.enqueue(
                "close_issue", {"repo": repo, "issue": issue}, coalesce_key=f"close:{repo}#{issue}"
            )

        merge_sha = pull.get("merge_commit_sha")
        short = str(merge_sha or head["sha"])[:7]
        today = datetime.fromtimestamp(ctx.clock(), UTC).date().isoformat()
        ctx.outbox.enqueue(
            "add_comment",
            {
                "repo": repo,
                "issue": issue,
                "body": render_event(today, issue, "merged", [f"PR #{number} merged as `{short}`"]),
            },
            coalesce_key=f"merged:{repo}#{issue}",
        )
        return Done("merged", {"pr": number, "merge_sha": merge_sha, "head_sha": head["sha"]})


def _reviewed_shas(ctx: ExecContext) -> list[str]:
    """The head shas that the check nodes and PR review nodes saw, in playbook order, without repeats."""
    shas: list[str] = []
    for node_id, output in ctx.template.nodes.items():
        node = ctx.playbook.nodes.get(node_id)
        is_pr_gate = isinstance(node, CheckNode) or (isinstance(node, ReviewNode) and node.target == "pr")
        sha = output.get("head_sha") if isinstance(output, dict) else None
        if is_pr_gate and sha and str(sha) not in shas:
            shas.append(str(sha))
    return shas
