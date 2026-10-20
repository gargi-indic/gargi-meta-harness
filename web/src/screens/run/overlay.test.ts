import { describe, it, expect } from 'vitest';
import { computeOverlay } from './overlay';
import { StepOut } from '../../api/types';

describe('computeOverlay', () => {
  it('defaults unvisited nodes to pending', () => {
    const playbook: any = { nodes: { n1: { kind: 'agent' }, n2: { kind: 'review' } } };
    const overlay = computeOverlay([], playbook);
    expect(overlay['n1']).toEqual({ status: 'pending', visits: 0 });
    expect(overlay['n2']).toEqual({ status: 'pending', visits: 0 });
  });

  it('computes overlay correctly', () => {
    const playbook: any = {
      nodes: {
        loop_node: { kind: 'agent' },
        gate_node: { kind: 'gate' },
        exhausted_node: { kind: 'agent' },
      }
    };

    const steps: StepOut[] = [
      { id: '1', node: 'loop_node', visit: 1, status: 'done', outcome: 'failed', ref: null, started_at: null, finished_at: null },
      { id: '2', node: 'loop_node', visit: 2, status: 'done', outcome: 'failed', ref: null, started_at: null, finished_at: null },
      { id: '3', node: 'loop_node', visit: 3, status: 'running', outcome: null, ref: null, started_at: null, finished_at: null },
      { id: '4', node: 'gate_node', visit: 1, status: 'waiting', outcome: null, ref: null, started_at: null, finished_at: null },
      { id: '5', node: 'exhausted_node', visit: 2, status: 'done', outcome: 'exhausted', ref: null, started_at: null, finished_at: null },
    ];

    const overlay = computeOverlay(steps, playbook);

    expect(overlay['loop_node']).toEqual({ status: 'running', visits: 3, badge: 'x3' });
    expect(overlay['gate_node']).toEqual({ status: 'needs-you', visits: 1 });
    expect(overlay['exhausted_node']).toEqual({ status: 'done', visits: 2, badge: 'EXHAUSTED' });
  });
});
