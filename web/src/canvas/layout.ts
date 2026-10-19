import ELK from 'elkjs/lib/elk.bundled.js';
import { CanvasNode, CanvasEdge } from './types';

const elk = new ELK();

const NODE_SIZE = {
  default: { w: 200, h: 72 },
  terminal: { w: 96, h: 40 },
  issue: { w: 240, h: 72 },
};

const cache = new Map<string, Record<string, { x: number; y: number }>>();

export async function layoutGraph(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  opts?: { direction?: "RIGHT" | "DOWN" }
): Promise<Record<string, { x: number; y: number }>> {
  const direction = opts?.direction || "RIGHT";

  const sortedNodes = [...nodes].sort((a, b) => a.id.localeCompare(b.id));
  const sortedEdges = [...edges].sort((a, b) => a.id.localeCompare(b.id));

  const cacheKey = JSON.stringify({
    n: sortedNodes.map(n => `${n.id}:${n.kind}`),
    e: sortedEdges.map(e => `${e.id}:${e.source}->${e.target}`),
    d: direction
  });

  if (cache.has(cacheKey)) {
    // move to end (LRU behavior)
    const val = cache.get(cacheKey)!;
    cache.delete(cacheKey);
    cache.set(cacheKey, val);
    return val;
  }

  const elkNodes = sortedNodes.map(node => {
    let size = NODE_SIZE.default;
    if (node.kind === "terminal") size = NODE_SIZE.terminal;
    if (node.kind === "issue") size = NODE_SIZE.issue;

    return {
      id: node.id,
      width: size.w,
      height: size.h,
    };
  });

  const elkEdges = sortedEdges.map(edge => ({
    id: edge.id,
    sources: [edge.source],
    targets: [edge.target],
  }));

  const graph = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction,
      "elk.layered.spacing.nodeNodeBetweenLayers": "64",
      "elk.spacing.nodeNode": "32",
      "elk.edgeRouting": "ORTHOGONAL",
    },
    children: elkNodes,
    edges: elkEdges,
  };

  const layoutedGraph = await elk.layout(graph);

  const positions: Record<string, { x: number; y: number }> = {};
  for (const node of layoutedGraph.children || []) {
    positions[node.id] = { x: node.x || 0, y: node.y || 0 };
  }

  if (cache.size >= 32) {
    const firstKey = cache.keys().next().value;
    if (firstKey) cache.delete(firstKey);
  }
  cache.set(cacheKey, positions);

  return positions;
}
