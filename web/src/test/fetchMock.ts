import { vi } from 'vitest';

export type RouteHandler = (body?: any) => { status: number; json?: any };

export function mockApi(routes: Record<string, RouteHandler>) {
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input.toString();
    const method = init?.method || 'GET';
    // Normalize URL path and search
    let pathAndSearch = url;
    if (url.startsWith('http://') || url.startsWith('https://')) {
      const parsed = new URL(url);
      pathAndSearch = parsed.pathname + parsed.search;
    }

    const key = `${method} ${pathAndSearch}`;
    const handler = routes[key];

    if (!handler) {
      throw new Error(`Unmatched request: ${key}`);
    }

    let parsedBody: any = undefined;
    if (init?.body && typeof init.body === 'string') {
      try {
        parsedBody = JSON.parse(init.body);
      } catch {
        parsedBody = init.body;
      }
    }

    const { status, json } = handler(parsedBody);

    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: async () => json,
    } as Response;
  });

  vi.stubGlobal('fetch', fetchMock);

  return fetchMock;
}
