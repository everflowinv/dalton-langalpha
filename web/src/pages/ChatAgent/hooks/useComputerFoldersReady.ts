import { useAllWorkspaces } from '@/hooks/useAllWorkspaces';
import { useWorkspace } from '@/hooks/useWorkspace';
import { useComputers } from './useComputers';

/**
 * Whether all folder-routing queries read by `useComputerFolders` have settled
 * (succeeded or failed) for `workspaceId`.
 *
 * Used by `useFileDeepLink` to hold rooted or sibling `?file=` deep links until
 * workspace folder, sibling workspace, and computer-root metadata are available,
 * while unblocking cleanly on query failure so the link degrades to a
 * current-workspace relative path rather than stalling.
 */
export function useComputerFoldersReady(workspaceId: string | null | undefined): boolean {
  const viewedQuery = useWorkspace(workspaceId);
  const viewed = viewedQuery.data;
  const allQuery = useAllWorkspaces({ includeHome: true, enabled: !!viewed?.computer_id });
  const computersQuery = useComputers({ enabled: !!viewed });

  const viewedSettled = !workspaceId || (!viewedQuery.isPending && !viewedQuery.isPlaceholderData);
  const allSettled = !viewed?.computer_id || !allQuery.isPending;
  const computersSettled = !viewed || !computersQuery.isPending;
  return viewedSettled && allSettled && computersSettled;
}
