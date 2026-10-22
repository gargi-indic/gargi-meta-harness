import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { describe, it, expect, afterEach } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PlaybooksListPage } from './PlaybooksListPage';
import { mockApi } from '../../test/fetchMock';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/playbooks']}>
        <Routes>
          <Route path="/playbooks" element={<PlaybooksListPage />} />
          <Route path="/playbooks/new" element={<div>new editor</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe('PlaybooksListPage', () => {
  afterEach(() => cleanup());

  it('lists playbooks with id link, source and validity', async () => {
    mockApi({
      'GET /api/playbooks': () => ({
        status: 200,
        json: [
          { id: 'plan-build-review', source: 'builtin', path: 'a.yaml', sha: null, valid: true },
          { id: 'my pb', source: 'repo', path: '.harness/playbooks/b.yaml', sha: 'abc', valid: false },
        ],
      }),
    });
    renderPage();

    expect(screen.getByRole('heading', { name: 'Playbooks' })).toBeInTheDocument();
    const link = await screen.findByRole('link', { name: 'plan-build-review' });
    expect(link).toHaveAttribute('href', '/playbooks/plan-build-review');
    expect(screen.getByRole('link', { name: 'my pb' })).toHaveAttribute('href', '/playbooks/my%20pb');
    expect(screen.getByText('builtin')).toBeInTheDocument();
    expect(screen.getByText('repo')).toBeInTheDocument();
    expect(screen.getByText('valid')).toBeInTheDocument();
    expect(screen.getByText('invalid')).toBeInTheDocument();
  });

  it('New playbook navigates to /playbooks/new', async () => {
    mockApi({ 'GET /api/playbooks': () => ({ status: 200, json: [] }) });
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'New playbook' }));
    await waitFor(() => expect(screen.getByText('new editor')).toBeInTheDocument());
  });
});
