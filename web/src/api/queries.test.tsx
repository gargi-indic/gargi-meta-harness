import { render } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useInvalidateOnEvents, createQueryClient, queryKeys } from './queries';
import { useEventStream, ServerEvent } from './sse';

vi.mock('./sse', () => ({
  useEventStream: vi.fn(),
  EVENT_TYPES: [],
}));

describe('useInvalidateOnEvents', () => {
  let queryClient: QueryClient;
  let invalidateSpy: any;

  beforeEach(() => {
    queryClient = createQueryClient();
    invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries').mockImplementation(() => Promise.resolve());
    vi.mocked(useEventStream).mockClear();
  });

  function TestComponent() {
    useInvalidateOnEvents();
    return null;
  }

  function renderHook() {
    render(
      <QueryClientProvider client={queryClient}>
        <TestComponent />
      </QueryClientProvider>
    );
    const handler = vi.mocked(useEventStream).mock.calls[0][0];
    return handler as (e: ServerEvent) => void;
  }

  it('invalidates on run.updated', () => {
    const handler = renderHook();
    handler({ seq: 1, type: 'run.updated', data: {} });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.runs });
  });

  it('invalidates on step.updated with run_id', () => {
    const handler = renderHook();
    handler({ seq: 2, type: 'step.updated', data: { run_id: '123' } });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.run('123') });
  });

  it('invalidates on step.updated without run_id', () => {
    const handler = renderHook();
    handler({ seq: 2, type: 'step.updated', data: {} });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.runs });
  });

  it('invalidates on inbox.changed', () => {
    const handler = renderHook();
    handler({ seq: 3, type: 'inbox.changed', data: {} });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.inbox });
  });

  it('invalidates on issue.updated', () => {
    const handler = renderHook();
    handler({ seq: 4, type: 'issue.updated', data: {} });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.issues });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.campaigns });
  });
});
