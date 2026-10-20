import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BoardPage } from './BoardPage';
import { mockApi } from '../../test/fetchMock';

const mockBoard = {
  columns: {
    ready: [{ repo: "a/b", issue: 3, title: "Issue 3", agent: "jules", playbook: "playbook-1", run_id: null, pr: null, blocked_by: [2], updated_at: "2023-01-01" }],
    running: [{ repo: "a/b", issue: 5, title: "Issue 5", agent: "jules", playbook: "playbook-1", run_id: "run-5", pr: null, blocked_by: [], updated_at: "2023-01-01" }],
    needs_you: [{ repo: "a/b", issue: 6, title: "Issue 6", agent: "jules", playbook: "playbook-1", run_id: "run-6", pr: null, blocked_by: [], updated_at: "2023-01-01" }],
    review: [],
    blocked: [{ repo: "a/b", issue: 4, title: "Issue 4", agent: "jules", playbook: "playbook-1", run_id: null, pr: null, blocked_by: [3], updated_at: "2023-01-01" }],
    done: [{ repo: "a/b", issue: 2, title: "Issue 2", agent: "jules", playbook: "playbook-1", run_id: "run-2", pr: null, blocked_by: [], updated_at: "2023-01-01" }],
    failed: []
  }
};

describe('BoardPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders cards in columns, empty columns show dash', async () => {
    mockApi({
      'GET /api/campaigns': () => ({ status: 200, json: [] }),
      'GET /api/board': () => ({ status: 200, json: mockBoard }),
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/board']}>
          <Routes>
            <Route path="/board" element={<BoardPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // Headers with counts
    expect(await screen.findByText('READY 1')).toBeInTheDocument();
    expect(screen.getByText('RUNNING 1')).toBeInTheDocument();
    expect(screen.getByText('NEEDS YOU 1')).toBeInTheDocument();
    expect(screen.getByText('REVIEW 0')).toBeInTheDocument();
    expect(screen.getByText('BLOCKED 1')).toBeInTheDocument();
    expect(screen.getByText('DONE 1')).toBeInTheDocument();
    expect(screen.getByText('FAILED 0')).toBeInTheDocument();

    // Empty columns show —
    const emptyDashes = screen.getAllByText('—');
    expect(emptyDashes).toHaveLength(2); // review and failed are empty

    // Blocked by shows correctly
    expect(screen.getByText('blocked by #2')).toBeInTheDocument();
  });
});
