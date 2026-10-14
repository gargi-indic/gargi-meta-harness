import os

import pytest

from harness.github.ledger import (
    LedgerCorrupt,
    LedgerTooLarge,
    find_ledger,
    is_ledger,
    parse_ledger,
    render_event,
    render_ledger,
)


def test_ledger_roundtrip_small():
    snap = {"status": "ok", "count": 42}
    summary = "Small snapshot"
    res = render_ledger(snap, summary)

    assert "<!-- harness:ledger -->\nSmall snapshot\n\n<!-- harness:state v1" in res

    parsed = parse_ledger(res)
    assert parsed == snap


def test_ledger_roundtrip_large_compressible():
    snap = {"data": "A" * 60000}
    res = render_ledger(snap, "Large compressible")
    assert "z:" in res
    assert len(res) < 65000

    parsed = parse_ledger(res)
    assert parsed == snap


def test_ledger_roundtrip_adversarial():
    snap = {
        "arrows": "-->",
        "start": "<!--",
        "newline": "\n\n",
        "unicode": "👾",
        "escaped": "<script>",
    }
    res = render_ledger(snap, "Adversarial payload")

    # Extract payload
    payload = res.split("<!-- harness:state v1 ")[1].removesuffix(" -->")
    # The JSON string should have been processed to ensure `-->` does not end the comment early
    assert "-->" not in payload

    parsed = parse_ledger(res)
    assert parsed == snap


def test_ledger_too_large():
    rand_data = os.urandom(60000).hex()
    snap = {"data": rand_data}

    with pytest.raises(LedgerTooLarge):
        render_ledger(snap, "Too large")


def test_ledger_corrupt():
    corrupt_body = "<!-- harness:ledger -->\nsum\n\n<!-- harness:state v1 z:invalidbase64 -->"
    with pytest.raises(LedgerCorrupt):
        parse_ledger(corrupt_body)

    corrupt_json = "<!-- harness:ledger -->\nsum\n\n<!-- harness:state v1 {badjson} -->"
    with pytest.raises(LedgerCorrupt):
        parse_ledger(corrupt_json)


def test_is_ledger():
    assert is_ledger("<!-- harness:ledger -->\nfoo") is True
    assert is_ledger("Just a comment") is False
    assert is_ledger(None) is False


def test_find_ledger():
    comments = [
        {"id": 1, "body": "Comment 1"},
        {"id": 2, "body": "<!-- harness:ledger -->\nfoo2", "created_at": "2023-01-02T00:00:00Z"},
        {"id": 3, "body": "<!-- harness:ledger -->\nfoo3", "created_at": "2023-01-01T00:00:00Z"},
    ]

    ledger = find_ledger(comments)
    assert ledger is not None
    assert ledger["id"] == 3


def test_render_event():
    event = render_event("2023-10-15", 42, "Status changed", ["Bullet 1", "Bullet 2"])
    expected = "**2023-10-15 · #42 Status changed.**\n- Bullet 1\n- Bullet 2"
    assert event == expected


def test_parse_ledger_regex_greediness():
    snap = {"key": "value"}
    res = render_ledger(snap, "Summary")
    res += "\nSome random trailing text -->"
    with pytest.raises(LedgerCorrupt):
        parse_ledger(res)
