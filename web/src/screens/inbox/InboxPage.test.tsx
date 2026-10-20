import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { InboxPage } from './InboxPage';
import { mockApi } from '../../test/fetchMock';

const mockInbox = [
  {
    id: "step-1",
    kind: "gate",
    run_id: "run-1",
    step_id: "step-1",
    repo: "a/b",
    issue: 1,
    title: "Issue 1",
    summary: "Need approval",
    pr: null,
    findings: [],
    agent: "jules",
    created_at: "2023-01-01",
    gate_id: "gate-1"
  },
  {
    id: "step-2",
    kind: "question",
    run_id: "run-2",
    step_id: "step-2",
    repo: "a/b",
    issue: 2,
    title: "Issue 2",
    summary: "Need input",
    pr: null,
    findings: [],
    agent: "jules",
    created_at: "2023-01-01",
    gate_id: null
  }
];

const mockAgents = {
  agents: [{ name: "jules", enabled: true }, { name: "other", enabled: true }]
};

describe('InboxPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('approve posts and removes item, double click sends one request', async () => {
    let currentInbox = [...mockInbox];
    const apiRoutes: any = {
      'GET /api/inbox': vi.fn(() => ({ status: 200, json: currentInbox })),
      'GET /api/agents/enabled?repo=a%2Fb': vi.fn(() => ({ status: 200, json: mockAgents })),
      'POST /api/inbox/step-1/approve': vi.fn(() => {
        currentInbox = currentInbox.filter(i => i.id !== "step-1");
        return { status: 202, json: { request_id: 1 } };
      }),
    };
    mockApi(apiRoutes);
    const user = userEvent.setup();

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <InboxPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    const itemPanel = (await screen.findByText(/Issue 1/)).closest('.panel') as HTMLElement;
    const firstApproveBtn = await within(itemPanel).findByRole('button', { name: 'Approve' });

    // Click twice rapidly to test disable state / multiple requests block
    await user.click(firstApproveBtn as HTMLElement);
    await user.click(firstApproveBtn as HTMLElement);

    await waitFor(() => {
      expect(screen.queryByText(/Issue 1/)).not.toBeInTheDocument();
    });

    expect(apiRoutes['POST /api/inbox/step-1/approve']).toHaveBeenCalledTimes(1);
  });

  it('reject cannot submit empty message, reroute handles errors', async () => {
    let currentInbox = [...mockInbox];
    const apiRoutes: any = {
      'GET /api/inbox': vi.fn(() => ({ status: 200, json: currentInbox })),
      'GET /api/agents/enabled?repo=a%2Fb': vi.fn(() => ({ status: 200, json: mockAgents })),
      'POST /api/inbox/step-1/reject': vi.fn(() => {
        currentInbox = currentInbox.filter(i => i.id !== "step-1");
        return { status: 202, json: {} };
      }),
      'POST /api/inbox/step-2/reroute': vi.fn(() => ({ status: 422, json: { detail: "unknown or disabled agent" } })),
    };
    mockApi(apiRoutes);
    const user = userEvent.setup();

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <InboxPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

    const itemPanel = (await screen.findByText(/Issue 1/)).closest('.panel') as HTMLElement;
    const rejectBtn = await waitFor(() => within(itemPanel).getByRole('button', { name: 'Reject' }));

    await user.click(rejectBtn as HTMLElement);

    const reasonInput = await waitFor(() => within(itemPanel).findByRole('textbox', { name: /reason/i }));
    const submitRejectBtn = within(itemPanel).getByRole('button', { name: /submit reject/i });

    expect(submitRejectBtn).toBeDisabled();

    await user.type(reasonInput as HTMLElement, 'Bad PR');
    expect(submitRejectBtn).toBeEnabled();

    await user.click(submitRejectBtn as HTMLElement);

    await waitFor(() => {
      expect(apiRoutes['POST /api/inbox/step-1/reject']).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/Issue 1/)).not.toBeInTheDocument();
    });

    // Reroute error
    const itemPanel2 = (await screen.findByText(/Issue 2/)).closest('.panel') as HTMLElement;
    const select = within(itemPanel2).getByRole('combobox');
    await user.selectOptions(select as HTMLElement, 'other');

    const rerouteBtn = within(itemPanel2).getByRole('button', { name: 'Reroute' });
    await user.click(rerouteBtn as HTMLElement);

    await waitFor(() => {
      const errorMsg = screen.getByRole('alert');
      expect(errorMsg).toHaveTextContent('unknown or disabled agent');
      // Item is still there
      expect(screen.getByText(/Issue 2/)).toBeInTheDocument();
    });
  });
});
