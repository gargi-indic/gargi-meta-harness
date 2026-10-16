import { render } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useEventStream } from './sse';

class FakeEventSource {
  url: string;
  listeners: Record<string, EventListener[]> = {};

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(type: string, listener: EventListener) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(listener);
  }

  removeEventListener(type: string, listener: EventListener) {
    if (!this.listeners[type]) return;
    this.listeners[type] = this.listeners[type].filter(l => l !== listener);
  }

  close = vi.fn();

  emit(type: string, event: Partial<MessageEvent>) {
    if (this.listeners[type]) {
      this.listeners[type].forEach(l => l(event as MessageEvent));
    }
  }
}

describe('useEventStream', () => {
  let fakeEs: FakeEventSource | null = null;
  let esConstructorMock: any;

  beforeEach(() => {
    fakeEs = null;
    esConstructorMock = vi.fn().mockImplementation((url) => {
      fakeEs = new FakeEventSource(url);
      return fakeEs;
    });
    vi.stubGlobal('EventSource', esConstructorMock);
  });

  it('calls handler with parsed data and seq', () => {
    const handlerProp = vi.fn();

    // Passing a fresh array instance `['test.event']` every render used to trigger re-renders if we don't handle dependency array well.
    // By passing an empty dependency array in `useEventStream`, we avoid it.

    function TestComponent({ h }: { h: any }) {
      useEventStream(h, ['test.event']);
      return <div />;
    }

    const { unmount, rerender } = render(<TestComponent h={handlerProp} />);
    expect(fakeEs).toBeTruthy();
    expect(fakeEs?.url).toBe('/api/events');

    fakeEs?.emit('test.event', { lastEventId: '42', data: JSON.stringify({ hello: 'world' }) });

    expect(handlerProp).toHaveBeenCalledWith({
      seq: 42,
      type: 'test.event',
      data: { hello: 'world' }
    });

    const newHandler = vi.fn();
    esConstructorMock.mockClear();

    // Rerender with a new handler
    rerender(<TestComponent h={newHandler} />);

    // Changing handler should not reconnect (new EventSource)
    expect(esConstructorMock).not.toHaveBeenCalled();

    fakeEs?.emit('test.event', { lastEventId: '43', data: JSON.stringify({ again: true }) });
    expect(newHandler).toHaveBeenCalledWith({
      seq: 43,
      type: 'test.event',
      data: { again: true }
    });

    unmount();
    expect(fakeEs?.close).toHaveBeenCalled();
  });
});
