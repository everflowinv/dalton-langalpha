// DALTON-HOOK(company-automations): paged agent and script reads. No poll and no prefetch.
import { useQuery, useQueryClient, type QueryObserverResult, type RefetchOptions } from '@tanstack/react-query';
import { apiErrorStatus } from '@/api/errors';
import { useAuth } from '@/contexts/AuthContext';
import { queryKeys } from '@/lib/queryKeys';
import type { AutomationHistory, AutomationRead, AutomationReadList, AutomationRunReadList } from '@/types/automation';
import {
  automationExecutionReadQuery,
  automationHistoryQuery,
  automationReadListQuery,
  automationRunReadQuery,
  getAutomationRead,
  listAutomationExecutionReads,
  listAutomationHistory,
  listAutomationReads,
  listAutomationRunReads,
  type AutomationReadListParams,
  type AutomationReadPageParams,
  type AutomationRunReadParams,
} from '../utils/api';

/** What `refetch` resolves to. A 403 sets `data` to undefined and `isSuccess` to
 *  false. That page is removed from this query, so a later failure cannot return it. */
export interface AutomationReadFetchResult<T> {
  data: T | undefined;
  error: Error | null;
  isError: boolean;
  isPending: boolean;
  isLoading: boolean;
  isLoadingError: boolean;
  isRefetchError: boolean;
  isFetching: boolean;
  isSuccess: boolean;
  status: QueryObserverResult<T, Error>['status'];
  fetchStatus: QueryObserverResult<T, Error>['fetchStatus'];
}

/** One page of one read. `data` is the server body, including `total` or `has_more`. */
export interface AutomationReadState<T> {
  data: T | undefined;
  error: Error | null;
  isError: boolean;
  isLoading: boolean;
  isFetching: boolean;
  isSuccess: boolean;
  refetch: (options?: RefetchOptions) => Promise<AutomationReadFetchResult<T>>;
}

export interface AutomationReadListArgs extends AutomationReadListParams {
  /** Off until the caller knows this read is allowed to start. */
  enabled?: boolean;
}

export interface AutomationReadArgs {
  enabled?: boolean;
}

export interface AutomationRunReadArgs extends AutomationRunReadParams {
  enabled?: boolean;
}

export interface AutomationReadPageArgs extends AutomationReadPageParams {
  enabled?: boolean;
}

function isForbidden(error: unknown): boolean {
  return apiErrorStatus(error) === 403;
}

/** The observer keeps the last page when a refetch fails. Copy a 403 result
 *  and drop that page. A 404 or 503 that never followed a 403 still carries
 *  its last successful page and is not success. */
function presentRefetch<T>(result: QueryObserverResult<T, Error>): AutomationReadFetchResult<T> {
  if (!isForbidden(result.error)) return result;
  return { ...result, data: undefined, isSuccess: false };
}

/**
 * A new scope, id, user, or page is a different key, so the previous page is
 * not kept. A 403 clears this query only while the rejected read is still
 * that query's active request: the cached page is dropped and the status is
 * the original 403. A canceled read that rejects later leaves the replacement
 * in place. A later success stores a new page.
 */
function useAutomationReadQuery<T>(
  queryKey: readonly unknown[],
  read: () => Promise<T>,
  enabled: boolean,
): AutomationReadState<T> {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey,
    enabled,
    queryFn: async () => {
      // `Query.fetch` assigns the retryer before this function runs, so the
      // promise here is this attempt. Do not read the query context `signal`:
      // that marks the abort as consumed and changes unmount rollback.
      const activeQuery = queryClient.getQueryCache().find({ queryKey, exact: true });
      const request = activeQuery?.promise;
      try {
        return await read();
      } catch (error) {
        const current = queryClient.getQueryCache().find({ queryKey, exact: true });
        if (
          isForbidden(error) &&
          error instanceof Error &&
          activeQuery &&
          request &&
          current === activeQuery &&
          activeQuery.promise === request
        ) {
          activeQuery.setState({
            data: undefined,
            dataUpdatedAt: 0,
            status: 'error',
            error,
          });
        }
        throw error;
      }
    },
  });
  const forbidden = isForbidden(query.error);
  return {
    data: forbidden ? undefined : query.data,
    error: query.error,
    isError: query.isError,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    isSuccess: query.isSuccess && !forbidden,
    refetch: (options) => query.refetch(options).then(presentRefetch),
  };
}

/** Empty, null, and undefined are the same missing id. The request is not built. */
function presentId(id: string | null | undefined): string | null {
  return id ? id : null;
}

/** One page of the task list. The caller passes `limit` and `offset`. */
export function useAutomationReadList(args: AutomationReadListArgs = {}): AutomationReadState<AutomationReadList> {
  const { userId } = useAuth();
  const { enabled, ...filters } = args;
  return useAutomationReadQuery(
    queryKeys.automations.readList(userId, automationReadListQuery(filters)),
    async () => (await listAutomationReads(filters)).data,
    (enabled ?? true) && !!userId,
  );
}

/** One task. An empty id does not request. */
export function useAutomationRead(
  automationId: string | null | undefined,
  args: AutomationReadArgs = {},
): AutomationReadState<AutomationRead> {
  const { userId } = useAuth();
  const id = presentId(automationId);
  return useAutomationReadQuery(
    queryKeys.automations.readDetail(userId, id ?? ''),
    async () => {
      if (!id) throw new Error('Missing automation id');
      return (await getAutomationRead(id)).data;
    },
    (args.enabled ?? true) && !!userId && id !== null,
  );
}

/** One page of the run feed. The caller passes `limit` and `offset`. */
export function useAutomationRunReads(args: AutomationRunReadArgs = {}): AutomationReadState<AutomationRunReadList> {
  const { userId } = useAuth();
  const { enabled, ...filters } = args;
  return useAutomationReadQuery(
    queryKeys.automations.readRuns(userId, automationRunReadQuery(filters)),
    async () => (await listAutomationRunReads(filters)).data,
    (enabled ?? true) && !!userId,
  );
}

/** One page of one task's runs. An empty id does not request. */
export function useAutomationExecutionReads(
  automationId: string | null | undefined,
  args: AutomationReadPageArgs = {},
): AutomationReadState<AutomationRunReadList> {
  const { userId } = useAuth();
  const { enabled, ...filters } = args;
  const id = presentId(automationId);
  return useAutomationReadQuery(
    queryKeys.automations.readExecutions(userId, id ?? '', automationExecutionReadQuery(filters)),
    async () => {
      if (!id) throw new Error('Missing automation id');
      return (await listAutomationExecutionReads(id, filters)).data;
    },
    (enabled ?? true) && !!userId && id !== null,
  );
}

/** One page of one task's modification history. An empty id does not request. */
export function useAutomationHistory(
  automationId: string | null | undefined,
  args: AutomationReadPageArgs = {},
): AutomationReadState<AutomationHistory> {
  const { userId } = useAuth();
  const { enabled, ...filters } = args;
  const id = presentId(automationId);
  return useAutomationReadQuery(
    queryKeys.automations.readHistory(userId, id ?? '', automationHistoryQuery(filters)),
    async () => {
      if (!id) throw new Error('Missing automation id');
      return (await listAutomationHistory(id, filters)).data;
    },
    (enabled ?? true) && !!userId && id !== null,
  );
}
