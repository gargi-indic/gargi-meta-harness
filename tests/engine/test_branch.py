"""Branch executor (M3.9 audit): first match wins, default port, no match, sandbox, validator errors."""

from typing import Any

import pytest

from harness.engine import Done, ExecutorRegistry
from harness.engine.executors.branch import BranchExecutor
from harness.playbook.load import load_playbook
from harness.playbook.validate import validate
from tests.engine.helpers import REPO, Scripted, drive, make_issue, make_playbook

RUN_ID = f"{REPO}#7@1"


def _playbook(cases: list[dict[str, str]], default: str | None = "other"):
    branch: dict[str, Any] = {"kind": "branch", "cases": cases}
    if default is not None:
        branch["default"] = default
    nodes = {"br": branch, **{n: {"kind": "notify", "message": "hi"} for n in ("x", "y", "z")}}
    edges = {"br.p1": "x", "br.p2": "y", "br.other": "z", "br.failed": "@failed"}
    edges.update({f"{n}.sent": "@done" for n in ("x", "y", "z")})
    return make_playbook(nodes, edges)


async def _run(env, playbook, labels: tuple[str, ...] = ()):
    registry = ExecutorRegistry()
    registry.register("branch", BranchExecutor())
    registry.register("notify", Scripted({n: [Done("sent")] for n in ("x", "y", "z")}))
    runner = env.runner(executors=registry)
    runner.start_run(REPO, make_issue(labels=labels), playbook, agent="fake")
    run = await drive(runner, env, RUN_ID)
    br = next(s for s in env.steps(RUN_ID) if s.node_id == "br")
    return run, br, [s.node_id for s in env.steps(RUN_ID)]


CASES = [{"when": "issue.number == 7", "port": "p1"}, {"when": "issue.number > 1", "port": "p2"}]


async def test_first_matching_case_wins_even_when_later_cases_match(env):
    run, br, nodes = await _run(env, _playbook(CASES))
    assert run.status == "done" and br.outcome == "p1" and br.output == {"port": "p1"}
    assert sorted(nodes) == ["br", "x"]


async def test_a_later_case_is_taken_when_the_earlier_ones_do_not_match(env):
    cases = [{"when": "issue.number == 8", "port": "p1"}, {"when": "'bug' in issue.labels", "port": "p2"}]
    run, br, nodes = await _run(env, _playbook(cases), labels=("bug",))
    assert run.status == "done" and br.outcome == "p2" and "y" in nodes


async def test_default_port_is_taken_when_nothing_matches(env):
    cases = [{"when": "issue.number == 8", "port": "p1"}]
    run, br, nodes = await _run(env, _playbook(cases))
    assert run.status == "done" and br.outcome == "other" and "z" in nodes


async def test_no_match_and_no_default_fails_naming_the_node(env):
    cases = [{"when": "issue.number == 8", "port": "p1"}]
    run, br, _ = await _run(env, _playbook(cases, default=None))
    assert run.status == "failed"
    assert br.outcome == "failed" and "br" in br.output["error"]


async def test_the_sandbox_forbids_dunder_attribute_access(env):
    run, _, _ = await _run(env, _playbook([{"when": "issue.__class__ == 1", "port": "p1"}]))
    assert run.status == "failed"
    assert "forbidden operation" in env.events("executor_error")[0]["data"]["error"]


def _validate(yaml_text: str):
    return validate(load_playbook(yaml_text))


def test_validator_reports_an_expression_syntax_error_with_the_node_id():
    issues = _validate(
        """
apiVersion: harness/v1
id: t
nodes:
  pick_path: {kind: branch, cases: [{when: "issue.number ==", port: a}], default: b}
edges: {pick_path.a: "@done", pick_path.b: "@done"}
"""
    )
    assert [(i.code, i.path) for i in issues] == [("bad_expression", "nodes.pick_path.cases[0].when")]
    assert "pick_path" in issues[0].message


def test_validator_requires_every_branch_port_to_be_wired_including_failed_without_default():
    with_default = _validate(
        """
apiVersion: harness/v1
id: t
nodes:
  br: {kind: branch, cases: [{when: "true", port: a}, {when: "false", port: a}], default: b}
edges: {br.a: "@done"}
"""
    )
    assert [i.message for i in with_default] == ["Unrouted port br.b"]
    without_default = _validate(
        """
apiVersion: harness/v1
id: t
nodes:
  br: {kind: branch, cases: [{when: "true", port: a}]}
edges: {br.a: "@done"}
"""
    )
    assert [i.message for i in without_default] == ["Unrouted port br.failed"]


@pytest.mark.parametrize("expr", ["issue.number == 7", "{{ 'bug' in issue.labels }}", "prev.verdict == 'x'"])
def test_validator_accepts_valid_expressions(expr):
    issues = _validate(
        f"""
apiVersion: harness/v1
id: t
nodes:
  br: {{kind: branch, cases: [{{when: "{expr}", port: a}}], default: b}}
edges: {{br.a: "@done", br.b: "@done"}}
"""
    )
    assert issues == []
