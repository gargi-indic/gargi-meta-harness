import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { describe, it, expect, afterEach } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from './Header';
import { mockApi } from '../test/fetchMock';

describe('Header', () => {
  afterEach(cleanup);

  it('toggles menu and active links', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mockApi({
      'GET /api/inbox': () => ({ status: 200, json: [] })
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/inbox']}>
          <Header />
          <Routes>
            <Route path="/*" element={<div />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    const menuBtn = screen.getByRole('button', { name: 'Menu' });
    const nav = screen.getByRole('navigation', { name: 'Main' });

    expect(menuBtn).toHaveAttribute('aria-expanded', 'false');
    expect(nav).toHaveAttribute('data-open', 'false');

    fireEvent.click(menuBtn);
    expect(menuBtn).toHaveAttribute('aria-expanded', 'true');
    expect(nav).toHaveAttribute('data-open', 'true');

    // Active link has aria-current="page"
    const inboxLink = screen.getByText('Inbox');
    expect(inboxLink).toHaveAttribute('aria-current', 'page');

    // Click a link to close the menu
    const boardLink = screen.getByText('Board');
    fireEvent.click(boardLink);
    expect(menuBtn).toHaveAttribute('aria-expanded', 'false');
    expect(nav).toHaveAttribute('data-open', 'false');
  });

  function renderWithHealth(demo: boolean) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const fetchMock = mockApi({
      'GET /api/ops/status': () => ({ status: 200, json: { halted: null, budget: { limit_usd: null, spent_usd: 0, remaining_usd: null, resets_at: '', exceeded: false, unknown_cost_runs: 0 } } }),
      'GET /api/health': () => ({ status: 200, json: { status: 'ok', version: '0', repo_count: 1, demo } })
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/inbox']}>
          <Header />
        </MemoryRouter>
      </QueryClientProvider>
    );
    return fetchMock;
  }

  it('shows the DEMO DATA tag when health.demo is true', async () => {
    renderWithHealth(true);
    expect(await screen.findByText('DEMO DATA')).toBeInTheDocument();
  });

  it('does not show the DEMO DATA tag when health.demo is false', async () => {
    const fetchMock = renderWithHealth(false);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => expect(fetchMock.mock.results[0].value).resolves.toBeDefined());
    expect(screen.queryByText('DEMO DATA')).not.toBeInTheDocument();
  });
});
