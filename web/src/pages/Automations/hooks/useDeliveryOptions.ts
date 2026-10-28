import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { apiErrorStatus } from '@/api/errors';
import type { DeliveryOptions } from '@/types/automation';
import { getDeliveryOptions } from '../utils/api';

interface DeliveryOptionsArgs {
  /** The workspace the automation runs in, Home's id included. */
  workspaceId: string | null | undefined;
  /** Off where nothing on screen reads the options. */
  enabled?: boolean;
}

export interface DeliveryOptionsState {
  options: DeliveryOptions | undefined;
  error: unknown;
  /** The options are still the previous workspace's, until this one's load. */
  isPlaceholderData: boolean;
  /** No answer yet, for this workspace or any before it. */
  isPending: boolean;
  /** The workspace whose default an app-only entry follows, as the server
   *  answered: null for a run in no workspace or in Home. */
  workspaceId: string | null;
}

/**
 * The chats an automation can deliver to, asked with the workspace it runs
 * in. The workspace whose default applies is the server's answer, null for a
 * run in Home, which has no default chat.
 */
export function useDeliveryOptions({ workspaceId, enabled = true }: DeliveryOptionsArgs): DeliveryOptionsState {
  const asked = workspaceId || null;

  const query = useQuery({
    queryKey: queryKeys.automationDelivery.options(asked),
    queryFn: async () => (await getDeliveryOptions(asked)).data,
    // A form with no place picked yet asks nothing.
    enabled: enabled && !!asked,
    staleTime: 60_000,
    // A workspace switch keeps the picker up while the new one's chats load.
    placeholderData: keepPreviousData,
    // A refusal or an unavailable service answers the same on a retry; only
    // a request that never got an answer is asked again.
    retry: (count, err) => count < 1 && apiErrorStatus(err) === null,
  });

  return {
    options: query.data,
    error: query.error,
    isPlaceholderData: query.isPlaceholderData,
    isPending: query.isPending,
    workspaceId: query.data?.workspace_id ?? null,
  };
}
