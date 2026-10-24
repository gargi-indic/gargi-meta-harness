import { describe, it, expect, afterEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from './Header';
import { mockApi } from '../test/fetchMock';

describe('Header planner link', () => {
  afterEach(cleanup);

  it('links to /planner', () => {
    mockApi({ 'GET /api/health': () => ({ status: 200, json: { status: 'ok', version: '0', repo_count: 1, demo: false } }) });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={['/planner']}>
          <Header />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const link = screen.getByRole('link', { name: 'Planner' });
    expect(link).toHaveAttribute('href', '/planner');
    expect(link).toHaveAttribute('aria-current', 'page');
  });
});
