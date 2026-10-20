import { describe, test, expect } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppRoutes } from './routes';
import { mockApi } from '../test/fetchMock';

describe('AppRoutes', () => {
  test('renders campaigns route', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mockApi({
      'GET /api/campaigns': () => ({ status: 200, json: [] })
    });

    // Basic rendering check
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/campaigns']}>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    );
    expect(container).toBeDefined();
  });
});
