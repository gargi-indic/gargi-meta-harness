"""Validator rules for parallel/join (M3.7)."""

from harness.playbook.load import load_playbook
from harness.playbook.validate import validate

VALID = """
apiVersion: harness/v1
id: test
nodes:
  p1: {kind: parallel, branches: [n1, n2]}
  n1: {kind: escalate}
  n2: {kind: escalate}
  j1: {kind: join, mode: all}
edges:
  "n1.resolved": j1
  "n2.resolved": j1
  "j1.done": "@done"
  "j1.failed": "@failed"
"""


def codes(yaml: str) -> list[str]:
    return sorted(i.code for i in validate(load_playbook(yaml)))


def test_valid_fork_join_passes():
    assert validate(load_playbook(VALID)) == []


def test_parallel_with_one_target():
    yaml = (
        VALID.replace("[n1, n2]", "[n1]")
        .replace('  "n2.resolved": j1\n', "")
        .replace("  n2: {kind: escalate}\n", "")
    )
    assert "parallel_min_targets" in codes(yaml)


def test_branches_not_reaching_a_common_join():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  p1: {kind: parallel, branches: [n1, n2]}
  n1: {kind: escalate}
  n2: {kind: escalate}
  j1: {kind: join, mode: all, inputs: [n1, n3]}
  n3: {kind: escalate}
edges:
  "n1.resolved": j1
  "n2.resolved": "@done"
  "n3.resolved": j1
  "j1.done": "@done"
  "j1.failed": "@failed"
"""
    assert "branch_not_joined" in codes(yaml)


def test_join_with_a_single_incoming_edge():
    yaml = """
apiVersion: harness/v1
id: test
nodes:
  n1: {kind: escalate}
  j1: {kind: join, mode: any}
edges:
  "n1.resolved": j1
  "j1.done": "@done"
  "j1.failed": "@failed"
"""
    assert codes(yaml) == ["join_min_inputs"]


def test_parallel_in_a_cycle_without_max_visits():
    yaml = VALID.replace('"j1.failed": "@failed"', '"j1.failed": p1')
    assert "parallel_cycle_without_max_visits" in codes(yaml)


def test_parallel_cycle_with_max_visits_is_fine():
    yaml = VALID.replace('"j1.failed": "@failed"', '"j1.failed": p1').replace(
        "p1: {kind: parallel, branches: [n1, n2]}", "p1: {kind: parallel, branches: [n1, n2], max_visits: 2}"
    )
    assert "parallel_cycle_without_max_visits" not in codes(yaml)


def test_select_mode_is_a_warning_pointing_at_m3_8():
    issues = validate(load_playbook(VALID.replace("mode: all", "mode: select")))
    assert [(i.code, i.severity) for i in issues] == [("join_select_not_implemented", "warning")]
    assert "M3.8" in issues[0].message
