"""Formats for the harness-owned ledger comment and the per-event comments.

Pure functions only: no network, no I/O. The snapshot schema is defined by the engine and is treated
as an opaque ``dict`` here.

Ledger layout::

    <!-- harness:ledger -->
    <summary_md>

    <!-- harness:state v1 <payload> -->

The payload is one of:

* compact JSON (``sort_keys``, ``ensure_ascii=False``) in which every ``<`` and ``>`` is written as a
  JSON ``\\u003c`` / ``\\u003e`` escape, so neither ``-->`` nor ``<!--`` can occur in it; or
* ``z:`` followed by ``base64(zlib(json, 9))``. The standard base64 alphabet (``A-Za-z0-9+/=``) cannot
  form ``-->``. (base85 was rejected: its alphabet contains ``-`` and ``>``.)
"""

import base64
import binascii
import json
import zlib
from typing import Any

LEDGER_MARKER = "<!-- harness:ledger -->"

_STATE_PREFIX = "<!-- harness:state v1 "
_STATE_SUFFIX = " -->"
_COMPRESSED_TAG = "z:"

# Compress when the plain JSON form is longer than this many characters.
COMPRESS_THRESHOLD = 50_000
# GitHub's comment limit is 65,536 characters; keep some headroom.
MAX_LEDGER_CHARS = 65_000
# Guard against decompression bombs in tampered payloads.
_MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024


class LedgerTooLarge(ValueError):
    """The rendered ledger comment would exceed the size limit, even after compression."""


class LedgerCorrupt(ValueError):
    """A ledger comment has a state block whose payload cannot be decoded."""


def _json_text(snapshot: dict[str, Any]) -> str:
    text = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    # In JSON, "<" and ">" can only appear inside strings, where < / > are equivalent.
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def _encode_payload(snapshot: dict[str, Any]) -> str:
    text = _json_text(snapshot)
    if len(text) <= COMPRESS_THRESHOLD:
        return text
    packed = zlib.compress(text.encode("utf-8"), 9)
    return _COMPRESSED_TAG + base64.b64encode(packed).decode("ascii")


def _decode_payload(payload: str) -> dict[str, Any]:
    try:
        if payload.startswith(_COMPRESSED_TAG):
            raw = base64.b64decode(payload[len(_COMPRESSED_TAG) :], validate=True)
            inflater = zlib.decompressobj()
            data = inflater.decompress(raw, _MAX_DECOMPRESSED_BYTES)
            if inflater.unconsumed_tail:
                raise LedgerCorrupt("ledger state payload decompresses to too much data")
            if not inflater.eof:
                raise LedgerCorrupt("ledger state payload has a truncated zlib stream")
            payload = data.decode("utf-8")
        snapshot = json.loads(payload)
    except LedgerCorrupt:
        raise
    except (binascii.Error, zlib.error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LedgerCorrupt(f"ledger state payload is corrupt: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise LedgerCorrupt("ledger state payload is not a JSON object")
    return snapshot


def render_ledger(snapshot: dict[str, Any], summary_md: str) -> str:
    """Render the ledger comment body for ``snapshot`` with a human-readable ``summary_md``."""
    block = f"{_STATE_PREFIX}{_encode_payload(snapshot)}{_STATE_SUFFIX}"
    body = f"{LEDGER_MARKER}\n{summary_md.rstrip()}\n\n{block}"
    if len(body) > MAX_LEDGER_CHARS:
        raise LedgerTooLarge(f"ledger comment is {len(body)} chars, limit is {MAX_LEDGER_CHARS}")
    return body


def is_ledger(body: str) -> bool:
    """True if ``body`` is a harness ledger comment (it starts with the ledger marker)."""
    return isinstance(body, str) and body.lstrip().startswith(LEDGER_MARKER)


def parse_ledger(comment_body: str) -> dict[str, Any] | None:
    """Inverse of :func:`render_ledger`.

    Returns None if the body is not a ledger or has no state block; raises :class:`LedgerCorrupt` if
    the state payload cannot be decoded.
    """
    if not is_ledger(comment_body):
        return None
    # The payload can never contain "<!--", so the last prefix occurrence is the real state block
    # even if the human-readable summary happens to mention one.
    start = comment_body.rfind(_STATE_PREFIX)
    if start == -1:
        return None
    payload_start = start + len(_STATE_PREFIX)
    end = comment_body.rfind(_STATE_SUFFIX)
    if end < payload_start:
        return None
    return _decode_payload(comment_body[payload_start:end])


def ledger_summary(comment_body: str) -> str:
    """The human-readable part of a ledger comment: what lies between the marker and the state block."""
    if not is_ledger(comment_body):
        return ""
    text = comment_body.lstrip()[len(LEDGER_MARKER) :]
    start = text.rfind(_STATE_PREFIX)
    if start != -1:
        text = text[:start]
    return text.strip()


def find_ledger(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the oldest ledger comment (by ``created_at``, then ``id``), or None."""
    ledgers = [c for c in comments if is_ledger(c.get("body") or "")]
    if not ledgers:
        return None
    return min(ledgers, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))


def render_event(date: str, issue: int, event: str, bullets: list[str]) -> str:
    """Render an event comment: ``**YYYY-MM-DD · #N <event>.**`` then one ``- bullet`` line each."""
    lines = [f"**{date} · #{issue} {event}.**"]
    lines.extend(f"- {bullet}" for bullet in bullets)
    return "\n".join(lines)
