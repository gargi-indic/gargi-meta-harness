from harness.dag import blocked, find_cycle, ready_set
from harness.store.repo import IssueRow


def _make_issue(number, state="open", blocked_by=None, parent=None, labels=None):
    return IssueRow(
        repo="owner/repo",
        number=number,
        id=number * 100,
        title=f"Issue {number}",
        body="",
        state=state,
        labels=labels or [],
        parent=parent,
        blocked_by=blocked_by or [],
        updated_at="2023-01-01T00:00:00Z",
    )


def test_blocked():
    issues = [
        _make_issue(1, state="closed"),
        _make_issue(2, state="open"),
        _make_issue(3, state="open", blocked_by=[1, 2]),
        _make_issue(4, state="open", blocked_by=[5]),  # 5 is missing, treated as open
    ]

    b = blocked(issues)
    assert b[1] == []
    assert b[2] == []
    assert b[3] == [2]  # 1 is closed, so only 2 is open
    assert b[4] == [5]  # 5 is open because missing


def test_find_cycle():
    issues = [
        _make_issue(1, blocked_by=[2]),
        _make_issue(2, blocked_by=[3]),
        _make_issue(3, blocked_by=[1]),
        _make_issue(4, blocked_by=[5]),
        _make_issue(5, blocked_by=[]),
    ]
    cycle = find_cycle(issues)
    assert cycle is not None
    assert set(cycle) == {1, 2, 3}

    issues_no_cycle = [
        _make_issue(1, blocked_by=[2]),
        _make_issue(2, blocked_by=[3]),
        _make_issue(3, blocked_by=[]),
    ]
    assert find_cycle(issues_no_cycle) is None


def test_ready_set():
    issues = [
        _make_issue(1, state="open"),  # Epic (parent of 2)
        _make_issue(2, state="open", parent=1),  # Ready
        _make_issue(3, state="open", blocked_by=[2]),  # Blocked by 2
        _make_issue(4, state="closed"),  # Closed, not ready
        _make_issue(5, state="open", labels=["needs-discussion"]),  # Escalate label
        _make_issue(6, state="open"),  # Running, not ready
    ]

    running = {6}
    escalate_labels = ["needs-discussion"]

    ready = ready_set(issues, running, escalate_labels)
    assert ready == [2]
