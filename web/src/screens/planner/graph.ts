import type { CanvasEdge } from '../../canvas/types';
import type { ProposedIssue } from '../../api/planner';

/** Separator of an edge id. It cannot occur in a key (keys are lowercase letters, digits and dashes). */
const EDGE_SEP = '>';

export type EdgeResult =
  | { ok: true; issues: ProposedIssue[] }
  | { ok: false; reason: 'self' | 'duplicate' | 'cycle' | 'unknown'; message: string };

export function edgeId(blocker: string, blocked: string): string {
  return `${blocker}${EDGE_SEP}${blocked}`;
}

export function parseEdgeId(id: string): { blocker: string; blocked: string } | null {
  const at = id.indexOf(EDGE_SEP);
  if (at < 1 || at === id.length - 1) return null;
  return { blocker: id.slice(0, at), blocked: id.slice(at + 1) };
}

/** Canvas edges (blocker -> blocked) of the issues; a blocker that names no issue is left out. */
export function edgesOf(issues: ProposedIssue[]): CanvasEdge[] {
  const keys = new Set(issues.map((i) => i.key));
  const edges: CanvasEdge[] = [];
  for (const issue of issues) {
    for (const blocker of new Set(issue.blocked_by)) {
      if (!keys.has(blocker)) continue;
      edges.push({ id: edgeId(blocker, issue.key), source: blocker, target: issue.key });
    }
  }
  return edges;
}

/** Whether `from` can reach `to` by following blocked_by links from `from` (i.e. `to` is among its ancestors). */
function reaches(issues: ProposedIssue[], from: string, to: string): boolean {
  const deps = new Map(issues.map((i) => [i.key, i.blocked_by]));
  const seen = new Set<string>();
  const stack = [from];
  while (stack.length > 0) {
    const key = stack.pop() as string;
    if (key === to) return true;
    if (seen.has(key)) continue;
    seen.add(key);
    for (const dep of deps.get(key) ?? []) stack.push(dep);
  }
  return false;
}

/** Would making `blocker` block `blocked` close a cycle? True when `blocked` already (transitively) blocks `blocker`. */
export function wouldCreateCycle(issues: ProposedIssue[], blocker: string, blocked: string): boolean {
  if (blocker === blocked) return true;
  return reaches(issues, blocker, blocked);
}

/** Add `blocker` to `blocked.blocked_by`; refused for a self edge, a duplicate, an unknown key or a cycle. */
export function addBlockedBy(issues: ProposedIssue[], blocker: string, blocked: string): EdgeResult {
  const keys = new Set(issues.map((i) => i.key));
  if (!keys.has(blocker) || !keys.has(blocked)) {
    return { ok: false, reason: 'unknown', message: 'Both ends of a dependency must be issues of the plan.' };
  }
  if (blocker === blocked) {
    return { ok: false, reason: 'self', message: `${blocked} cannot be blocked by itself.` };
  }
  const target = issues.find((i) => i.key === blocked) as ProposedIssue;
  if (target.blocked_by.includes(blocker)) {
    return { ok: false, reason: 'duplicate', message: `${blocked} is already blocked by ${blocker}.` };
  }
  if (wouldCreateCycle(issues, blocker, blocked)) {
    return {
      ok: false,
      reason: 'cycle',
      message: `Blocking ${blocked} by ${blocker} would close a cycle: ${blocker} already depends on ${blocked}.`,
    };
  }
  return {
    ok: true,
    issues: issues.map((i) => (i.key === blocked ? { ...i, blocked_by: [...i.blocked_by, blocker] } : i)),
  };
}

/** Remove `blocker` from `blocked.blocked_by`. */
export function removeBlockedBy(issues: ProposedIssue[], blocker: string, blocked: string): ProposedIssue[] {
  return issues.map((i) =>
    i.key === blocked ? { ...i, blocked_by: i.blocked_by.filter((b) => b !== blocker) } : i,
  );
}

/** Remove an issue and every dependency on it. */
export function removeIssue(issues: ProposedIssue[], key: string): ProposedIssue[] {
  return issues
    .filter((i) => i.key !== key)
    .map((i) => (i.blocked_by.includes(key) ? { ...i, blocked_by: i.blocked_by.filter((b) => b !== key) } : i));
}

/** A key not used yet: issue-1, issue-2, ... */
export function nextKey(issues: ProposedIssue[]): string {
  const used = new Set(issues.map((i) => i.key));
  let n = issues.length + 1;
  while (used.has(`issue-${n}`)) n += 1;
  return `issue-${n}`;
}
