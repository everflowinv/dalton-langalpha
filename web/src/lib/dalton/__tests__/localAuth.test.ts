import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/supabase', () => ({ supabase: null }));

import {
  bootstrapFirstAdmin,
  fetchBootstrapStatus,
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
});
