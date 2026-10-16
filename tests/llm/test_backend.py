"""Review backends: the scripted fake, the Claude SDK backend against a fake module, and the optional import."""

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import harness.llm
from harness.llm.backend import (
    DISALLOWED_TOOLS,
    READ_ONLY_TOOLS,
    BackendError,
    ClaudeSdkBackend,
    FakeBackend,
)

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}
VALID = {"verdict": "approve", "findings": [], "correction": "", "summary": "ok"}


class SdkResult:
    def __init__(self, result: str | None = None, structured_output: Any = None, is_error: bool = False):
        self.result = result
        self.structured_output = structured_output
        self.is_error = is_error


class SdkResultMessage(SdkResult):
    pass


SdkResultMessage.__name__ = "ResultMessage"


class SdkAssistant:
    pass


def install_sdk(monkeypatch, messages: list[Any], *, raises: Exception | None = None, old: bool = False):
    """Install a fake ``claude_agent_sdk`` that records the options and the prompt it was queried with."""
    seen: dict[str, Any] = {}
    module = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            if old and "output_format" in kwargs:
                raise TypeError("unexpected keyword argument 'output_format'")
            self.kwargs = kwargs
            seen["options"] = kwargs

    async def query(*, prompt: str, options: Any):
        seen["prompt"] = prompt
        yield SdkAssistant()
        if raises is not None:
            raise raises
        for m in messages:
            yield m

    module.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    module.ResultMessage = SdkResultMessage  # type: ignore[attr-defined]
    module.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return seen


# FakeBackend -------------------------------------------------------------------------------------------------


async def test_fake_backend_consumes_in_order_and_records_calls():
    backend = FakeBackend([VALID, "raw text", RuntimeError("boom")])

    assert await backend.review("s", "p1", SCHEMA, None) == VALID
    assert await backend.review("s", "p2", SCHEMA, Path("/w")) == "raw text"
    with pytest.raises(RuntimeError, match="boom"):
        await backend.review("s", "p3", SCHEMA, None)
    assert backend.calls == [
        {"system": "s", "prompt": "p1", "schema": SCHEMA, "workdir": None},
        {"system": "s", "prompt": "p2", "schema": SCHEMA, "workdir": Path("/w")},
        {"system": "s", "prompt": "p3", "schema": SCHEMA, "workdir": None},
    ]


async def test_fake_backend_runs_out_of_responses():
    with pytest.raises(BackendError, match="no scripted response"):
        await FakeBackend([]).review("s", "p", SCHEMA, None)


# ClaudeSdkBackend --------------------------------------------------------------------------------------------


async def test_sdk_backend_passes_read_only_options_and_schema(monkeypatch):
    seen = install_sdk(monkeypatch, [SdkResultMessage(structured_output=VALID)])

    out = await ClaudeSdkBackend(model="m-1", max_turns=7).review("SYS", "PROMPT", SCHEMA, Path("/work/tree"))

    assert out == VALID
    assert seen["prompt"] == "PROMPT"
    assert seen["options"] == {
        "system_prompt": "SYS",
        "model": "m-1",
        "max_turns": 7,
        "cwd": "/work/tree",
        "allowed_tools": [
            "Read",
            "Grep",
            "Glob",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Bash(gh pr diff:*)",
            "Bash(gh pr view:*)",
            "Bash(gh pr checks:*)",
        ],
        "disallowed_tools": ["Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        "output_format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert seen["options"]["allowed_tools"] == READ_ONLY_TOOLS
    assert seen["options"]["disallowed_tools"] == DISALLOWED_TOOLS


async def test_sdk_backend_defaults(monkeypatch):
    seen = install_sdk(monkeypatch, [SdkResultMessage(structured_output=VALID)])

    await ClaudeSdkBackend().review("S", "P", SCHEMA, None)

    assert seen["options"]["model"] is None
    assert seen["options"]["max_turns"] == 30
    assert seen["options"]["cwd"] is None


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "approve", "findings": [], "correction": "", "summary": "ok"}',
        '```json\n{"verdict": "approve", "findings": [], "correction": "", "summary": "ok"}\n```',
        '  ```\n{"verdict": "approve", "findings": [], "correction": "", "summary": "ok"}\n```  ',
    ],
)
async def test_sdk_backend_parses_result_text_when_no_structured_output(monkeypatch, text):
    install_sdk(monkeypatch, [SdkResultMessage(result=text)])

    assert await ClaudeSdkBackend().review("S", "P", SCHEMA, None) == VALID


async def test_sdk_backend_uses_the_last_result_message(monkeypatch):
    install_sdk(
        monkeypatch,
        [
            SdkResultMessage(structured_output={"verdict": "escalate"}),
            SdkResultMessage(structured_output=VALID),
        ],
    )

    assert await ClaudeSdkBackend().review("S", "P", SCHEMA, None) == VALID


@pytest.mark.parametrize(
    "messages, match",
    [
        ([], "no result message"),
        ([SdkResultMessage(result="not json at all")], "not valid JSON"),
        ([SdkResultMessage(result="[1, 2]")], "not a JSON object"),
        ([SdkResultMessage(result="")], "neither structured output nor text"),
        ([SdkResultMessage(result="max turns reached", is_error=True)], "reported an error"),
    ],
)
async def test_sdk_backend_errors(monkeypatch, messages, match):
    install_sdk(monkeypatch, messages)

    with pytest.raises(BackendError, match=match):
        await ClaudeSdkBackend().review("S", "P", SCHEMA, None)


async def test_sdk_backend_wraps_sdk_exceptions_and_redacts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret-key")
    install_sdk(monkeypatch, [], raises=ConnectionError("auth failed for sk-ant-very-secret-key"))

    with pytest.raises(BackendError) as info:
        await ClaudeSdkBackend().review("S", "P", SCHEMA, None)

    assert "ConnectionError" in str(info.value)
    assert "sk-ant-very-secret-key" not in str(info.value)


async def test_sdk_backend_falls_back_when_options_lack_output_format(monkeypatch):
    seen = install_sdk(monkeypatch, [SdkResultMessage(result='{"verdict": "approve"}')], old=True)

    assert await ClaudeSdkBackend().review("S", "P", SCHEMA, None) == {"verdict": "approve"}
    assert "output_format" not in seen["options"]
    assert seen["options"]["allowed_tools"] == READ_ONLY_TOOLS


# Optional import ---------------------------------------------------------------------------------------------


async def test_package_imports_and_review_fails_cleanly_without_the_sdk(monkeypatch):
    names = ["harness.llm.backend", "harness.llm.reviewer", "harness.engine.executors.review"]
    for name in names:
        # Restores both sys.modules and the parent package attribute, so other tests keep the original modules.
        parent, _, attr = name.rpartition(".")
        monkeypatch.setattr(importlib.import_module(parent), attr, importlib.import_module(name))
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    backend = importlib.import_module("harness.llm.backend")
    reviewer = importlib.import_module("harness.llm.reviewer")
    executor = importlib.import_module("harness.engine.executors.review")

    assert hasattr(reviewer, "run_review")
    assert hasattr(executor, "ReviewExecutor")
    with pytest.raises(
        backend.BackendError, match="claude-agent-sdk is not installed; install the 'claude' extra"
    ):
        await backend.ClaudeSdkBackend().review("S", "P", SCHEMA, None)


def test_llm_package_is_importable():
    assert harness.llm.__doc__
