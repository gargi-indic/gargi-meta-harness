import { describe, test, expect } from 'vitest';
import { layoutGraph } from './layout';

describe('layoutGraph', () => {
  test('returns positions for nodes', async () => {
    const nodes = [
      { id: "A", kind: "agent", label: "A" },
      { id: "B", kind: "agent", label: "B" },
    ];
    const edges = [
      { id: "A->B", source: "A", target: "B" },
    ];

    const pos1 = await layoutGraph(nodes, edges);
    expect(pos1.A).toBeDefined();
    expect(pos1.B).toBeDefined();

    // Deterministic
    const pos2 = await layoutGraph(nodes, edges);
    expect(pos1).toEqual(pos2);
  });
});
