"""The decomposer: ask the LLM for a ``ProposedPlan``, validate it, and repair it up to twice.

It goes through the same ``ReviewBackend`` as the reviewer and the selector (so no SDK import here) and
returns a proposal only; it creates nothing on GitHub. Untrusted text (the plan, repository files, issue
titles, operator guidance) reaches the prompt only as a template variable wrapped by ``fence``.

Invalid output, a failing backend and a proposal that ``validate_proposal`` rejects are all failed attempts.
After the first attempt there are at most ``MAX_REPAIRS`` repair attempts that feed the error list back to the
model; then ``DecompositionError`` carries the errors of the last attempt. Codes that ``validate_proposal``
does not produce, used for output that never became a proposal: ``invalid_json``, ``invalid_plan`` and
``backend_error``.
"""

import json
from collections.abc import Collection, Mapping
from pathlib import Path

from pydantic import ValidationError

from harness.adapters.base import Capabilities
from harness.llm.backend import BackendError, ReviewBackend
from harness.llm.reviewer import _error_text, load_prompt, render_prompt
from harness.planner.context import RepoContext
from harness.planner.plan_doc import PlanDoc
from harness.planner.proposal import (
    DEFAULT_MAX_ISSUES,
    ProposalIssue,
    ProposedPlan,
    proposal_json_schema,
    validate_proposal,
)
from harness.playbook.models import Playbook
from harness.secrets import redact

MAX_REPAIRS = 2
_FEEDBACK_MAX = 3000

DECOMPOSE_SCHEMA = proposal_json_schema()


class DecompositionError(Exception):
    """No valid proposal after the repair attempts; ``issues`` are the problems of the last attempt."""

    def __init__(self, issues: list[ProposalIssue]) -> None:
        self.issues = issues
        shown = "; ".join(_format_issue(i) for i in issues[:5])
        more = f" (and {len(issues) - 5} more)" if len(issues) > 5 else ""
        super().__init__(f"could not produce a valid proposal: {shown}{more}")


def _format_issue(issue: ProposalIssue) -> str:
    where = f" {issue.key}:" if issue.key else ""
    return f"[{issue.code}]{where} {issue.message}"


def _feedback(issues: list[ProposalIssue]) -> str:
    text = "\n".join(f"- {_format_issue(i)}" for i in issues)
    return redact(text)[:_FEEDBACK_MAX]


def _capability_rows(agents: Mapping[str, Capabilities]) -> list[dict[str, object]]:
    rows = []
    for name in sorted(agents):
        caps = agents[name]
        have = caps.names()
        rows.append(
            {
                "name": name,
                "plan": "plan" in have,
                "message": "message" in have,
                "pr": "pr" in have,
                "local": "local" in have,
                "max_concurrency": caps.max_concurrency,
            }
        )
    return rows


def _playbook_rows(playbooks: Mapping[str, Playbook] | Collection[str]) -> list[dict[str, str]]:
    rows = []
    for pid in sorted(playbooks):
        pb = playbooks[pid] if isinstance(playbooks, Mapping) else None
        description = getattr(pb, "description", None) or ""
        rows.append({"id": pid, "description": " ".join(description.split())})
    return rows


def render_decompose_prompt(
    *,
    plan_doc: PlanDoc,
    repo_context: RepoContext,
    agents: Mapping[str, Capabilities],
    playbooks: Mapping[str, Playbook] | Collection[str],
    guidance: str | None = None,
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> str:
    return render_prompt(
        "decompose",
        plan=plan_doc,
        repo=repo_context,
        agents=_capability_rows(agents),
        playbooks=_playbook_rows(playbooks),
        guidance=guidance,
        max_issues=max_issues,
    )


async def decompose(
    *,
    backend: ReviewBackend,
    plan_doc: PlanDoc,
    repo_context: RepoContext,
    agents: Mapping[str, Capabilities],
    playbooks: Mapping[str, Playbook] | Collection[str],
    guidance: str | None = None,
    max_issues: int = DEFAULT_MAX_ISSUES,
    workdir: Path | None = None,
) -> ProposedPlan:
    """Return a proposal that passes ``validate_proposal``, or raise ``DecompositionError``."""
    system = load_prompt("system_decompose")
    prompt = render_decompose_prompt(
        plan_doc=plan_doc,
        repo_context=repo_context,
        agents=agents,
        playbooks=playbooks,
        guidance=guidance,
        max_issues=max_issues,
    )
    problems: list[ProposalIssue] = []
    for attempt in range(1 + MAX_REPAIRS):
        text = prompt
        if attempt:
            text = (
                f"{prompt}\n\nYour previous reply was rejected for these reasons:\n{_feedback(problems)}\n\n"
                "Reply again with only the corrected JSON object matching the schema."
            )
        try:
            response = await backend.review(system, text, DECOMPOSE_SCHEMA, workdir)
        except BackendError as e:
            problems = [ProposalIssue("backend_error", None, _error_text(e))]
            continue
        try:
            data = json.loads(response) if isinstance(response, str) else response
        except json.JSONDecodeError as e:
            problems = [ProposalIssue("invalid_json", None, _error_text(e))]
            continue
        try:
            proposal = ProposedPlan.model_validate(data)
        except ValidationError as e:
            problems = [ProposalIssue("invalid_plan", None, _error_text(e))]
            continue
        problems = validate_proposal(proposal, agents=agents, playbooks=playbooks, max_issues=max_issues)
        if not problems:
            return proposal
    raise DecompositionError(problems)
