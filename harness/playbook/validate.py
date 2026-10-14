from dataclasses import dataclass

from harness.playbook.models import Playbook


@dataclass
class PlaybookIssue:
    path: str
    code: str
    message: str


def validate(pb: Playbook, *, agent_caps: dict[str, set[str]] | None = None) -> list[PlaybookIssue]:
    issues: list[PlaybookIssue] = []

    if not pb.nodes:
        return issues

    # 1. Edge keys, ports, targets
    routes: dict[str, dict[str, str]] = {node_id: {} for node_id in pb.nodes}
    wildcards: dict[str, str] = {}

    for key, target in pb.edges.items():
        if key.startswith("*."):
            port = key[2:]
            wildcards[port] = target
        elif "." in key:
            node_id, port = key.split(".", 1)
            if node_id not in pb.nodes:
                issues.append(PlaybookIssue(f"edges.{key}", "unknown_node", f"Unknown node {node_id}"))
            else:
                routes[node_id][port] = target
        else:
            issues.append(PlaybookIssue(f"edges.{key}", "bad_edge_key", f"Malformed edge key {key}"))

        if target not in pb.nodes and target not in ("@done", "@failed"):
            issues.append(PlaybookIssue(f"edges.{key}", "bad_target", f"Bad target {target}"))

    # Check node ports
    for node_id, node in pb.nodes.items():
        ports = node.ports()
        if node.kind == "branch":
            for i, case in enumerate(node.cases):
                if case.port not in routes[node_id] and case.port not in wildcards:
                    issues.append(
                        PlaybookIssue(
                            f"nodes.{node_id}.cases[{i}]", "unrouted_port", f"Unrouted port {case.port}"
                        )
                    )
            if node.default not in routes[node_id] and node.default not in wildcards:
                issues.append(
                    PlaybookIssue(
                        f"nodes.{node_id}.default", "unrouted_port", f"Unrouted port {node.default}"
                    )
                )

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

            if agent_caps and not node.agent.startswith("{{") and not node.agent.endswith("}}"):
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
