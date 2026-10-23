content = open("tests/test_dispatcher.py").read()
# Fix test_plan_ordering_and_overlap
content = content.replace('labels=["priority:2"]', 'labels=["priority:2", "agent:claude"]')
content = content.replace('labels=["priority:1"]', 'labels=["priority:1", "agent:claude"]')
content = content.replace('body="paths: src/"', 'body="paths: src/", labels=["agent:claude"]')

# Fix default_registry missing argument
content = content.replace("default_registry(),", "default_registry(review_backend=FakeBackend()),")
content = content.replace(
    "from harness.github.outbox import Outbox",
    "from harness.github.outbox import Outbox\n    from harness.llm.backend import FakeBackend",
)

with open("tests/test_dispatcher.py", "w") as f:
    f.write(content)
