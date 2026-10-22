import '@testing-library/jest-dom/vitest';
import { expect, vi } from 'vitest';
import * as matchers from '@testing-library/jest-dom/matchers';

expect.extend(matchers as any);

class FakeEventSource {
  onmessage: any;
  listeners: Record<string, any[]> = {};

  constructor() {}

  addEventListener(type: string, listener: any) {
    if (!this.listeners[type]) {
      this.listeners[type] = [];
    }
    this.listeners[type].push(listener);
  }

  removeEventListener(type: string, listener: any) {
    if (this.listeners[type]) {
      this.listeners[type] = this.listeners[type].filter(l => l !== listener);
    }
  }

  close() {}
}

vi.stubGlobal('EventSource', FakeEventSource);

class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ResizeObserver);

class IntersectionObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('IntersectionObserver', IntersectionObserver);

global.DOMMatrixReadOnly = class DOMMatrixReadOnly {
  a = 1; b = 0; c = 0; d = 1; e = 0; f = 0;
  m11 = 1; m12 = 0; m13 = 0; m14 = 0;
  m21 = 0; m22 = 1; m23 = 0; m24 = 0;
  m31 = 0; m32 = 0; m33 = 1; m34 = 0;
  m41 = 0; m42 = 0; m43 = 0; m44 = 1;
  is2D = true; isIdentity = true;
  constructor() {}
  multiply() { return this; }
  inverse() { return this; }
  scale() { return this; }
  translate() { return this; }
  transformPoint() { return { x: 0, y: 0, z: 0, w: 1 }; }
} as any;
