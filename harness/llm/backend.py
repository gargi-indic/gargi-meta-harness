"""Review backends: the protocol, a scripted fake for tests and the Claude Agent SDK implementation.

``claude_agent_sdk`` lives in the optional ``claude`` extra, so it is imported inside
``ClaudeSdkBackend.review`` and nowhere else. The SDK calls below follow its documented shape
(``query``, ``ClaudeAgentOptions``, ``ResultMessage``) but were not verified against an installed
version; everything version-sensitive is read with ``getattr``.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol

from harness.adapters.base import Usage
from harness.ops.pricing import PricingTable
from harness.ops.sdk_usage import usage_from_result
from harness.secrets import redact

READ_ONLY_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr checks:*)",
]
DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"]

_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


# Where a backend reports what one ``review`` call cost. ``review`` returns a plain dict, so the spend travels
# on this side channel: a caller that has a run context wraps its call in ``usage_sink(callback)``; backends
# call ``emit_usage`` and nothing happens when no sink is set. A ContextVar is per task, so concurrent runs
# sharing one backend never see each other's sink.
_SINK: ContextVar[Callable[[Usage], None] | None] = ContextVar("review_usage_sink", default=None)


@contextmanager
def usage_sink(on_usage: Callable[[Usage], None] | None) -> Iterator[None]:
    token = _SINK.set(on_usage)
    try:
        yield
    finally:
        _SINK.reset(token)


def emit_usage(usage: Usage | None) -> None:
    sink = _SINK.get()
    if usage is not None and sink is not None:
        sink(usage)


class ReviewBackend(Protocol):
    async def review(self, system: str, prompt: str, schema: dict, workdir: Path | None) -> dict: ...


class BackendError(RuntimeError):
    """The backend could not produce a response (missing SDK, SDK failure, no result)."""


class FakeBackend:
    """Returns scripted responses in order. A ``str`` stands for raw text, an Exception is raised."""

    def __init__(
        self, responses: list[dict | str | Exception], usage: list[Usage | None] | None = None
    ) -> None:
        self._responses = list(responses)
        # Scripted spend, one entry per call (None or exhausted: the call reports nothing).
        self._usage = list(usage or [])
        self.calls: list[dict[str, Any]] = []

    async def review(self, system: str, prompt: str, schema: dict, workdir: Path | None) -> dict:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "workdir": workdir})
        if not self._responses:
            raise BackendError("FakeBackend has no scripted response left")
        item = self._responses.pop(0)
        emit_usage(self._usage.pop(0) if self._usage else None)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]  # a str is parsed by the caller


class ClaudeSdkBackend:
    def __init__(
        self, *, model: str | None = None, max_turns: int = 30, pricing: PricingTable | None = None
    ) -> None:
        self.pricing = pricing
        self.model = model
        self.max_turns = max_turns

    async def review(self, system: str, prompt: str, schema: dict, workdir: Path | None) -> dict:
        try:
            import claude_agent_sdk as sdk
        except ImportError as e:
            raise BackendError("claude-agent-sdk is not installed; install the 'claude' extra") from e

        kwargs: dict[str, Any] = {
            "system_prompt": system,
            "model": self.model,
            "max_turns": self.max_turns,
            "cwd": str(workdir) if workdir is not None else None,
            "allowed_tools": list(READ_ONLY_TOOLS),
            "disallowed_tools": list(DISALLOWED_TOOLS),
            "output_format": {"type": "json_schema", "schema": schema},
        }
        try:
            options = sdk.ClaudeAgentOptions(**kwargs)
        except TypeError:
            # An older SDK without structured output: the system prompt already states the JSON shape.
            kwargs.pop("output_format")
            options = sdk.ClaudeAgentOptions(**kwargs)

        result_type = getattr(sdk, "ResultMessage", None)
        final: Any = None
        try:
            async for message in sdk.query(prompt=prompt, options=options):
                is_result = (isinstance(result_type, type) and isinstance(message, result_type)) or (
                    type(message).__name__ == "ResultMessage"
                )
                if is_result:
                    final = message
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(redact(f"claude-agent-sdk failed: {type(e).__name__}: {e}")[:500]) from e

        if final is None:
            raise BackendError("claude-agent-sdk returned no result message")
        emit_usage(
            usage_from_result(final, default_model=self.model, table=self.pricing)
        )  # a failed call cost money too
        if getattr(final, "is_error", False):
            detail = getattr(final, "result", None) or getattr(final, "subtype", None) or "unknown error"
            raise BackendError(redact(f"claude-agent-sdk reported an error: {detail}")[:500])

        structured = getattr(final, "structured_output", None)
        if isinstance(structured, dict):
            return structured
        text = getattr(final, "result", None)
        if not isinstance(text, str) or not text.strip():
            raise BackendError("claude-agent-sdk result has neither structured output nor text")
        return _parse_json_text(text)


def _parse_json_text(text: str) -> dict:
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise BackendError(f"claude-agent-sdk result is not valid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise BackendError("claude-agent-sdk result is not a JSON object")
    return data
