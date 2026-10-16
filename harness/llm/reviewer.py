"""The reviewer: result models, prompt loading and rendering, and the validate-and-retry loop.

Untrusted text (issue, plan, diff, CI output, agent questions) only ever reaches a prompt as a template
variable wrapped by ``fence``; it is never used as template source.
"""

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import jinja2
import jinja2.sandbox
from pydantic import BaseModel, ValidationError, model_validator

from harness.llm.backend import BackendError, ReviewBackend
from harness.secrets import redact

_ERROR_MAX = 500
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class Finding(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    file: str | None = None
    problem: str
    required_change: str


class ReviewResult(BaseModel, extra="forbid"):
    verdict: Literal["approve", "changes", "escalate"]
    findings: list[Finding] = []
    correction: str = ""
    summary: str

    @model_validator(mode="after")
    def _changes_need_correction(self) -> "ReviewResult":
        # The correction is the binding spec of the fix round, so "changes" without one is unusable.
        if self.verdict == "changes" and not self.correction.strip():
            raise ValueError("verdict 'changes' requires a non-empty correction")
        return self


REVIEW_SCHEMA: dict[str, Any] = ReviewResult.model_json_schema()


def load_prompt(name: str) -> str:
    return (resources.files("harness.llm.prompts") / f"{name}.md").read_text(encoding="utf-8")


def valid_checklist_name(name: str) -> bool:
    """Checklist names become file names; reject anything with a path separator or a leading dot."""
    return bool(_NAME_RE.match(name))


def load_builtin_checklist(name: str) -> str | None:
    if not valid_checklist_name(name):
        return None
    path = resources.files("harness.llm.prompts") / "checklists" / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def fence(text: Any) -> str:
    """Wrap untrusted text in a ````text fence that the text itself cannot close."""
    body = "" if text is None else str(text)
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    ticks = "`" * max(4, longest + 1)
    return f"{ticks}text\n{body}\n{ticks}"


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str, sort_keys=True)


def _environment() -> jinja2.sandbox.SandboxedEnvironment:
    env = jinja2.sandbox.SandboxedEnvironment(
        undefined=jinja2.StrictUndefined, autoescape=False, keep_trailing_newline=True
    )
    env.globals["fence"] = fence
    env.filters["pretty"] = _pretty
    return env


_ENV = _environment()


def render_prompt(name: str, **variables: Any) -> str:
    """Render the packaged template ``name`` with ``variables``; the output is redacted."""
    return redact(_ENV.from_string(load_prompt(name)).render(**variables))


def _parse(response: dict | str) -> ReviewResult:
    data = json.loads(response) if isinstance(response, str) else response
    return ReviewResult.model_validate(data)


def _error_text(error: Exception) -> str:
    if isinstance(error, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc']) or 'response'}: {e['msg']}" for e in error.errors()]
        text = "; ".join(parts)
    elif isinstance(error, json.JSONDecodeError):
        text = f"not valid JSON ({error.msg} at position {error.pos})"
    else:
        text = str(error) or type(error).__name__
    return redact(" ".join(text.split()))[:_ERROR_MAX]


async def run_review(
    backend: ReviewBackend, *, system: str, prompt: str, workdir: Path | None = None
) -> ReviewResult:
    """Ask the backend once, retry once with the error on invalid output, then escalate."""
    error = ""
    for attempt in range(2):
        text = prompt
        if attempt:
            text = (
                f"{prompt}\n\nYour previous reply was invalid: {error}. "
                "Reply again with only JSON matching the schema."
            )
        try:
            response = await backend.review(system, text, REVIEW_SCHEMA, workdir)
            return _parse(response)
        except (BackendError, ValidationError, ValueError, TypeError) as e:
            # json.JSONDecodeError is a ValueError; a non-dict response surfaces as ValidationError.
            error = _error_text(e)
    return ReviewResult(
        verdict="escalate",
        findings=[],
        correction="",
        summary=f"reviewer returned invalid output: {error}",
    )
