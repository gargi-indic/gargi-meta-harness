"""The decompose node (M4.3): fields, ports and the validator rules."""

import pytest
from pydantic import ValidationError

from harness.planner.proposal import DEFAULT_MAX_ISSUES
from harness.playbook.load import load_playbook
from harness.playbook.models import DecomposeNode
from harness.playbook.validate import validate


def codes(yaml: str) -> list[str]:
    return sorted(i.code for i in validate(load_playbook(yaml)))


def test_defaults_and_fields() -> None:
    node = DecomposeNode()
    assert (node.plan, node.guidance, node.create, node.max_issues) == (None, None, False, DEFAULT_MAX_ISSUES)
    full = DecomposeNode(plan="{{ inputs.plan_path }}", guidance="g", create=True, max_issues=5)
    assert full.max_issues == 5 and full.create
    with pytest.raises(ValidationError):
        DecomposeNode(max_issues=0)
    with pytest.raises(ValidationError):
        DecomposeNode(unknown=1)  # type: ignore[call-arg]


def test_ports_follow_create_and_max_visits() -> None:
    assert DecomposeNode().ports() == ("proposed", "invalid", "failed")
    assert DecomposeNode(create=True).ports() == ("proposed", "created", "invalid", "failed")
    assert DecomposeNode(max_visits=2).ports() == ("proposed", "invalid", "failed", "exhausted")


PLAIN = """
apiVersion: harness/v1
id: t
nodes:
  d: {kind: decompose, plan: docs/PLAN.md}
edges:
  d.proposed: "@done"
  d.invalid: "@failed"
  d.failed: "@failed"
"""


def test_a_fully_wired_decompose_is_valid() -> None:
    assert codes(PLAIN) == []


def test_wildcards_cover_ports_and_unwired_ports_are_flagged() -> None:
    wildcard = (
        PLAIN.split("edges:")[0]
        + 'edges:\n  "*.proposed": "@done"\n  "*.invalid": "@failed"\n  "*.failed": "@failed"\n'
    )
    assert codes(wildcard) == []
    missing = PLAIN.replace('  d.invalid: "@failed"\n', "")
    assert codes(missing) == ["unrouted_port"]
    old = PLAIN.replace("d.proposed", "d.created")
    assert codes(old) == ["bad_port", "unrouted_port"]


GATED = """
apiVersion: harness/v1
id: t
gates: {plan: human}
nodes:
  first: {kind: decompose, plan: docs/PLAN.md}
  plan: {kind: gate, gate: plan, scope: campaign}
  make: {kind: decompose, plan: docs/PLAN.md, create: true}
edges:
  first.proposed: plan
  first.invalid: "@failed"
  first.failed: "@failed"
  plan.approved: make
  plan.rejected: "@failed"
  make.proposed: "@done"
  make.created: "@done"
  make.invalid: "@failed"
  make.failed: "@failed"
"""


def test_create_needs_a_plan_gate_upstream() -> None:
    assert codes(GATED) == []
    alone = """
apiVersion: harness/v1
id: t
nodes:
  make: {kind: decompose, create: true}
edges:
  make.proposed: "@done"
  make.created: "@done"
  make.invalid: "@failed"
  make.failed: "@failed"
"""
    assert codes(alone) == ["create_without_plan_gate"]
    other_gate = GATED.replace("gates: {plan: human}", "gates: {plan: human, ok: human}").replace(
        "plan: {kind: gate, gate: plan, scope: campaign}", "plan: {kind: gate, gate: ok}"
    )
    assert codes(other_gate) == ["create_without_plan_gate"]


def test_a_plan_gate_after_the_node_does_not_count() -> None:
    after = """
apiVersion: harness/v1
id: t
gates: {plan: human}
nodes:
  make: {kind: decompose, create: true}
  plan: {kind: gate, gate: plan, scope: campaign}
edges:
  make.proposed: plan
  make.created: plan
  make.invalid: "@failed"
  make.failed: "@failed"
  plan.approved: "@done"
  plan.rejected: "@failed"
"""
    assert codes(after) == ["create_without_plan_gate"]


def test_bad_templates_are_flagged() -> None:
    bad = PLAIN.replace("plan: docs/PLAN.md", 'plan: "{{ inputs.x "')
    assert codes(bad) == ["bad_template"]
    bad_guidance = PLAIN.replace("plan: docs/PLAN.md", 'guidance: "{% if %}"')
    assert codes(bad_guidance) == ["bad_template"]
