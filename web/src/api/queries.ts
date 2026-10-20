
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
import type { CampaignSummary, CampaignGraph, Board, InboxItem, AgentsEnabled } from './types';

export const queryKeys = {
  runs: ["runs"],
  run: (id: string) => ["runs", id],
  inbox: ["inbox"],
  issues: ["issues"],
  campaigns: ["campaigns"],
  graph: (id: string) => ["campaigns", id, "graph"],
  board: (campaign?: string) => ["board", campaign ?? null],
  agents: (repo: string) => ["agents", repo]
};

export function useInvalidateOnEvents() {
  const queryClient = useQueryClient();

  useEventStream((e) => {
    switch (e.type) {
      case 'run.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.runs });
        queryClient.invalidateQueries({ queryKey: ["board"] });
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
    }
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
