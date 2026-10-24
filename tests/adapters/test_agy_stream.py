import json
from pathlib import Path

from harness.adapters.agy_stream import (
    AGY_EVENT_MAP,
    AgyConversation,
    AgyDeniedTool,
    LineAssembler,
    parse_stream_line,
    parse_stream_text,
)

SYNTH = Path(__file__).resolve().parents[1] / "fixtures" / "antigravity" / "synthetic"


def test_basic_message():
    ev = parse_stream_line('{"type": "message", "text": "hi"}')
    assert ev is not None and ev.type == "message" and ev.text == "hi"


def test_ansi_and_cr_stripped():
    ev = parse_stream_line('\x1b[?25l\x1b[32m{"type": "result", "text": "ok"}\x1b[0m\r')
    assert ev is not None and ev.type == "result"


def test_unknown_type_preserved():
    ev = parse_stream_line('{"type": "brand_new", "x": 1}')
    assert ev is not None and ev.type == "unknown"
    assert ev.raw == {"type": "brand_new", "x": 1} and ev.raw_type == "brand_new"


def test_missing_type_is_unknown():
    ev = parse_stream_line('{"x": 1}')
    assert ev is not None and ev.type == "unknown"


def test_invalid_json_returns_none():
    for bad in ["", "   ", "not json", '{"type": "mess', "[1, 2]", "42", "\x1b[0m"]:
        assert parse_stream_line(bad) is None


def test_non_str_returns_none():
    assert parse_stream_line(None) is None  # type: ignore[arg-type]


def test_event_map_is_table_driven():
    assert AGY_EVENT_MAP["denied_tool"] == "denied_tool"
    assert isinstance(parse_stream_line('{"type": "permission_denied", "tool": "sh"}'), AgyDeniedTool)
    conv = parse_stream_line('{"type": "conversation", "conversation_id": "c1"}')
    assert isinstance(conv, AgyConversation) and conv.conversation_id == "c1"


def test_assembler_split_chunks():
    asm = LineAssembler()
    assert asm.feed('{"type": "mess') == []
    assert asm.feed('age", "text": "a"}\n{"type"') == ['{"type": "message", "text": "a"}']
    assert asm.feed(': "result"}\n') == ['{"type": "result"}']
    assert asm.flush() == []


def test_assembler_crlf_across_chunks():
    asm = LineAssembler()
    assert asm.feed("a\r") == ["a"]
    assert asm.feed("\nb\r\nc") == ["b"]
    assert asm.flush() == ["c"]


def test_assembler_flush_partial_and_events():
    asm = LineAssembler()
    assert asm.feed_events('{"type": "result"}') == []
    assert asm.flush() == ['{"type": "result"}']


def test_assembler_feed_events_skips_junk():
    asm = LineAssembler()
    evs = asm.feed_events('garbage\n{"type": "message", "text": "x"}\n\n')
    assert [e.type for e in evs] == ["message"]


def test_synthetic_fixtures_marked_and_parse():
    files = sorted(SYNTH.glob("*.jsonl"))
    assert files
    for f in files:
        first = json.loads(f.read_text().splitlines()[0])
        assert first.get("synthetic") is True
        assert parse_stream_text(f.read_text())


def test_synthetic_edit_file_has_unknown_event():
    evs = parse_stream_text((SYNTH / "edit_file.jsonl").read_text())
    assert "unknown" in [e.type for e in evs]
    assert [e.type for e in evs][:3] == ["conversation", "tool_call", "tool_result"]


def test_synthetic_pty_raw():
    evs = parse_stream_text((SYNTH / "hello_pty.raw").read_bytes().decode())
    assert [e.type for e in evs] == ["message", "result"]
