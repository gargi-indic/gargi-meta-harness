"""POST /api/playbooks/validate (M2.8)."""

import time

import pytest

from harness.playbook.models import Playbook
from harness.server.playbook_service import validate_yaml
from tests.server.playbooks_helpers import builtin_text, fixture_text, make_playbook_app, playbook_client


@pytest.fixture
async def client():
    async with playbook_client(make_playbook_app()) as c:
        yield c


async def validate(client, text: str, **extra):
    response = await client.post("/api/playbooks/validate", json={"yaml": text, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def issues_by_code(body: dict, code: str) -> list[dict]:
    return [i for i in body["issues"] if i["code"] == code]


async def test_the_builtin_playbook_is_valid(client) -> None:
    body = await validate(client, builtin_text())
    assert body == {"ok": True, "issues": []}


async def test_unrouted_port(client) -> None:
    body = await validate(client, fixture_text("unrouted_port.yaml"))
    assert body["ok"] is False
    [issue] = issues_by_code(body, "unrouted_port")
    assert issue["node"] == "implement"
    assert issue["severity"] == "error"
    assert "timeout" in issue["message"]


async def test_unreachable_node(client) -> None:
    body = await validate(client, fixture_text("unreachable.yaml"))
    [issue] = issues_by_code(body, "unreachable")
    assert issue["node"] == "orphan"
    assert body["ok"] is False


async def test_cycle_without_max_visits(client) -> None:
    body = await validate(client, fixture_text("unbounded_cycle.yaml"))
    assert {i["node"] for i in issues_by_code(body, "unbounded_cycle")} == {"ping", "pong"}


async def test_undefined_gate(client) -> None:
    body = await validate(client, fixture_text("unknown_gate.yaml"))
    [issue] = issues_by_code(body, "unknown_gate")
    assert issue["node"] == "approve"


async def test_plan_action_needs_the_plan_capability(client) -> None:
    text = fixture_text("plan_action.yaml")
    assert (await validate(client, text))["ok"] is True  # no agents given: the check is off
    body = await validate(client, text, agents={"codex": ["implement"]})
    [issue] = issues_by_code(body, "capability")
    assert issue["node"] == "plan"
    assert body["ok"] is False
    assert (await validate(client, text, agents={"codex": ["plan"]}))["ok"] is True


async def test_edge_issues_carry_the_node_and_the_port(client) -> None:
    body = await validate(client, fixture_text("bad_port_and_target.yaml"))
    [bad_port] = issues_by_code(body, "bad_port")
    assert (bad_port["node"], bad_port["port"], bad_port["path"]) == (
        "implement",
        "bogus",
        "edges.implement.bogus",
    )
    [bad_target] = issues_by_code(body, "bad_target")
    assert (bad_target["node"], bad_target["port"]) == ("implement", "pr_ready")


async def test_wildcard_edge_issue_has_no_node() -> None:
    text = fixture_text("unrouted_port.yaml") + '  "*.nonsense": "@done"\n'
    [issue] = [i for i in validate_yaml(text).issues if i.code == "bad_port"]
    assert (issue.node, issue.port) == (None, "nonsense")


async def test_yaml_syntax_error_is_a_200_with_the_position(client) -> None:
    body = await validate(client, "nodes: [unclosed\nedges: {a: b}\n")
    assert body["ok"] is False
    [issue] = body["issues"]
    assert issue["code"] == "yaml_syntax"
    assert issue["path"].startswith("line ") and ", column " in issue["path"]
    line = int(issue["path"].split()[1].rstrip(","))
    assert line >= 1


@pytest.mark.parametrize("text", ["[1, 2]", '"str"', "null", "", "42"])
async def test_non_mapping_yaml_does_not_500(client, text: str) -> None:
    body = await validate(client, text)
    assert body["ok"] is False
    assert [i["code"] for i in body["issues"]] == ["not_mapping"]


async def test_schema_errors_name_the_node_and_no_python_types(client) -> None:
    text = (
        "apiVersion: harness/v1\nid: bad\nnodes:\n  a: {kind: check, check: ci, bogus: 1}\n"
        "  b: {kind: check, check: nope}\nedges: {}\n"
    )
    body = await validate(client, text)
    issues = issues_by_code(body, "schema")
    assert {i["node"] for i in issues} == {"a", "b"}
    assert all(i["path"].startswith("nodes.") for i in issues)
    joined = " ".join(i["message"] for i in issues)
    assert "<class" not in joined and "NoneType" not in joined and "dict[" not in joined


async def test_schema_error_outside_the_nodes_has_no_node(client) -> None:
    body = await validate(client, "id: x\nnodes: {}\nedges: {}\n")
    [issue] = [i for i in body["issues"] if i["path"] == "apiVersion"]
    assert issue["node"] is None and issue["code"] == "schema"


async def test_alias_bomb_is_rejected_quickly(client) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for n in range(1, 12):
        lines.append(f"a{n}: &a{n} [{', '.join([f'*a{n - 1}'] * 9)}]")
    started = time.monotonic()
    body = await validate(client, "\n".join(lines))
    assert time.monotonic() - started < 1.0
    assert [i["code"] for i in body["issues"]] == ["yaml_aliases"]
    assert body["issues"][0]["message"] == "anchors and aliases are not supported"


async def test_an_anchor_alone_is_rejected(client) -> None:
    body = await validate(client, "a: &x 1\n")
    assert [i["code"] for i in body["issues"]] == ["yaml_aliases"]


async def test_a_scanner_error_is_a_syntax_issue_not_aliases(client) -> None:
    body = await validate(client, "a: &\n")
    assert body["issues"][0]["code"] in ("yaml_syntax", "yaml_aliases")


@pytest.mark.parametrize(
    "text",
    ["a: " + "9" * 5000, "a: 9999-99-99", "a: 2001-12-14t21:59:43.10+99:99", "a: 2001-13-14"],
    ids=["huge-int", "bad-date", "bad-tz", "bad-month"],
)
async def test_constructor_value_errors_are_syntax_issues(client, text: str) -> None:
    body = await validate(client, text)
    assert body["ok"] is False
    [issue] = body["issues"]
    assert issue["code"] == "yaml_syntax"
    assert issue["message"]


async def test_oversize_text_is_a_422(client) -> None:
    response = await client.post("/api/playbooks/validate", json={"yaml": "a" * 256_001})
    assert response.status_code == 422
    assert validate_yaml("a" * 256_001).issues[0].code == "too_large"


async def test_a_body_shape_error_is_a_422(client) -> None:
    assert (await client.post("/api/playbooks/validate", json={"nope": 1})).status_code == 422
    assert (await client.post("/api/playbooks/validate", json={"yaml": 5})).status_code == 422


@pytest.mark.parametrize(
    "text",
    ["[" * 250_000, "a:\n" + "".join(f"{' ' * n}b:\n" for n in range(1, 300))],
    ids=["flow", "block"],
)
async def test_deeply_nested_yaml_is_rejected_quickly(client, text: str) -> None:
    started = time.monotonic()
    body = await validate(client, text)
    assert time.monotonic() - started < 2.0
    assert [i["code"] for i in body["issues"]] == ["yaml_too_deep"]


async def test_internal_failures_become_an_issue_not_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("harness.server.playbook_service.validate", boom)
    result = validate_yaml(builtin_text())
    assert result.ok is False
    assert [i.code for i in result.issues] == ["internal"]
    assert "boom" not in result.issues[0].message


async def test_the_parsed_playbook_is_returned_by_the_service() -> None:
    result = validate_yaml(fixture_text("unrouted_port.yaml"))
    assert isinstance(result.playbook, Playbook) and result.playbook.id == "unrouted"
