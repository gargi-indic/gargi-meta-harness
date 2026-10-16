"""Review executor: ask the LLM reviewer to judge a plan, a pull request or an agent question.

A review is a single call: ``advance`` always returns ``Done`` with the verdict as its outcome. The result is
stored on the step before the PR comment is enqueued, so a re-advance after a crash neither asks the model
again nor posts a second comment. The runner already mirrors the verdict to the issue ledger; this executor
adds only the PR comment (``--request-changes`` fails on PRs opened under the user's own account).
"""

from typing import Any

from harness.engine.executors.base import Done, ExecContext
from harness.llm.backend import ReviewBackend
from harness.llm.reviewer import (
    ReviewResult,
    load_builtin_checklist,
    load_prompt,
    render_prompt,
    run_review,
    valid_checklist_name,
)
from harness.playbook.models import ReviewNode
from harness.secrets import redact
from harness.store.repo import StepRow, append_event, get_issue, steps_for_run, update_step

_TRUNCATED = "\n[... truncated]"
_VERDICTS = ("approve", "changes", "escalate")


class ReviewExecutor:
    def __init__(
        self, backend: ReviewBackend, *, diff_budget: int = 60_000, per_file_budget: int = 8_000
    ) -> None:
        self.backend = backend
        self.diff_budget = diff_budget
        self.per_file_budget = per_file_budget

    async def advance(self, step: StepRow, node: ReviewNode, ctx: ExecContext) -> Done:
        stored = step.output
        if isinstance(stored, dict) and stored.get("verdict") in _VERDICTS:
            # Re-advance after a crash: the model already answered. Only the (idempotent) comment may be missing.
            if node.target == "pr" and isinstance(stored.get("head_sha"), str):
                self._post_comment(step, ctx, stored)
            return Done(str(stored["verdict"]), dict(stored))

        system = load_prompt("system_review")
        if node.target == "plan":
            built = await self._plan_prompt(node, ctx)
        elif node.target == "pr":
            built = await self._pr_prompt(node, ctx)
        else:
            built = await self._question_prompt(node, ctx)
        if isinstance(built, Done):
            return built
        prompt, extra = built

        result = await run_review(self.backend, system=system, prompt=prompt, workdir=None)
        output = _output(result)
        output.update(extra)
        update_step(ctx.store, step.id, output=output)
        if node.target == "pr":
            self._post_comment(step, ctx, output)
        return Done(result.verdict, output)

    # prompts -----------------------------------------------------------------------------------------------

    async def _plan_prompt(self, node: ReviewNode, ctx: ExecContext) -> tuple[str, dict[str, Any]] | Done:
        plan = _text((ctx.template.prev or {}).get("plan")) or _latest_plan(ctx)
        if not plan:
            return _escalate("no plan to review")
        prompt = render_prompt(
            "plan_review",
            issue=self._issue(ctx),
            plan=redact(plan),
            checklists=await self._checklists(node, ctx),
            repo_rules=self._rules(ctx),
        )
        return prompt, {}

    async def _question_prompt(self, node: ReviewNode, ctx: ExecContext) -> tuple[str, dict[str, Any]] | Done:
        question = _text((ctx.template.prev or {}).get("question"))
        if not question:
            return _escalate("no question to answer")
        prompt = render_prompt(
            "question",
            issue=self._issue(ctx),
            question=redact(question),
            plan=redact(_latest_plan(ctx) or ""),
            checklists=await self._checklists(node, ctx),
        )
        return prompt, {}

    async def _pr_prompt(self, node: ReviewNode, ctx: ExecContext) -> tuple[str, dict[str, Any]] | Done:
        pr = ctx.run.context.get("pr")
        number = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(number, int):
            return _escalate("no PR in run context")
        if ctx.gh is None:
            raise RuntimeError("review executors with target 'pr' need a GitHub client")

        repo = ctx.run.repo
        # Read the head first: files fetched afterwards can only be newer, and the merge executor refuses a
        # PR whose head differs from this sha.
        head_sha = str((await ctx.gh.pull(repo, number))["head"]["sha"])
        raw_files = await ctx.gh.pull_files(repo, number)
        files, omitted = self._budget_files(raw_files)
        ci = ctx.template.nodes.get("ci")
        prompt = render_prompt(
            "pr_review",
            issue=self._issue(ctx),
            approved_plan=redact(_latest_plan(ctx) or "") or None,
            files=files,
            truncated=bool(omitted),
            omitted=omitted,
            ci=_scrub(ci) if ci else None,
            checklists=await self._checklists(node, ctx),
            repo_rules=self._rules(ctx),
        )
        return prompt, {"head_sha": head_sha}

    def _budget_files(self, raw_files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        files: list[dict[str, Any]] = []
        omitted: list[str] = []
        total = 0
        full = False
        for item in raw_files:
            name = redact(str(item.get("filename", "")))
            if full:
                omitted.append(name)
                continue
            # Redact before cutting so a secret split by the cut cannot survive as a prefix.
            patch = redact(str(item.get("patch") or ""))
            if len(patch) > self.per_file_budget:
                patch = patch[: self.per_file_budget] + _TRUNCATED
            if total + len(patch) > self.diff_budget:
                full = True
                omitted.append(name)
                continue
            total += len(patch)
            files.append(
                {
                    "filename": name,
                    "status": redact(str(item.get("status", ""))),
                    "additions": item.get("additions", 0),
                    "deletions": item.get("deletions", 0),
                    "patch": patch,
                }
            )
        return files, omitted

    def _issue(self, ctx: ExecContext) -> dict[str, Any]:
        row = get_issue(ctx.store, ctx.run.repo, ctx.run.issue)
        return {
            "number": ctx.run.issue,
            "title": redact(row.title if row else ""),
            "body": redact((row.body if row else "") or ""),
        }

    def _rules(self, ctx: ExecContext) -> dict[str, Any]:
        return {"protected_paths": [redact(p) for p in ctx.repo_config.protected_paths]}

    async def _checklists(self, node: ReviewNode, ctx: ExecContext) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        for name in node.checklist:
            text: str | None = None
            if valid_checklist_name(name):
                if ctx.gh is not None:
                    # The repo's own checklist wins over the built-in one.
                    text = await ctx.gh.file_text(ctx.run.repo, f".harness/checklists/{name}.md")
                if text is None:
                    text = load_builtin_checklist(name)
            if text is None:
                append_event(
                    ctx.store,
                    kind="checklist_missing",
                    data={"name": name},
                    repo=ctx.run.repo,
                    issue=ctx.run.issue,
                    run_id=ctx.run.id,
                )
                continue
            found.append({"name": redact(name), "text": redact(text.strip())})
        return found

    # PR comment --------------------------------------------------------------------------------------------

    def _post_comment(self, step: StepRow, ctx: ExecContext, output: dict[str, Any]) -> None:
        if ctx.outbox is None:
            return
        pr = ctx.run.context.get("pr")
        number = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(number, int):
            return
        key = f"review:{step.id}"
        marker = f"<!-- harness-review:{step.id} -->"
        # The coalesce key only merges rows that are still pending (a sent row loses it), so after a crash the
        # comment may already have been sent. The hidden marker in the body tells either case apart.
        sent_or_queued = ctx.store.query(
            "SELECT 1 FROM outbox WHERE op = 'add_comment' AND instr(payload_json, ?) > 0", (marker,)
        )
        if sent_or_queued:
            return
        body = f"{_comment_body(output)}\n\n{marker}"
        ctx.outbox.enqueue(
            "add_comment", {"repo": ctx.run.repo, "issue": number, "body": body}, coalesce_key=key
        )


def _comment_body(output: dict[str, Any]) -> str:
    lines = [f"**Harness review: {output['verdict']}**", "", str(output.get("summary", ""))]
    findings = output.get("findings") or []
    if findings:
        lines += [
            "",
            "| Severity | File | Problem | Required change |",
            "| --- | --- | --- | --- |",
        ]
        for f in findings:
            file = f.get("file")
            cells = [
                f["severity"],
                f"`{_cell(file)}`" if file else "",
                _cell(f["problem"]),
                _cell(f["required_change"]),
            ]
            lines.append("| " + " | ".join(cells) + " |")
    if output.get("correction"):
        lines += ["", "### Required changes", "", str(output["correction"])]
    return redact("\n".join(lines))


def _cell(text: Any) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _output(result: ReviewResult) -> dict[str, Any]:
    return {
        "verdict": result.verdict,
        "findings": [f.model_dump() for f in result.findings],
        "correction": result.correction,
        "summary": result.summary,
    }


def _escalate(summary: str) -> Done:
    return Done("escalate", {"verdict": "escalate", "findings": [], "correction": "", "summary": summary})


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(str(k)): _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def _latest_plan(ctx: ExecContext) -> str | None:
    """The plan of the most recent finished step of this run that produced one."""
    best: tuple[str, int] | None = None
    plan: str | None = None
    for s in steps_for_run(ctx.store, ctx.run.id):
        output = s.output
        if s.status != "done" or not isinstance(output, dict):
            continue
        text = _text(output.get("plan")).strip()
        if not text:
            continue
        key = (s.ended_at or "", s.visit)
        if best is None or key >= best:
            best, plan = key, _text(output.get("plan"))
    return plan
