"""Gate scope (M4.1): `scope: campaign` is accepted on the plan gate only."""

import pytest
from pydantic import ValidationError

from harness.playbook.load import load_playbook
from harness.playbook.models import GateNode, Playbook
from harness.playbook.validate import validate


def pb(gate_name: str = "plan", scope: str | None = "campaign", extra_node: str = "") -> Playbook:
    scope_line = f", scope: {scope}" if scope else ""
    return load_playbook(
        f"""
apiVersion: harness/v1
id: p
gates: {{{gate_name}: human}}
nodes:
  g: {{kind: gate, gate: {gate_name}{scope_line}}}
{extra_node}
edges: {{g.approved: "@done", g.rejected: "@failed"}}
"""
    )


def test_scope_defaults_to_issue() -> None:
    assert GateNode(gate="plan").scope == "issue"
    assert pb(scope=None).nodes["g"].scope == "issue"


def test_campaign_scope_on_the_plan_gate_validates() -> None:
    playbook = pb()
    assert playbook.nodes["g"].scope == "campaign"
    assert validate(playbook) == []


def test_campaign_scope_on_another_gate_is_a_validation_issue() -> None:
    issues = validate(pb(gate_name="merge"))
    assert [i.code for i in issues] == ["campaign_scope"]


def test_an_unknown_scope_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GateNode.model_validate({"gate": "plan", "scope": "world"})


def test_scope_is_only_allowed_on_gate_nodes() -> None:
    with pytest.raises(ValidationError):
        Playbook.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "p",
                "nodes": {"c": {"kind": "check", "check": "ci", "scope": "campaign"}},
                "edges": {"c.pass": "@done", "c.fail": "@failed"},
            }
        )
