import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CampaignsPage } from './CampaignsPage';
import { mockApi } from '../../test/fetchMock';

// Stub observers as required by #27 FlowCanvas
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
global.DOMMatrixReadOnly = class DOMMatrixReadOnly {
  a = 1; b = 0; c = 0; d = 1; e = 0; f = 0;
  is2D = true;
  isIdentity = true;
  constructor() {}
  multiply() { return this; }
  inverse() { return this; }
  scale() { return this; }
  translate() { return this; }
  flipX() { return this; }
  flipY() { return this; }
  rotate() { return this; }
  rotateFromVector() { return this; }
  rotateAxisAngle() { return this; }
  skewX() { return this; }
  skewY() { return this; }
  multiplySelf() { return this; }
  preMultiplySelf() { return this; }
  translateSelf() { return this; }
  scaleSelf() { return this; }
  scale3dSelf() { return this; }
  rotateSelf() { return this; }
  rotateFromVectorSelf() { return this; }
  rotateAxisAngleSelf() { return this; }
  skewXSelf() { return this; }
  skewYSelf() { return this; }
  invertSelf() { return this; }
  setMatrixValue() { return this; }
  toFloat32Array() { return new Float32Array(); }
  toFloat64Array() { return new Float64Array(); }
  transformPoint() { return { x: 0, y: 0, z: 0, w: 1 }; }
} as any;

const mockGraph = {
  nodes: [
    { id: 2, title: "Issue 2", status: "done", agent: "jules", playbook: "playbook-1", run_id: "run-2", url: "http://github.com/a/b/issues/2" },
    { id: 3, title: "Issue 3", status: "ready", agent: "jules", playbook: "playbook-1", run_id: null, url: "http://github.com/a/b/issues/3" },
    { id: 4, title: "Issue 4", status: "blocked", agent: "jules", playbook: "playbook-1", run_id: null, url: "http://github.com/a/b/issues/4" },
    { id: 5, title: "Issue 5", status: "running", agent: "jules", playbook: "playbook-1", run_id: "run-5", url: "http://github.com/a/b/issues/5" },
    { id: 6, title: "Issue 6", status: "needs-you", agent: "jules", playbook: "playbook-1", run_id: "run-6", url: "http://github.com/a/b/issues/6" },
    { id: 7, title: "Issue 7", status: "ready", agent: "fake", playbook: "playbook-1", run_id: null, url: "http://github.com/a/b/issues/7" }
  ],
  edges: [
    { source: 2, target: 3 },
    { source: 3, target: 4 }
  ]
};

const mockCampaigns = [
  {
    id: "gargi-indic/harness",
    repo: "gargi-indic/harness",
    control_issue: 1,
    title: "gargi-indic/harness",
    counts: { ready: 2, running: 1, needs_you: 1, review: 0, blocked: 1, done: 1, failed: 0 }
  }
];

describe('CampaignsPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders 6 issue nodes and legend, clicking opens panel, esc closes it', async () => {
    mockApi({
      'GET /api/campaigns': () => ({ status: 200, json: mockCampaigns }),
      'GET /api/campaigns/gargi-indic/harness/graph': () => ({ status: 200, json: mockGraph }),
    });

    const user = userEvent.setup();

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/campaigns/gargi-indic/harness']}>
          <Routes>
            <Route path="/campaigns/:owner/:repo" element={<CampaignsPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // Legend
    expect((await screen.findAllByText('READY'))[0]).toBeInTheDocument();
    expect(screen.getAllByText('RUNNING')[0]).toBeInTheDocument();
    expect(screen.getAllByText('NEEDS-YOU')[0]).toBeInTheDocument();
    expect(screen.getAllByText('REVIEW')[0]).toBeInTheDocument();
    expect(screen.getAllByText('BLOCKED')[0]).toBeInTheDocument();
    expect(screen.getAllByText('DONE')[0]).toBeInTheDocument();
    expect(screen.getAllByText('FAILED')[0]).toBeInTheDocument();

    // Issue nodes
    expect(await screen.findByText('#2')).toBeInTheDocument();
    expect(screen.getByText('#3')).toBeInTheDocument();
    expect(screen.getByText('#4')).toBeInTheDocument();
    expect(screen.getByText('#5')).toBeInTheDocument();
    expect(screen.getByText('#6')).toBeInTheDocument();
    expect(screen.getByText('#7')).toBeInTheDocument();

    // Click #5 to open panel
    const issueNode = screen.getAllByText('#5')[0].closest('.issue-node')!;
    fireEvent.click(issueNode);

    const panelTitle = await screen.findByRole('heading', { name: '#5 Issue 5' });
    expect(panelTitle).toBeInTheDocument();

    const runLink = screen.getByRole('link', { name: 'Run' });
    expect(runLink).toHaveAttribute('href', '/runs/run-5');

    const githubLink = screen.getByRole('link', { name: 'Issue on GitHub' });
    expect(githubLink).toHaveAttribute('href', 'http://github.com/a/b/issues/5');

    // Close with Esc
    const container = screen.getByRole('heading', { name: '#5 Issue 5' }).closest('.campaigns-page')?.querySelector('.campaigns-canvas-container');
    await user.type(container as HTMLElement, '{Escape}');

    // Panel title should disappear
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: '#5 Issue 5' })).not.toBeInTheDocument();
    });
  });

  it('shows empty text with no campaigns', async () => {
    mockApi({
      'GET /api/campaigns': () => ({ status: 200, json: [] }),
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/campaigns']}>
          <Routes>
            <Route path="/campaigns" element={<CampaignsPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText('No campaigns registered. Run harness init.')).toBeInTheDocument();
  });
});
