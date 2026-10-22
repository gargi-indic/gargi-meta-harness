import { Playbook } from '../generated/playbook';
import { CanvasNode, CanvasEdge, NodeStatus } from './types';
import { portsFor, PlaybookNode } from './ports';

export function playbookToGraph(pb: Playbook): { nodes: CanvasNode[]; edges: CanvasEdge[]; problems: string[] } {
  const nodes: CanvasNode[] = [];
  const edges: CanvasEdge[] = [];
  const problems: string[] = [];

  const terminalTargets = new Set<string>();
  const allNodeIds = new Set<string>(Object.keys(pb.nodes));

  for (const [id, node] of Object.entries(pb.nodes)) {
    const label = node.title ?? id;
    const ports = portsFor(node as PlaybookNode);

    nodes.push({
      id,
      kind: node.kind || "agent", // fallback for types without kind
      label,
      ports,
      data: node
    });
  }

  // Edges parsing
  const explicitEdges = new Map<string, string>(); // source.port -> target

  for (const [key, target] of Object.entries(pb.edges)) {
    if (!key.startsWith("*.")) {
      explicitEdges.set(key, target as string);
    }
  }

  // Build explicit edges
  for (const [key, target] of Object.entries(pb.edges)) {
    const targetStr = target as string;

    if (!allNodeIds.has(targetStr) && !targetStr.startsWith("@")) {
      problems.push(`Unknown target: ${targetStr}`);
    }

    if (targetStr.startsWith("@")) {
      terminalTargets.add(targetStr);
    }

    if (key.startsWith("*.")) {
      continue; // Handled later
    }

    const [source, sourcePort] = key.split(".");
    if (!allNodeIds.has(source)) {
      problems.push(`Unknown source: ${source}`);
    }

    edges.push({
      id: key,
      source,
      target: targetStr,
      sourcePort,
      label: sourcePort,
    });
  }

  // Handle wildcards
  // source -> target -> Set of ports
  const wildcards = new Map<string, Map<string, Set<string>>>();

  for (const [key, target] of Object.entries(pb.edges)) {
    if (!key.startsWith("*.")) continue;

    const targetStr = target as string;
    const portName = key.slice(2); // remove "*. "

    for (const node of nodes) {
      if (node.id === targetStr) continue; // no self wildcard edges

      const hasPort = node.ports?.includes(portName);
      if (!hasPort) continue;

      const hasExplicit = explicitEdges.has(`${node.id}.${portName}`);
      if (hasExplicit) continue;

      if (!wildcards.has(node.id)) {
        wildcards.set(node.id, new Map());
      }
      if (!wildcards.get(node.id)!.has(targetStr)) {
        wildcards.get(node.id)!.set(targetStr, new Set());
      }

      wildcards.get(node.id)!.get(targetStr)!.add(`*.${portName}`);
    }
  }

  for (const [source, targetMap] of wildcards.entries()) {
    for (const [target, portSet] of targetMap.entries()) {
      const sortedPorts = Array.from(portSet).sort();
      edges.push({
        id: `wild:${source}->${target}`,
        source,
        target,
        sourcePort: undefined,
        label: sortedPorts.join(", "),
        dashed: true
      });
    }
  }

  // Append terminal nodes
  const sortedTerminals = Array.from(terminalTargets).sort();
  for (const t of sortedTerminals) {
    const label = t.slice(1); // remove @
    nodes.push({
      id: t,
      kind: "terminal",
      label,
      ports: [],
    });
  }

  return { nodes, edges, problems };
}

type IssueGraphResponse = {
  nodes: {
    id: number;
    title: string;
    status: string;
    agent?: string | null;
    playbook?: string | null;
    run_id?: string | null;
    url?: string;
  }[];
  edges: {
    source: number;
    target: number;
  }[];
};

export function issueGraphToCanvas(g: IssueGraphResponse): { nodes: CanvasNode[]; edges: CanvasEdge[] } {
  const nodes: CanvasNode[] = g.nodes.map(n => {
    let status: NodeStatus = "pending";
    if (n.status === "ready") status = "pending";
    if (n.status === "running") status = "running";
    if (n.status === "needs-you") status = "needs-you";
    if (n.status === "review") status = "waiting";
    if (n.status === "blocked") status = "skipped";
    if (n.status === "done") status = "done";
    if (n.status === "failed") status = "failed";

    return {
      id: `#${n.id}`,
      kind: "issue",
      label: `#${n.id} ${n.title}`,
      status,
      ports: [],
      data: n,
    };
  });

  const edges: CanvasEdge[] = g.edges.map(e => ({
    id: `#${e.source}->#${e.target}`,
    source: `#${e.source}`,
    target: `#${e.target}`,
  }));

  return { nodes, edges };
}

/**
 * Apply canvas edits (added/removed nodes, rewired edges) onto the parsed playbook object.
 * Unknown fields, top-level metadata and wildcard (`*.port`) edges of `current` are preserved.
 */
export function graphToPlaybook(current: any, nodes: CanvasNode[], edges: CanvasEdge[]): any {
  const currentNodes: Record<string, any> = current?.nodes ?? {};
  const currentEdges: Record<string, string> = current?.edges ?? {};

  const nextNodes: Record<string, any> = {};
  for (const n of nodes) {
    if (n.kind === 'terminal' || n.id.startsWith('@')) continue;
    nextNodes[n.id] = n.data !== undefined ? n.data : (currentNodes[n.id] ?? { kind: n.kind });
  }

  const explicit: Record<string, string> = {};
  for (const e of edges) {
    if (e.dashed || e.id.startsWith('wild:')) continue; // derived from `*.port` edges
    if (!(e.source in nextNodes)) continue;
    const key = e.sourcePort ? `${e.source}.${e.sourcePort}` : e.id;
    explicit[key] = e.target;
  }

  // Keep original key order where possible, then append new edges.
  const nextEdges: Record<string, string> = {};
  for (const [key, target] of Object.entries(currentEdges)) {
    if (key.startsWith('*.')) {
      nextEdges[key] = target;
    } else if (key in explicit) {
      nextEdges[key] = explicit[key];
    }
  }
  for (const [key, target] of Object.entries(explicit)) {
    if (!(key in nextEdges)) nextEdges[key] = target;
  }

  return { ...current, nodes: nextNodes, edges: nextEdges };
}
