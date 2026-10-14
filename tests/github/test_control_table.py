from harness.github.control_table import (
    ControlRow,
    render_table,
    replace_table,
    upsert_row,
)


def test_control_table_roundtrip():
    # Includes an escaped pipe in the note, spaces instead of newlines.
    row1 = ControlRow(issue=1, title="Test 1", status="queued", note="Some|Pipe and\nnewline")
    row2 = ControlRow(issue=2, title="Test 2", status="done", agent="Jules")

    table = render_table([row1, row2])

    assert "Some\\|Pipe and newline" in table
    assert "`QUEUED`" in table
    assert "`DONE`" in table

    body = f"Hello\n{table}\nWorld"

    # Upsert a modification
    row1_mod = ControlRow(issue=1, title="Test 1 Mod", status="running", note="No Pipe")
    new_body = upsert_row(body, row1_mod)

    assert "Test 1 Mod" in new_body
    assert "`RUNNING`" in new_body
    assert "Some\\|Pipe" not in new_body
    assert "`DONE`" in new_body

    # Upsert a new row
    row3 = ControlRow(issue=3, title="Test 3", status="failed")
    new_body = upsert_row(new_body, row3)

    assert "`FAILED`" in new_body

    # Sort order checking (upsert 1.5 -> issue=4, but let's just make sure it stays sorted)
    # Actually wait, rows are sorted by issue number
    row_mid = ControlRow(issue=2, title="Test 2 Mod", status="ready")
    new_body = upsert_row(new_body, row_mid)

    # 1, 2, 3 should appear in order in the markdown
    idx1 = new_body.find("Test 1 Mod")
    idx2 = new_body.find("Test 2 Mod")
    idx3 = new_body.find("Test 3")

    assert idx1 < idx2 < idx3


def test_replace_table_crlf_preservation():
    body = "Hello\r\nWorld\r\n<!-- harness:table:start -->\n| Issue |\n<!-- harness:table:end -->\r\nGoodbye"
    table = "<!-- harness:table:start -->\nNEW TABLE\n<!-- harness:table:end -->"
    new_body = replace_table(body, table)

    assert (
        new_body
        == "Hello\r\nWorld\r\n<!-- harness:table:start -->\nNEW TABLE\n<!-- harness:table:end -->\r\nGoodbye"
    )


def test_lookalike_table_ignored():
    body = """Here is a table:

| Issue | Title |
|---|---|
| #999 | Lookalike |

<!-- harness:table:start -->
| Issue | Title | Status | Agent | Run ID | PR | Note |
|---|---|---|---|---|---|---|
| #1 | Test 1 | `QUEUED` |  |  |  | |
<!-- harness:table:end -->
"""
    row = ControlRow(issue=2, title="T2", status="done")
    new_body = upsert_row(body, row)

    # The lookalike table should remain untouched
    assert "| #999 | Lookalike |" in new_body

    # The new row should be in the correct table
    assert "| #2 | T2 | `DONE`" in new_body.split("<!-- harness:table:start -->")[1]


def test_missing_table_append():
    body = "No table here."
    row = ControlRow(issue=1, title="New", status="ready")
    new_body = upsert_row(body, row)

    assert "No table here.\n\n<!-- harness:table:start -->" in new_body
    assert "`READY`" in new_body
