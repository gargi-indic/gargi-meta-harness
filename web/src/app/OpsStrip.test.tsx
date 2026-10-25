import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from './Header';
import { useInvalidateOnEvents } from '../api/queries';
import { mockApi } from '../test/fetchMock';

const budget = (spent: number, limit: number | null) => ({
  limit_usd: limit,
  spent_usd: spent,
  remaining_usd: null,
  resets_at: '',
  exceeded: false,
  unknown_cost_runs: 0,
});
const halted = { reason: 'kill_switch', since: '2026-01-01T00:00:00Z', by: 'api' };

let emit: ((type: string) => void) | null = null;
vi.mock('../api/sse', async (orig) => {
  const actual = await orig<typeof import('../api/sse')>();
  return {
    ...actual,
    useEventStream: (handler: (e: { seq: number; type: string; data: unknown }) => void) => {
      emit = (type) => handler({ seq: 1, type, data: {} });
    },
  };
});

function Invalidator() {
  useInvalidateOnEvents();
  return null;
}

function renderHeader(route = '/inbox') {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Invalidator />
      <MemoryRouter initialEntries={[route]}>
        <Header />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('header ops strip', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('shows spend against the cap on any route and spend alone without a cap', async () => {
    mockApi({ 'GET /api/ops/status': () => ({ status: 200, json: { halted: null, budget: budget(12.4, 50) } }) });
    renderHeader('/board');
    expect(await screen.findByText('SPEND $12.40 / $50.00')).toBeInTheDocument();
    expect(screen.queryByText('HALTED')).not.toBeInTheDocument();
    cleanup();
    mockApi({ 'GET /api/ops/status': () => ({ status: 200, json: { halted: null, budget: budget(3, null) } }) });
    renderHeader('/runs');
    expect(await screen.findByText('SPEND $3.00')).toBeInTheDocument();
  });

  it('Kill all does nothing until KILL is typed exactly, then posts the confirmation', async () => {
    const posts: unknown[] = [];
    mockApi({
      'GET /api/ops/status': () => ({ status: 200, json: { halted: null, budget: budget(1, null) } }),
      'POST /api/ops/kill': (body: unknown) => {
        posts.push(body);
        return { status: 200, json: { halted: true, cancels_queued: 0 } };
      },
    });
    renderHeader();
    fireEvent.click(await screen.findByRole('button', { name: 'Kill all' }));
    expect(posts).toEqual([]);
    const confirm = screen.getByRole('button', { name: 'Confirm kill' });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Type KILL to confirm'), { target: { value: 'kill' } });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Type KILL to confirm'), { target: { value: 'KILL' } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(posts).toEqual([{ confirm: 'KILL' }]));
  });

  it('shows a kill error inline', async () => {
    mockApi({
      'GET /api/ops/status': () => ({ status: 200, json: { halted: null, budget: budget(1, null) } }),
      'POST /api/ops/kill': () => ({ status: 409, json: { detail: 'no control issue' } }),
    });
    renderHeader();
    fireEvent.click(await screen.findByRole('button', { name: 'Kill all' }));
    fireEvent.change(screen.getByLabelText('Type KILL to confirm'), { target: { value: 'KILL' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm kill' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('no control issue');
  });

  it('shows HALTED with Resume, which posts to resume', async () => {
    let calls = 0;
    mockApi({
      'GET /api/ops/status': () => ({ status: 200, json: { halted, budget: budget(1, null) } }),
      'POST /api/ops/resume': () => {
        calls += 1;
        return { status: 200, json: { resumed_runs: [] } };
      },
    });
    renderHeader();
    expect(await screen.findByText('HALTED')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Kill all' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(calls).toBe(1));
  });

  it('updates on an ops.updated event', async () => {
    let current: unknown = { halted: null, budget: budget(1, 10) };
    mockApi({ 'GET /api/ops/status': () => ({ status: 200, json: current }) });
    renderHeader();
    expect(await screen.findByText('SPEND $1.00 / $10.00')).toBeInTheDocument();
    current = { halted, budget: budget(2, 10) };
    act(() => emit!('ops.updated'));
    expect(await screen.findByText('SPEND $2.00 / $10.00')).toBeInTheDocument();
    expect(screen.getByText('HALTED')).toBeInTheDocument();
  });
});
