
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

export const queryKeys = {
  runs: ["runs"],
  run: (id: string) => ["runs", id],
  inbox: ["inbox"],
  issues: ["issues"],
  campaigns: ["campaigns"]
};

export function useInvalidateOnEvents() {
  const queryClient = useQueryClient();

  useEventStream((e) => {
    switch (e.type) {
      case 'run.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.runs });
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
        break;
      case 'issue.updated':
        queryClient.invalidateQueries({ queryKey: queryKeys.issues });
        queryClient.invalidateQueries({ queryKey: queryKeys.campaigns });
        break;
    }
  });
}
