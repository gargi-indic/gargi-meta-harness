"""The decomposer's output: a proposed epic with sub-issues, and the checks a proposal must pass.

A ``ProposedPlan`` is a proposal only. Nothing here talks to GitHub; creating issues is a later step that
consumes a plan that ``validate_proposal`` accepted.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from harness.adapters.base import Capabilities
from harness.playbook.models import AgentNode, Playbook
from harness.playbook.validate import validate

KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,40}$"
MAX_TITLE_CHARS = 100
DEFAULT_MAX_ISSUES = 40

_ISSUE_AGENT = re.compile(r"^\{\{\s*issue\.agent\s*\}\}$")


class _Model(BaseModel):
    # Unknown fields are rejected: a misspelt ``blocked_by`` must not silently become "no dependencies".
    model_config = ConfigDict(extra="forbid")


class ProposedIssue(_Model):
    key: Annotated[str, Field(pattern=KEY_PATTERN)]  # stable local id, unique within the plan
    title: str  # at most 100 characters, imperative (checked by validate_proposal)
    body: str  # markdown: Context / Scope / Acceptance criteria / Out of scope
    acceptance: list[str]  # at least one, each verifiable (checked by validate_proposal)
    blocked_by: list[str]  # keys of other proposed issues
    agent: str  # one of the enabled agents
    playbook: str  # id of an existing playbook
    paths: list[str] = []  # optional path hints
    labels: list[str] = []


class ProposedPlan(_Model):
    epic_title: str
    epic_body: str
    issues: Annotated[list[ProposedIssue], Field(min_length=1)]
    notes: str = ""


def proposal_json_schema() -> dict[str, Any]:
    return ProposedPlan.model_json_schema()


@dataclass(frozen=True)
class ProposalIssue:
    """One reason a proposal is unusable. ``key`` is the proposed issue concerned, None for plan-wide problems."""

    code: str
    key: str | None
    message: str


def _find_cycles(keys: list[str], deps: dict[str, list[str]]) -> list[list[str]]:
    """Dependency cycles as key paths ``[a, b, c, a]`` (a is blocked by b ...), found without recursion.

    Every back edge of the depth-first search closes one cycle; each distinct set of keys is reported once.
    Self-dependencies are left to the ``self_dep`` check.
    """
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(keys, white)
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    for root in keys:
        if colour[root] != white:
            continue
        colour[root] = grey
        path = [root]
        stack = [(root, iter(deps.get(root, [])))]
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child == node or child not in colour:
                    continue
                if colour[child] == white:
                    colour[child] = grey
                    path.append(child)
                    stack.append((child, iter(deps.get(child, []))))
                    advanced = True
                    break
                if colour[child] == grey:
                    cycle = [*path[path.index(child) :], child]
                    members = frozenset(cycle)
                    if members not in seen:
                        seen.add(members)
                        cycles.append(cycle)
            if not advanced:
                colour[node] = black
                path.pop()
                stack.pop()
    return cycles


def _plan_capability_problem(playbook: Playbook, agent: str, agent_caps: dict[str, set[str]]) -> bool:
    """Whether ``playbook`` plans on ``agent`` although the agent cannot plan.

    The playbook validator only checks nodes whose agent is a literal name, and the stock playbooks use
    ``{{ issue.agent }}``. So the proposed agent is substituted into those nodes and the validator's own
    capability check does the work; only findings on substituted ``plan`` nodes are reported.
    """
    substituted: set[str] = set()
    nodes = {}
    for node_id, node in playbook.nodes.items():
        if isinstance(node, AgentNode) and _ISSUE_AGENT.match(node.agent):
            substituted.add(node_id)
            node = node.model_copy(update={"agent": agent})
        nodes[node_id] = node
    if not substituted:
        return False
    pinned = playbook.model_copy(update={"nodes": nodes})
    for found in validate(pinned, agent_caps=agent_caps):
        if found.code != "capability" or not found.path.startswith("nodes."):
            continue
        node_id = found.path.split(".")[1]
        node = nodes.get(node_id)
        if node_id in substituted and isinstance(node, AgentNode) and node.action == "plan":
            return True
    return False


def validate_proposal(
    plan: ProposedPlan,
    *,
    agents: Mapping[str, Capabilities],
    playbooks: Mapping[str, Playbook] | Collection[str],
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> list[ProposalIssue]:
    """Every problem with ``plan``, in a stable order; ``[]`` when it is valid.

    ``playbooks`` is the set of playbook ids that exist. When the values are ``Playbook`` objects, each
    ``(playbook, agent)`` pair is also checked for a ``plan`` action on an agent that cannot plan.
    """
    found: list[ProposalIssue] = []
    issues = plan.issues

    if len(issues) > max_issues:
        found.append(
            ProposalIssue(
                "too_many_issues", None, f"the plan has {len(issues)} issues, the limit is {max_issues}"
            )
        )

    first_seen: dict[str, int] = {}
    reported_duplicates: set[str] = set()
    for index, issue in enumerate(issues):
        if issue.key in first_seen:
            if issue.key not in reported_duplicates:
                reported_duplicates.add(issue.key)
                found.append(ProposalIssue("duplicate_key", issue.key, f"key {issue.key!r} is used twice"))
        else:
            first_seen[issue.key] = index
    keys = list(first_seen)

    playbook_objects: Mapping[str, Playbook] = {}
    if isinstance(playbooks, Mapping):
        playbook_objects = {k: v for k, v in playbooks.items() if isinstance(v, Playbook)}
    agent_caps = {name: caps.names() for name, caps in agents.items()}

    deps: dict[str, list[str]] = {}
    for issue in issues:
        key = issue.key
        if len(issue.title) > MAX_TITLE_CHARS:
            found.append(
                ProposalIssue(
                    "title_too_long",
                    key,
                    f"title has {len(issue.title)} characters, the limit is {MAX_TITLE_CHARS}",
                )
            )
        if not issue.body.strip():
            found.append(ProposalIssue("empty_body", key, "the body is empty"))
        if not any(item.strip() for item in issue.acceptance):
            found.append(ProposalIssue("no_acceptance", key, "there is no acceptance criterion"))

        for dep in dict.fromkeys(issue.blocked_by):
            if dep == key:
                found.append(ProposalIssue("self_dep", key, f"{key!r} is blocked by itself"))
            elif dep not in first_seen:
                found.append(ProposalIssue("unknown_dep", key, f"blocked_by names unknown key {dep!r}"))
            else:
                deps.setdefault(key, []).append(dep)

        agent_known = issue.agent in agents
        if not agent_known:
            known = ", ".join(sorted(agents)) or "(none)"
            found.append(
                ProposalIssue("unknown_agent", key, f"agent {issue.agent!r} is not enabled; known: {known}")
            )
        playbook_known = issue.playbook in playbooks
        if not playbook_known:
            found.append(
                ProposalIssue("unknown_playbook", key, f"playbook {issue.playbook!r} does not exist")
            )
        if agent_known and playbook_known and issue.playbook in playbook_objects:
            if _plan_capability_problem(playbook_objects[issue.playbook], issue.agent, agent_caps):
                found.append(
                    ProposalIssue(
                        "plan_on_agent_without_plan",
                        key,
                        f"playbook {issue.playbook!r} has a plan action but agent {issue.agent!r} "
                        "cannot plan with approval",
                    )
                )

    for cycle in _find_cycles(keys, deps):
        found.append(ProposalIssue("cycle", cycle[0], "dependency cycle: " + " -> ".join(cycle)))
    return found
