import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from './Header';
import { mockApi } from '../test/fetchMock';

describe('Header', () => {
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
});
