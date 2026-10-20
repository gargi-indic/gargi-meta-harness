import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RunPage } from './RunPage';
import { mockApi } from '../../test/fetchMock';

describe('RunPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the run view and correctly calls controls', async () => {
    const runId = 'acme/widgets#1@a1';

    mockApi({
      [`GET /api/runs/${encodeURIComponent(runId)}`]: () => ({
        status: 200,
        json: {
          run: {
            id: runId,
            issue: 1,
            title: 'Implement search',
            status: 'running',
            playbook_id: 'default',
            created_at: '2023-01-01T12:00:00Z',
            updated_at: '2023-01-01T12:05:00Z',
          },
          playbook: {
            nodes: { n1: { kind: 'agent' } },
            edges: {}
          },
          steps: [
            { id: 's1', node: 'n1', visit: 1, status: 'done', outcome: 'ok', started_at: '2023-01-01T12:00:00Z', finished_at: '2023-01-01T12:02:00Z', ref: null }
          ],
          visit_counts: { n1: 1 }
        }
      }),
      [`GET /api/runs/${encodeURIComponent(runId)}/activity?after=0&limit=200`]: () => ({
        status: 200,
        json: { items: [], next: 0 }
      }),
      [`POST /api/runs/${encodeURIComponent(runId)}/pause`]: () => ({
        status: 202,
        json: { request_id: 123 }
      }),
      [`GET /api/control-requests/123`]: () => ({
        status: 200,
        json: { id: 123, run_id: runId, kind: 'pause', status: 'pending', created_at: '2023-01-01T12:06:00Z', handled_at: null, result: null }
      })
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/runs/${encodeURIComponent(runId)}`]}>
          <Routes>
            <Route path="/runs/:id" element={<RunPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(screen.getByText('Loading...')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('#1 Implement search')).toBeInTheDocument();
    });

    // Contains timeline
    expect(screen.getAllByText('n1')[0]).toBeInTheDocument();

    // Trigger Pause
    const pauseBtn = screen.getByRole('button', { name: 'Pause' });
    fireEvent.click(pauseBtn);

    await waitFor(() => {
      expect(screen.getByText('Action pending...')).toBeInTheDocument();
    });
  });

  it('handles send message and auto-scroll pausing correctly', async () => {
    const runId = 'acme/widgets#1@a1';

    mockApi({
      [`GET /api/runs/${encodeURIComponent(runId)}`]: () => ({
        status: 200,
        json: {
          run: { id: runId, issue: 1, title: 'Implement search', status: 'running', playbook_id: 'default', created_at: '2023-01-01T12:00:00Z', updated_at: '2023-01-01T12:05:00Z' },
          playbook: { nodes: {}, edges: {} },
          steps: [],
          visit_counts: {}
        }
      }),
      [`GET /api/runs/${encodeURIComponent(runId)}/activity?after=0&limit=200`]: () => ({
        status: 200,
        json: { items: [], next: 0 }
      }),
      [`POST /api/runs/${encodeURIComponent(runId)}/message`]: (body) => {
        expect(body.text).toBe('hello jules');
        return {
          status: 202,
          json: { request_id: 124 }
        };
      },
      [`GET /api/control-requests/124`]: () => ({
        status: 200,
        json: { id: 124, run_id: runId, kind: 'send_message', status: 'done', created_at: '2023-01-01T12:06:00Z', handled_at: '2023-01-01T12:06:01Z', result: {} }
      })
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/runs/${encodeURIComponent(runId)}`]}>
          <Routes>
            <Route path="/runs/:id" element={<RunPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('#1 Implement search')).toBeInTheDocument();
    });

    const msgInput = screen.getByLabelText('Send message');
    fireEvent.change(msgInput, { target: { value: 'hello jules' } });

    const sendBtn = screen.getByRole('button', { name: 'Send message' });
    fireEvent.click(sendBtn);

    await waitFor(() => {
      expect(screen.getByText('Action succeeded.')).toBeInTheDocument();
    });

    // check textarea is cleared
    expect(msgInput).toHaveValue('');
  });
});
