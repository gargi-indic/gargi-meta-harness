import { vi } from 'vitest';

export class FakeEventSource {
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
