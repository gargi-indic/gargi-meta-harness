content = open("tests/test_dispatcher.py").read()
content = content.replace('issue_row(99, state="open")', "issue_row(99)")
content = content.replace(
    'issue_row(100, state="closed")',
    'IssueRow(repo="test/repo", number=100, id=100, title="Issue 100", body="", state="closed", labels=[], parent=None, blocked_by=[], updated_at="2023-01-01T00:00:00Z")',
)

with open("tests/test_dispatcher.py", "w") as f:
    f.write(content)
