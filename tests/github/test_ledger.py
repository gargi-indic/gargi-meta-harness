import base64
import json
import os
import random
import string
import zlib
from typing import Any

import pytest

from harness.github.ledger import (
    LEDGER_MARKER,
    LedgerCorrupt,
    LedgerTooLarge,
    find_ledger,
    is_ledger,
    parse_ledger,
    render_event,
    render_ledger,
)

SMALL = {"run": 7, "steps": [{"node": "plan", "status": "done"}], "ok": True, "none": None, "f": 1.5}

ADVERSARIAL: list[dict[str, Any]] = [
    {"a": "-->"},
    {"a": "<!--", "b": "<!-- harness:state v1 {} -->"},
    {"k-->": "v<!--", "<!--": ["-->", "--", ">", "<", "-- >"]},
    {"unicode": "café ☃ \U0001f600 हिन्दी", "nl": "a\nb\r\nc\td"},
    {"nested": {"x": [{"y": {"z": ["-->", {"<!--": "-->"}]}}], "empty": {}, "list": []}},
    {"quote": 'he said "hi" \\ back\\slash', "ctrl": "\x00\x1f  "},
    {"marker": LEDGER_MARKER, "state": "<!-- harness:state v1 z:AAAA -->"},
    {},
]


def _large(n: int = 4000) -> dict[str, Any]:
    # Compressible but over the 50,000 char plain-JSON threshold.
    return {"events": [{"i": i, "msg": f"step {i} --> done <!-- ok -->", "tag": "x" * 20} for i in range(n)]}


def _state_payload(body: str) -> str:
    prefix = "<!-- harness:state v1 "
    start = body.rindex(prefix) + len(prefix)
    return body[start : body.rindex(" -->")]


def test_marker_value():
    assert LEDGER_MARKER == "<!-- harness:ledger -->"


def test_layout_is_exact():
    body = render_ledger(SMALL, "## Status\n\nrunning  \n\n")
    payload = json.dumps(SMALL, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    assert body == f"{LEDGER_MARKER}\n## Status\n\nrunning\n\n<!-- harness:state v1 {payload} -->"


def test_small_is_plain_json():
    payload = _state_payload(render_ledger(SMALL, "s"))
    assert not payload.startswith("z:")
    assert json.loads(payload) == SMALL


@pytest.mark.parametrize("snapshot", [SMALL, *ADVERSARIAL, _large()], ids=lambda s: str(s)[:30])
def test_round_trip(snapshot):
    assert parse_ledger(render_ledger(snapshot, "summary")) == snapshot


def test_round_trip_large_is_compressed():
    snapshot = _large()
    assert len(json.dumps(snapshot)) > 50_000
    body = render_ledger(snapshot, "summary")
    assert _state_payload(body).startswith("z:")
    assert len(body) < 65_000
    assert parse_ledger(body) == snapshot


def test_round_trip_large_adversarial_compressed():
    snapshot = {"junk": "-->" * 20_000, "u": "☃<!--" * 5000, "nested": [[["-->"]]]}
    body = render_ledger(snapshot, "summary --> with <!-- stuff")
    assert _state_payload(body).startswith("z:")
    assert parse_ledger(body) == snapshot


def test_summary_may_contain_comment_syntax():
    snapshot = {"a": "-->"}
    body = render_ledger(snapshot, "see <!-- harness:state v1 {} --> and --> here")
    assert parse_ledger(body) == snapshot


def test_plain_payload_never_contains_comment_delimiters():
    for snapshot in ADVERSARIAL:
        payload = _state_payload(render_ledger(snapshot, "s"))
        assert "-->" not in payload
        assert "<!--" not in payload
        assert "<" not in payload and ">" not in payload


def test_compressed_payload_never_contains_terminator_random():
    rng = random.Random(1234)
    alphabet = string.printable + "☃é-->-<!"
    for _ in range(300):
        words = ["".join(rng.choices(alphabet, k=rng.randint(1, 40))) for _ in range(rng.randint(1, 6))]
        # Large enough to force compression.
        snapshot = {"w": words, "pad": "".join(rng.choices(["-->", "--", ">", "ab"], k=30_000))}
        body = render_ledger(snapshot, "s")
        payload = _state_payload(body)
        assert payload.startswith("z:")
        assert "-->" not in payload
        assert set(payload[2:]) <= set(string.ascii_letters + string.digits + "+/=")
        assert parse_ledger(body) == snapshot


def test_too_large_with_incompressible_data():
    snapshot = {"blob": [os.urandom(2000).hex() for _ in range(40)]}  # ~160k hex chars
    with pytest.raises(LedgerTooLarge):
        render_ledger(snapshot, "summary")


def test_too_large_due_to_summary():
    with pytest.raises(LedgerTooLarge):
        render_ledger({"a": 1}, "x" * 65_001)


def test_just_under_limit_ok():
    body = render_ledger({"a": 1}, "x" * 60_000)
    assert len(body) <= 65_000
    assert parse_ledger(body) == {"a": 1}


def test_is_ledger():
    assert is_ledger(render_ledger(SMALL, "s"))
    assert is_ledger(f"{LEDGER_MARKER}\nno state")
    assert not is_ledger("hello")
    assert not is_ledger("")
    assert not is_ledger("quoting: " + LEDGER_MARKER)


def test_parse_ledger_none_for_non_ledger():
    assert parse_ledger("just a human comment") is None
    assert parse_ledger("") is None
    # A state block without the marker is not a ledger.
    assert parse_ledger('<!-- harness:state v1 {"a":1} -->') is None


def test_parse_ledger_none_when_state_block_missing():
    assert parse_ledger(f"{LEDGER_MARKER}\nsummary only") is None
    assert parse_ledger(f"{LEDGER_MARKER}\nsummary\n\n<!-- harness:state v1 {{}}") is None


def _with_payload(payload: str) -> str:
    return f"{LEDGER_MARKER}\nsummary\n\n<!-- harness:state v1 {payload} -->"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"a":',
        "[1,2,3]",
        '"string"',
        "z:@@@not-base64@@@",
        "z:" + base64.b64encode(b"not zlib data").decode(),
        "z:" + base64.b64encode(zlib.compress(b"not json")).decode(),
        "z:" + base64.b64encode(zlib.compress(b"[1]")).decode(),
        "z:" + base64.b64encode(zlib.compress(b"\xff\xfe")).decode(),
        "z:" + base64.b64encode(zlib.compress(b'{"a":1}')[:-6]).decode(),
        "z:",
        "",
    ],
)
def test_corrupt_payloads(payload):
    with pytest.raises(LedgerCorrupt):
        parse_ledger(_with_payload(payload))


def test_tampered_rendered_payloads():
    plain = render_ledger(SMALL, "s")
    with pytest.raises(LedgerCorrupt):
        parse_ledger(plain.replace('"run":7', '"run":'))
    packed = render_ledger(_large(), "s")
    payload = _state_payload(packed)
    flipped = payload[:20] + ("A" if payload[20] != "A" else "B") + payload[21:]
    with pytest.raises(LedgerCorrupt):
        parse_ledger(packed.replace(payload, flipped))
    with pytest.raises(LedgerCorrupt):
        parse_ledger(packed.replace(payload, payload[:-9] + "!!!!"))
    with pytest.raises(LedgerCorrupt):
        parse_ledger(packed.replace(payload, payload[: len(payload) // 2]))


def test_decompression_bomb_is_corrupt():
    bomb = base64.b64encode(zlib.compress(b" " * (40 * 1024 * 1024), 9)).decode()
    with pytest.raises(LedgerCorrupt):
        parse_ledger(_with_payload("z:" + bomb))


def _comment(id_: int, created_at: str, body: str) -> dict[str, Any]:
    return {"id": id_, "created_at": created_at, "body": body}


def test_find_ledger_none():
    assert find_ledger([]) is None
    assert find_ledger([_comment(1, "2026-01-01T00:00:00Z", "hi")]) is None


def test_find_ledger_picks_oldest():
    ledger = render_ledger(SMALL, "s")
    old = _comment(30, "2026-01-01T00:00:00Z", ledger)
    new = _comment(10, "2026-02-01T00:00:00Z", ledger)
    human = _comment(1, "2025-12-01T00:00:00Z", "hello")
    assert find_ledger([human, new, old]) is old
    assert find_ledger([new, old, human]) is old


def test_find_ledger_ties_broken_by_id():
    ledger = render_ledger(SMALL, "s")
    a = _comment(5, "2026-01-01T00:00:00Z", ledger)
    b = _comment(3, "2026-01-01T00:00:00Z", ledger)
    assert find_ledger([a, b]) is b


def test_render_event_exact():
    assert render_event("2026-10-14", 7, "merged", ["PR #12 squashed", "CI green"]) == (
        "**2026-10-14 · #7 merged.**\n- PR #12 squashed\n- CI green"
    )


def test_render_event_no_bullets():
    assert render_event("2026-10-14", 7, "queued", []) == "**2026-10-14 · #7 queued.**"
