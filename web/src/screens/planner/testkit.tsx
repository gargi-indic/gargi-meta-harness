// Shared helpers for the planner screen tests: a fetch layer with request headers and a controllable EventSource.
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';
import { PlannerPage } from './PlannerPage';
import { useInvalidateOnEvents } from '../../api/queries';
import type { JobOut, OptionsOut, PlanOut, ProposalOut } from '../../api/planner';

export interface Req {
  method: string;
  url: string;
  body: any;
  headers: Record<string, string>;
}

export type Handler = (req: Req) => { status: number; json?: any } | Promise<{ status: number; json?: any }>;

/** Stub `fetch`: routes are keyed `METHOD /path?query`; every request is recorded in `calls`. */
export function plannerFetch(routes: Record<string, Handler>) {
  const calls: Req[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req: Req = {
      method: init?.method ?? 'GET',
      url: input.toString(),
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
    };
    calls.push(req);
    const handler = routes[`${req.method} ${req.url}`];
    if (!handler) throw new Error(`Unmatched request: ${req.method} ${req.url}`);
    const { status, json } = await handler(req);
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: async () => json,
    } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock, count: (method: string, url: string) => calls.filter((c) => c.method === method && c.url === url).length };
}

export class TestEventSource {
  static last: TestEventSource | null = null;
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  constructor(public url: string) {
    TestEventSource.last = this;
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  removeEventListener(type: string, fn: (e: MessageEvent) => void) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((l) => l !== fn);
  }
  close() {}
  emit(type: string, data: unknown) {
    (this.listeners[type] ?? []).forEach((l) => l({ data: JSON.stringify(data), lastEventId: '1' } as MessageEvent));
  }
}

export function installEventSource() {
  TestEventSource.last = null;
  vi.stubGlobal('EventSource', TestEventSource);
}

function Invalidator() {
  useInvalidateOnEvents();
  return null;
}

export function renderPlanner(path = '/planner') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <Invalidator />
      <MemoryRouter initialEntries={[path]}>
        <PlannerPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---- fixtures --------------------------------------------------------------------------------------------

export const REPO = 'acme/widgets';
export const PID = 'acme~widgets~0123456789ab';

export function issue(key: string, blocked_by: string[] = [], over: Record<string, unknown> = {}) {
  return {
    key,
    title: `Do ${key}`,
    body: `Body of ${key}`,
    acceptance: [`${key} works`, `${key} is tested`],
    blocked_by,
    agent: 'jules',
    playbook: 'plan-build-review',
    paths: [],
    labels: [],
    ...over,
  };
}

export function proposalOut(over: Partial<ProposalOut> = {}): ProposalOut {
  return {
    id: PID,
    repo: REPO,
    status: 'ready',
    plan_sha: 'sha1111111111',
    plan: 'docs/PLAN.md',
    rev: 3,
    saved: true,
    save_error: null,
    proposal: {
      epic_title: 'The epic',
      epic_body: 'Epic body',
      issues: [issue('a'), issue('b', ['a']), issue('c', ['b']), issue('d')] as any,
      notes: '',
    },
    issues_validation: [],
    ...over,
  };
}

export function planOut(over: Partial<PlanOut> = {}): PlanOut {
  return {
    repo: REPO,
    path: 'docs/PLAN.md',
    issue: null,
    ref: null,
    sha: 'sha1111111111',
    title: 'The plan',
    markdown: '# The plan\n\nBuild widgets.\n\n## Steps\n\n- one\n',
    headings: ['The plan', 'Steps'],
    approval: { status: 'approved', via: 'cli', approved_by: 'acme', note: null },
    ...over,
  };
}

export const optionsOut: OptionsOut = {
  agents: [
    { name: 'claude', enabled: true, caps: ['plan'] },
    { name: 'codex', enabled: false, caps: [] },
    { name: 'jules', enabled: true, caps: ['plan', 'pr'] },
  ],
  playbooks: ['plan-build-review', 'tournament'],
};

export function jobOut(over: Partial<JobOut> = {}): JobOut {
  return {
    job_id: `${PID}.create`,
    kind: 'create',
    proposal_id: PID,
    status: 'running',
    result: null,
    error: null,
    issues: [],
    ...over,
  };
}

export const PLAN_URL = `/api/planner/plan?repo=${encodeURIComponent(REPO)}&path=${encodeURIComponent('docs/PLAN.md')}`;
export const OPTIONS_URL = `/api/planner/options?repo=${encodeURIComponent(REPO)}`;
export const PROPOSAL_URL = `/api/planner/proposals/${encodeURIComponent(PID)}`;
export const DECOMPOSE_JOB_URL = `/api/planner/jobs/${encodeURIComponent(`${PID}.decompose`)}`;
export const CREATE_JOB_URL = `/api/planner/jobs/${encodeURIComponent(`${PID}.create`)}`;
