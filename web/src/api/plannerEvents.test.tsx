import { render } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useInvalidateOnEvents, createQueryClient, queryKeys } from './queries';
import { useEventStream, EVENT_TYPES, ServerEvent } from './sse';

vi.mock('./sse', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./sse')>()),
  useEventStream: vi.fn(),
}));

describe('planner.updated', () => {
  let queryClient: QueryClient;
  let invalidateSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    queryClient = createQueryClient();
    invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries').mockImplementation(() => Promise.resolve());
    vi.mocked(useEventStream).mockClear();
  });

  function Probe() {
    useInvalidateOnEvents();
    return null;
  }

  it('is one of the event types the stream listens for', () => {
    expect(EVENT_TYPES).toContain('planner.updated');
  });

  it('invalidates every planner query', () => {
    render(
      <QueryClientProvider client={queryClient}>
        <Probe />
      </QueryClientProvider>,
    );
    const handler = vi.mocked(useEventStream).mock.calls[0][0] as (e: ServerEvent) => void;
    handler({ seq: 1, type: 'planner.updated', data: { proposal_id: 'x' } });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.planner });
  });
});
