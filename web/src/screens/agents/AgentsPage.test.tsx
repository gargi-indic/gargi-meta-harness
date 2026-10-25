import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AgentsPage } from './AgentsPage';
import { mockApi } from '../../test/fetchMock';

const caps = { plan_approval: true, messaging: true, opens_pr: true, local_workspace: false };
const agents = [
  {
    name: 'jules',
    enabled: true,
    health: { status: 'ok', detail: 'JULES_API_KEY is set', checked_at: '2026-01-01T00:00:00Z' },
    caps,
    max_concurrency: 4,
    in_use: 1,
    active_sessions: [
      { run_id: 'acme/widgets#5@1', issue: 5, step: 'acme/widgets#5@1/plan/1', started_at: null, state: 'running', cost_usd: null },
    ],
    today_cost_usd: 1.5,
  },
  {
    name: 'codex',
    enabled: true,
    health: { status: 'down', detail: 'codex is not on PATH', checked_at: '2026-01-01T00:00:00Z' },
    caps,
    max_concurrency: 2,
    in_use: 0,
    active_sessions: [],
    today_cost_usd: null,
  },
];
const costs = [
  { run_id: 'acme/widgets#5@1', issue: 5, agent: 'jules', usd: 1.75, estimated: true },
  { run_id: 'acme/widgets#6@1', issue: 6, agent: 'jules', usd: null, estimated: false },
];
const ops = (limit: number | null) => ({
  halted: null,
  budget: { limit_usd: limit, spent_usd: 1, remaining_usd: null, resets_at: '', exceeded: false, unknown_cost_runs: 0 },
});

function renderPage(extra: Record<string, (body?: unknown) => { status: number; json?: unknown }> = {}) {
  const fetchMock = mockApi({
    'GET /api/agents': () => ({ status: 200, json: agents }),
    'GET /api/agents/costs': () => ({ status: 200, json: costs }),
    'GET /api/ops/status': () => ({ status: 200, json: ops(50) }),
    ...extra,
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <AgentsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetchMock;
}

describe('AgentsPage', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders the grid, health tags, session links and the in-use bar ratio', async () => {
    renderPage();
    expect(await screen.findByText('JULES_API_KEY is set')).toBeInTheDocument();
    expect(screen.getByText('down')).toBeInTheDocument();
    expect(screen.getByText('IN USE 1 / 4')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '#5' })).toHaveAttribute('href', '/runs/acme%2Fwidgets%235%401');
    const fills = screen.getAllByTestId('usebar-fill');
    expect(fills[0].style.width).toBe('25%');
    expect(fills[1].style.width).toBe('0%');
  });

  it('shows n/a for unknown cost and the ESTIMATED tag in the cost table', async () => {
    renderPage();
    expect(await screen.findByText('ESTIMATED')).toBeInTheDocument();
    expect(screen.getByText('$1.75')).toBeInTheDocument();
    expect(screen.getAllByText('n/a').length).toBeGreaterThan(0);
    expect(screen.getByText('#6')).toBeInTheDocument();
  });

  it('posts a valid cap, rejects a negative one and posts null when cleared', async () => {
    const bodies: unknown[] = [];
    renderPage({
      'POST /api/ops/budget': (body: unknown) => {
        bodies.push(body);
        return { status: 200, json: ops(12.5).budget };
      },
    });
    const input = (await screen.findByLabelText('Daily cap (USD)')) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe('50'));

    fireEvent.change(input, { target: { value: '-3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cap' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('non-negative');
    expect(bodies).toEqual([]);

    fireEvent.change(input, { target: { value: '12.5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cap' }));
    await waitFor(() => expect(bodies).toEqual([{ daily_usd: 12.5 }]));

    fireEvent.change(input, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cap' }));
    await waitFor(() => expect(bodies).toEqual([{ daily_usd: 12.5 }, { daily_usd: null }]));
  });

  it('shows a server error inline', async () => {
    renderPage({ 'POST /api/ops/budget': () => ({ status: 409, json: { detail: 'no control issue' } }) });
    const input = await screen.findByLabelText('Daily cap (USD)');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cap' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('no control issue');
  });
});
