import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/supabase', () => ({ supabase: null }));

import {
  bootstrapFirstAdmin,
  fetchBootstrapStatus,
  fetchLocalWsTicket,
  localChangePassword,
  localLogin,
  localLogout,
  localRefresh,
  LocalAuthError,
  parseJwtSubject,
} from '../localAuth';

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.sig`;
}

describe('localAuth HTTP client', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('parses JWT sub claim without verifying signature', () => {
    const token = makeJwt({ sub: 'user-123', exp: 9999999999 });
    expect(parseJwtSubject(token)).toBe('user-123');
    expect(parseJwtSubject('not-a-jwt')).toBeNull();
  });

  it('posts credentials to /api/v1/dalton/auth/local/login with credentials: include', async () => {
    const token = makeJwt({ sub: 'u-1' });
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: token,
          token_type: 'bearer',
          expires_in: 900,
          user: { user_id: 'u-1', email: 'a@example.com', tier: 'admin' },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const res = await localLogin('a@example.com', 'supersecret1');
    expect(res.access_token).toBe(token);
    expect(res.user.user_id).toBe('u-1');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/dalton/auth/local/login',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
      }),
    );
  });

  it('returns null from localRefresh on 401 and retries once on 409 concurrent rotation', async () => {
    const rotated = makeJwt({ sub: 'u-2' });
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'conflict' }), { status: 409 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ access_token: rotated, token_type: 'bearer', expires_in: 900 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );

    const refreshed = await localRefresh();
    expect(refreshed?.access_token).toBe(rotated);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 401 }));
    expect(await localRefresh()).toBeNull();
  });

  it('sends Bearer token on localChangePassword and raises LocalAuthError on failure', async () => {
    const token = makeJwt({ sub: 'u-3' });
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Current password is incorrect.' }), { status: 401 }),
    );

    await expect(localChangePassword(token, 'wrong', 'newpassword123')).rejects.toBeInstanceOf(
      LocalAuthError,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/dalton/auth/local/change-password',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({ Authorization: `Bearer ${token}` }),
      }),
    );
  });

  it('fetches bootstrap status, bootstraps first admin, and posts logout', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ needs_bootstrap: true }), { status: 200 }),
    );
    expect(await fetchBootstrapStatus()).toEqual({ needs_bootstrap: true });

    const adminToken = makeJwt({ sub: 'admin-1' });
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          access_token: adminToken,
          token_type: 'bearer',
          expires_in: 900,
          user: { user_id: 'admin-1', email: 'admin@example.com', tier: 'admin' },
        }),
        { status: 201 },
      ),
    );
    const created = await bootstrapFirstAdmin({
      bootstrap_token: 'setup-secret',
      email: 'admin@example.com',
      password: 'longpassword123',
    });
    expect(created.access_token).toBe(adminToken);

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await localLogout();
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/dalton/auth/local/logout',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
  });

  it('stores local access token strictly in memory in authToken.ts without touching localStorage', async () => {
    vi.stubEnv('VITE_DALTON_AUTH', 'local');
    vi.stubEnv('VITE_SUPABASE_URL', '');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', '');
    vi.resetModules();
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    const authToken = await import('@/lib/authToken');
    const token = makeJwt({ sub: 'mem-only' });
    authToken.publishLocalToken(token, 'mem-only', 900);
    expect(await authToken.getAccessToken()).toBe(token);
    expect(setItemSpy).not.toHaveBeenCalled();
    authToken.publishLocalToken(null, null);
    expect(setItemSpy).not.toHaveBeenCalled();
    setItemSpy.mockRestore();
  });

  it('throws LocalAuthError("登出未完成，请重试") on 500 or network failure, and succeeds on 401', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 500 }));
    await expect(localLogout()).rejects.toMatchObject({
      name: 'LocalAuthError',
      message: '登出未完成，请重试',
      status: 500,
    });

    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(localLogout()).rejects.toMatchObject({
      name: 'LocalAuthError',
      message: '登出未完成，请重试',
      status: 0,
    });

    // 401 means session is already revoked on server -> treat as completed
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 401 }));
    await expect(localLogout()).resolves.toBeUndefined();
  });

  it('fetches short-lived HMAC WebSocket ticket via /api/v1/dalton/auth/local/ws-ticket', async () => {
    const token = makeJwt({ sub: 'u-ws' });
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ticket: 'payload.sig', expires_in: 60 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const res = await fetchLocalWsTicket(token);
    expect(res).toEqual({ ticket: 'payload.sig', expires_in: 60 });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/dalton/auth/local/ws-ticket',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({ Authorization: `Bearer ${token}` }),
      }),
    );
  });

  it('clears state and fires expired handler when cross-tab cookie refresh returns a different JWT sub', async () => {
    vi.stubEnv('VITE_DALTON_AUTH', 'local');
    vi.stubEnv('VITE_SUPABASE_URL', '');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', '');
    vi.resetModules();

    const authToken = await import('@/lib/authToken');
    const expiredSpy = vi.fn();
    authToken.setLocalAuthExpiredHandler(expiredSpy);

    const userAToken = makeJwt({ sub: 'user-a' });
    const userBToken = makeJwt({ sub: 'user-b' });
    // Publish token for user-a that is already expired (expiresInSeconds = 0)
    authToken.publishLocalToken(userAToken, 'user-a', 0);

    // Sibling tab logged in as user-b, so cookie refresh returns user-b's JWT
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ access_token: userBToken, token_type: 'bearer', expires_in: 900 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const rotated = await authToken.refreshAccessToken(userAToken);
    expect(rotated).toBeNull();
    expect(expiredSpy).toHaveBeenCalledTimes(1);
    expect(await authToken.getAccessToken()).toBeNull();
    authToken.setLocalAuthExpiredHandler(null);
  });

  it('does not replay SSE 401 request after account switches during refresh', async () => {
    vi.stubEnv('VITE_DALTON_AUTH', 'local');
    vi.stubEnv('VITE_SUPABASE_URL', '');
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', '');
    vi.resetModules();

    const authToken = await import('@/lib/authToken');
    const { runAuthResets } = await import('@/lib/authResets');
    const { streamFetch } = await import('@/pages/ChatAgent/utils/api/transport');

    const userAToken = makeJwt({ sub: 'user-a' });
    const userBToken = makeJwt({ sub: 'user-b' });
    authToken.publishLocalToken(userAToken, 'user-a', 900);

    // 1st fetch: SSE returns 401; 2nd fetch: /refresh returns user-b while session resets
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'expired' }), { status: 401 }))
      .mockImplementationOnce(async () => {
        runAuthResets();
        authToken.publishLocalToken(userBToken, 'user-b', 900);
        return new Response(
          JSON.stringify({ access_token: userBToken, token_type: 'bearer', expires_in: 900 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      });

    await expect(
      streamFetch(
        '/api/v1/threads/t1/messages',
        { method: 'POST', headers: { Authorization: `Bearer ${userAToken}` } },
        () => undefined,
      ),
    ).rejects.toMatchObject({ status: 401 });

    // Only the initial SSE POST and the refresh call ran — NO retry POST as user-b!
    const sseCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).includes('/api/v1/threads/t1/messages'),
    );
    expect(sseCalls).toHaveLength(1);
  });
});
