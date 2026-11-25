import { useCallback, useContext, useEffect, useMemo, useSyncExternalStore } from 'react';
import {
  QueryClient,
  QueryClientContext,
  useQuery,
  type QueryState,
} from '@tanstack/react-query';
import { api } from '@/api/client';
import { isClientError } from '@/api/errors';
import { isDaltonBuild } from '@/config/brand';
import * as daltonAuthModule from '@/config/daltonAuth';
import { isPlatformMode } from '@/config/hostMode';
import { hasAuthEnv, LOCAL_USER_ID } from '@/config/localUser';
import * as authModule from '@/contexts/AuthContext';
import * as teamCredentialsModule from '@/hooks/useTeamCredentials';
import type { TeamCredentialsStatus } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';

export type CallerTier = 'admin' | 'internal' | 'external' | 'unknown';
export type CallerIdentityStatus = 'loading' | 'ready' | 'error';
export type ServerDaltonAuthMode = 'supabase' | 'local' | 'oss' | 'platform';

export interface CallerIdentitySnapshot {
  tier: CallerTier;
  status: CallerIdentityStatus;
  authMode: ServerDaltonAuthMode | null;
  /** Upstream platform mode (`!isDaltonBuild || isPlatformMode`). */
  isPlatform: boolean;
  /** Initial server confirmation completed with a valid tier. */
  isKnown: boolean;
  /** True during initial load or while any instance sharing `(queryClient, queryKey)` is force-revalidating. */
  isConfirming: boolean;
  isMultiUserAuth: boolean;
  isSingleUserOss: boolean;
  /** Semantic member-tier booleans (resource ownership is still checked orthogonally by callers). */
  isAdmin: boolean;
  isInternal: boolean;
  isExternal: boolean;
  canActAsOwner: boolean;
  canPublishPublic: boolean;
  canInvite: boolean;
  /** Team credentials fields preserved with existing semantics (decoupled from `isAdmin`). */
  isTeamCredentialsShared: boolean;
  canManageCredentials: boolean;
  isPm: boolean;
  colleagueSharingEnabled: boolean;
}

export interface CallerIdentity extends CallerIdentitySnapshot {
  /**
   * Synchronously reads `queryClient.getQueryState(queryKey)` and the shared
   * revalidation registry, computing the latest snapshot via the same pure
   * resolver without waiting for React to re-render.
   */
  getLatestIdentity: () => CallerIdentitySnapshot;
  /** Synchronous shorthand for `getLatestIdentity().isConfirming`. */
  isConfirmingNow: () => boolean;
  /**
   * Synchronously marks shared `entry.isConfirming = true` and notifies all
   * subscribers for `(queryClient, queryKey)`, cancels any in-flight request
   * (aborting its `AbortSignal`), and forces a fresh network fetch (`staleTime: 0`).
   */
  revalidate: () => Promise<void>;
}

export interface UseCallerIdentityOptions {
  /**
   * When true (alongside `open !== false`), triggers `revalidate()` on mount /
   * open and on window return-to-foreground (`visibilitychange` / `focus`),
   * plus silent 60s background refresh while open.
   */
  revalidateOnOpen?: boolean;
  /** Paired with `revalidateOnOpen` for dialogs/popovers that stay mounted while closed. */
  open?: boolean;
}

interface RevalidationState {
  activeSeq: number;
  isConfirming: boolean;
  listeners: Set<() => void>;
}

const revalidationRegistry = new WeakMap<QueryClient, Map<string, RevalidationState>>();

const FALLBACK_QUERY_CLIENT = new QueryClient({
  defaultOptions: {
    queries: {
      enabled: false,
      retry: false,
    },
  },
});

const PLATFORM_READY_SNAPSHOT: CallerIdentitySnapshot = Object.freeze({
  tier: 'admin',
  status: 'ready',
  authMode: 'platform',
  isPlatform: true,
  isKnown: true,
  isConfirming: false,
  isMultiUserAuth: false,
  isSingleUserOss: false,
  isAdmin: true,
  isInternal: true,
  isExternal: false,
  canActAsOwner: true,
  canPublishPublic: true,
  canInvite: false,
  isTeamCredentialsShared: false,
  canManageCredentials: true,
  isPm: true,
  colleagueSharingEnabled: false,
});

const LOADING_SNAPSHOT: CallerIdentitySnapshot = Object.freeze({
  tier: 'unknown',
  status: 'loading',
  authMode: null,
  isPlatform: false,
  isKnown: false,
  isConfirming: true,
  isMultiUserAuth: false,
  isSingleUserOss: false,
  isAdmin: false,
  isInternal: false,
  isExternal: false,
  canActAsOwner: false,
  canPublishPublic: false,
  canInvite: false,
  isTeamCredentialsShared: false,
  canManageCredentials: false,
  isPm: false,
  colleagueSharingEnabled: false,
});

const ERROR_SNAPSHOT: CallerIdentitySnapshot = Object.freeze({
  tier: 'unknown',
  status: 'error',
  authMode: null,
  isPlatform: false,
  isKnown: false,
  isConfirming: false,
  isMultiUserAuth: false,
  isSingleUserOss: false,
  isAdmin: false,
  isInternal: false,
  isExternal: false,
  canActAsOwner: false,
  canPublishPublic: false,
  canInvite: false,
  isTeamCredentialsShared: false,
  canManageCredentials: false,
  isPm: false,
  colleagueSharingEnabled: false,
});

function keyIdOf(queryKey: readonly unknown[]): string {
  return JSON.stringify(queryKey);
}

function getRevalidationEntry(
  qc: QueryClient,
  keyId: string,
  create: true,
): RevalidationState;
function getRevalidationEntry(
  qc: QueryClient,
  keyId: string,
  create?: false,
): RevalidationState | undefined;
function getRevalidationEntry(
  qc: QueryClient,
  keyId: string,
  create = false,
): RevalidationState | undefined {
  let byKey = revalidationRegistry.get(qc);
  if (!byKey) {
    if (!create) return undefined;
    byKey = new Map();
    revalidationRegistry.set(qc, byKey);
  }
  let entry = byKey.get(keyId);
  if (!entry && create) {
    entry = { activeSeq: 0, isConfirming: false, listeners: new Set() };
    byKey.set(keyId, entry);
  }
  return entry;
}

function cleanupRevalidationEntry(
  qc: QueryClient,
  keyId: string,
  entry: RevalidationState,
): void {
  if (entry.listeners.size === 0 && !entry.isConfirming) {
    const byKey = revalidationRegistry.get(qc);
    if (byKey?.get(keyId) === entry) {
      byKey.delete(keyId);
    }
  }
}

function notifyRevalidationListeners(entry: RevalidationState): void {
  for (const listener of Array.from(entry.listeners)) {
    listener();
  }
}

function parseValidTier(raw: unknown): 'admin' | 'internal' | 'external' | null {
  if (raw === 'admin' || raw === 'internal' || raw === 'external') {
    return raw;
  }
  return null;
}

export interface SnapshotQueryLike {
  status?: QueryState<TeamCredentialsStatus>['status'];
  data?: TeamCredentialsStatus;
  error?: unknown;
}

/**
 * Pure snapshot resolver shared by render state and synchronous `getLatestIdentity()`.
 */
export function resolveCallerIdentitySnapshot({
  isPlatformIdentityMode,
  isMisconfiguredSupabase,
  queryState,
  isRevalidating,
}: {
  isPlatformIdentityMode: boolean;
  isMisconfiguredSupabase: boolean;
  queryState: SnapshotQueryLike | undefined;
  isRevalidating: boolean;
}): CallerIdentitySnapshot {
  if (isPlatformIdentityMode) {
    return PLATFORM_READY_SNAPSHOT;
  }
  if (isMisconfiguredSupabase) {
    return ERROR_SNAPSHOT;
  }
  if (!queryState) {
    return LOADING_SNAPSHOT;
  }
  if (queryState.status === 'error') {
    return isRevalidating ? { ...ERROR_SNAPSHOT, isConfirming: true } : ERROR_SNAPSHOT;
  }
  if (queryState.status === 'pending' || queryState.data === undefined) {
    return LOADING_SNAPSHOT;
  }

  const data = queryState.data;
  const isTeamCredentialsShared = Boolean(data.enabled);
  const isPm = Boolean(data.is_pm);
  const canManageCredentials = !isTeamCredentialsShared || isPm;
  const colleagueSharingEnabled = Boolean(data.colleague_sharing_enabled);

  if (data.auth_mode === 'oss') {
    return {
      tier: 'admin',
      status: 'ready',
      authMode: 'oss',
      isPlatform: false,
      isKnown: true,
      isConfirming: isRevalidating,
      isMultiUserAuth: false,
      isSingleUserOss: true,
      isAdmin: true,
      isInternal: true,
      isExternal: false,
      canActAsOwner: true,
      canPublishPublic: true,
      canInvite: false,
      isTeamCredentialsShared,
      canManageCredentials,
      isPm,
      colleagueSharingEnabled,
    };
  }

  const tier = parseValidTier(data.caller_tier);
  if (!tier) {
    return isRevalidating ? { ...ERROR_SNAPSHOT, isConfirming: true } : ERROR_SNAPSHOT;
  }

  const daltonAuthMode = getDaltonAuthMode();
  const authMode: ServerDaltonAuthMode | null =
    data.auth_mode === 'supabase' || data.auth_mode === 'local'
      ? data.auth_mode
      : daltonAuthMode === 'supabase' || daltonAuthMode === 'local'
        ? daltonAuthMode
        : null;
  const isAdmin = tier === 'admin';
  const isInternal = tier === 'admin' || tier === 'internal';
  const isExternal = tier === 'external';
  const canInvite = isInternal && Boolean(data.can_invite);

  return {
    tier,
    status: 'ready',
    authMode,
    isPlatform: false,
    isKnown: true,
    isConfirming: isRevalidating,
    isMultiUserAuth: true,
    isSingleUserOss: false,
    isAdmin,
    isInternal,
    isExternal,
    canActAsOwner: isInternal,
    canPublishPublic: isAdmin,
    canInvite,
    isTeamCredentialsShared,
    canManageCredentials,
    isPm,
    colleagueSharingEnabled,
  };
}

function fallbackFetchTeamCredentialsStatus(context?: {
  signal?: AbortSignal;
}): Promise<TeamCredentialsStatus> {
  return api
    .get<TeamCredentialsStatus>('/api/v1/dalton/credentials/status', {
      signal: context?.signal,
    })
    .then((r) => r.data);
}

function getDaltonAuthMode(): string {
  const mod = daltonAuthModule as Partial<typeof daltonAuthModule>;
  if ('DALTON_AUTH_MODE' in mod && typeof mod.DALTON_AUTH_MODE === 'string') {
    return mod.DALTON_AUTH_MODE;
  }
  if ('isDaltonLocalAuth' in mod && mod.isDaltonLocalAuth) {
    return 'local';
  }
  if ('isDaltonSupabaseAuth' in mod && mod.isDaltonSupabaseAuth) {
    return 'supabase';
  }
  return 'isDaltonAuth' in mod && mod.isDaltonAuth ? 'local' : 'oss';
}

function getIsDaltonAuth(): boolean {
  const mod = daltonAuthModule as Partial<typeof daltonAuthModule>;
  return 'isDaltonAuth' in mod ? Boolean(mod.isDaltonAuth) : false;
}

function getCredentialsQueryFn(): (context?: {
  signal?: AbortSignal;
}) => Promise<TeamCredentialsStatus> {
  const mod = teamCredentialsModule as Partial<typeof teamCredentialsModule>;
  return 'fetchTeamCredentialsStatus' in mod &&
    typeof mod.fetchTeamCredentialsStatus === 'function'
    ? mod.fetchTeamCredentialsStatus
    : fallbackFetchTeamCredentialsStatus;
}

function useSafeAuthUserId(): string | null {
  try {
    return authModule.useAuth().userId ?? null;
  } catch {
    return LOCAL_USER_ID;
  }
}

export function useCallerIdentity(options?: UseCallerIdentityOptions): CallerIdentity {
  const contextClient = useContext(QueryClientContext);
  const queryClient = contextClient ?? FALLBACK_QUERY_CLIENT;
  const userId = useSafeAuthUserId();

  // Upstream platform mode (`!isDaltonBuild || isPlatformMode`), while still letting
  // Dalton unit tests that toggle `isDaltonAuth` without mocking `brand.ts` run the
  // Dalton identity path.
  const daltonAuthMode = getDaltonAuthMode();
  const isDaltonAuthFlag = getIsDaltonAuth();
  const isPlatformIdentityMode =
    isPlatformMode ||
    (!isDaltonBuild &&
      !isDaltonAuthFlag &&
      daltonAuthMode !== 'supabase' &&
      daltonAuthMode !== 'local');
  const isMisconfiguredSupabase =
    !isPlatformIdentityMode && daltonAuthMode === 'supabase' && !hasAuthEnv;
  const isSensitiveOpen =
    !isPlatformIdentityMode &&
    !isMisconfiguredSupabase &&
    Boolean(options?.revalidateOnOpen) &&
    (options?.open ?? true);

  const queryKey = useMemo(
    () => queryKeys.dalton.credentialsStatus(userId),
    [userId],
  );
  const keyId = useMemo(() => keyIdOf(queryKey), [queryKey]);

  const queryEnabled =
    !isPlatformIdentityMode &&
    !isMisconfiguredSupabase &&
    contextClient !== undefined &&
    Boolean(userId);

  const credentialsQueryFn = getCredentialsQueryFn();
  const query = useQuery<TeamCredentialsStatus>(
    {
      queryKey,
      queryFn: credentialsQueryFn,
      enabled: queryEnabled,
      staleTime: 5 * 60_000,
      retry: (failureCount, err) => failureCount < 1 && !isClientError(err),
      refetchOnWindowFocus: isSensitiveOpen ? 'always' : false,
      refetchInterval: isSensitiveOpen ? 60_000 : false,
      refetchIntervalInBackground: false,
    },
    queryClient,
  );

  const subscribeConfirming = useCallback(
    (listener: () => void) => {
      const entry = getRevalidationEntry(queryClient, keyId, true);
      entry.listeners.add(listener);
      return () => {
        entry.listeners.delete(listener);
        cleanupRevalidationEntry(queryClient, keyId, entry);
      };
    },
    [queryClient, keyId],
  );

  const getConfirmingSnapshot = useCallback(
    () => getRevalidationEntry(queryClient, keyId)?.isConfirming ?? false,
    [queryClient, keyId],
  );

  const isRevalidating = useSyncExternalStore(
    subscribeConfirming,
    getConfirmingSnapshot,
    getConfirmingSnapshot,
  );

  const revalidate = useCallback(async (): Promise<void> => {
    if (isPlatformIdentityMode || isMisconfiguredSupabase || contextClient === undefined || !userId) {
      return;
    }
    const entry = getRevalidationEntry(queryClient, keyId, true);
    const seq = ++entry.activeSeq;
    entry.isConfirming = true;
    notifyRevalidationListeners(entry);
    try {
      await queryClient.cancelQueries({ queryKey });
      await queryClient.fetchQuery({
        queryKey,
        queryFn: getCredentialsQueryFn(),
        staleTime: 0,
        retry: false,
      });
    } catch {
      // React Query has recorded the failure state on the query entry;
      // callers inspect status === 'error' via hook state or getLatestIdentity().
    } finally {
      if (entry.activeSeq === seq) {
        entry.isConfirming = false;
        notifyRevalidationListeners(entry);
        cleanupRevalidationEntry(queryClient, keyId, entry);
      }
    }
  }, [
    isPlatformIdentityMode,
    isMisconfiguredSupabase,
    contextClient,
    userId,
    queryClient,
    keyId,
    queryKey,
  ]);

  useEffect(() => {
    if (!isSensitiveOpen) return;
    void revalidate();
  }, [isSensitiveOpen, revalidate]);

  useEffect(() => {
    if (!isSensitiveOpen || typeof window === 'undefined' || typeof document === 'undefined') {
      return;
    }
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        void revalidate();
      }
    };
    const onFocus = () => {
      void revalidate();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('focus', onFocus);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onFocus);
    };
  }, [isSensitiveOpen, revalidate]);

  const getLatestIdentity = useCallback((): CallerIdentitySnapshot => {
    const state = queryClient.getQueryState<TeamCredentialsStatus>(queryKey);
    const confirming = getRevalidationEntry(queryClient, keyId)?.isConfirming ?? false;
    return resolveCallerIdentitySnapshot({
      isPlatformIdentityMode,
      isMisconfiguredSupabase,
      queryState: state,
      isRevalidating: confirming,
    });
  }, [
    queryClient,
    queryKey,
    keyId,
    isPlatformIdentityMode,
    isMisconfiguredSupabase,
  ]);

  const isConfirmingNow = useCallback(
    () => getLatestIdentity().isConfirming,
    [getLatestIdentity],
  );

  const snapshot = useMemo(
    () =>
      resolveCallerIdentitySnapshot({
        isPlatformIdentityMode,
        isMisconfiguredSupabase,
        queryState: {
          status: query.status,
          data: query.data,
          error: query.error,
        },
        isRevalidating,
      }),
    [
      isPlatformIdentityMode,
      isMisconfiguredSupabase,
      query.status,
      query.data,
      query.error,
      isRevalidating,
    ],
  );

  return useMemo<CallerIdentity>(
    () => ({
      ...snapshot,
      getLatestIdentity,
      isConfirmingNow,
      revalidate,
    }),
    [snapshot, getLatestIdentity, isConfirmingNow, revalidate],
  );
}
