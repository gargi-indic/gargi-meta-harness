import importlib.resources

from harness.playbook.load import load_playbook
from harness.playbook.validate import validate


# Test that the default playbook loads and validates cleanly
def test_plan_build_review_validates():
    from harness.playbook.load import builtin_playbooks

    playbooks = builtin_playbooks()
    pb = playbooks["plan-build-review"]
    issues = validate(pb)
    assert not issues


def test_unbounded_cycle():
    pb_text = importlib.resources.files("harness.playbooks").joinpath("plan-build-review.yaml").read_text()

    # Remove max_visits from fix
    pb_text = pb_text.replace(
        'fix:         {kind: agent, session: main, action: revise, expect: pr, message: "{{ prev.correction }}", max_visits: 3}',
        'fix:         {kind: agent, session: main, action: revise, expect: pr, message: "{{ prev.correction }}"}',
    )

    pb = load_playbook(pb_text)
    issues = validate(pb)

    assert any(issue.code == "unbounded_cycle" for issue in issues)


def test_bad_edge_key():
    yaml = """
apiVersion: harness/v1
id: test-bad-key
nodes:
  n1: {kind: escalate}
edges:
  "badkey": n1
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "bad_edge_key" for issue in issues)


def test_unknown_node():
    yaml = """
apiVersion: harness/v1
id: test-unknown
nodes:
  n1: {kind: escalate}
edges:
  "n2.port": n1
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "unknown_node" for issue in issues)


def test_bad_port():
    yaml = """
apiVersion: harness/v1
id: test-bad-port
nodes:
  n1: {kind: escalate}
edges:
  "n1.badport": n1
  "*.resolved": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "bad_port" for issue in issues)


def test_bad_target():
    yaml = """
apiVersion: harness/v1
id: test-bad-target
nodes:
  n1: {kind: escalate}
edges:
  "n1.resolved": "n2"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "bad_target" for issue in issues)


def test_unrouted_port():
    yaml = """
apiVersion: harness/v1
id: test-unrouted
nodes:
  n1: {kind: escalate}
edges: {}
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "unrouted_port" for issue in issues)


def test_unreachable():
    yaml = """
apiVersion: harness/v1
id: test-unreachable
nodes:
  n1: {kind: escalate}
  n2: {kind: escalate}
edges:
  "n1.resolved": "@done"
  "n2.resolved": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "unreachable" for issue in issues)


def test_unknown_gate():
    yaml = """
apiVersion: harness/v1
id: test-unknown-gate
gates: {}
nodes:
  n1: {kind: gate, gate: plan}
edges:
  "n1.approved": "@done"
  "n1.rejected": "@failed"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "unknown_gate" for issue in issues)


def test_missing_message():
    yaml = """
apiVersion: harness/v1
id: test-missing-message
nodes:
  n1: {kind: agent, session: s1, action: revise, expect: plan}
edges:
  "n1.plan_ready": "@done"
  "*.needs_input": "@done"
  "*.failed": "@done"
  "*.timeout": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "missing_message" for issue in issues)


def test_capability():
    yaml = """
apiVersion: harness/v1
id: test-capability
nodes:
  n1: {kind: agent, session: s1, action: plan, agent: "codex"}
edges:
  "n1.plan_ready": "@done"
  "n1.pr_ready": "@done"
  "*.needs_input": "@done"
  "*.failed": "@done"
  "*.timeout": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb, agent_caps={"codex": {"implement"}})
    assert any(issue.code == "capability" for issue in issues)


def test_unknown_branch():
    yaml = """
apiVersion: harness/v1
id: test-unknown-branch
nodes:
  n1: {kind: parallel, branches: ["n2"]}
edges: {}
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "unknown_branch" for issue in issues)


def test_self_loop():
    yaml = """
apiVersion: harness/v1
id: test-self-loop
nodes:
  n1: {kind: escalate}
edges:
  "n1.resolved": "n1"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert any(issue.code == "unbounded_cycle" for issue in issues)
