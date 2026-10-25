
import { QueryClient, useQueryClient } from '@tanstack/react-query';
import { useEventStream } from './sse';

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        staleTime: 5000,
      },
    },
  });
}

import { useQuery } from '@tanstack/react-query';
import { apiGet } from './client';
import type {
  CampaignSummary,
  CampaignGraph,
  Board,
  InboxItem,
  AgentsEnabled,
  OpsStatusOut,
  AgentOut,
  AgentRunCostRow,
} from './types';

export const queryKeys = {
  runs: ["runs"],
  run: (id: string) => ["runs", id],
  inbox: ["inbox"],
  issues: ["issues"],
  campaigns: ["campaigns"],
  graph: (id: string) => ["campaigns", id, "graph"],
  board: (campaign?: string) => ["board", campaign ?? null],
  agents: (repo: string) => ["agents", repo],
  planner: ["planner"],
  ops: ["ops", "status"],
  agentList: ["agents", "list"],
  agentCosts: ["agents", "costs"],
};

/** Fallback poll for the ops strip and the Agents screen, in case an SSE event is missed. */
export const OPS_POLL_MS = 15_000;

export function useInvalidateOnEvents() {
  const queryClient = useQueryClient();

  useEventStream((e) => {
    switch (e.type) {
      case 'run.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.runs });
        queryClient.invalidateQueries({ queryKey: ["board"] });
        queryClient.invalidateQueries({ queryKey: queryKeys.agentList });
        queryClient.invalidateQueries({ queryKey: queryKeys.agentCosts });
        break;
      case 'step.updated': {
        const data = e.data as { run_id?: string } | null;
        if (data?.run_id) {
          queryClient.invalidateQueries({ queryKey: queryKeys.run(data.run_id) });
        } else {
          queryClient.invalidateQueries({ queryKey: queryKeys.runs });
        }
        break;
      }
      case 'inbox.changed':
        queryClient.invalidateQueries({ queryKey: queryKeys.inbox });
        queryClient.invalidateQueries({ queryKey: ["board"] });
        break;
      case 'issue.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.issues });
        queryClient.invalidateQueries({ queryKey: queryKeys.campaigns });
        queryClient.invalidateQueries({ queryKey: ["board"] });
        break;
      case 'ops.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.ops });
        queryClient.invalidateQueries({ queryKey: queryKeys.agentList });
        break;
      case 'planner.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.planner });
        break;
    }
  });
}

export interface Health {
  status: string;
  version: string;
  repo_count: number;
  demo: boolean;
}

export function useHealth() {
  return useQuery<Health>({
    queryKey: ["health"],
    queryFn: () => apiGet('/api/health'),
    staleTime: Infinity,
    retry: false,
  });
}

export function useCampaigns() {
  return useQuery<CampaignSummary[]>({
    queryKey: queryKeys.campaigns,
    queryFn: () => apiGet('/api/campaigns'),
  });
}

export function useCampaignGraph(id: string) {
  return useQuery<CampaignGraph>({
    queryKey: queryKeys.graph(id),
    queryFn: () => {
      const parts = id.split('/');
      if (parts.length !== 2) throw new Error("Invalid id");
      const owner = encodeURIComponent(parts[0]);
      const repo = encodeURIComponent(parts[1]);
      return apiGet(`/api/campaigns/${owner}/${repo}/graph`);
    },
    enabled: !!id,
  });
}

export function useBoard(campaign?: string) {
  return useQuery<Board>({
    queryKey: queryKeys.board(campaign),
    queryFn: () => {
      let url = '/api/board';
      if (campaign) {
        url += `?campaign=${encodeURIComponent(campaign)}`;
      }
      return apiGet(url);
    },
  });
}

export function useInbox() {
  return useQuery<InboxItem[]>({
    queryKey: queryKeys.inbox,
    queryFn: () => apiGet('/api/inbox'),
  });
}

export function useEnabledAgents(repo: string) {
  return useQuery<AgentsEnabled>({
    queryKey: queryKeys.agents(repo),
    queryFn: () => apiGet(`/api/agents/enabled?repo=${encodeURIComponent(repo)}`),
    enabled: !!repo,
  });
}

export function useRuns(status?: string, issue?: number, repo?: string) {
  return useQuery<import('./types').RunSummary[]>({
    queryKey: [...queryKeys.runs, { status, issue, repo }],
    queryFn: () => {
      const params = new URLSearchParams();
      if (status) params.set('status', status);
      if (issue !== undefined) params.set('issue', issue.toString());
      if (repo) params.set('repo', repo);
      const query = params.toString();
      return apiGet(`/api/runs${query ? `?${query}` : ''}`);
    },
  });
}

export function useRun(id: string) {
  return useQuery<import('./types').RunDetail>({
    queryKey: queryKeys.run(id),
    queryFn: () => apiGet(`/api/runs/${encodeURIComponent(id)}`),
    enabled: !!id,
  });
}

export function useControlRequest(id: number | null) {
  return useQuery<import('./types').ControlRequestOut>({
    queryKey: ['control-request', id],
    queryFn: () => apiGet(`/api/control-requests/${id}`),
    enabled: id !== null,
    refetchInterval: (query) => {
      if (query.state.data && query.state.data.handled_at !== null) {
        return false;
      }
      return 1000; // poll every second until handled
    },
  });
}

export function useRunActivity(id: string, after: number = 0, limit: number = 200) {
  return useQuery<import('./types').ActivityPage>({
    queryKey: ['runs', id, 'activity', { after, limit }],
    queryFn: () => apiGet(`/api/runs/${encodeURIComponent(id)}/activity?after=${after}&limit=${limit}`),
    enabled: !!id,
  });
}

export function useOpsStatus() {
  return useQuery<OpsStatusOut>({
    queryKey: queryKeys.ops,
    queryFn: () => apiGet('/api/ops/status'),
    refetchInterval: OPS_POLL_MS,
  });
}

export function useAgents() {
  return useQuery<AgentOut[]>({
    queryKey: queryKeys.agentList,
    queryFn: () => apiGet('/api/agents'),
    refetchInterval: OPS_POLL_MS,
  });
}

export function useAgentCosts() {
  return useQuery<AgentRunCostRow[]>({
    queryKey: queryKeys.agentCosts,
    queryFn: () => apiGet('/api/agents/costs'),
    refetchInterval: OPS_POLL_MS,
  });
}
