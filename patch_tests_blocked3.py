content = open("tests/test_dispatcher.py").read()
# Note that in the patched file I replaced blocked_by=[99] with blocked_by=[] globally. This broke the first issue.
content = content.replace(
    'issue_row(1, labels=["agent:claude"], blocked_by=[]), # blocked by open issue',
    'issue_row(1, labels=["agent:claude"], blocked_by=[99]), # blocked by open issue',
)

with open("tests/test_dispatcher.py", "w") as f:
    f.write(content)
