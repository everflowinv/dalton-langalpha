import { useQuery } from '@tanstack/react-query';
import { isDaltonBuild } from '@/config/brand';
import { isDaltonAuth } from '@/config/daltonAuth';
import { featuresQuery, isFeatureEnabled } from '@/hooks/useFeatures';
import { useSharedWorkspaces } from '@/hooks/useSharedWorkspaces';
import { useUser } from '@/hooks/useUser';
import { useWorkspace } from '@/hooks/useWorkspace';
import { useTeamReadOnly } from '@/pages/ChatAgent/components/dalton/TeamSharing';
import type { Thread } from '@/types/api';

export type ThreadWriteStatus = 'writable' | 'shared' | 'unresolved';

export interface ThreadWriteAccess {
  /** Whether PATCHing thread model/tuning is confirmed allowed, confirmed shared/foreign (send-with-message), or still unresolved. */
  status: ThreadWriteStatus;
  /** True when status is 'unresolved' because a required permission read failed. */
  isError: boolean;
}

interface UseThreadWriteAccessOptions {
  workspaceId: string | null | undefined;
  liveThreadId: string | null;
  thread: Thread | undefined;
  threadError?: boolean;
  /** When false, skip upstream shared-workspace deferral (keeps non-Dalton builds identical to upstream). */
  checkUpstreamShared?: boolean;
}

/**
 * Determine whether the current user may PATCH thread-level settings (model,
 * reasoning effort, fast mode) or must keep changes local and send them with
 * each message.
 *
 * Covers both:
 * 1. Upstream shared workspaces (`shared_workspaces` feature flag + org +
 *    shared list), distinguishing "flag/user/list not yet loaded" (unresolved)
 *    from "confirmed personal" (writable) and "confirmed shared" (shared).
 * 2. Dalton team sharing (`isDaltonAuth`), mirroring backend
 *    `require_thread_owner` (`COALESCE(t.user_id, w.user_id) === current_user_id`)
 *    and ChatView's colleague/foreign-thread checks.
 */
export function useThreadWriteAccess({
  workspaceId,
  liveThreadId,
  thread,
  threadError = false,
  checkUpstreamShared = isDaltonBuild,
}: UseThreadWriteAccessOptions): ThreadWriteAccess {
  const hasWorkspace = Boolean(workspaceId);

  const sharedWorkspaces = useSharedWorkspaces();
  const needUpstreamMeta = checkUpstreamShared && hasWorkspace && !sharedWorkspaces.enabled;
  const features = useQuery({ ...featuresQuery, enabled: needUpstreamMeta });
  const needUser = needUpstreamMeta || (hasWorkspace && isDaltonAuth);
  const { user: currentUser, isError: userError } = useUser({ enabled: needUser });

  const { data: activeWorkspace, isError: workspaceError } = useWorkspace(
    hasWorkspace && isDaltonAuth ? workspaceId : null,
  );
  const teamReadOnly = useTeamReadOnly(
    hasWorkspace && isDaltonAuth ? workspaceId : null,
    activeWorkspace,
  );

  // Confirmed shared directly from the thread detail (`thread.shared` is present
  // only on shared-workspace threads) — never depends on features/account/list.
  if (checkUpstreamShared && Boolean(thread?.shared)) {
    return { status: 'shared', isError: false };
  }

  if (!hasWorkspace) {
    return { status: 'writable', isError: false };
  }

  let upstreamStatus: ThreadWriteStatus = 'writable';
  let upstreamError = false;
  if (checkUpstreamShared) {
    if (sharedWorkspaces.enabled) {
      if (sharedWorkspaces.data === undefined) {
        upstreamStatus = 'unresolved';
        upstreamError = sharedWorkspaces.isError;
      } else if (sharedWorkspaces.data.workspaces.some((e) => e.workspace.workspace_id === workspaceId)) {
        upstreamStatus = 'shared';
      }
    } else if (features.data === undefined) {
      upstreamStatus = 'unresolved';
      upstreamError = features.isError;
    } else if (isFeatureEnabled(features.data, 'shared_workspaces') && currentUser === null) {
      upstreamStatus = 'unresolved';
      upstreamError = userError;
    }
  }

  let daltonStatus: ThreadWriteStatus = 'writable';
  let daltonError = false;
  if (isDaltonAuth) {
    const currentUserId = currentUser?.user_id ?? null;
    const threadUserId = thread?.thread_user_id ?? null;
    const workspaceOwnerId = thread?.workspace_owner_id ?? activeWorkspace?.user_id ?? null;
    const dispatchedByUserId = thread?.dispatched_by?.user_id ?? null;

    const isConfirmedForeign = Boolean(
      (dispatchedByUserId && (!currentUserId || dispatchedByUserId !== currentUserId))
      || (threadUserId && currentUserId && threadUserId !== currentUserId)
      || (thread && !threadUserId && workspaceOwnerId && currentUserId && workspaceOwnerId !== currentUserId)
      || (teamReadOnly.isColleague && workspaceOwnerId && currentUserId && (teamReadOnly.isReadOnly || !teamReadOnly.colleagueDispatchEnabled)),
    );

    if (isConfirmedForeign) {
      daltonStatus = 'shared';
    } else if (teamReadOnly.isColleague) {
      // Either fail-closed while workspace/user loads, or an rw colleague
      // workspace where write permission depends on thread ownership.
      if (
        workspaceOwnerId
        && currentUserId
        && !teamReadOnly.isReadOnly
        && teamReadOnly.colleagueDispatchEnabled
        && liveThreadId
        && thread
      ) {
        daltonStatus = threadUserId === currentUserId ? 'writable' : 'shared';
      } else {
        daltonStatus = 'unresolved';
        daltonError = workspaceError || userError || (Boolean(liveThreadId) && threadError);
      }
    } else if (liveThreadId && (!thread || (threadUserId && !currentUserId))) {
      // Existing thread under Dalton team sharing: never treat workspace
      // ownership alone as thread write permission before thread detail
      // confirms thread ownership (a colleague may have started this thread
      // even if colleague dispatch is currently disabled).
      daltonStatus = 'unresolved';
      daltonError = threadError || (Boolean(threadUserId && !currentUserId) && userError);
    }
  }

  if (upstreamStatus === 'shared' || daltonStatus === 'shared') {
    return { status: 'shared', isError: false };
  }
  if (upstreamStatus === 'unresolved' || daltonStatus === 'unresolved') {
    return {
      status: 'unresolved',
      isError:
        (upstreamStatus === 'unresolved' && upstreamError)
        || (daltonStatus === 'unresolved' && daltonError),
    };
  }
  return { status: 'writable', isError: false };
}
