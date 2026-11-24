import React, { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { AuthResponse } from '@supabase/supabase-js';
import { AuthContext, type AuthContextValue } from '@/contexts/AuthContext';
import { toast } from '@/components/ui/use-toast';
import {
  clearAuthToken,
  getAccessToken,
  publishLocalToken,
  setLocalAuthExpiredHandler,
} from '@/lib/authToken';
import { runAuthResets } from '@/lib/authResets';
import { queryKeys } from '@/lib/queryKeys';
import { setSentryUser } from '@/lib/sentry';
import { resetSharedWorkspaceThreads } from '@/lib/navThreadsStore';
import { resetNavPanelExpansion } from '@/pages/ChatAgent/components/navExpansionStore';
import { resetStableNavOrder } from '@/pages/ChatAgent/hooks/navOrderFreeze';
import {
  bootstrapFirstAdmin,
  fetchBootstrapStatus,
  localChangePassword,
  localLogin,
  localLogout,
  localRefresh,
  LocalAuthError,
  parseJwtSubject,
  type BootstrapAdminPayload,
} from './localAuth';

/**
 * Auth provider for standalone core local accounts (`VITE_DALTON_AUTH=local`).
 *
 * - Access tokens live strictly in memory (`lib/authToken.ts`).
 * - Sessions persist across page reloads via the HttpOnly `dalton_refresh_token` cookie.
 * - Probes `/api/v1/dalton/bootstrap/status` on startup so fresh installs route to `/setup/admin`.
 */
export function LocalAuthProvider({ children }: { children: React.ReactNode }) {
  const [userId, setUserId] = useState<string | null>(null);
  const [isInitialized, setIsInitialized] = useState(false);
  const [needsBootstrap, setNeedsBootstrap] = useState(false);
  const queryClient = useQueryClient();

  const clearLocalSessionState = useCallback(() => {
    clearAuthToken();
    setUserId(null);
    setSentryUser(null);
    queryClient.clear();
    resetNavPanelExpansion();
    resetStableNavOrder();
    resetSharedWorkspaceThreads();
    runAuthResets();
  }, [queryClient]);

  useEffect(() => {
    setLocalAuthExpiredHandler(clearLocalSessionState);
    return () => setLocalAuthExpiredHandler(null);
  }, [clearLocalSessionState]);

  useEffect(() => {
    let active = true;

    (async () => {
      try {
        const status = await fetchBootstrapStatus();
        if (!active) return;
        if (status.needs_bootstrap) {
          clearAuthToken();
          setNeedsBootstrap(true);
          setUserId(null);
          setSentryUser(null);
          setIsInitialized(true);
          return;
        }
        setNeedsBootstrap(false);
      } catch (err) {
        console.error('[auth] bootstrap status probe failed:', err);
        if (!active) return;
        setNeedsBootstrap(false);
      }

      try {
        const refreshed = await localRefresh();
        if (!active) return;
        if (refreshed?.access_token) {
          const uid = parseJwtSubject(refreshed.access_token);
          publishLocalToken(refreshed.access_token, uid, refreshed.expires_in);
          setUserId(uid);
          setSentryUser(uid);
          queryClient.invalidateQueries({ queryKey: queryKeys.user.all });
        } else {
          clearAuthToken();
          setUserId(null);
          setSentryUser(null);
        }
      } catch (err) {
        console.error('[auth] local session bootstrap refresh failed:', err);
        if (!active) return;
        clearAuthToken();
        setUserId(null);
        setSentryUser(null);
      } finally {
        if (active) setIsInitialized(true);
      }
    })();

    return () => {
      active = false;
    };
  }, [queryClient]);

  const loginWithEmail = useCallback(
    async (identifier: string, password: string): Promise<AuthResponse> => {
      const res = await localLogin(identifier, password);
      const nextUid = res.user.user_id;
      if (userId && userId !== nextUid) {
        queryClient.clear();
        resetNavPanelExpansion();
        resetStableNavOrder();
        resetSharedWorkspaceThreads();
        runAuthResets();
      }
      publishLocalToken(res.access_token, nextUid, res.expires_in);
      setNeedsBootstrap(false);
      setUserId(nextUid);
      setSentryUser(nextUid);
      queryClient.invalidateQueries({ queryKey: queryKeys.user.all });
      return { data: { user: null, session: null }, error: null } as unknown as AuthResponse;
    },
    [queryClient, userId],
  );

  const logout = useCallback(async () => {
    try {
      await localLogout(userId);
    } catch (err) {
      if (err instanceof LocalAuthError && err.status === 409) {
        toast({
          variant: 'destructive',
          title: '另一个标签页已切换账号，请刷新',
        });
        return;
      }
      toast({
        variant: 'destructive',
        title: '登出未完成，请重试',
      });
      return;
    }
    clearLocalSessionState();
  }, [clearLocalSessionState, userId]);

  const changePassword = useCallback(
    async (currentPassword: string, newPassword: string) => {
      const token = await getAccessToken();
      if (!token) {
        throw new Error('Authentication required');
      }
      await localChangePassword(token, currentPassword, newPassword);
      // Server revokes all sessions and clears the refresh cookie on password change.
      clearLocalSessionState();
    },
    [clearLocalSessionState],
  );

  const bootstrapAdmin = useCallback(
    async (payload: BootstrapAdminPayload) => {
      const res = await bootstrapFirstAdmin(payload);
      const nextUid = res.user.user_id;
      publishLocalToken(res.access_token, nextUid, res.expires_in);
      setNeedsBootstrap(false);
      setUserId(nextUid);
      setSentryUser(nextUid);
      queryClient.invalidateQueries({ queryKey: queryKeys.user.all });
    },
    [queryClient],
  );

  const markBootstrapCompleted = useCallback(() => {
    setNeedsBootstrap(false);
  }, []);

  const noop = useCallback(() => Promise.resolve(), []);

  const value: AuthContextValue = {
    userId,
    isInitialized,
    isLoggedIn: Boolean(userId),
    needsBootstrap,
    loginWithEmail,
    signupWithEmail: noop,
    loginWithProvider: noop,
    logout,
    sendMagicLink: noop,
    sendPasswordReset: noop,
    resendConfirmation: noop,
    verifyEmailOtp: noop,
    updatePassword: noop,
    changePassword,
    bootstrapAdmin,
    markBootstrapCompleted,
  };

  return <AuthContext value={value}>{children}</AuthContext>;
}
