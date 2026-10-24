"""The marker that makes issue creation idempotent: ``<!-- harness:planned run=<run_key> key=<key> -->``.

Every issue the planner creates ends with this marker, so a crash between "GitHub created the issue" and "the
harness noted its number" is recovered by searching the planned issues for the marker instead of creating a
second issue. Pure functions only: no network, no I/O.
"""

import re
from typing import Any

# Keys of proposed issues match ``[a-z0-9][a-z0-9-]*``, so this one cannot collide with any of them.
EPIC_KEY = "@epic"

_RUN_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_MARKER = re.compile(
    r"<!-- harness:planned run=(?P<run>[A-Za-z0-9._:-]+) key=(?P<key>@?[A-Za-z0-9-]+) -->\s*\Z"
)


def check_run_key(run_key: str) -> str:
    """``run_key`` itself, or ValueError: it ends up inside an HTML comment, so its alphabet is restricted."""
    if not _RUN_KEY.match(run_key):
        raise ValueError(f"run key {run_key!r} must be 1-80 characters of A-Z a-z 0-9 . _ : -")
    return run_key


def planned_marker(run_key: str, key: str) -> str:
    return f"<!-- harness:planned run={check_run_key(run_key)} key={key} -->"


def neutralise(text: str) -> str:
    """``text`` with any ``harness:planned`` comment opener defused, so proposed text cannot forge a marker."""
    return re.sub(r"<!--(\s*harness:planned)", r"&lt;!--\1", text)


def marker_of(body: str | None) -> tuple[str, str] | None:
    """``(run_key, key)`` from the marker at the very end of ``body``, or None."""
    found = _MARKER.search(body or "")
    return (found.group("run"), found.group("key")) if found else None


def find_by_marker(issues: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    """The issue whose body ends with exactly ``marker`` (the lowest number if several do), or None."""
    hits = [i for i in issues if (i.get("body") or "").rstrip().endswith(marker)]
    return min(hits, key=lambda i: i["number"]) if hits else None


def index_planned(issues: list[dict[str, Any]], run_key: str) -> dict[str, dict[str, Any]]:
    """The issues of run ``run_key`` by proposal key (the lowest number wins on a duplicate)."""
    found: dict[str, dict[str, Any]] = {}
    for issue in sorted(issues, key=lambda i: i["number"]):
        parsed = marker_of(issue.get("body"))
        if parsed is not None and parsed[0] == run_key:
            found.setdefault(parsed[1], issue)
    return found
