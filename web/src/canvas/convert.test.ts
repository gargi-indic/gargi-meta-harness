import { describe, test, expect } from 'vitest';
import { playbookToGraph, issueGraphToCanvas } from './convert';
import { portsFor, PlaybookNode } from './ports';
import portsCases from '../generated/ports.cases.json';
import planBuildReview from './__fixtures__/plan-build-review.json';
import { Playbook } from '../generated/playbook';

describe('portsFor', () => {
  test('matches python ports.cases.json exactly', () => {
    for (const caseDef of portsCases) {
      const ports = portsFor(caseDef.node as PlaybookNode);
      expect(ports).toEqual(caseDef.ports);
    }
  });

  test('covers every kind in the schema', () => {
    // Collect kinds from portsCases
    const kindsInCases = new Set(portsCases.map((c: any) => c.node.kind));

    // We expect these kinds in the schema definition
    const expectedKinds = [
      "agent", "review", "check", "gate", "branch",
      "parallel", "join", "decompose", "merge", "escalate", "notify"
    ];

    for (const kind of expectedKinds) {
      expect(kindsInCases.has(kind)).toBe(true);
    }
  });
});

describe('convert', () => {
  test('default playbook -> graph', () => {
    const pb = planBuildReview as unknown as Playbook;
    const { nodes, edges, problems } = playbookToGraph(pb);

    expect(problems).toEqual([]);

    // 12 playbook nodes + 1 terminal (@done) = 13 nodes
    expect(nodes.length).toBe(13);

    // 19 explicit edges + 11 dashed = 30 edges
    expect(edges.length).toBe(30);

    const dashedEdges = edges.filter(e => e.dashed);
    expect(dashedEdges.length).toBe(11);

    const solidEdges = edges.filter(e => !e.dashed);
    expect(solidEdges.length).toBe(19);

    const planNode = nodes.find(n => n.id === "plan");
    expect(planNode?.ports).toEqual(["plan_ready", "pr_ready", "needs_input", "failed", "timeout"]);

    const planReviewNode = nodes.find(n => n.id === "plan_review");
    expect(planReviewNode?.ports).toContain("exhausted");

    // No dangling endpoints
    const nodeIds = new Set(nodes.map(n => n.id));
    for (const edge of edges) {
      expect(nodeIds.has(edge.source)).toBe(true);
      expect(nodeIds.has(edge.target)).toBe(true);
    }
  });

  test('playbook with explicit failed edge -> no wildcard for failed', () => {
    const pb = {
      nodes: {
        x: { kind: "agent", session: "main", action: "plan" },
        y: { kind: "check", check: "ci" }
      },
      edges: {
        "x.failed": "y",
        "*.failed": "y"
      }
    } as unknown as Playbook;

    const { edges } = playbookToGraph(pb);
    const wildEdge = edges.find(e => e.id === "wild:x->y");
    expect(wildEdge).toBeUndefined(); // Because it has an explicit x.failed
  });

  test('issueGraphToCanvas', () => {
    const issueGraph = {
      nodes: [
        { id: 12, title: "test", status: "ready" }
      ],
      edges: [
        { source: 12, target: 13 }
      ]
    };

    const { nodes, edges } = issueGraphToCanvas(issueGraph);
    expect(nodes[0].id).toBe("#12");
    expect(nodes[0].status).toBe("pending");
    expect(edges[0].id).toBe("#12->#13");
  });
});
