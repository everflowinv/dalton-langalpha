import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useChatRoute } from '@/components/Sidebar/useChatRoute';
import { toast } from '@/components/ui/use-toast';
import { isDaltonAuth } from '@/config/daltonAuth'; // DALTON-HOOK(colleague-dispatch)
import { useAllWorkspaces } from '@/hooks/useAllWorkspaces';
import { homeWorkspaceQuery } from '@/hooks/useHomeWorkspace';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { useUser } from '@/hooks/useUser'; // DALTON-HOOK(colleague-dispatch)
import { useWorkspace } from '@/hooks/useWorkspace'; // DALTON-HOOK(colleague-dispatch)
import { useWorkspaceLabel } from '@/hooks/useWorkspaceLabel';
import { copyText } from '@/lib/clipboard';
import { isDesktopShell } from '@/lib/desktop';
import { relativeTime } from '@/lib/format';
import { isHomeWorkspace } from '@/lib/homeWorkspace';
import { useArchiveThreadConfirm } from '@/pages/ChatAgent/components/threadArchiveAction';
import { useTeamWorkspaces } from '@/pages/ChatAgent/components/dalton/TeamSharing'; // DALTON-HOOK(colleague-dispatch)
import { archiveThreadRow, pinThreadRow, renameThreadRow } from '@/pages/ChatAgent/hooks/threadRowMutations';
import { useRemoveThread } from '@/pages/ChatAgent/hooks/useRemoveThread';
import { buildMarketViewUrl } from '@/pages/MarketView/utils/marketRoute';
// DALTON-HOOK(colleague-dispatch): palette checks workspace permissions before offering new-thread actions
import {
  resolveNavWorkspacePermissions,
  useNavAccessContext,
} from '@/pages/ChatAgent/components/NavigationRows';
import type { Workspace } from '@/types/api';
import { fileHref, type Names, type NewWork, type ThreadRef, type Verbs } from './rows';

/**
 * DALTON-HOOK(colleague-dispatch): Subscribe to user identity and team
 * workspaces so palette permissions stay reactive instead of reading a stale cache snapshot.
 */
export function usePaletteAccess() {
  const { user: currentUser, isLoading: userLoading } = useUser({ enabled: isDaltonAuth });
  const {
    data: teamData,
    isPending: teamPending,
    isLoading: teamLoading,
    isFetching: teamFetching,
  } = useTeamWorkspaces(isDaltonAuth);
  const currentUserId = isDaltonAuth ? (currentUser?.user_id ?? null) : undefined;
  const callerTier = isDaltonAuth ? (teamData?.caller_tier ?? null) : undefined;
  const access = useNavAccessContext(currentUserId, callerTier);
  const permissionsLoading =
    isDaltonAuth &&
    Boolean(
      userLoading ||
        teamPending ||
        teamLoading ||
        (!teamData && teamFetching),
    );

  const teamWorkspaceById = new Map<string, Workspace>();
  // DALTON-HOOK(external-share-guard): do not surface colleague workspaces while loading or for external members.
  if (!permissionsLoading && teamData?.caller_tier !== 'external') {
    for (const member of teamData?.members ?? []) {
      if (currentUser?.user_id && member.user_id === currentUser.user_id) continue;
      for (const ws of member.workspaces) {
        if (!teamWorkspaceById.has(ws.workspace_id)) {
          teamWorkspaceById.set(ws.workspace_id, {
            workspace_id: ws.workspace_id,
            user_id: member.user_id,
            name: ws.name,
            description: ws.description ?? undefined,
            status: (ws.status as Workspace['status']) ?? 'stopped',
            is_home: Boolean(ws.is_home),
            created_at: ws.created_at ?? '',
            updated_at: ws.updated_at ?? '',
            effective_web_access: ws.web_access ?? ws.access ?? 'ro',
            colleague_dispatch_enabled: teamData?.colleague_dispatch_enabled,
          } as unknown as Workspace);
        }
      }
    }
  }
  return { access, permissionsLoading, teamWorkspaceById };
}

/** The user's workspaces by id, and Home as its own lookup knows it. */
function useWorkspaces() {
  const { data, isPending: homePending } = useQuery(homeWorkspaceQuery(useQueryClient()));
  const { data: all } = useAllWorkspaces({ sortBy: 'activity', includeHome: true });
  // DALTON-HOOK(colleague-dispatch): include shared colleague workspaces alongside the user's own
  const { teamWorkspaceById } = usePaletteAccess();
  const workspaces = all?.workspaces ?? [];
  const byId = new Map<string, Workspace>();
  for (const [id, ws] of teamWorkspaceById) byId.set(id, ws);
  for (const ws of workspaces) byId.set(ws.workspace_id, ws);
  return { workspaces, byId, home: data, homePending };
}

/** How rows (and the preview) name workspaces, agents and times. */
export function useNames(): Names {
  const locale = useLocale();
  const now = useNow();
  const label = useWorkspaceLabel();
  const { byId, home } = useWorkspaces();
  return {
    workspaceName: (id, given = '') => {
      const ws = byId.get(id);
      return ws ? label(ws) : given;
    },
    isHome: (id) => (byId.has(id) ? isHomeWorkspace(byId.get(id)) : id === home?.workspace_id),
    when: (iso) => relativeTime(iso, locale, now),
  };
}

/**
 * Where new work goes: the workspace on screen, else Home and its Chief of
 * Staff. A thread opened by its link names its workspace only once looked
 * up; until then the place is pending, so Enter waits rather than send new
 * work to Home, and no row names a place yet.
 */
export function usePlace(): NewWork {
  const { t } = useTranslation();
  const label = useWorkspaceLabel();
  const { currentWorkspaceId, resolvingWorkspace } = useChatRoute();
  const { workspaces, byId, home, homePending } = useWorkspaces();
  const { data: currentWsDetail, isPending: currentWsPending, isLoading: currentWsLoading } = useWorkspace(currentWorkspaceId);
  const workspaceId = currentWorkspaceId ?? home?.workspace_id ?? workspaces.find(isHomeWorkspace)?.workspace_id;
  // Outside a workspace on a cold load, Home may not be read yet either.
  const pending = resolvingWorkspace || (!workspaceId && homePending);
  const cachedWs = workspaceId ? byId.get(workspaceId) : undefined;
  const workspace =
    workspaceId && workspaceId === currentWorkspaceId && currentWsDetail
      ? ({ ...cachedWs, ...currentWsDetail } as Workspace)
      : cachedWs;
  // DALTON-HOOK(colleague-dispatch): wait for identity/team permissions and current workspace detail before enabling creation
  const { access, permissionsLoading } = usePaletteAccess();
  const detailPending = Boolean(
    isDaltonAuth &&
      currentWorkspaceId &&
      !workspace &&
      (currentWsPending || currentWsLoading),
  );
  const blockedByPermissions = permissionsLoading || detailPending;
  const placePerms = workspace ? resolveNavWorkspacePermissions(workspace, access) : null;
  return {
    workspaceId,
    pending,
    // With Home unread for good (its lookup failed), a new thread would open
    // in whichever workspace was in view last, so rows that start one are
    // left out. (A thread whose lookup fails is left for the chat home,
    // where Home is the place.)
    canStartThread: blockedByPermissions
      ? false
      : pending || (!!workspaceId && (placePerms ? placePerms.canCreateThread : !isDaltonAuth)),
    where: pending
      ? undefined
      : (workspace ? isHomeWorkspace(workspace) : !currentWorkspaceId)
        ? t('agents.chiefOfStaff')
        : t('palette.analystIn', { workspace: label(workspace) }),
  };
}

/**
 * Open a new thread, in `fallbackWorkspaceId` unless told another, carrying
 * the workspace's name and status so the chat draws it before its own lookup
 * lands. The palette and the shell's New Thread menu item both start one here.
 */
export function useNewThread(fallbackWorkspaceId: string | undefined): Verbs['newThread'] {
  const navigate = useNavigate();
  const label = useWorkspaceLabel();
  const { byId } = useWorkspaces();
  return (workspaceId = fallbackWorkspaceId, draft = {}) => {
    const ws = workspaceId ? byId.get(workspaceId) : undefined;
    navigate('/chat/t/__default__', {
      state: {
        workspaceId,
        workspaceName: label(ws),
        workspaceStatus: ws?.status ?? null,
        draftMessage: draft.message,
        draftCommands: draft.commands,
      },
    });
  };
}

/**
 * Archiving a thread from the palette, which outlives the palette's own
 * content: a thread with a run going asks first, in a dialog that opens
 * after the palette has closed (a turn later, or the palette's focus trap
 * would take focus back from it) and stays up once it has. So the host,
 * mounted for good once the palette is first used, holds the dialog, and
 * the thread on screen gives way to the next one as it does in the sidebar.
 */
export function useArchiveThread() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { removeThread } = useRemoveThread(useChatRoute().currentThreadId);
  const { requestArchive, dialog } = useArchiveThreadConfirm();

  const archive = ({ threadId, workspaceId }: ThreadRef) =>
    removeThread(workspaceId, threadId, async () => {
      const ok = await archiveThreadRow(queryClient, workspaceId, threadId);
      toast(ok ? { title: t('thread.archived') } : { variant: 'destructive', title: t('thread.failedArchiveThread') });
      return ok;
    });
  return {
    archiveThread: (thread: ThreadRef) => void window.setTimeout(() => requestArchive(thread.threadId, () => void archive(thread)), 0),
    dialog,
  };
}

/** What the palette's rows do in the app. Each runs after the palette has closed. */
export function useVerbs(archiveThread: Verbs['archiveThread']): Verbs {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const label = useWorkspaceLabel();
  const { currentWorkspaceId, currentThreadId } = useChatRoute();
  const { byId } = useWorkspaces();
  // DALTON-HOOK(colleague-dispatch): subscribe to identity/team permissions and live workspace access
  const { data: currentWsDetail } = useWorkspace(currentWorkspaceId);
  const { access, permissionsLoading } = usePaletteAccess();
  const names = useNames();
  const newWork = usePlace();
  const newThread = useNewThread(newWork.workspaceId);

  return {
    ...names,
    t,
    newWork,
    // DALTON-HOOK(colleague-dispatch): expose reactive creation permission gate to Ask and target workspace rows
    permissionsLoading,
    canCreateThreadIn: (targetWorkspaceId) => {
      if (permissionsLoading) return false;
      const targetId = targetWorkspaceId ?? newWork.workspaceId;
      if (!targetId) return newWork.canStartThread;
      const cached = byId.get(targetId);
      const ws =
        targetId === currentWorkspaceId && currentWsDetail
          ? ({ ...cached, ...currentWsDetail } as Workspace)
          : cached;
      if (!ws) return !isDaltonAuth;
      return resolveNavWorkspacePermissions(ws, access).canCreateThread;
    },
    // A thread opens in its workspace without looking it up, as a restored
    // session does; a match opens it at the turn (ChatView reads `focusTurn`).
    openThread: (threadId, workspaceId, also = {}) => navigate(`/chat/t/${threadId}`, { state: { workspaceId, ...also } }),
    newThread,
    // In the chat on screen when the file is in its workspace, else on the workspace's page.
    openFile: (workspaceId, path) => {
      if (currentThreadId && currentWorkspaceId === workspaceId) {
        navigate({ pathname: location.pathname, search: `?${new URLSearchParams({ file: path })}` }, { state: location.state });
      } else {
        navigate(fileHref(workspaceId, path), { state: { workspaceName: label(byId.get(workspaceId)) || undefined } });
      }
    },
    openChart: (symbol) => navigate(buildMarketViewUrl({ symbol })),
    copy: (text) =>
      void copyText(text).then(
        () => toast({ title: t('common.copied') }),
        () => toast({ variant: 'destructive', title: t('palette.copyFailed') }),
      ),
    openInNewTab: isDesktopShell() ? null : (path) => void window.open(path, '_blank', 'noopener'),
    renameThread: (thread, title) => void renameThreadRow(queryClient, thread.workspaceId, thread.threadId, title, thread.savedTitle, t),
    pinThread: (thread, pinned) =>
      void pinThreadRow(queryClient, thread.workspaceId, thread.threadId, pinned).then((ok) =>
        toast(
          ok
            ? { title: pinned ? t('palette.pinned') : t('palette.unpinned') }
            : { variant: 'destructive', title: pinned ? t('thread.failedPinThread') : t('thread.failedUnpinThread') },
        ),
      ),
    archiveThread,
  };
}
