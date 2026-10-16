export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`API Error ${status}: ${detail}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch {
      // Ignored
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) {
    return undefined as unknown as T;
  }

  return res.json();
}

export async function apiGet<T>(path: string, init?: { signal?: AbortSignal }): Promise<T> {
  const res = await fetch(path, {
    method: 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    signal: init?.signal,
  });
  return handleResponse<T>(res);
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(res);
}
