import { useEffect, useRef } from 'react';

export const EVENT_TYPES = ["run.updated", "step.updated", "inbox.changed", "issue.updated"] as const;

export interface ServerEvent {
  seq: number;
  type: string;
  data: unknown;
}

export function useEventStream(
  handler: (e: ServerEvent) => void,
  types: readonly string[] = EVENT_TYPES
) {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  // Store types so we don't trigger effect on every render
  const typesRef = useRef(types);

  useEffect(() => {
    const es = new EventSource("/api/events");

    const onMessage = (type: string, e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        const seq = e.lastEventId ? parseInt(e.lastEventId, 10) : 0;
        handlerRef.current({ seq, type, data });
      } catch (err) {
        console.error("Failed to parse SSE data", err);
      }
    };

    const listeners = typesRef.current.map(type => {
      const listener = (e: MessageEvent) => onMessage(type, e);
      es.addEventListener(type, listener);
      return { type, listener };
    });

    const defaultListener = (e: MessageEvent) => onMessage('message', e);
    es.addEventListener('message', defaultListener);

    return () => {
      listeners.forEach(({ type, listener }) => {
        es.removeEventListener(type, listener);
      });
      es.removeEventListener('message', defaultListener);
      es.close();
    };
  }, []);
}
