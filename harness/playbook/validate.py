from dataclasses import dataclass

from harness.playbook.models import JoinNode, Playbook


@dataclass
class PlaybookIssue:
    path: str
    code: str
    message: str
    severity: str = "error"


def _join_sources(pb: Playbook, join_id: str, node: JoinNode) -> list[str]:
    """The nodes a join waits for: explicit ``inputs``, else every node with an edge into it."""
    if node.inputs:
        return list(dict.fromkeys(node.inputs))
    found: list[str] = []
    for key, target in pb.edges.items():
        source = key.rsplit(".", 1)[0]
        if target == join_id and source != "*" and source not in found:
            found.append(source)
    return found


def _reachable_joins(pb: Playbook, adj: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        curr = stack.pop()
        if curr in seen:
            continue
        seen.add(curr)
        stack.extend(adj.get(curr, []))
    return {n for n in seen if pb.nodes[n].kind == "join"}


def validate(pb: Playbook, *, agent_caps: dict[str, set[str]] | None = None) -> list[PlaybookIssue]:
    issues: list[PlaybookIssue] = []

    if not pb.nodes:
        return issues

    # 1. Edge keys, ports, targets
    routes: dict[str, dict[str, str]] = {node_id: {} for node_id in pb.nodes}
    wildcards: dict[str, str] = {}

    for key, target in pb.edges.items():
        parts = key.split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            issues.append(PlaybookIssue(f"edges.{key}", "bad_edge_key", f"Malformed edge key {key}"))
        else:
            node_id, port = parts
            if node_id == "*":
                wildcards[port] = target
            else:
                if node_id not in pb.nodes:
                    issues.append(PlaybookIssue(f"edges.{key}", "unknown_node", f"Unknown node {node_id}"))
                else:
                    routes[node_id][port] = target

        if target not in pb.nodes and target not in ("@done", "@failed"):
            issues.append(PlaybookIssue(f"edges.{key}", "bad_target", f"Bad target {target}"))

    # Check node ports
    for node_id, node in pb.nodes.items():
        ports = node.ports()
        for port in ports:
            if port not in routes.get(node_id, {}) and port not in wildcards:
                issues.append(
                    PlaybookIssue(f"nodes.{node_id}", "unrouted_port", f"Unrouted port {node_id}.{port}")
                )

        for port in routes.get(node_id, {}):
            if port not in ports:
                issues.append(
                    PlaybookIssue(
                        f"edges.{node_id}.{port}", "bad_port", f"Port {port} not in node {node_id} ports"
                    )
                )

        # Additional node-specific checks
        if node.kind == "gate" and node.gate not in pb.gates:
            issues.append(PlaybookIssue(f"nodes.{node_id}.gate", "unknown_gate", f"Unknown gate {node.gate}"))

        if node.kind == "agent":
            if node.action == "revise" and not node.message:
                issues.append(
                    PlaybookIssue(
                        f"nodes.{node_id}.message", "missing_message", "Revise actions require a message"
                    )
                )

            if agent_caps is not None and not node.agent.startswith("{{") and not node.agent.endswith("}}"):
                caps = agent_caps.get(node.agent, set())
                if node.action == "plan" and "plan" not in caps:
                    issues.append(
                        PlaybookIssue(
                            f"nodes.{node_id}.action",
                            "capability",
                            f"Agent {node.agent} missing capability 'plan'",
                        )
                    )
                if node.action == "revise" and "message" not in caps:
                    issues.append(
                        PlaybookIssue(
                            f"nodes.{node_id}.action",
                            "capability",
                            f"Agent {node.agent} missing capability 'message'",
                        )
                    )

        if node.kind == "parallel":
            for branch in node.branches:
                if branch not in pb.nodes:
                    issues.append(
                        PlaybookIssue(f"nodes.{node_id}.branches", "unknown_branch", f"Unknown node {branch}")
                    )

        if node.kind == "join" and node.inputs is not None:
            for inp in node.inputs:
                if inp not in pb.nodes:
                    issues.append(
                        PlaybookIssue(f"nodes.{node_id}.inputs", "unknown_branch", f"Unknown node {inp}")
                    )

        if node.kind == "join":
            sources = _join_sources(pb, node_id, node)
            if all(src in pb.nodes for src in sources) and len(sources) < 2:
                issues.append(
                    PlaybookIssue(
                        f"nodes.{node_id}",
                        "join_min_inputs",
                        f"Join {node_id} needs at least 2 incoming edges or inputs, found {len(sources)}",
                    )
                )

        if node.kind == "parallel" and all(b in pb.nodes for b in node.branches):
            if len(node.branches) < 2:
                issues.append(
                    PlaybookIssue(
                        f"nodes.{node_id}.branches",
                        "parallel_min_targets",
                        f"Parallel {node_id} needs at least 2 branches, found {len(node.branches)}",
                    )
                )

    # Wildcard checks against all known ports
    all_ports = set()
    for node in pb.nodes.values():
        all_ports.update(node.ports())

    for port in wildcards:
        if port not in all_ports:
            issues.append(
                PlaybookIssue(
                    f"edges.*.{port}", "bad_port", f"Wildcard port {port} doesn't match any node port"
                )
            )

    # Reachability and cycles
    adj: dict[str, list[str]] = {node_id: [] for node_id in pb.nodes}
    for node_id, node in pb.nodes.items():
        if node.kind == "parallel":
            for branch in node.branches:
                if branch in pb.nodes:
                    adj[node_id].append(branch)

        for port in node.ports():
            target = routes.get(node_id, {}).get(port, wildcards.get(port))
            if target and target in pb.nodes:
                adj[node_id].append(target)

    # Every branch of a parallel must reach one common join.
    for node_id, node in pb.nodes.items():
        if node.kind != "parallel" or len(node.branches) < 2 or any(b not in pb.nodes for b in node.branches):
            continue
        per_branch = [_reachable_joins(pb, adj, b) for b in node.branches]
        if not set.intersection(*per_branch):
            issues.append(
                PlaybookIssue(
                    f"nodes.{node_id}.branches",
                    "branch_not_joined",
                    f"The branches of parallel {node_id} do not all reach the same join",
                )
            )

    # Reachability from entry (first node)
    entry_node = next(iter(pb.nodes.keys()))
    visited = set()
    stack = [entry_node]
    while stack:
        curr = stack.pop()
        if curr not in visited:
            visited.add(curr)
            stack.extend(adj.get(curr, []))

    for node_id in pb.nodes:
        if node_id not in visited:
            issues.append(PlaybookIssue(f"nodes.{node_id}", "unreachable", f"Node {node_id} is unreachable"))

    # Cycles (Tarjan's algorithm for SCC)
    iter_index = 0
    iter_indices: dict[str, int] = {}
    iter_lowlinks: dict[str, int] = {}
    iter_on_stack: set[str] = set()
    iter_stack: list[str] = []

    def iterative_tarjan():
        nonlocal iter_index
        for v in pb.nodes:
            if v not in iter_indices:
                call_stack = [(v, 0)]
                while call_stack:
                    curr, neighbor_idx = call_stack.pop()
                    if neighbor_idx == 0:
                        iter_indices[curr] = iter_index
                        iter_lowlinks[curr] = iter_index
                        iter_index += 1
                        iter_stack.append(curr)
                        iter_on_stack.add(curr)

                    neighbors = adj.get(curr, [])
                    if neighbor_idx < len(neighbors):
                        w = neighbors[neighbor_idx]
                        call_stack.append((curr, neighbor_idx + 1))
                        if w not in iter_indices:
                            call_stack.append((w, 0))
                        elif w in iter_on_stack:
                            iter_lowlinks[curr] = min(iter_lowlinks[curr], iter_indices[w])
                    else:
                        if iter_lowlinks[curr] == iter_indices[curr]:
                            scc = []
                            while True:
                                w = iter_stack.pop()
                                iter_on_stack.remove(w)
                                scc.append(w)
                                if w == curr:
                                    break
                            if len(scc) > 1 or (len(scc) == 1 and scc[0] in adj.get(scc[0], [])):
                                if any(pb.nodes[n].kind == "parallel" for n in scc) and all(
                                    pb.nodes[n].max_visits is None for n in scc
                                ):
                                    issues.append(
                                        PlaybookIssue(
                                            f"nodes.{scc[0]}",
                                            "parallel_cycle_without_max_visits",
                                            "A cycle containing a parallel node needs max_visits on a node of the cycle: "
                                            + ", ".join(sorted(scc)),
                                        )
                                    )
                                for node_id in scc:
                                    if pb.nodes[node_id].max_visits is None:
                                        issues.append(
                                            PlaybookIssue(
                                                f"nodes.{node_id}.max_visits",
                                                "unbounded_cycle",
                                                f"Node {node_id} is in a cycle but lacks max_visits",
                                            )
                                        )

                        if call_stack:
                            parent, _ = call_stack[-1]
                            iter_lowlinks[parent] = min(iter_lowlinks[parent], iter_lowlinks[curr])

    iterative_tarjan()

    return issues
