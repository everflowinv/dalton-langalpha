import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { authGeneration, getAccessToken, refreshAccessToken } from '@/lib/authToken';

const authMocks = vi.hoisted(() => ({
  getAccessToken: vi.fn<typeof getAccessToken>(),
  refreshAccessToken: vi.fn<typeof refreshAccessToken>(),
  authGeneration: vi.fn<typeof authGeneration>(() => 1),
}));

vi.mock('@/lib/authToken', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/authToken')>();
  return {
    ...actual,
    getAccessToken: (...args: Parameters<typeof getAccessToken>) =>
      authMocks.getAccessToken(...args),
    refreshAccessToken: (...args: Parameters<typeof refreshAccessToken>) =>
      authMocks.refreshAccessToken(...args),
    authGeneration: (...args: Parameters<typeof authGeneration>) =>
      authMocks.authGeneration(...args),
  };
});

import { ApiError } from '@/api/client';
import {
  DEPLOYMENT_SETTINGS_ENDPOINT,
  testDeploymentCredentials,
} from '@/lib/dalton/deploymentCredentials';

describe('testDeploymentCredentials end-to-end deadline & auth refresh handling', () => {
  const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    authMocks.getAccessToken.mockReset();
    authMocks.refreshAccessToken.mockReset();
    authMocks.authGeneration.mockReset();
    authMocks.authGeneration.mockReturnValue(1);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('times out a hung 401 token refresh under the total budget and blocks any late retry', async () => {
    authMocks.getAccessToken.mockResolvedValue('expired-token-1');

    let resolveRefresh!: (token: string | null) => void;
    authMocks.refreshAccessToken.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve;
        }),
    );

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'expired' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      }),
    );

    const probePromise = testDeploymentCredentials(
      'google_oauth',
      {
        DALTON_GOOGLE_OAUTH_CLIENT_ID: 'client-id-1',
        DALTON_GOOGLE_OAUTH_CLIENT_SECRET: 'test-secret-not-real-0001',
      },
      { timeoutMs: 200 },
    );
    const settled = probePromise.catch((err) => err);

    await vi.advanceTimersByTimeAsync(200);
    const err = await settled;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('ECONNABORTED');
    expect(authMocks.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Even when the hung refresh eventually resolves with a fresh token, no late retry POST is sent
    resolveRefresh('fresh-token-2');
    await vi.runAllTimersAsync();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('times out a hung getAccessToken lookup and suppresses any late initial POST', async () => {
    let resolveToken!: (token: string | null) => void;
    authMocks.getAccessToken.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveToken = resolve;
        }),
    );

    const probePromise = testDeploymentCredentials('feishu_app', {}, { timeoutMs: 150 });
    const settled = probePromise.catch((err) => err);

    await vi.advanceTimersByTimeAsync(150);
    const err = await settled;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('ECONNABORTED');
    expect(fetchMock).not.toHaveBeenCalled();

    // Resolving getAccessToken after timeout must not send a late probe request
    resolveToken('late-token-1');
    await vi.runAllTimersAsync();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('enforces a single total deadline across slow first 401, refresh, and slow retry', async () => {
    authMocks.getAccessToken
      .mockResolvedValueOnce('stale-token')
      .mockResolvedValueOnce('fresh-token');
    authMocks.refreshAccessToken.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          setTimeout(() => resolve('fresh-token'), 60);
        }),
    );

    let retrySignal: AbortSignal | undefined;
    fetchMock
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            setTimeout(
              () =>
                resolve(
                  new Response(JSON.stringify({ detail: 'expired' }), {
                    status: 401,
                    headers: { 'content-type': 'application/json' },
                  }),
                ),
              80,
            );
          }),
      )
      .mockImplementationOnce(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            retrySignal = init?.signal ?? undefined;
            init?.signal?.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            );
          }),
      );

    const probePromise = testDeploymentCredentials('google_oauth', {}, { timeoutMs: 200 });
    const settled = probePromise.catch((err) => err);

    // 80ms first 401 + 60ms refresh = 140ms; retry starts with only 60ms left in the 200ms total budget
    await vi.advanceTimersByTimeAsync(150);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toBe(`${DEPLOYMENT_SETTINGS_ENDPOINT}/test`);
    expect(retrySignal?.aborted).toBe(false);

    // At 200ms total elapsed, the retry is aborted and the call fails with ECONNABORTED
    await vi.advanceTimersByTimeAsync(50);
    const err = await settled;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('ECONNABORTED');
    expect(retrySignal?.aborted).toBe(true);
  });

  it('cancels immediately on caller AbortSignal and suppresses late 401 replay', async () => {
    authMocks.getAccessToken.mockResolvedValue('expired-token-3');
    let resolveRefresh!: (token: string | null) => void;
    authMocks.refreshAccessToken.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve;
        }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'expired' }), { status: 401 }),
    );

    const callerCtrl = new AbortController();
    const probePromise = testDeploymentCredentials(
      'feishu_app',
      {},
      { timeoutMs: 5_000, signal: callerCtrl.signal },
    );
    const settled = probePromise.catch((err) => err);

    await vi.advanceTimersByTimeAsync(0);
    callerCtrl.abort();
    const err = await settled;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('ERR_CANCELED');

    resolveRefresh('rotated-after-cancel');
    await vi.runAllTimersAsync();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
