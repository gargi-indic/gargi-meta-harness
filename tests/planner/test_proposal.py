"""ProposedPlan models and validate_proposal: one test per code, cycles, the capability pair check."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from harness.adapters.base import Capabilities
from harness.planner.proposal import ProposalIssue, ProposedIssue, ProposedPlan, validate_proposal
from harness.playbook.load import builtin_playbooks, load_playbook

AGENTS = {
    "jules": Capabilities(plan_approval=True, messaging=True, opens_pr=True, local_workspace=False),
    "codex": Capabilities(plan_approval=False, messaging=False, opens_pr=False, local_workspace=True),
}
PLAYBOOKS = builtin_playbooks()
IMPLEMENT_ONLY = load_playbook(
    """
apiVersion: harness/v1
id: implement-only
nodes:
  implement: {kind: agent, session: main, action: implement}
edges: {}
"""
)


def issue(key: str, *deps: str, **over: Any) -> ProposedIssue:
    data: dict[str, Any] = {
        "key": key,
        "title": f"Do {key}",
        "body": "## Context\nc\n## Scope\ns\n## Acceptance criteria\n- a\n## Out of scope\n- o",
        "acceptance": ["it works"],
        "blocked_by": list(deps),
        "agent": "jules",
        "playbook": "plan-build-review",
    }
    data.update(over)
    return ProposedIssue(**data)


def plan(*issues: ProposedIssue) -> ProposedPlan:
    return ProposedPlan(epic_title="Epic", epic_body="Body", issues=list(issues))


def check(p: ProposedPlan, **kw: Any) -> list[ProposalIssue]:
    kw.setdefault("agents", AGENTS)
    kw.setdefault("playbooks", PLAYBOOKS)
    return validate_proposal(p, **kw)


def codes(found: list[ProposalIssue]) -> list[str]:
    return [i.code for i in found]


def test_valid_six_issue_plan_has_no_problems():
    p = plan(
        issue("schema"),
        issue("api", "schema"),
        issue("ui", "api"),
        issue("docs"),
        issue("tests", "api", "ui"),
        issue("release", "tests", "docs", agent="codex", playbook="implement-only"),
    )
    playbooks = {**PLAYBOOKS, "implement-only": IMPLEMENT_ONLY}
    assert check(p, playbooks=playbooks) == []
    assert check(p, playbooks=set(playbooks)) == []  # ids only


CASES: list[tuple[str, Callable[[], ProposedPlan], dict[str, Any], str, str | None]] = [
    ("duplicate_key", lambda: plan(issue("a"), issue("a")), {}, "duplicate_key", "a"),
    ("unknown_dep", lambda: plan(issue("a", "ghost")), {}, "unknown_dep", "a"),
    ("self_dep", lambda: plan(issue("a", "a")), {}, "self_dep", "a"),
    ("cycle", lambda: plan(issue("a", "b"), issue("b", "a")), {}, "cycle", "a"),
    ("unknown_agent", lambda: plan(issue("a", agent="gpt")), {}, "unknown_agent", "a"),
    ("unknown_playbook", lambda: plan(issue("a", playbook="nope")), {}, "unknown_playbook", "a"),
    (
        "plan_on_agent_without_plan",
        lambda: plan(issue("a", agent="codex")),
        {},
        "plan_on_agent_without_plan",
        "a",
    ),
    ("no_acceptance", lambda: plan(issue("a", acceptance=[])), {}, "no_acceptance", "a"),
    ("blank_acceptance", lambda: plan(issue("a", acceptance=["  "])), {}, "no_acceptance", "a"),
    (
        "too_many_issues",
        lambda: plan(issue("a"), issue("b"), issue("c")),
        {"max_issues": 2},
        "too_many_issues",
        None,
    ),
    ("title_too_long", lambda: plan(issue("a", title="x" * 101)), {}, "title_too_long", "a"),
    ("empty_body", lambda: plan(issue("a", body=" \n ")), {}, "empty_body", "a"),
]


@pytest.mark.parametrize(("name", "build", "kwargs", "code", "key"), CASES, ids=[c[0] for c in CASES])
def test_each_code_is_reported_exactly(name, build, kwargs, code, key):
    found = check(build(), **kwargs)
    assert codes(found) == [code]
    assert found[0].key == key
    assert found[0].message


def test_title_of_exactly_100_characters_is_fine():
    assert check(plan(issue("a", title="x" * 100))) == []


def test_exactly_max_issues_is_fine():
    assert check(plan(issue("a"), issue("b")), max_issues=2) == []


def test_several_problems_are_all_reported():
    found = check(plan(issue("a", "ghost", agent="gpt"), issue("b", body="", acceptance=[])))
    assert sorted(codes(found)) == ["empty_body", "no_acceptance", "unknown_agent", "unknown_dep"]


def test_duplicate_key_is_reported_once_however_often_it_repeats():
    found = check(plan(issue("a"), issue("a"), issue("a")))
    assert codes(found) == ["duplicate_key"]


def test_cycle_message_has_the_path():
    found = check(plan(issue("a", "b"), issue("b", "c"), issue("c", "a")))
    assert [i.code for i in found] == ["cycle"]
    assert found[0].message.endswith("a -> b -> c -> a")


def test_thirty_issue_chain_is_valid_and_closing_it_is_a_cycle():
    keys = [f"i{n}" for n in range(30)]
    chain = [issue(k, *([keys[n - 1]] if n else [])) for n, k in enumerate(keys)]
    assert check(plan(*chain), max_issues=40) == []
    closed = [issue(keys[0], keys[-1]), *chain[1:]]
    found = check(plan(*closed), max_issues=40)
    assert codes(found) == ["cycle"]
    path = found[0].message.split(": ", 1)[1].split(" -> ")
    assert path[0] == path[-1] and len(path) == 31 and set(path) == set(keys)


def test_diamond_is_valid_and_a_back_edge_makes_a_cycle():
    diamond = [issue("top", "left", "right"), issue("left", "base"), issue("right", "base"), issue("base")]
    assert check(plan(*diamond)) == []
    broken = [
        issue("top", "left", "right"),
        issue("left", "base"),
        issue("right", "base"),
        issue("base", "top"),
    ]
    found = check(plan(*broken))
    # The search reports the first cycle it closes; the other one shows up after that is fixed.
    assert codes(found) == ["cycle"]
    assert found[0].message.endswith("top -> left -> base -> top")


def test_five_hundred_node_chain_does_not_hit_the_recursion_limit():
    n = 500
    chain = [issue(f"n{i}", *([f"n{i - 1}"] if i else [])) for i in range(n)]
    assert check(plan(*chain), max_issues=1000) == []
    closed = [issue("n0", f"n{n - 1}"), *chain[1:]]
    found = check(plan(*closed), max_issues=1000)
    assert codes(found) == ["cycle"]
    assert found[0].message.count("->") == n


def test_self_dep_is_not_also_a_cycle():
    assert codes(check(plan(issue("a", "a")))) == ["self_dep"]


def test_plan_check_uses_the_validators_capability_rule_and_only_applies_to_templated_agents():
    # codex cannot plan: plan-build-review plans on {{ issue.agent }}, implement-only never plans.
    both = {**PLAYBOOKS, "implement-only": IMPLEMENT_ONLY}
    found = check(
        plan(issue("a", agent="codex"), issue("b", agent="codex", playbook="implement-only")), playbooks=both
    )
    assert [(i.code, i.key) for i in found] == [("plan_on_agent_without_plan", "a")]
    # With ids only the pair cannot be checked.
    assert check(plan(issue("a", agent="codex")), playbooks=set(PLAYBOOKS)) == []


def test_unknown_agent_or_playbook_skips_the_pair_check():
    found = check(plan(issue("a", agent="gpt"), issue("b", playbook="nope", agent="codex")))
    assert sorted(codes(found)) == ["unknown_agent", "unknown_playbook"]


def test_model_rejects_bad_keys_unknown_fields_and_empty_issue_lists():
    for key in ["", "-a", "A", "a_b", "x" * 42, "a b"]:
        with pytest.raises(ValidationError):
            issue(key)
    assert issue("x" * 41).key == "x" * 41
    with pytest.raises(ValidationError):
        issue("a", blockedBy=["b"])
    with pytest.raises(ValidationError):
        ProposedPlan(epic_title="e", epic_body="b", issues=[])
