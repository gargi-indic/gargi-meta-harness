"""Decompose executor: turn the campaign's plan into a stored ``ProposedPlan``, and optionally create the issues.

``advance`` loads the plan (a repo file or an issue, the same loader as the plan gate), then looks for a proposal
in this order (``harness.planner.proposal_store``; GitHub is the state store):

1. the draft that the user edited and validated in the Planner for this plan sha (stored under a run id of the
   Planner API, ``<repo>#planner-<token>``): it is copied to this run and used as it is, the model is not asked;
2. the proposal of this run for this plan sha (the local mirror first, then the proposal comments on the control
   issue);
3. otherwise it builds the repository context and calls ``decompose()`` (one LLM conversation, repaired up to
   twice).

The proposal is saved GitHub first before the step finishes, so a crash after the model answered re-advances into
the stored proposal and never asks the model again. The plan sha is part of the key: a plan that changed since gets
a new decomposition. A node that is entered again after a rejection (visit 2 or later, ``create: false``) asks the
model again with its ``guidance`` (which can use ``prev.correction``) unless a Planner draft exists; the new
proposal is tagged with the step id, so a crash and re-advance of that same step reuses it.

Outcomes (the node's ports):

* ``proposed``: the proposal is stored; the step output holds ``proposal`` (so ``{{ decompose.proposal.issues |
  length }}`` works in templates), ``plan_sha``, ``plan`` and ``issue_count``;
* ``created`` (``create: true`` only): the issues were created by ``materialize`` (or the injected ``MaterializeFn``)
  and the output also holds ``created`` with the epic number and the issue number of every proposal key;
* ``invalid``: no valid proposal after the repair attempts (or the stored proposal no longer passes creation checks);
  ``errors`` keeps ``{code, key, message}`` of every problem;
* ``failed``: the plan cannot be read, GitHub or the outbox failed, or the model backend was down on every attempt.

The executor writes only through the planner's own paths (proposal comments and the ledger's ``proposal`` key); the
labels and the ledger of the run are the runner's.
"""

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from harness.adapters.base import Capabilities
from harness.engine.executors.base import Done, ExecContext, Waiting
from harness.github.client import GitHub, GitHubError
from harness.github.outbox import Outbox, OutboxOpFailed
from harness.llm.backend import ReviewBackend, usage_sink
from harness.llm.decomposer import DecompositionError, decompose
from harness.ops.costs import llm_usage_sink
from harness.planner.context import RepoContext, build_repo_context
from harness.planner.materialize import (
    MaterializeContext,
    MaterializeError,
    MaterializeResult,
    materialize,
    plan_run_key,
)
from harness.planner.options import enabled_agents, repo_playbooks
from harness.planner.plan_doc import PlanDoc, PlanDocError, load_plan_doc
from harness.planner.proposal import ProposalIssue, ProposedPlan
from harness.planner.proposal_store import (
    StoredProposal,
    find_planner_draft,
    get_proposal,
    save_proposal,
    update_proposal,
)
from harness.playbook.models import DecomposeNode, Playbook
from harness.playbook.templating import TemplateError, render
from harness.secrets import redact
from harness.store.repo import StepRow

MaterializeFn = Callable[[ProposedPlan, MaterializeContext], Awaitable[MaterializeResult]]

STATUS_PROPOSED = "proposed"
STATUS_CREATED = "created"
META_ADOPTED_FROM = "adopted_from"  # the Planner run id that a copied draft came from
META_STEP = "step"  # the step that asked the model again after a rejection
_ISSUE_SOURCE = re.compile(r"^#?(\d+)$")


class DecomposeExecutor:
    def __init__(self, backend: ReviewBackend, *, materialize_fn: MaterializeFn = materialize) -> None:
        self.backend = backend
        # What ``create: true`` calls once there is a proposal; tests inject a fake.
        self.materialize_fn = materialize_fn

    async def advance(self, step: StepRow, node: DecomposeNode, ctx: ExecContext) -> Waiting | Done:
        control_issue = ctx.repo_config.control_issue
        if ctx.gh is None or ctx.outbox is None or control_issue is None:
            return Done("failed", {"error": "a decompose node needs GitHub, an outbox and a control_issue"})
        try:
            return await self._run(step, node, ctx, ctx.gh, control_issue)
        except (PlanDocError, GitHubError, OutboxOpFailed, TemplateError, ValueError) as exc:
            return Done("failed", {"error": redact(f"{type(exc).__name__}: {exc}")})

    async def _run(
        self, step: StepRow, node: DecomposeNode, ctx: ExecContext, gh: GitHub, control_issue: int
    ) -> Done:
        repo, run_id = ctx.run.repo, ctx.run.id
        outbox = ctx.outbox
        assert outbox is not None
        plan_doc = await _load_plan(node, ctx, gh)

        playbooks: dict[str, Playbook] | None = None
        draft = await find_planner_draft(gh, repo=repo, control_issue=control_issue, plan_sha=plan_doc.sha)
        if draft is not None:
            stored = await _adopt(ctx, outbox, control_issue, plan_doc.sha, draft)
        else:
            stored = await get_proposal(
                ctx.store, gh, repo=repo, control_issue=control_issue, run_id=run_id, plan_sha=plan_doc.sha
            )
            if stored is not None and _rejected_before(step, node, stored):
                stored = None
        reused = stored is not None
        if stored is None:
            playbooks = await _playbooks(gh, repo)
            guidance = None
            if node.guidance is not None:
                guidance = render(node.guidance, ctx.template, path="decompose.guidance").strip() or None
            try:
                repo_context = await _repo_context(gh, ctx, plan_doc)
                with usage_sink(llm_usage_sink(ctx.store, run_id, step.id, "decompose")):
                    proposal = await decompose(
                        backend=self.backend,
                        plan_doc=plan_doc,
                        repo_context=repo_context,
                        agents=_agents(ctx),
                        playbooks=playbooks,
                        guidance=guidance,
                        max_issues=node.max_issues,
                    )
            except DecompositionError as exc:
                return _refused(exc.issues, plan_doc, str(exc))
            stored = await save_proposal(
                ctx.store,
                outbox,
                repo=repo,
                control_issue=control_issue,
                run_id=run_id,
                plan_sha=plan_doc.sha,
                proposal=proposal,
                status=STATUS_PROPOSED,
                meta={META_STEP: step.id} if step.visit > 1 and not node.create else None,
            )

        output = _output(stored, plan_doc, reused)
        if draft is not None:
            output["draft"] = draft.run_id
        if not node.create:
            return Done("proposed", output)

        if playbooks is None:
            playbooks = await _playbooks(gh, repo)
        mctx = _materialize_context(node, ctx, control_issue, stored, sorted(playbooks))
        try:
            result = await self.materialize_fn(stored.proposal, mctx)
        except MaterializeError as exc:
            if exc.problems:
                return _refused(exc.problems, plan_doc, str(exc))
            return Done("failed", {**output, "error": redact(str(exc))})
        if stored.status != STATUS_CREATED:
            await update_proposal(
                ctx.store,
                outbox,
                repo=repo,
                control_issue=control_issue,
                run_id=run_id,
                plan_sha=plan_doc.sha,
                status=STATUS_CREATED,
            )
            output["status"] = STATUS_CREATED
        output["created"] = result.model_dump(mode="json")
        return Done("created", output)


# ---- helpers --------------------------------------------------------------------------------------------


def _output(stored: StoredProposal, plan_doc: PlanDoc, reused: bool) -> dict[str, Any]:
    return {
        "proposal": stored.proposal.model_dump(mode="json"),
        "plan_sha": stored.plan_sha,
        "plan": plan_doc.source,
        "status": stored.status,
        "issue_count": len(stored.proposal.issues),
        "reused": reused,
    }


def _refused(problems: list[ProposalIssue], plan_doc: PlanDoc, message: str) -> Done:
    """``invalid`` with every error code kept; ``failed`` when the backend was the only problem."""
    errors = [{"code": p.code, "key": p.key, "message": redact(p.message)} for p in problems]
    output = {"error": redact(message), "errors": errors, "plan_sha": plan_doc.sha, "plan": plan_doc.source}
    if problems and all(p.code == "backend_error" for p in problems):
        return Done("failed", output)
    return Done("invalid", output)


async def _load_plan(node: DecomposeNode, ctx: ExecContext, gh: GitHub) -> PlanDoc:
    inputs = {**ctx.playbook.inputs, **(ctx.run.context.get("inputs") or {})}
    ref = inputs.get("plan_ref")
    path: str | None = None
    issue: int | None = None
    if node.plan is not None:
        source = render(node.plan, ctx.template, path="decompose.plan").strip()
        found = _ISSUE_SOURCE.match(source)
        if found:
            issue = int(found.group(1))
        elif source:
            path = source
    else:
        path = inputs.get("plan_path")
        issue = int(inputs["plan_issue"]) if inputs.get("plan_issue") is not None else None
    return await load_plan_doc(gh, ctx.run.repo, path=path, issue=issue, ref=ref)


async def _repo_context(gh: GitHub, ctx: ExecContext, plan_doc: PlanDoc) -> RepoContext:
    return await build_repo_context(gh, ctx.run.repo, ctx.repo_config, plan_doc.ref)


def _agents(ctx: ExecContext) -> Mapping[str, Capabilities]:
    """The registered adapters, minus the ones that the repo config switches off."""
    caps = {name: ctx.adapters.get(name).caps for name in ctx.adapters.names()}
    return enabled_agents(caps, ctx.repo_config.agents)


async def _playbooks(gh: GitHub, repo: str) -> dict[str, Playbook]:
    """The built-in playbooks plus the valid ones in the repo's ``.harness/playbooks`` (those win by id)."""
    return await repo_playbooks(gh, repo)


async def _adopt(
    ctx: ExecContext, outbox: Outbox, control_issue: int, plan_sha: str, draft: StoredProposal
) -> StoredProposal:
    """Copy the Planner's draft to this run (once; the copy is kept while the draft stays the same).

    The status of the copy starts at ``proposed`` and follows this run's creation like any proposal.
    """
    mine = await get_proposal(
        ctx.store,
        outbox.gh,
        repo=ctx.run.repo,
        control_issue=control_issue,
        run_id=ctx.run.id,
        plan_sha=plan_sha,
    )
    if (
        mine is not None
        and mine.proposal == draft.proposal
        and mine.meta.get(META_ADOPTED_FROM) == draft.run_id
    ):
        return mine
    return await save_proposal(
        ctx.store,
        outbox,
        repo=ctx.run.repo,
        control_issue=control_issue,
        run_id=ctx.run.id,
        plan_sha=plan_sha,
        proposal=draft.proposal,
        status=STATUS_PROPOSED,
        meta={META_ADOPTED_FROM: draft.run_id},
    )


def _rejected_before(step: StepRow, node: DecomposeNode, stored: StoredProposal) -> bool:
    """True when this step is a later visit that has not produced its own proposal yet: it must ask again."""
    return step.visit > 1 and not node.create and stored.meta.get(META_STEP) != step.id


def _materialize_context(
    node: DecomposeNode, ctx: ExecContext, control_issue: int, stored: StoredProposal, playbooks: list[str]
) -> MaterializeContext:
    assert ctx.gh is not None and ctx.outbox is not None
    # A copied Planner draft is created under the key that the Planner API uses (the plan's content), so issues that
    # the Planner already made are found again and never created twice.
    if stored.meta.get(META_ADOPTED_FROM):
        run_key = plan_run_key(stored.proposal)
    else:
        run_key = _run_key(ctx.run.id, stored.plan_sha)
    return MaterializeContext(
        repo=ctx.run.repo,
        gh=ctx.gh,
        outbox=ctx.outbox,
        store=ctx.store,
        control_issue=control_issue,
        run_key=run_key,
        agents=sorted(_agents(ctx)),
        playbooks=playbooks,
        max_issues=node.max_issues,
    )


def _run_key(run_id: str, plan_sha: str) -> str:
    """The materialization key of a run and plan version: repeating the step resumes the same creation."""
    return "run-" + hashlib.sha256(f"{run_id}\n{plan_sha}".encode()).hexdigest()[:16]
