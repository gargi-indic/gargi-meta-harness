"""The tournament judge: pick one candidate pull request, with a deterministic fallback.

The selector goes through the same ``ReviewBackend`` as the reviewer, so it gets the same read-only tool
allowlist (``READ_ONLY_TOOLS`` / ``DISALLOWED_TOOLS`` in ``harness.llm.backend``) and adds no tools of its own.
It asks for JSON, validates it, and retries once with the error on invalid output.

Fallback tiebreak (documented and tested): when the model's reply stays invalid after the retry, the backend
fails twice, or the winner it names is not a candidate, the winner is the candidate with passing CI, then the
smallest diff (additions + deletions), then the alphabetically first node name.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from harness.llm.backend import BackendError, ReviewBackend
from harness.llm.reviewer import _error_text, load_prompt, render_prompt
from harness.playbook.templating import IssueCtx


class Candidate(BaseModel):
    node: str
    pr_number: int
    head_sha: str
    agent: str
    summary: str | None = None
    # Facts the executor read from GitHub; the fallback tiebreak and the prompt use them.
    ci_passing: bool = False
    diff_size: int = 0
    diff: str | None = None


class SelectionResult(BaseModel):
    winner: str
    ranking: list[str] = []
    rationale: str
    per_candidate: dict[str, list[str]] = {}


SELECTION_SCHEMA = SelectionResult.model_json_schema()


def fallback_winner(candidates: list[Candidate]) -> Candidate:
    """Passing CI first, then the smallest diff, then the alphabetically first node name."""
    return min(candidates, key=lambda c: (not c.ci_passing, c.diff_size, c.node))


def _normalise(result: SelectionResult, candidates: list[Candidate]) -> SelectionResult:
    """Keep only known node names in the ranking, winner first, then any candidate the model left out."""
    known = [c.node for c in candidates]
    ranking = [result.winner]
    for name in [*result.ranking, *sorted(known)]:
        if name in known and name not in ranking:
            ranking.append(name)
    per_candidate = {k: v for k, v in result.per_candidate.items() if k in known}
    return SelectionResult(
        winner=result.winner, ranking=ranking, rationale=result.rationale, per_candidate=per_candidate
    )


def _fallback(candidates: list[Candidate], reason: str) -> SelectionResult:
    winner = fallback_winner(candidates)
    others = sorted((c for c in candidates if c.node != winner.node), key=_order_key)
    return SelectionResult(
        winner=winner.node,
        ranking=[winner.node, *(c.node for c in others)],
        rationale=(
            f"{reason}; chose {winner.node} by the fallback tiebreak "
            "(passing CI, then smallest diff, then node name)."
        ),
        per_candidate={},
    )


def _order_key(c: Candidate) -> tuple[bool, int, str]:
    return (not c.ci_passing, c.diff_size, c.node)


async def select_candidate(
    *,
    backend: ReviewBackend,
    issue: IssueCtx,
    candidates: list[Candidate],
    rubric: list[str] | None = None,
    workdir: Path | None = None,
) -> SelectionResult:
    """Ask the judge to pick a winner among ``candidates`` (at least one)."""
    if not candidates:
        raise ValueError("select_candidate needs at least one candidate")
    names = {c.node for c in candidates}
    system = load_prompt("system_select")
    prompt = render_prompt("select", issue=issue, candidates=candidates, rubric=list(rubric or []))
    error = ""
    for attempt in range(2):
        text = prompt
        if attempt:
            text = (
                f"{prompt}\n\nYour previous reply was invalid: {error}. "
                "Reply again with only JSON matching the schema."
            )
        try:
            response = await backend.review(system, text, SELECTION_SCHEMA, workdir)
            data = json.loads(response) if isinstance(response, str) else response
            result = SelectionResult.model_validate(data)
        except (BackendError, ValidationError, ValueError, TypeError) as e:
            error = _error_text(e)
            continue
        if result.winner in names:
            return _normalise(result, candidates)
        # A valid reply naming an unknown winner is not worth another model call.
        return _fallback(candidates, f"the judge named an unknown winner ({result.winner[:80]!r})")
    return _fallback(candidates, f"the judge returned invalid output ({error})")
