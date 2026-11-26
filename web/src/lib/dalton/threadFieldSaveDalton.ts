import type { QueryClient } from '@tanstack/react-query';
import { isDaltonBuild } from '@/config/brand';
import { queryKeys } from '@/lib/queryKeys';
import { threadFieldMutationKey, type ThreadField } from '@/lib/threadFieldMutations';
import type { Thread } from '@/types/api';
import type { ThreadUpdates } from '@/pages/ChatAgent/utils/api';

const confirmedEffortByClient = new WeakMap<
  QueryClient,
  Map<string, Thread['reasoning_effort']>
>();

/**
 * Returns the per-QueryClient map tracking server-confirmed `reasoning_effort`
 * while serialized `llm_model` / `reasoning_effort` PATCHes are in flight in
 * Dalton builds.
 */
export function getConfirmedEffortMap(
  queryClient: QueryClient,
): Map<string, Thread['reasoning_effort']> {
  let map = confirmedEffortByClient.get(queryClient);
  if (!map) {
    map = new Map<string, Thread['reasoning_effort']>();
    confirmedEffortByClient.set(queryClient, map);
  }
  return map;
}

/**
 * Checks whether any pending `llm_model` mutation on `threadId` also modifies
 * `reasoning_effort` (for example, an Antigravity switch atomically sending
 * `reasoning_effort: null`). Plain model switches do not touch `reasoning_effort`,
 * so a failed effort PATCH queued ahead of them rolls back immediately.
 */
export function hasPendingModelEffortUpdate(
  queryClient: QueryClient,
  threadId: string,
  field: ThreadField,
): boolean {
  if (!isDaltonBuild || field !== 'reasoning_effort') return false;
  return queryClient
    .getMutationCache()
    .findAll({ mutationKey: threadFieldMutationKey(threadId, 'llm_model'), status: 'pending' })
    .some((mutation) => {
      const vars = mutation.state.variables as { extraUpdates?: Record<string, unknown> } | undefined;
      return Boolean(vars?.extraUpdates && 'reasoning_effort' in vars.extraUpdates);
    });
}

/**
 * Computes the field + extraUpdates rollback payload for `useThreadFieldSave`'s
 * `onError` handler, respecting serialized `llm_model` / `reasoning_effort`
 * peer mutations and latest server-confirmed `reasoning_effort` in Dalton
 * builds. Returns `null` when rollback must be skipped.
 */
export function resolveDaltonFieldRollback<F extends ThreadField>(params: {
  queryClient: QueryClient;
  field: F;
  save: { threadId: string; extraUpdates?: Partial<ThreadUpdates> };
  restored: { value: Thread[F]; extra?: Partial<Thread> } | null | undefined;
}): { value: Thread[F]; extra?: Partial<Thread> } | null {
  const { queryClient, field, save, restored } = params;
  if (!restored) return null;
  if (!isDaltonBuild) return { value: restored.value, extra: restored.extra };
  if (hasPendingModelEffortUpdate(queryClient, save.threadId, field)) return null;

  const isModelOrEffortField = field === 'llm_model' || field === 'reasoning_effort';
  const effortMap = isModelOrEffortField ? getConfirmedEffortMap(queryClient) : null;
  const effortOut = field === 'llm_model'
    && queryClient.isMutating({ mutationKey: threadFieldMutationKey(save.threadId, 'reasoning_effort') }) > 0;

  const currentRow = queryClient.getQueryData<Thread>(queryKeys.threads.detail(save.threadId));
  const restoredValue = field === 'reasoning_effort' && effortMap?.has(save.threadId)
    ? (effortMap.get(save.threadId) as Thread[F])
    : restored.value;

  let restoredExtra = restored.extra;
  if (restoredExtra && 'reasoning_effort' in restoredExtra && effortMap?.has(save.threadId)) {
    restoredExtra = { ...restoredExtra, reasoning_effort: effortMap.get(save.threadId) };
  }

  const safeExtra = restoredExtra && save.extraUpdates && currentRow
    ? Object.fromEntries(
        Object.entries(restoredExtra).filter(
          ([k]) => !(effortOut && k === 'reasoning_effort')
            && currentRow[k as keyof Thread] === save.extraUpdates?.[k as keyof ThreadUpdates],
        ),
      ) as Partial<Thread>
    : restoredExtra && effortOut
      ? Object.fromEntries(Object.entries(restoredExtra).filter(([k]) => k !== 'reasoning_effort')) as Partial<Thread>
      : restoredExtra;

  return { value: restoredValue, extra: safeExtra };
}
