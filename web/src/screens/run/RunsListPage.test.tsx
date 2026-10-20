import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RunsListPage } from './RunsListPage';
import { mockApi } from '../../test/fetchMock';

describe('RunsListPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders a list of runs', async () => {
    mockApi({
      'GET /api/runs': () => ({
        status: 200,
        json: [
          {
            id: 'acme/widgets#1@a1',
            issue: 1,
            title: 'Implement search',
            status: 'running',
            playbook_id: 'default',
            created_at: '2023-01-01T12:00:00Z',
          }
        ]
      })
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <RunsListPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(screen.getByText('Loading...')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('#1 Implement search')).toBeInTheDocument();
    });

    expect(screen.getByText('default')).toBeInTheDocument();
  });
});
