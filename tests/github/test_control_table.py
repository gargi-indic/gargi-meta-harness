import random
import string
from typing import get_args

import pytest

from harness.github.control_table import (
    STATUS_TAGS,
    TABLE_END,
    TABLE_START,
    ControlRow,
    Status,
    parse_table,
    render_table,
    replace_table,
    upsert_row,
)

STATUSES = ["queued", "ready", "planning", "running", "review", "needs-you", "blocked", "done", "failed"]


def row(issue: int, status: Status = "running", **kw) -> ControlRow:
    kw.setdefault("title", f"Task {issue}")
    return ControlRow(issue=issue, status=status, **kw)


def test_status_tags():
    assert list(STATUS_TAGS) == STATUSES
    assert list(get_args(Status)) == STATUSES
    assert STATUS_TAGS["running"] == "RUNNING"
    assert STATUS_TAGS["needs-you"] == "NEEDS-YOU"
    for tag in STATUS_TAGS.values():
        assert tag.isascii()


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        ControlRow(issue=1, title="t", status="bogus")  # type: ignore[arg-type]


def test_render_table_exact():
    table = render_table(
        [
            row(8, "done", agent="jules", run_id="r8", pr="#20", note="ok"),
            row(7, "needs-you"),
        ]
    )
    assert table == "\n".join(
        [
            "<!-- harness:table:start -->",
            "| Issue | Title | Status | Agent | Run | PR | Note |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| #7 | Task 7 | `NEEDS-YOU` |  |  |  |  |",
            "| #8 | Task 8 | `DONE` | jules | r8 | #20 | ok |",
            "<!-- harness:table:end -->",
        ]
    )


def test_render_empty_table():
    table = render_table([])
    assert table.startswith(TABLE_START) and table.endswith(TABLE_END)
    assert parse_table(table) == []


def test_cell_escaping_and_newlines():
    table = render_table([row(1, title="a|b", note="line1\nline2\r\nline3")])
    assert "a\\|b" in table
    assert "line1 line2 line3" in table
    assert len(table.splitlines()) == 5


def test_no_emojis_in_output():
    table = render_table([row(i, s) for i, s in enumerate(STATUSES, 1)])
    assert table.isascii()


def test_marker_in_cell_cannot_break_table():
    body = upsert_row("", row(1, note=f"x {TABLE_END} y"))
    assert body.count(TABLE_END) == 1
    assert [r.issue for r in parse_table(body)] == [1]


def test_replace_appends_when_absent():
    table = render_table([row(1)])
    assert replace_table("", table) == table
    assert replace_table("Intro\n", table) == f"Intro\n\n{table}"
    assert replace_table("Intro", table) == f"Intro\n\n{table}"


def test_replace_between_markers_only():
    old = render_table([row(1)])
    new = render_table([row(1, "done"), row(2)])
    body = f"before\n\ntext $1 \\1 {{}}\n\n{old}\n\nafter\n"
    assert replace_table(body, new) == f"before\n\ntext $1 \\1 {{}}\n\n{new}\n\nafter\n"


def test_replace_accepts_table_without_markers():
    old = render_table([row(1)])
    new = render_table([row(2)])
    inner = new.removeprefix(TABLE_START + "\n").removesuffix("\n" + TABLE_END)
    body = f"a\n{old}\nb"
    assert replace_table(body, inner) == f"a\n{new}\nb"


def test_replace_is_idempotent():
    body = f"x\n\n{render_table([row(1)])}\n\ny"
    table = render_table([row(1)])
    assert replace_table(body, table) == body
    assert replace_table(replace_table(body, table), table) == body


def test_replace_unmatched_start_marker_raises():
    with pytest.raises(ValueError):
        replace_table(f"a\n{TABLE_START}\nb", render_table([]))


def test_lookalike_table_outside_markers_untouched():
    lookalike = "| Issue | Title | Status | Agent | Run | PR | Note |\n| --- | --- | --- | --- | --- | --- | --- |\n| #1 | decoy | `DONE` |  |  |  |  |\n"
    body = f"{lookalike}\nmiddle\n\n{render_table([row(1, title='real')])}\n\n{lookalike}tail"
    out = upsert_row(body, row(1, "done", title="real v2"))
    assert out.startswith(f"{lookalike}\nmiddle\n\n")
    assert out.endswith(f"\n\n{lookalike}tail")
    assert out.count("decoy") == 2
    assert [r.title for r in parse_table(out)] == ["real v2"]
    out2 = replace_table(body, render_table([row(9, title="n")]))
    assert out2.count("decoy") == 2 and "real" not in out2


def test_lookalike_table_without_markers_is_not_parsed_or_modified():
    lookalike = "| Issue | Title | Status |\n| --- | --- | --- |\n| #1 | decoy | `DONE` |\n"
    body = f"Intro\n\n{lookalike}"
    assert parse_table(body) == []
    out = upsert_row(body, row(2))
    assert out.startswith(body)
    assert [r.issue for r in parse_table(out)] == [2]


def test_naive_str_replace_corruption_case():
    # The old orchestrator did a naive str.replace of the table text, so a copy of the same rows quoted
    # elsewhere in the body (outside the markers) was rewritten too, and "$"/"\\" in text were mangled.
    table = render_table([row(1)])
    inner = table.removeprefix(TABLE_START + "\n").removesuffix("\n" + TABLE_END)
    body = f"Docs, quoted:\n\n```\n{inner}\n```\n\nLive:\n\n{table}\n\nCost $1 \\1 \\g<0>\n"
    out = replace_table(body, render_table([row(1, "done")]))
    assert out.startswith(f"Docs, quoted:\n\n```\n{inner}\n```\n\nLive:\n\n")
    assert out.endswith("\n\nCost $1 \\1 \\g<0>\n")
    assert out.count("`RUNNING`") == 1 and out.count("`DONE`") == 1


def test_crlf_outside_markers_preserved_byte_for_byte():
    prefix = "Title line\r\n\r\nSome *text*\r\n- item\r\n\r\n"
    suffix = "\r\n\r\nTrailing\r\nlines\r\n"
    body = f"{prefix}{render_table([row(1)]).replace(chr(10), chr(13) + chr(10))}{suffix}"
    out = upsert_row(body, row(2, agent="jules"))
    assert out.startswith(prefix) and out.endswith(suffix)
    assert out.encode().count(b"\r\n") > body.encode().count(b"\r\n") - 1
    assert "\n" not in out.replace("\r\n", "")
    assert [r.issue for r in parse_table(out)] == [1, 2]
    out2 = replace_table(body, render_table([row(5)]))
    assert out2.startswith(prefix) and out2.endswith(suffix)


def test_lf_only_body_stays_lf_only():
    out = upsert_row("a\nb\n", row(1))
    assert "\r" not in out


def test_upsert_into_body_without_table():
    out = upsert_row("Campaign\n", row(3))
    assert out.startswith("Campaign\n\n" + TABLE_START)
    assert parse_table(out) == [row(3)]


def test_upsert_replaces_existing_row():
    body = upsert_row(upsert_row("", row(1)), row(2))
    out = upsert_row(body, row(1, "done", title="new", pr="#5"))
    assert parse_table(out) == [row(1, "done", title="new", pr="#5"), row(2)]


def test_upsert_sorts_by_issue():
    body = ""
    for issue in (10, 2, 33, 7):
        body = upsert_row(body, row(issue))
    assert [r.issue for r in parse_table(body)] == [2, 7, 10, 33]


def test_upsert_preserves_other_rows_exactly():
    rows = [row(1, agent="a|b", note="x\\|y"), row(2, "failed", pr="#9")]
    body = f"x\n{render_table(rows)}\ny"
    out = upsert_row(body, row(3))
    assert parse_table(out) == [*rows, row(3)]


@pytest.mark.parametrize(
    "value",
    ["a|b", "|", "||", "\\|", "\\", "\\\\", "a\\", "trailing\\", "x\\|y|z", "`code|pipe`", "☃ | é"],
)
def test_escaped_pipes_round_trip(value):
    r = row(4, title=value, agent=value, run_id=value, pr=value, note=value)
    assert parse_table(render_table([r])) == [r]
    out = upsert_row(f"pre\n{render_table([r])}\npost", row(5))
    assert parse_table(out) == [r, row(5)]
    assert out.startswith("pre\n") and out.endswith("\npost")


def test_round_trip_random_rows():
    rng = random.Random(99)
    alphabet = string.ascii_letters + string.digits + "|\\ `#_-:.é☃"

    def cell() -> str | None:
        if rng.random() < 0.2:
            return None
        text = "".join(rng.choices(alphabet, k=rng.randint(1, 15))).strip()
        return text or None

    rows = [
        ControlRow(
            issue=i,
            title="".join(rng.choices(alphabet, k=rng.randint(1, 15))).strip() or "t",
            status=rng.choice(STATUSES),  # type: ignore[arg-type]
            agent=cell(),
            run_id=cell(),
            pr=cell(),
            note=cell(),
        )
        for i in rng.sample(range(1, 500), 60)
    ]
    assert parse_table(render_table(rows)) == sorted(rows, key=lambda r: r.issue)


def test_none_renders_empty_and_parses_back():
    r = row(1)
    assert "|  |  |  |  |" in render_table([r])
    assert parse_table(render_table([r])) == [r]


def test_malformed_row_in_table_raises():
    body = f"{TABLE_START}\n| Issue | Title |\n| --- | --- |\n| #1 | only two |\n{TABLE_END}"
    with pytest.raises(ValueError):
        upsert_row(body, row(2))
    bad_status = render_table([row(1)]).replace("`RUNNING`", "`WAT`")
    with pytest.raises(ValueError):
        parse_table(bad_status)
