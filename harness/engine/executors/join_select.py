"""Join ``select`` mode: a judge picks one candidate PR among the branches, the losers are closed.

Called by ``JoinExecutor`` once every input branch has finished. Candidates are the branches that finished
without failing and produced a PR (``output["pr"]``); the rest are excluded. No candidate fails the join, one
candidate wins without a model call, several go to ``select_candidate``.

Crash safety: the selection is stored in the join step's own ``output`` before anything is written to GitHub,
so a re-advance reuses it and never asks the model again. Every GitHub write is guarded: a comment carries the
marker ``<!-- harness:select run=<id> step=<id> -->`` and is skipped when a comment with the marker exists, a PR
is closed only while it is open, and branch deletion already tolerates a missing branch.

The output carries ``pr`` (the winner's PR dict) so the runner copies it into the run context and the
following ``ci`` / ``pr_review`` / ``merge`` nodes work on the winner unchanged.
"""

from typing import Any

from harness.engine.executors.base import Done, ExecContext
from harness.llm.backend import ReviewBackend, usage_sink
from harness.llm.selector import Candidate, SelectionResult, select_candidate
from harness.ops.costs import llm_usage_sink
from harness.playbook.models import JoinNode
from harness.secrets import redact
from harness.store.repo import StepRow, update_step

_DIFF_BUDGET = 20_000
_PER_FILE_BUDGET = 4_000
_EXCERPT = 300
_TRUNCATED = "\n[... truncated]"
_CI_OK = ("success", "neutral", "skipped")


def marker(ctx: ExecContext, step: StepRow) -> str:
    return f"<!-- harness:select run={ctx.run.id} step={step.id} -->"


def _pr_number(output: dict[str, Any]) -> int | None:
    pr = output.get("pr")
    number = pr.get("number") if isinstance(pr, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) else None


async def select_join(
    step: StepRow,
    node: JoinNode,
    ctx: ExecContext,
    *,
    candidates: dict[str, StepRow],
    failed: list[str],
    backend: ReviewBackend | None,
) -> Done:
    stored = step.output
    if isinstance(stored, dict) and isinstance(stored.get("selection"), dict):
        # Re-advance after a crash: the choice is made, only the (guarded) GitHub writes may be missing.
        return await _apply(step, ctx, stored)

    with_pr = {n: s for n, s in candidates.items() if _pr_number(s.output or {}) is not None}
    if not with_pr:
        return Done("failed", {"note": "no candidate produced a PR", "failed": failed, "candidates": []})
    if ctx.gh is None:
        raise RuntimeError("join select needs a GitHub client")

    facts: dict[str, dict[str, Any]] = {}
    for name, cstep in sorted(with_pr.items()):
        facts[name] = await _facts(ctx, name, cstep)

    if len(facts) == 1:
        (winner,) = facts
        rationale = f"{winner} was the only branch that produced a PR."
        result = SelectionResult(winner=winner, ranking=[winner], rationale=rationale, per_candidate={})
    else:
        if backend is None:
            raise RuntimeError("join select with several candidates needs a selector backend")
        with usage_sink(llm_usage_sink(ctx.store, ctx.run.id, step.id, "select")):
            result = await select_candidate(
                backend=backend,
                issue=ctx.template.issue,
                candidates=[f["candidate"] for f in facts.values()],
                rubric=node.rubric or [],
            )

    winner = result.winner
    output: dict[str, Any] = {
        "selection": result.model_dump(),
        "winner": winner,
        "winner_pr": facts[winner]["number"],
        "pr": with_pr[winner].output["pr"],  # type: ignore[index]  # filtered on a PR number above
        "losers": [
            {"node": n, "pr": f["number"], "branch": f["branch"]} for n, f in facts.items() if n != winner
        ],
        "failed": failed,
    }
    update_step(ctx.store, step.id, output=output)
    ctx.crash("select_after_choose")
    return await _apply(step, ctx, output)


async def _facts(ctx: ExecContext, name: str, cstep: StepRow) -> dict[str, Any]:
    assert ctx.gh is not None
    repo = ctx.run.repo
    output = cstep.output or {}
    number = _pr_number(output)
    assert number is not None
    pull = await ctx.gh.pull(repo, number)
    head = pull.get("head") or {}
    sha = str(head.get("sha") or (output["pr"].get("head_sha") or ""))
    size = int(pull.get("additions") or 0) + int(pull.get("deletions") or 0)
    files = await ctx.gh.pull_files(repo, number)
    summary = output.get("summary")
    candidate = Candidate(
        node=name,
        pr_number=number,
        head_sha=sha,
        agent=str((cstep.ref or {}).get("agent") or "unknown"),
        summary=redact(str(summary or pull.get("title") or "")) or None,
        ci_passing=await _ci_passing(ctx, sha),
        diff_size=size,
        diff=_diff(files),
    )
    same_repo = (head.get("repo") or {}).get("full_name") == repo
    return {
        "candidate": candidate,
        "number": number,
        "branch": str(head["ref"]) if same_repo and head.get("ref") else None,
    }


async def _ci_passing(ctx: ExecContext, sha: str) -> bool:
    assert ctx.gh is not None
    if not sha:
        return False
    runs = await ctx.gh.check_runs(ctx.run.repo, sha)
    status = await ctx.gh.combined_status(ctx.run.repo, sha)
    if any(r.get("status") != "completed" or r.get("conclusion") not in _CI_OK for r in runs):
        return False
    if status.get("statuses") and status.get("state") != "success":
        return False
    return bool(runs) or bool(status.get("statuses"))


def _diff(files: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    total = 0
    for f in files:
        patch = redact(str(f.get("patch") or ""))
        if len(patch) > _PER_FILE_BUDGET:
            patch = patch[:_PER_FILE_BUDGET] + _TRUNCATED
        head = f"--- {redact(str(f.get('filename', '')))} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
        if total + len(patch) > _DIFF_BUDGET:
            parts.append(f"{head}\n[omitted: diff budget reached]")
            continue
        total += len(patch)
        parts.append(f"{head}\n{patch}")
    return "\n".join(parts) or None


async def _apply(step: StepRow, ctx: ExecContext, output: dict[str, Any]) -> Done:
    """Comment on the winner, then comment on, close and (when configured) delete the branch of each loser."""
    assert ctx.gh is not None
    gh, repo = ctx.gh, ctx.run.repo
    tag = marker(ctx, step)
    result = SelectionResult.model_validate(output["selection"])
    winner_pr = output["winner_pr"]
    issue = ctx.run.issue
    excerpt = " ".join(redact(result.rationale).split())[:_EXCERPT]

    losers = output["losers"]
    if losers:
        await _comment_once(ctx, winner_pr, tag, f"Selected in tournament for #{issue}: {excerpt}\n\n{tag}")
    for i, loser in enumerate(losers):
        number = loser["pr"]
        body = f"Not selected in tournament for #{issue} (winner: PR #{winner_pr}): {excerpt}\n\n{tag}"
        await _comment_once(ctx, number, tag, body)
        if i == 0:
            ctx.crash("select_after_loser_comment")
        pull = await gh.pull(repo, number)
        if pull.get("state") == "open":
            await gh.patch(f"repos/{repo}/pulls/{number}", state="closed")
        if ctx.repo_config.merge.delete_branch and loser.get("branch"):
            await gh.delete_branch(repo, loser["branch"])
    done = {k: v for k, v in output.items() if k != "selection"}
    done["rationale"] = result.rationale
    done["ranking"] = result.ranking
    return Done("done", done)


async def _comment_once(ctx: ExecContext, number: int, tag: str, body: str) -> None:
    assert ctx.gh is not None
    existing = await ctx.gh.comments(ctx.run.repo, number)
    if any(tag in str(c.get("body") or "") for c in existing):
        return
    await ctx.gh.add_comment(ctx.run.repo, number, body)
