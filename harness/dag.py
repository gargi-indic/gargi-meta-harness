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
    """Find a cycle in the blocked_by dependencies using iterative 3-color DFS.
    Returns the list of issue numbers in the cycle, or None.
    """
    blocker_map = {i.number: i.blocked_by for i in issues}

    white = set(blocker_map.keys())
    grey = set()
    black = set()

    for start_node in list(white):
        if start_node not in white:
            continue

        stack = [(start_node, False)]  # (node, is_backtrack)
        path_stack = []

        while stack:
            node, is_backtrack = stack.pop()

            if is_backtrack:
                grey.remove(node)
                black.add(node)
                if path_stack:
                    path_stack.pop()
                continue

            if node in black:
                continue

            if node in grey:
                # Cycle found!
                cycle_start_idx = path_stack.index(node)
                return path_stack[cycle_start_idx:] + [node]

            if node in white:
                white.remove(node)
            grey.add(node)
            path_stack.append(node)
            stack.append((node, True))

            for neighbor in blocker_map.get(node, []):
                if neighbor in black:
                    continue
                stack.append((neighbor, False))

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
