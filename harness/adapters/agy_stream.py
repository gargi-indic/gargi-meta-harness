"""Pure parser for the Antigravity CLI (`agy -p --output-format stream-json`) output.

No network and no subprocess here: this module only turns text into events, so the
M5.3 adapter can reuse it. The real wire format is UNVERIFIED (see
docs/spikes/antigravity.md); everything shape-specific lives in AGY_EVENT_MAP and
_FIELD_ALIASES so it can be corrected once real captures exist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# CSI sequences, OSC sequences (BEL or ST terminated) and lone two-byte escapes.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# UNVERIFIED: the raw "type" strings below are guesses from public docs, not real
# captures. Map raw type string -> normalized AgyEvent.type. Edit this table (and only
# this table) when the maintainer's captures show the real names. Unlisted types become
# "unknown" with the raw payload preserved.
AGY_EVENT_MAP: dict[str, str] = {
    "message": "message",
    "text": "message",
    "assistant": "message",
    "tool_call": "tool_call",
    "tool_use": "tool_call",
    "tool_result": "tool_result",
    "denied_tool": "denied_tool",
    "permission_denied": "denied_tool",
    "permission_request": "denied_tool",
    "conversation": "conversation",
    "conversation_id": "conversation",
    "session": "conversation",
    "result": "result",
    "final": "result",
    "error": "error",
}

# Candidate field names per normalized attribute, first match wins. Also UNVERIFIED.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "text": ("text", "content", "message", "delta"),
    "tool_name": ("tool", "tool_name", "name"),
    "conversation_id": ("conversation_id", "conversationId", "conversation", "session_id", "id"),
}


@dataclass
class AgyEvent:
    """Normalized event. `raw` is the decoded JSON object (or the line for unparsed)."""

    type: str
    raw: Any = None
    text: str | None = None
    tool_name: str | None = None
    conversation_id: str | None = None
    raw_type: str | None = None


@dataclass
class AgyMessage(AgyEvent):
    type: str = "message"


@dataclass
class AgyToolCall(AgyEvent):
    type: str = "tool_call"


@dataclass
class AgyToolResult(AgyEvent):
    type: str = "tool_result"


@dataclass
class AgyDeniedTool(AgyEvent):
    type: str = "denied_tool"


@dataclass
class AgyConversation(AgyEvent):
    type: str = "conversation"


@dataclass
class AgyResult(AgyEvent):
    type: str = "result"


@dataclass
class AgyError(AgyEvent):
    type: str = "error"


_CLASSES: dict[str, type[AgyEvent]] = {
    "message": AgyMessage,
    "tool_call": AgyToolCall,
    "tool_result": AgyToolResult,
    "denied_tool": AgyDeniedTool,
    "conversation": AgyConversation,
    "result": AgyResult,
    "error": AgyError,
}


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _first_str(obj: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = obj.get(name)
        if isinstance(value, str):
            return value
    return None


def parse_stream_line(line: str) -> AgyEvent | None:
    """Parse one output line. Returns None for blank lines or non-JSON; never raises."""
    if not isinstance(line, str):
        return None
    cleaned = strip_ansi(line).replace("\r", "").strip()
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    raw_type = obj.get("type")
    kind = AGY_EVENT_MAP.get(raw_type) if isinstance(raw_type, str) else None
    if kind is None:
        return AgyEvent(type="unknown", raw=obj, raw_type=raw_type if isinstance(raw_type, str) else None)
    cls = _CLASSES[kind]
    return cls(
        raw=obj,
        raw_type=raw_type,
        text=_first_str(obj, _FIELD_ALIASES["text"]),
        tool_name=_first_str(obj, _FIELD_ALIASES["tool_name"]) if kind != "conversation" else None,
        conversation_id=_first_str(obj, _FIELD_ALIASES["conversation_id"])
        if kind in ("conversation", "result")
        else None,
    )


@dataclass
class LineAssembler:
    """Joins arbitrary chunks into complete lines (split on \\n, \\r\\n or lone \\r)."""

    _buf: str = field(default="", repr=False)
    _pending_cr: bool = field(default=False, repr=False)

    def feed(self, chunk: str) -> list[str]:
        lines: list[str] = []
        for ch in chunk:
            if self._pending_cr:
                self._pending_cr = False
                if ch == "\n":
                    continue  # second half of CRLF, line already emitted
            if ch == "\r":
                lines.append(self._buf)
                self._buf = ""
                self._pending_cr = True
            elif ch == "\n":
                lines.append(self._buf)
                self._buf = ""
            else:
                self._buf += ch
        return lines

    def flush(self) -> list[str]:
        """Return any trailing partial line (call at EOF)."""
        rest, self._buf, self._pending_cr = self._buf, "", False
        return [rest] if rest else []

    def feed_events(self, chunk: str) -> list[AgyEvent]:
        return [e for e in map(parse_stream_line, self.feed(chunk)) if e is not None]


def parse_stream_text(text: str) -> list[AgyEvent]:
    """Parse a whole captured stream (e.g. a .raw fixture) into events."""
    asm = LineAssembler()
    lines = asm.feed(text) + asm.flush()
    return [e for e in map(parse_stream_line, lines) if e is not None]
