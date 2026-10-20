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
      'GET /api/campaigns': () => ({ status: 200, json: [] })
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
