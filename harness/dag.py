from harness.store.repo import IssueRow


def blocked(issues: list[IssueRow]) -> dict[int, list[int]]:
    """Return a mapping from issue number to its open blockers."""
    open_numbers = {i.number for i in issues if i.state == "open"}
    issue_numbers = {i.number for i in issues}
    result = {}
    for issue in issues:
        open_blockers = []
        for blocker in issue.blocked_by:
            # If the blocker is not in our list (not fetched/not known), we treat it as open
            # as per the instruction "a blocker missing from the cache counts as open"
            is_open = blocker not in issue_numbers or blocker in open_numbers

            if is_open:
                open_blockers.append(blocker)
        result[issue.number] = open_blockers
    return result


def find_cycle(issues: list[IssueRow]) -> list[int] | None:
    """Find a cycle in the blocked_by dependencies using iterative DFS.
    Returns the list of issue numbers in the cycle, or None.
    """
    blocker_map = {i.number: i.blocked_by for i in issues}

    # We want to find cycles.
    for start_node in blocker_map:
        stack = [(start_node, [start_node])]

        while stack:
            node, path = stack.pop()
            for neighbor in blocker_map.get(node, []):
                if neighbor in path:
                    # Cycle found!
                    cycle_start_idx = path.index(neighbor)
                    return path[cycle_start_idx:] + [neighbor]
                stack.append((neighbor, path + [neighbor]))

    return None


def ready_set(issues: list[IssueRow], running: set[int], escalate_labels: list[str]) -> list[int]:
    """An issue is ready if it is open, every blocker is closed, it is not running,
    it is not an epic (has no sub-issues), and it carries no label from escalate_labels.
    The result is ordered by issue number.
    """
    # An epic is any issue that is the parent of another issue in the list.
    parent_numbers = {i.parent for i in issues if i.parent is not None}

    blocked_map = blocked(issues)

    ready = []
    for issue in issues:
        if issue.state != "open":
            continue

        if issue.number in running:
            continue

        if issue.number in parent_numbers:
            continue

        if any(label in escalate_labels for label in issue.labels):
            continue

        if blocked_map.get(issue.number):  # has open blockers
            continue

        ready.append(issue.number)

    return sorted(ready)
