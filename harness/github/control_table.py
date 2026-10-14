"""The control issue's status table, edited by anchored replacement between HTML comment markers.

Pure functions only: no network, no I/O. Text outside the markers is never rewritten, so an issue
body with surrounding prose, CRLF line endings or look-alike tables survives byte for byte.
"""

from dataclasses import dataclass
from typing import Literal, get_args

TABLE_START = "<!-- harness:table:start -->"
TABLE_END = "<!-- harness:table:end -->"

Status = Literal["queued", "ready", "planning", "running", "review", "needs-you", "blocked", "done", "failed"]

# Status -> text tag shown in the table (no emojis).
STATUS_TAGS: dict[str, str] = {status: status.upper() for status in get_args(Status)}
_TAG_TO_STATUS = {tag: status for status, tag in STATUS_TAGS.items()}

_HEADERS = ("Issue", "Title", "Status", "Agent", "Run", "PR", "Note")


@dataclass(frozen=True)
class ControlRow:
    issue: int
    title: str
    status: Status
    agent: str | None = None
    run_id: str | None = None
    pr: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUS_TAGS:
            raise ValueError(f"unknown status {self.status!r}")


def _escape_cell(value: str | None) -> str:
    if not value:
        return ""
    text = " ".join(value.splitlines()) if ("\n" in value or "\r" in value) else value
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    # Never let a cell smuggle in a marker comment.
    return text.replace("<!--", "&lt;!--")


def _split_cells(line: str) -> list[str]:
    """Split a table line on unescaped pipes, unescaping ``\\|`` and ``\\\\`` in each cell."""
    cells: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] in ("|", "\\"):
            current.append(line[i + 1])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current))
    return cells


def _row_cells(row: ControlRow) -> list[str]:
    return [
        f"#{row.issue}",
        _escape_cell(row.title),
        f"`{STATUS_TAGS[row.status]}`",
        _escape_cell(row.agent),
        _escape_cell(row.run_id),
        _escape_cell(row.pr),
        _escape_cell(row.note),
    ]


def _render_lines(rows: list[ControlRow]) -> list[str]:
    lines = [
        "| " + " | ".join(_HEADERS) + " |",
        "|" + "|".join(" --- " for _ in _HEADERS) + "|",
    ]
    for row in sorted(rows, key=lambda r: r.issue):
        lines.append("| " + " | ".join(_row_cells(row)) + " |")
    return lines


def render_table(rows: list[ControlRow]) -> str:
    """Render ``rows`` (sorted by issue number) as a markdown table between the table markers."""
    return "\n".join([TABLE_START, *_render_lines(rows), TABLE_END])


def _locate(body: str) -> tuple[int, int] | None:
    """Return (index of start marker, index just past end marker), or None if no table block."""
    start = body.find(TABLE_START)
    if start == -1:
        return None
    end = body.find(TABLE_END, start + len(TABLE_START))
    if end == -1:
        raise ValueError("control table start marker has no matching end marker")
    return start, end + len(TABLE_END)


def _inner_lines(table_md: str) -> list[str]:
    """The table lines of ``table_md``, with surrounding markers (if any) removed."""
    text = table_md.strip()
    if text.startswith(TABLE_START) and text.endswith(TABLE_END):
        text = text[len(TABLE_START) : len(text) - len(TABLE_END)]
    return [line for line in text.splitlines() if line.strip()]


def replace_table(issue_body: str, table_md: str) -> str:
    """Replace only the block between the table markers; append the block if there is none.

    ``table_md`` may be the output of :func:`render_table` (with markers) or just the table lines.
    Everything outside the markers is preserved byte for byte.
    """
    nl = "\r\n" if "\r\n" in issue_body else "\n"
    block = nl.join([TABLE_START, *_inner_lines(table_md), TABLE_END])
    span = _locate(issue_body)
    if span is not None:
        start, end = span
        return issue_body[:start] + block + issue_body[end:]
    if not issue_body:
        return block
    sep = nl if not issue_body.endswith("\n") else ""
    return issue_body + sep + nl + block


def _parse_row(line: str) -> ControlRow:
    cells = _split_cells(line.strip())
    if len(cells) != len(_HEADERS) + 2 or cells[0].strip() or cells[-1].strip():
        raise ValueError(f"malformed control table row: {line!r}")
    issue_c, title, status_c, agent, run_id, pr, note = (c.strip() for c in cells[1:-1])
    try:
        issue = int(issue_c.lstrip("#"))
    except ValueError:
        raise ValueError(f"bad issue number in control table row: {line!r}") from None
    status = _TAG_TO_STATUS.get(status_c.strip("`").strip())
    if status is None:
        raise ValueError(f"unknown status in control table row: {line!r}")
    return ControlRow(
        issue=issue,
        title=title,
        status=status,  # type: ignore[arg-type]
        agent=agent or None,
        run_id=run_id or None,
        pr=pr or None,
        note=note or None,
    )


def parse_table(issue_body: str) -> list[ControlRow]:
    """Parse the rows of the table between the markers (empty list if there is no table)."""
    span = _locate(issue_body)
    if span is None:
        return []
    inner = issue_body[span[0] + len(TABLE_START) : span[1] - len(TABLE_END)]
    rows: list[ControlRow] = []
    for line in inner.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in _split_cells(stripped)[1:-1]]
        if all(c and set(c) <= set(":-") for c in cells):
            continue  # separator line
        if cells and cells[0].lower() == _HEADERS[0].lower():
            continue  # header line
        rows.append(_parse_row(stripped))
    return rows


def upsert_row(issue_body: str, row: ControlRow) -> str:
    """Insert or replace the row for ``row.issue`` and re-render the table (sorted by issue)."""
    rows = [r for r in parse_table(issue_body) if r.issue != row.issue]
    rows.append(row)
    return replace_table(issue_body, render_table(rows))
