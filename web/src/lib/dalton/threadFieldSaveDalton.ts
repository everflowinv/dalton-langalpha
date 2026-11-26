import type { QueryClient } from '@tanstack/react-query';
import { isDaltonBuild } from '@/config/brand';
import { threadFieldMutationKey, type ThreadField } from '@/lib/threadFieldMutations';

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
