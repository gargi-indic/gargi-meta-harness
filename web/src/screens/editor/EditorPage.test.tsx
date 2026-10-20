import { describe, test, expect, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { EditorPage } from './EditorPage';
import { mockApi } from '../../test/fetchMock';

global.DOMMatrixReadOnly = class DOMMatrixReadOnly {
  a = 1; b = 0; c = 0; d = 1; e = 0; f = 0;
  is2D = true; isIdentity = true;
  constructor() {}
  multiply() { return this; }
  inverse() { return this; }
  scale() { return this; }
  translate() { return this; }
  transformPoint() { return { x: 0, y: 0, z: 0, w: 1 }; }
} as any;

describe('EditorPage', () => {
  afterEach(() => {
    cleanup();
  });

  test('renders palette, canvas, and inspector', async () => {
    mockApi({
      'POST /api/playbooks/validate': () => ({
        status: 200,
        json: { ok: true, issues: [] }
      }),
      'GET /api/playbooks/test-pb': () => ({
        status: 200,
        json: {
          id: 'test-pb',
          source: 'repo',
          yaml: `
id: test-pb
apiVersion: harness/v1
nodes:
  agent_1:
    kind: agent
edges: {}
          `
        }
      })
    });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/playbooks/test-pb']}>
          <Routes>
            <Route path="/playbooks/:id" element={<EditorPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // wait for load
    await waitFor(() => {
      expect(screen.getByText('Palette')).toBeInTheDocument();
    });

    expect(screen.getAllByText('agent_1').length).toBeGreaterThanOrEqual(1);
  });

  test('clicking palette adds node', async () => {
    mockApi({
      'POST /api/playbooks/validate': () => ({
        status: 200,
        json: { ok: true, issues: [] }
      }),
      'GET /api/playbooks/test-pb': () => ({
        status: 200,
        json: {
          id: 'test-pb',
          source: 'repo',
          yaml: `
id: test-pb
apiVersion: harness/v1
nodes: {}
edges: {}
          `
        }
      })
    });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/playbooks/new']}>
          <Routes>
            <Route path="/playbooks/:id" element={<EditorPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => screen.getByText('Palette'));

    const agentBtn = screen.getAllByText('agent').find(el => el.tagName === 'BUTTON');
    if (agentBtn) {
      fireEvent.click(agentBtn);
    }

    await waitFor(() => {
      expect(screen.getAllByText('agent_1').length).toBeGreaterThanOrEqual(1);
    }, { timeout: 2000 });
  });
});
