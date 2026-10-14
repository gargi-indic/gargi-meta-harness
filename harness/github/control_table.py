import re
from typing import Literal

from pydantic import BaseModel

STATUS_TAGS = (
    "queued",
    "ready",
    "planning",
    "running",
    "review",
    "needs-you",
    "blocked",
    "done",
    "failed",
)

StatusType = Literal[
    "queued",
    "ready",
    "planning",
    "running",
    "review",
    "needs-you",
    "blocked",
    "done",
    "failed",
]


class ControlRow(BaseModel, extra="forbid"):
    issue: int
    title: str
    status: StatusType
    agent: str | None = None
    run_id: str | None = None
    pr: str | None = None
    note: str | None = None


MARKER_START = "<!-- harness:table:start -->"
MARKER_END = "<!-- harness:table:end -->"


def _escape_cell(text: str | None) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\n", " ").replace("\r", " ")
    text = text.replace("|", r"\|")
    return text.strip()


def _unescape_cell(text: str) -> str:
    text = text.strip()
    return text.replace(r"\|", "|")


def render_table(rows: list[ControlRow]) -> str:
    lines = []
    lines.append(MARKER_START)
    lines.append("| Issue | Title | Status | Agent | Run ID | PR | Note |")
    lines.append("|---|---|---|---|---|---|---|")

    for row in rows:
        issue_str = f"#{row.issue}"
        title_str = _escape_cell(row.title)
        status_str = f"`{row.status.upper()}`"
        agent_str = _escape_cell(row.agent)
        run_id_str = _escape_cell(row.run_id)
        pr_str = _escape_cell(row.pr)
        note_str = _escape_cell(row.note)

        lines.append(
            f"| {issue_str} | {title_str} | {status_str} | {agent_str} | {run_id_str} | {pr_str} | {note_str} |"
        )

    lines.append(MARKER_END)
    return "\n".join(lines)


def replace_table(issue_body: str, table_md: str) -> str:
    if not issue_body:
        return table_md

    pattern = re.compile(rf"{re.escape(MARKER_START)}.*?{re.escape(MARKER_END)}", re.DOTALL)

    if pattern.search(issue_body):
        # We replace only the first occurrence to avoid messing up other things,
        # though there should be only one table.
        # We need to make sure we don't alter surrounding CRLF or newlines.
        # A simple string replacement using the matched block is safest.
        match = pattern.search(issue_body)
        matched_str = match.group(0)
        return issue_body.replace(matched_str, table_md, 1)
    else:
        # Append to the end, ensuring it's separated by newlines
        if issue_body.endswith("\n\n"):
            return f"{issue_body}{table_md}"
        elif issue_body.endswith("\n"):
            return f"{issue_body}\n{table_md}"
        else:
            return f"{issue_body}\n\n{table_md}"


def _parse_table(issue_body: str) -> list[ControlRow]:
    if not issue_body:
        return []

    pattern = re.compile(rf"{re.escape(MARKER_START)}(.*?){re.escape(MARKER_END)}", re.DOTALL)
    match = pattern.search(issue_body)
    if not match:
        return []

    table_content = match.group(1).strip()
    lines = table_content.split("\n")

    rows = []
    # Skip header and separator (at least 2 lines)
    if len(lines) < 2:
        return rows

    # The actual data starts after the separator
    data_lines = lines[2:]

    for line in data_lines:
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue

        # Split by `|`, but ignore escaped `\|`. We can use a regex for this or custom split.
        # However, since `\|` is just a string, we can temporarily replace `\|` with a placeholder,
        # split by `|`, and then restore it.
        # Using a character that doesn't appear in the table (e.g., \x00)
        temp_line = line.replace(r"\|", "\x00")
        parts = temp_line.split("|")
        # Remove empty parts from start and end
        if parts:
            parts = parts[1:-1]

        if len(parts) < 7:
            continue

        parts = [p.replace("\x00", r"\|") for p in parts]

        try:
            issue_str = parts[0].strip()
            if issue_str.startswith("#"):
                issue_str = issue_str[1:]
            issue = int(issue_str)

            title = _unescape_cell(parts[1])

            status_str = parts[2].strip()
            # Remove backticks and convert to lowercase
            if status_str.startswith("`") and status_str.endswith("`"):
                status_str = status_str[1:-1]
            status_str = status_str.lower()

            if status_str not in STATUS_TAGS:
                continue

            agent = _unescape_cell(parts[3]) or None
            run_id = _unescape_cell(parts[4]) or None
            pr = _unescape_cell(parts[5]) or None
            note = _unescape_cell(parts[6]) or None

            rows.append(
                ControlRow(
                    issue=issue, title=title, status=status_str, agent=agent, run_id=run_id, pr=pr, note=note
                )
            )
        except (ValueError, IndexError):
            continue

    return rows


def upsert_row(issue_body: str, row: ControlRow) -> str:
    rows = _parse_table(issue_body)

    # Check if row with this issue exists
    existing = False
    for i, r in enumerate(rows):
        if r.issue == row.issue:
            rows[i] = row
            existing = True
            break

    if not existing:
        rows.append(row)

    # Sort by issue number
    rows.sort(key=lambda r: r.issue)

    table_md = render_table(rows)
    return replace_table(issue_body, table_md)
