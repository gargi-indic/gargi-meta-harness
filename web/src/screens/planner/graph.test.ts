import { describe, it, expect } from 'vitest';
import type { ProposedIssue } from '../../api/planner';
import {
  addBlockedBy,
  edgeId,
  edgesOf,
  nextKey,
  parseEdgeId,
  removeBlockedBy,
  removeIssue,
  wouldCreateCycle,
} from './graph';

function issue(key: string, ...blocked_by: string[]): ProposedIssue {
  return {
    key,
    title: `Do ${key}`,
    body: 'body',
    acceptance: ['works'],
    blocked_by,
    agent: 'jules',
    playbook: 'plan-build-review',
  };
}

// c is blocked by b, b by a: a -> b -> c
const chain = [issue('a'), issue('b', 'a'), issue('c', 'b'), issue('d')];

describe('wouldCreateCycle', () => {
  it('detects a direct reverse edge', () => {
    expect(wouldCreateCycle(chain, 'b', 'a')).toBe(true);
  });
  it('detects a transitive cycle', () => {
    expect(wouldCreateCycle(chain, 'c', 'a')).toBe(true);
  });
  it('accepts a forward shortcut and an unrelated edge', () => {
    expect(wouldCreateCycle(chain, 'a', 'c')).toBe(false);
    expect(wouldCreateCycle(chain, 'd', 'a')).toBe(false);
  });
  it('treats a self edge as a cycle', () => {
    expect(wouldCreateCycle(chain, 'a', 'a')).toBe(true);
  });
  it('terminates on an input that already has a cycle', () => {
    const cyclic = [issue('a', 'b'), issue('b', 'a'), issue('c')];
    expect(wouldCreateCycle(cyclic, 'c', 'a')).toBe(false);
  });
});

describe('addBlockedBy', () => {
  it('adds the blocker to blocked_by of the blocked issue only', () => {
    const res = addBlockedBy(chain, 'd', 'c');
    expect(res.ok).toBe(true);
    if (!res.ok) return;
    expect(res.issues.find((i) => i.key === 'c')?.blocked_by).toEqual(['b', 'd']);
    expect(res.issues.find((i) => i.key === 'd')?.blocked_by).toEqual([]);
    expect(chain[2].blocked_by).toEqual(['b']); // the input is not mutated
  });
  it('rejects a self edge', () => {
    const res = addBlockedBy(chain, 'a', 'a');
    expect(res).toMatchObject({ ok: false, reason: 'self' });
  });
  it('rejects a duplicate edge', () => {
    const res = addBlockedBy(chain, 'a', 'b');
    expect(res).toMatchObject({ ok: false, reason: 'duplicate' });
  });
  it('rejects an edge that closes a cycle, with a message naming both issues', () => {
    const res = addBlockedBy(chain, 'c', 'a');
    expect(res).toMatchObject({ ok: false, reason: 'cycle' });
    if (!res.ok) expect(res.message).toContain('cycle');
  });
  it('rejects an unknown key', () => {
    expect(addBlockedBy(chain, 'zzz', 'a')).toMatchObject({ ok: false, reason: 'unknown' });
  });
});

describe('removeBlockedBy / removeIssue', () => {
  it('removes one dependency', () => {
    const next = removeBlockedBy(chain, 'a', 'b');
    expect(next.find((i) => i.key === 'b')?.blocked_by).toEqual([]);
    expect(next.find((i) => i.key === 'c')?.blocked_by).toEqual(['b']);
  });
  it('removes an issue and the dependencies on it', () => {
    const next = removeIssue(chain, 'b');
    expect(next.map((i) => i.key)).toEqual(['a', 'c', 'd']);
    expect(next.find((i) => i.key === 'c')?.blocked_by).toEqual([]);
  });
});

describe('edges', () => {
  it('maps blocked_by to blocker -> blocked edges and skips unknown blockers', () => {
    const edges = edgesOf([issue('a'), issue('b', 'a', 'ghost')]);
    expect(edges).toEqual([{ id: 'a>b', source: 'a', target: 'b' }]);
  });
  it('round-trips edge ids', () => {
    expect(parseEdgeId(edgeId('first-', 'second'))).toEqual({ blocker: 'first-', blocked: 'second' });
    expect(parseEdgeId('nonsense')).toBeNull();
  });
  it('generates keys that are not taken', () => {
    expect(nextKey([issue('issue-2'), issue('x')])).toBe('issue-3');
    expect(nextKey([issue('issue-1')])).toBe('issue-2');
  });
});
