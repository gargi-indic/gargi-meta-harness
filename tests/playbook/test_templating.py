import pytest

from harness.playbook.load import builtin_playbooks
from harness.playbook.models import AgentNode
from harness.playbook.templating import (
    Context,
    IssueCtx,
    PrCtx,
    RunCtx,
    TemplateError,
    evaluate,
    render,
    template_variables,
)


def make_ctx(**overrides) -> Context:
    data = {
        "issue": IssueCtx(
            number=12,
            title="Templating",
            body="Body text",
            labels=["area/engine", "agent:claude"],
            agent="claude",
            playbook="plan-build-review",
        ),
        "pr": PrCtx(number=7, url="https://example.test/pr/7", head_sha="abc123", additions=150, deletions=3),
        "inputs": {"flavor": "strict"},
        "nodes": {
            "plan_review": {"verdict": "changes", "correction": "Add tests"},
            "ci": {"status": "pass"},
        },
        "prev": {"correction": "Fix the thing", "verdict": "approve"},
        "run": RunCtx(id="run-1", visits={"plan": 2}),
    }
    data.update(overrides)
    return Context(**data)


def test_render_issue_fields():
    ctx = make_ctx()
    assert (
        render("#{{ issue.number }}: {{ issue.title }} ({{ issue.agent }})", ctx)
        == "#12: Templating (claude)"
    )
    assert render("{{ issue.labels | join(',') }}", ctx) == "area/engine,agent:claude"
    assert render("{{ run.id }} {{ run.visits.plan }} {{ inputs.flavor }}", ctx) == "run-1 2 strict"


def test_render_node_outputs_both_access_styles():
    ctx = make_ctx()
    assert render("{{ nodes.plan_review.correction }}", ctx) == "Add tests"
    assert render("{{ plan_review.correction }}", ctx) == "Add tests"
    assert render("{{ nodes['ci']['status'] }}", ctx) == "pass"


def test_render_prev_correction():
    assert render("{{ prev.correction }}", make_ctx()) == "Fix the thing"


def test_render_pr_none_handling():
    ctx = make_ctx(pr=None)
    assert render("{% if pr %}PR {{ pr.number }}{% else %}no pr{% endif %}", ctx) == "no pr"
    assert render("{% if pr %}PR {{ pr.number }}{% else %}no pr{% endif %}", make_ctx()) == "PR 7"
    with pytest.raises(TemplateError):
        render("{{ pr.number }}", ctx)


def test_prev_none():
    ctx = make_ctx(prev=None)
    assert render("{{ prev is none }}", ctx) == "True"
    with pytest.raises(TemplateError):
        render("{{ prev.correction }}", ctx)


def test_default_filter():
    ctx = make_ctx()
    assert render("{{ missing | default('fallback') }}", ctx) == "fallback"
    assert render("{{ issue.playbook | default('x') }}", ctx) == "plan-build-review"
    assert render("{{ plan_review.nope | default('none-here') }}", ctx) == "none-here"


def test_trailing_newline_kept():
    assert render("hi\n", make_ctx()) == "hi\n"


def test_undefined_raises_with_path():
    with pytest.raises(TemplateError) as ei:
        render("{{ nope }}", make_ctx(), path="nodes.x.message")
    assert ei.value.path == "nodes.x.message"
    assert "nope" in ei.value.message
    assert str(ei.value).startswith("nodes.x.message: ")


def test_undefined_attribute_raises():
    with pytest.raises(TemplateError):
        render("{{ issue.nope }}", make_ctx())
    with pytest.raises(TemplateError):
        render("{{ plan_review.nope }}", make_ctx())


def test_syntax_error_is_template_error():
    with pytest.raises(TemplateError) as ei:
        render("{{ issue.title ", make_ctx(), path="p")
    assert ei.value.path == "p"


def test_runtime_errors_are_wrapped():
    with pytest.raises(TemplateError):
        render("{{ 1 / 0 }}", make_ctx())


@pytest.mark.parametrize(
    "tpl",
    [
        "{{ ''.__class__ }}",
        "{{ ().__class__.__bases__ }}",
        "{{ cycler.__init__ }}",
        "{{ issue.__class__ }}",
        "{{ issue|attr('__class__') }}",
        "{{ lipsum.__globals__ }}",
        "{{ namespace }}",
        "{{ range.__call__.__globals__ }}",
    ],
)
def test_sandbox_escapes_raise_template_error(tpl):
    with pytest.raises(TemplateError):
        render(tpl, make_ctx(), path="p")


def test_sandbox_is_immutable():
    with pytest.raises(TemplateError):
        render("{% set _ = issue.labels.append('x') %}", make_ctx())
    with pytest.raises(TemplateError):
        render("{% set _ = inputs.update({'a': 1}) %}", make_ctx())


def test_globals_restricted():
    ctx = make_ctx()
    assert render("{{ range(3) | list }}", ctx) == "[0, 1, 2]"
    assert render("{{ dict(a=1).a }}", ctx) == "1"
    assert render("{{ true }} {{ false }} {{ none }}", ctx) == "True False None"
    for name in ("cycler", "joiner", "lipsum", "namespace"):
        with pytest.raises(TemplateError):
            render("{{ " + name + " }}", ctx)


def test_evaluate_true_false():
    ctx = make_ctx()
    assert evaluate("issue.number == 12", ctx) is True
    assert evaluate("issue.number == 13", ctx) is False


def test_evaluate_wrapped():
    ctx = make_ctx()
    assert evaluate("{{ issue.number == 12 }}", ctx) is True
    assert evaluate("  {{ issue.number == 13 }}  ", ctx) is False


def test_evaluate_comparison():
    expr = "pr.additions < 200 and prev.verdict == 'approve'"
    assert evaluate(expr, make_ctx()) is True
    assert evaluate(expr, make_ctx(prev={"verdict": "changes"})) is False
    assert evaluate(expr, make_ctx(pr=PrCtx(number=1, url="u", additions=500))) is False


def test_evaluate_non_bool_raises():
    ctx = make_ctx()
    for expr in ("issue.title", "issue.number", "1", "issue.labels", "none", "'yes'", "{{ issue.title }}"):
        with pytest.raises(TemplateError, match="condition must evaluate to a boolean") as ei:
            evaluate(expr, ctx, path="gates.plan")
        assert ei.value.path == "gates.plan"
    with pytest.raises(TemplateError, match="got str"):
        evaluate("issue.title", ctx)


def test_evaluate_undefined_raises():
    ctx = make_ctx()
    with pytest.raises(TemplateError) as ei:
        evaluate("nope", ctx, path="branch.when")
    assert ei.value.path == "branch.when"
    with pytest.raises(TemplateError):
        evaluate("nope == 1", ctx)
    with pytest.raises(TemplateError):
        evaluate("issue.nope", ctx)
    with pytest.raises(TemplateError):
        evaluate("pr.additions < 1", make_ctx(pr=None))


def test_evaluate_syntax_error_and_escape():
    ctx = make_ctx()
    with pytest.raises(TemplateError):
        evaluate("issue.number ==", ctx)
    with pytest.raises(TemplateError):
        evaluate("", ctx)
    with pytest.raises(TemplateError):
        evaluate("''.__class__ == 1", ctx)


def test_reserved_name_collision_only_via_nodes():
    ctx = make_ctx(
        nodes={
            "issue": {"marker": "node-issue"},
            "pr": {"marker": "node-pr"},
            "run": {"marker": "node-run"},
            "plan_review": {"correction": "c"},
        }
    )
    ns = ctx.namespace()
    assert ns["issue"]["number"] == 12
    assert ns["run"]["id"] == "run-1"
    assert ns["pr"]["number"] == 7
    assert render("{{ issue.number }}", ctx) == "12"
    assert render("{{ nodes.issue.marker }} {{ nodes.pr.marker }} {{ nodes.run.marker }}", ctx) == (
        "node-issue node-pr node-run"
    )
    with pytest.raises(TemplateError):
        render("{{ issue.marker }}", ctx)
    assert render("{{ plan_review.correction }}", ctx) == "c"


def test_namespace_shape():
    ns = make_ctx().namespace()
    assert {"issue", "pr", "inputs", "nodes", "prev", "run", "plan_review", "ci"} <= set(ns)
    assert type(ns["issue"]) is dict
    assert type(ns["pr"]) is dict
    assert type(ns["run"]) is dict
    assert make_ctx(pr=None, prev=None).namespace()["pr"] is None
    assert make_ctx(pr=None, prev=None).namespace()["prev"] is None


def test_context_defaults_are_independent():
    a = Context(issue=IssueCtx(number=1, title="t", agent="a"), run=RunCtx(id="r"))
    b = Context(issue=IssueCtx(number=2, title="t", agent="a"), run=RunCtx(id="r"))
    a.inputs["k"] = 1
    a.issue.labels.append("x")
    a.run.visits["n"] = 1
    assert b.inputs == {} and b.issue.labels == [] and b.run.visits == {}
    assert a.pr is None and a.prev is None and a.nodes == {}


def test_context_forbids_extra_fields():
    with pytest.raises(ValueError):
        IssueCtx(number=1, title="t", agent="a", bogus=1)
    with pytest.raises(ValueError):
        PrCtx(number=1, url="u", bogus=1)
    with pytest.raises(ValueError):
        RunCtx(id="r", bogus=1)


def test_template_variables():
    assert template_variables("{{ issue.title }} {{ prev.correction }}") == {"issue", "prev"}
    assert template_variables("{% if pr %}{{ plan_review.correction }}{% endif %}") == {"pr", "plan_review"}
    assert template_variables("no variables") == set()
    assert template_variables("{% for x in inputs.items %}{{ x }}{% endfor %}") == {"inputs"}
    with pytest.raises(TemplateError):
        template_variables("{{ oops", path="p")


def test_shipped_playbook_strings_render():
    pb = builtin_playbooks()["plan-build-review"]
    ctx = make_ctx()
    agent_nodes = [n for n in pb.nodes.values() if isinstance(n, AgentNode)]
    assert agent_nodes
    for node in agent_nodes:
        assert render(node.agent, ctx) == "claude"
    messages = [n.message for n in agent_nodes if n.message is not None]
    assert messages == ["{{ prev.correction }}", "{{ prev.correction }}"]
    for message in messages:
        assert render(message, ctx) == "Fix the thing"
    assert render("{{ issue.agent }}", ctx) == "claude"
