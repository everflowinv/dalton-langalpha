import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

vi.mock('@/lib/sentry', () => ({
  setSentryUser: vi.fn(),
}));

import { useAuth } from '@/contexts/AuthContext';
import { LocalAuthProvider } from '../LocalAuthProvider';

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.sig`;
}

describe('LocalAuthProvider logout error handling', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    mockToast.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('shows destructive toast and preserves local state when server logout fails', async () => {
    const token = makeJwt({ sub: 'u-local-1' });
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_bootstrap: false }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ access_token: token, token_type: 'bearer', expires_in: 900 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>
        <LocalAuthProvider>{children}</LocalAuthProvider>
      </QueryClientProvider>
    );

    const { result } = renderHook(() => useAuth(), { wrapper });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.isLoggedIn).toBe(true);
    expect(result.current.userId).toBe('u-local-1');

    // Server returns 500 on logout -> toast shown, local session preserved
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 500 }));
    await act(async () => {
      await result.current.logout();
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'destructive',
        title: '登出未完成，请重试',
      }),
    );
    expect(result.current.isLoggedIn).toBe(true);
    expect(result.current.userId).toBe('u-local-1');

    // Server returns 409 on stale-tab cross-account logout -> specific toast shown, state preserved
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Active cookie session belongs to another account' }), {
        status: 409,
      }),
    );
    await act(async () => {
      await result.current.logout();
    });
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/dalton/auth/local/logout',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ sub: 'u-local-1' }),
      }),
    );
    expect(mockToast).toHaveBeenLastCalledWith(
      expect.objectContaining({
        variant: 'destructive',
        title: '另一个标签页已切换账号，请刷新',
      }),
    );
    expect(result.current.isLoggedIn).toBe(true);
    expect(result.current.userId).toBe('u-local-1');

    // Retry succeeds -> local state clears
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ status: 'logged_out' }), { status: 200 }),
    );
    await act(async () => {
      await result.current.logout();
    });
    expect(result.current.isLoggedIn).toBe(false);
    expect(result.current.userId).toBeNull();
  });
});
