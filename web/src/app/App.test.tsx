import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppRoutes } from './routes';
import { App } from './App';
import { mockApi } from '../test/fetchMock';

describe('AppRoutes', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    mockApi({
      'GET /api/campaigns': () => ({ status: 200, json: [] }),
      'GET /api/playbooks': () => ({ status: 200, json: [] }),
      'GET /api/playbooks/x': () => ({ status: 200, json: { id: 'x', source: 'repo', path: 'p', sha: null, valid: true, parsed: null, yaml: 'apiVersion: harness/v1\nid: x\nnodes: {}\nedges: {}\n' } }),
      'POST /api/playbooks/validate': () => ({ status: 200, json: { ok: true, issues: [] } })
    });
  });

  it('redirects / to campaigns', () => {
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/']}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    );
  });

  it('renders known route', () => {
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/playbooks']}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    );
    expect(screen.getByRole('heading', { name: 'Playbooks' })).toBeInTheDocument();
  });

  it('routes /playbooks/new and /playbooks/:id to the editor', async () => {
    for (const path of ['/playbooks/new', '/playbooks/x']) {
      const { unmount } = render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[path]}>
            <AppRoutes />
          </MemoryRouter>
        </QueryClientProvider>
      );
      expect(await screen.findByText('Palette')).toBeInTheDocument();
      unmount();
    }
  });

  it('renders not found for unknown route', () => {
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/something-else']}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    );
    expect(screen.getByRole('heading', { name: 'Not found' })).toBeInTheDocument();
  });
});

describe('App', () => {
  let esConstructorMock: any;
  beforeEach(() => {
    esConstructorMock = vi.fn(function(this: any) {
      this.addEventListener = vi.fn();
      this.removeEventListener = vi.fn();
      this.close = vi.fn();
    });
    vi.stubGlobal('EventSource', esConstructorMock);
  });

  it('mounts EventInvalidator which creates exactly one EventSource for /api/events', () => {
    render(<App />);
    expect(esConstructorMock).toHaveBeenCalledTimes(1);
    expect(esConstructorMock).toHaveBeenCalledWith('/api/events');
  });
});
