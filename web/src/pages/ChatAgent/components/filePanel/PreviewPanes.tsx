import React, { Suspense, useEffect, useMemo } from 'react';
import { useShareLink } from '@/hooks/useShareLink';
import { appGrantKey } from '@/lib/appBridge/useAppScopes';
import type { FileTab } from './useFileTabs';
import type { PreviewEntry, PreviewsApi } from './usePreviews';
import './PreviewPanes.css';

const PreviewViewer = React.lazy(() => import('../viewers/PreviewViewer'));

interface PreviewPanesProps {
  tabs: FileTab[];
  activeId: string;
  previews: PreviewsApi;
  // DALTON-HOOK(team-sharing): suppress preview registration, ensure, and refresh in colleague mirror mode
  teamReadOnly?: boolean;
  /** The owner's workspace; null on a read-only panel, whose apps read nothing. */
  workspaceId?: string | null;
}

/**
 * Every open app keeps its iframe mounted and only the active one is shown: a
 * tab switch must not reload a running server, drop its scroll position or
 * discard what was typed into it.
 */
export function PreviewPanes({ tabs, activeId, previews, teamReadOnly = false, workspaceId = null }: PreviewPanesProps): React.ReactElement | null {
  const previewTabs = useMemo(
    () => tabs.filter((t): t is Extract<FileTab, { kind: 'preview' }> => t.kind === 'preview'),
    [tabs],
  );
  const active = previewTabs.find((t) => t.id === activeId);
  const activePort = active?.port ?? null;

  // A tab strip restored from storage names ports nothing has resolved yet;
  // registering them is what puts them in the tree's list and gives the tab
  // something to mint against.
  const { register, ensure, byPort } = previews;
  useEffect(() => {
    // DALTON-HOOK(team-sharing): do not register restored preview entries in colleague mirror mode
    if (teamReadOnly) return;
    previewTabs.forEach((tab) => register({
      port: tab.port, title: tab.title, path: tab.previewPath, command: tab.command,
    }));
  }, [teamReadOnly, previewTabs, register]);

  // A signed preview URL is short-lived, so arriving at the tab is when it is
  // minted, not when the tab was opened, and not for tabs nobody is looking at.
  // Keyed on the map as well as the port: a tab restored from storage is
  // registered and activated in the same commit, and the mint has nothing to
  // read until that registration lands.
  useEffect(() => {
    // DALTON-HOOK(team-sharing): do not mint preview URL / start sandbox in colleague mirror mode
    if (!teamReadOnly && activePort != null) ensure(activePort);
  }, [teamReadOnly, activePort, ensure, byPort]);

  if (!previewTabs.length) return null;
  return (
    <Suspense fallback={null}>
      {previewTabs.map((tab) => {
        const entry = byPort.get(tab.port);
        return (
          <div key={tab.id} className="file-panel-preview-pane" hidden={tab.id !== activeId}>
            <PreviewPane
              tab={tab}
              entry={entry}
              active={tab.id === activeId}
              workspaceId={workspaceId}
              // DALTON-HOOK(team-sharing): do not refresh preview in colleague mirror mode
              onRefresh={teamReadOnly ? undefined : () => previews.refresh(tab.port)}
            />
          </div>
        );
      })}
    </Suspense>
  );
}

interface PreviewPaneProps {
  tab: Extract<FileTab, { kind: 'preview' }>;
  entry: PreviewEntry | undefined;
  /** The tab picked: the others stay mounted, hidden, and may open nothing. */
  active: boolean;
  workspaceId: string | null;
  onRefresh: (() => void) | undefined;
}

/**
 * One app's frame. Its `/a/` code and start command key what the user let it
 * read (`appGrantKey`), the same grant it has as the dashboard. The command
 * comes with the URL it started, so a new program on the port asks again; the
 * crumb row asks for the same link, so the code costs no second request.
 */
function PreviewPane({ tab, entry, active, workspaceId, onRefresh }: PreviewPaneProps): React.ReactElement {
  const { data: link } = useShareLink(workspaceId, { kind: 'app', port: tab.port });
  return (
    <PreviewViewer
      chrome={false}
      url={entry?.url ?? ''}
      port={tab.port}
      title={tab.title}
      loading={entry?.loading ?? true}
      error={entry?.error}
      reloadToken={entry?.reloadToken}
      onRefresh={onRefresh}
      bridge={workspaceId
        ? { grantKey: link && entry?.url ? appGrantKey({ code: link.code, command_key: entry.commandKey }) : null, active }
        : undefined}
    />
  );
}
