import { useState } from 'react';
import { useCallerIdentity } from '@/hooks/useCallerIdentity'; // DALTON-HOOK(external-share-guard)
import { useWorkspaceFiles } from '../../hooks/useWorkspaceFiles';

/**
 * Which workspace's files the chat's file panel shows, and what it may do
 * with them. A view shows its own unless a link named a sibling's, which it
 * shows read-only: each agent works only in its own folder.
 *
 * `refreshOwn` is for after the agent writes, and lists the view's own files
 * even while a sibling's are on screen: the turn wrote its own, and listing
 * the sibling's would start its computer. It never starts the view's own
 * either: a write replayed into a thread opened later finds it stopped.
 */
export function usePanelFiles({
  isHome,
  workspaceId,
  workspaceName = null,
  includeSystem,
  // DALTON-HOOK(workspace-sharing-settings): separate file write lock (readOnly) from colleague identity (isColleague)
  readOnly = false,
  isColleague = readOnly,
  // DALTON-HOOK(external-share-guard): external members cannot create public file share links
  callerTier = null,
}: {
  isHome: boolean;
  workspaceId: string;
  /** The view's own workspace, which the way back from a sibling is labelled with; Home's says Home. */
  workspaceName?: string | null;
  includeSystem: boolean;
  // DALTON-HOOK(workspace-sharing-settings): separate file write lock (readOnly) from colleague identity (isColleague)
  readOnly?: boolean;
  isColleague?: boolean;
  // DALTON-HOOK(external-share-guard): external members cannot create public file share links
  callerTier?: string | null;
}) {
  const identity = useCallerIdentity();
  // DALTON-HOOK(external-share-guard): fail closed while caller identity is loading/error/confirming; external and unknown members cannot create public file share links
  const effectiveCallerTier =
    callerTier !== undefined && callerTier !== null
      ? callerTier
      : identity.isPlatform
        ? null
        : identity.status === 'ready' && !identity.isConfirming
          ? identity.tier
          : 'unknown';
  const canShareByTier =
    effectiveCallerTier === null ||
    effectiveCallerTier === 'admin' ||
    effectiveCallerTier === 'internal';
  const [override, setOverride] = useState<string | null>(null);
  const shownId = override ?? workspaceId;
  const showsSibling = !!override && override !== workspaceId;
  const shown = useWorkspaceFiles(shownId, { includeSystem });
  // An @-mention is a bare path the agent reads in its own folder, so the
  // composer offers the view's own files even while the panel shows a sibling's.
  const own = useWorkspaceFiles(showsSibling ? workspaceId : null, { includeSystem });

  return {
    override,
    setOverride,
    /** The workspace the panel lists. */
    shownWorkspaceId: shownId,
    files: shown.files,
    loading: shown.loading,
    error: shown.error,
    refresh: shown.refresh,
    mentionFiles: showsSibling ? own.files : shown.files,
    refreshOwn: showsSibling ? own.refreshInBackground : shown.refreshInBackground,
    /** FilePanel's access props, spread onto every mount. */
    panelAccess: showsSibling
      ? {
          // DALTON-HOOK(workspace-sharing-settings): teamReadOnly follows isColleague; readOnly is file write lock; canShare is owner-only
          readOnly: true, teamReadOnly: isColleague, singleFileMode: true, canShare: false, isHome: false,
          onReturnHome: () => setOverride(null), returnLabel: isHome ? null : workspaceName,
          filesAway: shown.away,
        }
      : {
          // DALTON-HOOK(workspace-sharing-settings): teamReadOnly follows isColleague; readOnly is file write lock
          // DALTON-HOOK(external-share-guard): external/unknown members get no publish entry; canShare is owner-only
          readOnly, teamReadOnly: isColleague, singleFileMode: false, canShare: !isColleague && !readOnly && canShareByTier, callerTier: effectiveCallerTier, isHome,
          onReturnHome: null, returnLabel: null,
          filesAway: shown.away,
        },
  };
}
