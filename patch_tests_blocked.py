content = open("tests/test_dispatcher.py").read()

# Fix 1: The test expects issue 3 to start, but issue 3 has blocked_by=[99]. 99 is not in the db, so it is treated as open (missing = open), hence blocked.
# We will insert issue 99 into db and make it closed to test "not blocked", or just remove blocked_by=[99] and change depth logic test.
# The prompt says: "Table-driven plan unit tests... blocked issue never selected; issue with non-terminal run skipped".
# I'll create dedicated tests for these rather than overloading test_plan_ordering_and_overlap.

if "def test_blocked_and_running_skipped" not in content:
    content += """
def test_blocked_and_running_skipped():
    store = setup_store()
    dispatcher = Dispatcher(store=store, caps={"claude": 5})

    from harness.store.repo import upsert_issue
    # Create issue 99 as open
    upsert_issue(store, issue_row(99, state="open"))
    upsert_issue(store, issue_row(100, state="closed"))

    ready = [
        issue_row(1, labels=["agent:claude"], blocked_by=[99]), # blocked by open issue
        issue_row(2, labels=["agent:claude"], blocked_by=[100]), # blocked by closed issue -> ready
        issue_row(3, labels=["agent:claude"], blocked_by=[101]), # blocked by missing issue -> blocked
        issue_row(4, labels=["agent:claude"]), # running
    ]

    # Actually we just pass ready to plan.
    decisions = dispatcher.plan(ready, [run_row(4, "claude")])

    reasons = {d.issue: d.reason for d in decisions}
    assert reasons[1] == "blocked"
    assert reasons[2] == "started"
    assert reasons[3] == "blocked"
    assert reasons[4] == "running"
"""

# Now fix test_plan_ordering_and_overlap
content = content.replace("blocked_by=[99]", "blocked_by=[]")

with open("tests/test_dispatcher.py", "w") as f:
    f.write(content)
