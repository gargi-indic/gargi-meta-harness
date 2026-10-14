import importlib.resources

import pytest

from harness.playbook.load import PlaybookError, load_playbook
from harness.playbook.validate import validate


def test_plan_build_review_validates():
    from harness.playbook.load import builtin_playbooks

    playbooks = builtin_playbooks()
    pb = playbooks["plan-build-review"]
    issues = validate(pb)
    assert not issues


def test_unbounded_cycle():
    pb_text = importlib.resources.files("harness.playbooks").joinpath("plan-build-review.yaml").read_text()
    pb_text = pb_text.replace(
        'fix:         {kind: agent, session: main, action: revise, expect: pr, message: "{{ prev.correction }}", max_visits: 3}',
        'fix:         {kind: agent, session: main, action: revise, expect: pr, message: "{{ prev.correction }}"}',
    )

    pb = load_playbook(pb_text)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unbounded_cycle"}


def test_bad_edge_key():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
edges:
  "badkey": n1
  "a.": n1
  ".b": n1
  "a.b.c": n1
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"bad_edge_key", "unrouted_port"}


def test_unknown_node():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
edges:
  "n2.port": n1
  "n1.resolved": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unknown_node"}


def test_bad_port():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
edges:
  "n1.badport": n1
  "*.resolved": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"bad_port"}


def test_bad_target():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
edges:
  "n1.resolved": "n2"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"bad_target"}


def test_unrouted_port():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
edges: {}
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unrouted_port"}


def test_branch_unrouted_port_once():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: branch, cases: [{when: "foo", port: "a"}], default: "b"}
edges: {}
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert len(issues) == 2
    assert {i.code for i in issues} == {"unrouted_port"}
    ports_reported = [i.path for i in issues]
    assert "nodes.n1" in ports_reported[0]
    assert "nodes.n1" in ports_reported[1]


def test_unreachable():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
  n2: {kind: escalate}
edges:
  "n1.resolved": "@done"
  "n2.resolved": "@done"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unreachable"}


def test_unknown_gate():
    yaml = """
apiVersion: harness/v1
id: test
gates: {}
nodes:
  n1: {kind: gate, gate: plan}
edges:
  "n1.approved": "@done"
  "n1.rejected": "@failed"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unknown_gate"}


def test_missing_message():
    yaml = """
apiVersion: harness/v1
id: test
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
    assert {i.code for i in issues} == {"missing_message"}


def test_capability():
    yaml = """
apiVersion: harness/v1
id: test
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
    assert {i.code for i in issues} == {"capability"}


def test_unknown_branch():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: parallel, branches: ["n2"]}
edges: {}
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unknown_branch"}


def test_self_loop():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
edges:
  "n1.resolved": "n1"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unbounded_cycle"}


def test_terminal_targets():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: check, check: ci}
edges:
  "n1.pass": "@done"
  "n1.fail": "@failed"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert not issues

    yaml_bad = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: check, check: ci}
edges:
  "n1.pass": "@x"
  "n1.fail": "@done"
    """
    pb_bad = load_playbook(yaml_bad)
    issues_bad = validate(pb_bad)
    assert {i.code for i in issues_bad} == {"bad_target"}


def test_wildcard_resolution():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: check, check: ci}
edges:
  "n1.pass": "@done"
  "*.fail": "@failed"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert not issues


def test_multi_node_scc():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: check, check: ci}
  n2: {kind: escalate}
edges:
  "n1.pass": "n2"
  "n1.fail": "@failed"
  "n2.resolved": "n1"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert {i.code for i in issues} == {"unbounded_cycle"}
    assert len(issues) == 2  # n1 and n2


def test_parallel_join_reachability():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  p1: {kind: parallel, branches: ["n1", "n2"]}
  n1: {kind: escalate}
  n2: {kind: escalate}
  j1: {kind: join, mode: all, inputs: ["n1", "n2"]}
edges:
  "n1.resolved": "j1"
  "n2.resolved": "j1"
  "j1.done": "@done"
  "j1.failed": "@failed"
    """
    pb = load_playbook(yaml)
    issues = validate(pb)
    assert not issues


def test_per_action_port_sets():
    from harness.playbook.models import AgentNode

    n_plan = AgentNode(session="s", action="plan")
    assert n_plan.ports() == ("plan_ready", "pr_ready", "needs_input", "failed", "timeout")
    n_implement = AgentNode(session="s", action="implement")
    assert n_implement.ports() == ("pr_ready", "needs_input", "failed", "timeout")
    n_revise = AgentNode(session="s", action="revise", expect="pr")
    assert n_revise.ports() == ("pr_ready", "needs_input", "failed", "timeout")


def test_exhausted_present_only_with_max_visits():
    from harness.playbook.models import CheckNode

    n1 = CheckNode(check="ci")
    assert n1.ports() == ("pass", "fail")
    n2 = CheckNode(check="ci", max_visits=3)
    assert n2.ports() == ("pass", "fail", "exhausted")


def test_duration_pattern_error():
    yaml = """
apiVersion: harness/v1
id: test
limits:
  timeout: "5x"
nodes: {}
edges: {}
    """
    with pytest.raises(PlaybookError) as exc:
        load_playbook(yaml)
    assert "timeout" in str(exc.value)
