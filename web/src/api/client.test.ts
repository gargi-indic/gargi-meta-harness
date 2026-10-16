import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiGet, apiPost, ApiError } from './client';

describe('api client', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  it('apiGet returns json on success', async () => {
    const mockResponse = { ok: true, status: 200, json: async () => ({ hello: 'world' }) };
    vi.mocked(fetch).mockResolvedValueOnce(mockResponse as any);

    const result = await apiGet('/test');
    expect(result).toEqual({ hello: 'world' });
    expect(fetch).toHaveBeenCalledWith('/test', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: undefined,
    });
  });

  it('apiGet throws ApiError on 401 with detail', async () => {
    const mockResponse = {
      ok: false,
      status: 401,
      statusText: 'Unauthorized',
      json: async () => ({ detail: 'unauthorized' })
    };
    vi.mocked(fetch).mockResolvedValueOnce(mockResponse as any);

    try {
      await apiGet('/test');
      expect.fail('Should have thrown');
    } catch (err: any) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(401);
      expect(err.detail).toBe('unauthorized');
    }
  });

  it('apiGet throws ApiError using statusText when not json', async () => {
    const mockResponse = {
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => { throw new Error('Not JSON'); }
    };
    vi.mocked(fetch).mockResolvedValueOnce(mockResponse as any);

    try {
      await apiGet('/test');
      expect.fail('Should have thrown');
    } catch (err: any) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(500);
      expect(err.detail).toBe('Internal Server Error');
    }
  });

  it('apiPost sends JSON and credentials', async () => {
    const mockResponse = { ok: true, status: 200, json: async () => ({ ok: true }) };
    vi.mocked(fetch).mockResolvedValueOnce(mockResponse as any);

    const result = await apiPost('/test', { value: 123 });
    expect(result).toEqual({ ok: true });
    expect(fetch).toHaveBeenCalledWith('/test', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: 123 }),
    });
  });

  it('handles 204 No Content', async () => {
    const mockResponse = { ok: true, status: 204 };
    vi.mocked(fetch).mockResolvedValueOnce(mockResponse as any);

    const result = await apiGet('/test');
    expect(result).toBeUndefined();
  });
});
