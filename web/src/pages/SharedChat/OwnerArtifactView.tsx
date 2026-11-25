import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query'; // DALTON-HOOK(monitors)
import { FileDown, Link2, Link2Off, MoreHorizontal, RefreshCw } from 'lucide-react'; // DALTON-HOOK(mobile-web)
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { Button } from '@/components/ui/button'; // DALTON-HOOK(mobile-web)
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'; // DALTON-HOOK(mobile-web)
import { toast } from '@/components/ui/use-toast';
import { useCallerIdentity } from '@/hooks/useCallerIdentity'; // DALTON-HOOK(external-share-guard)
import { useEnsureArtifact } from '@/hooks/useArtifact';
import { useIsMobile } from '@/hooks/useIsMobile'; // DALTON-HOOK(mobile-web)
import { queryKeys } from '@/lib/queryKeys'; // DALTON-HOOK(monitors)
import { ThreadLifecycleFeed } from '@/lib/threadLifecycle/ThreadLifecycleFeed';
import { getMonitorByArtifact, type MonitorSummary } from '@/pages/Monitors/api'; // DALTON-HOOK(monitors)
import PublishDialog from '../ChatAgent/components/PublishDialog';
import {
  useArtifactVersionView, versionReducer, type VersionAction, type VersionStore,
} from '../ChatAgent/components/filePanel/useArtifactVersionView';
import HtmlViewer from '../ChatAgent/components/viewers/HtmlViewer'; // DALTON-HOOK(external-share-guard)
import { ArtifactFrames } from '../ChatAgent/components/viewers/html/ArtifactFrames';
import { ArtifactVersionPicker } from '../ChatAgent/components/viewers/html/ArtifactVersionPicker';
import { CommentTools } from '../ChatAgent/components/viewers/html/comments/CommentTools';
import { useCommentUi } from '../ChatAgent/components/viewers/html/comments/useCommentUi';
import { exportServedPdf } from '../ChatAgent/components/viewers/html/useHtmlActions';
import { buildServeUrl } from '../ChatAgent/components/viewers/html/wsfilesUrl';
import { shareLinkHref } from '../ChatAgent/utils/api/shareLinks';
import { ShareTopBar, ShareTopBarAction } from './ShareTopBar';
import type { SharedOwnerFileMetadata } from './api';
import './SharePage.css';

/** The one file this route shows stands where a panel's tab id would. */
const VIEW_ID = 'artifact-view';

/** `?v=` as a version, or null for the live file when it names none. */
function versionParam(raw: string | null): number | null {
  const v = Number(raw);
  return raw && Number.isInteger(v) && v >= 1 ? v : null;
}

/**
 * The page's version in its address, `?v=`. A move replaces the entry rather
 * than adding a step, so Back leaves the page instead of walking its versions.
 */
function useAddressVersionStore(): VersionStore {
  const [params, setParams] = useSearchParams();
  const [updatedTo, setUpdatedTo] = useState<number | null>(null);
  const state = { version: versionParam(params.get('v')), updatedTo };
  return {
    state,
    dispatch: (action: VersionAction) => {
      const next = versionReducer(state, action);
      if (next.version !== state.version) {
        setParams(next.version == null ? {} : { v: String(next.version) }, { replace: true });
      }
      setUpdatedTo(next.updatedTo);
    },
  };
}

/**
 * The owner's own HTML file at its `/a/` link: the live file, with comments,
 * in a tab of its own. `?v=` opens a published version, read only.
 *
 * No chat sits beside it, so a comment sent from here starts a turn on its
 * destination (the bound thread, else the one whose agent wrote the file,
 * else a new one) and the card links there.
 */
export default function OwnerArtifactView({ code, metadata }: {
  code: string;
  metadata: SharedOwnerFileMetadata;
}): React.ReactElement {
  const { t } = useTranslation();
  const isMobile = useIsMobile(); // DALTON-HOOK(mobile-web)
  const { path, name, workspace_id: workspaceId, frame_base: frameBase } = metadata;
  // The record moves with a publish made here or anywhere, so the view reads
  // it rather than the link's answer, which is only where the page started.
  const versionStore = useAddressVersionStore();
  const versionView = useArtifactVersionView(workspaceId, path, versionStore);
  const [reloads, setReloads] = useState(0);
  // DALTON-HOOK(external-share-guard): allow opening dedicated unshareOnly dialog independently of publish permission state
  const [dialogMode, setDialogMode] = useState<'publish' | 'unshare' | null>(null);
  const [pdfBusy, setPdfBusy] = useState(false);
  const ui = useCommentUi();
  // DALTON-HOOK(external-share-guard): external and unknown callers lose CommentTools and use a read-only frame; publish stays gated on confirmed identity while stop-sharing stays available for active shares and public monitors
  const identity = useCallerIdentity();
  const canUseOwnerComments = identity.canActAsOwner && !identity.isConfirming;
  const canShowPublish = identity.isKnown;
  const publishDisabled = identity.status !== 'ready' || identity.isConfirming;
  const artifactId = versionView.record?.artifact_id ?? metadata.artifact_id ?? '';
  const { data: existingMonitor } = useQuery<MonitorSummary | null>({
    queryKey: queryKeys.daltonMonitors.byArtifact(artifactId),
    queryFn: () => getMonitorByArtifact(artifactId),
    enabled: Boolean(artifactId),
    staleTime: 30_000,
    retry: false,
  });
  const isArtifactShared = versionView.record
    ? Boolean(versionView.record.shared)
    : Boolean(metadata.shared);
  const hasPublicMonitor = existingMonitor?.visibility === 'public';
  const canShowUnshareOnly = isArtifactShared || hasPublicMonitor;
  const ensureArtifact = useEnsureArtifact();

  // No panel on this page, so a linked file opens on its own, as a link does:
  // in this tab, at the section it named, and Back returns here. The section
  // comes as the link's own fragment, already escaped.
  const openLink = (linked: string, fragment: string) => {
    ensureArtifact(workspaceId, linked).then(
      (artifact) => window.location.assign(fragment ? `${artifact.url}#${fragment}` : artifact.url),
      () => toast({ description: t('publish:linkFailed'), variant: 'destructive' }),
    );
  };
  const [anchor] = useState(() => {
    try {
      return decodeURIComponent(window.location.hash.slice(1)) || null;
    } catch {
      return null;
    }
  });

  const savePdf = async () => {
    if (pdfBusy) return;
    setPdfBusy(true);
    try {
      await exportServedPdf({
        filePath: path,
        servedUrl: buildServeUrl(frameBase, path),
        // The print fallback opens this page again, never the served URL,
        // which carries the grant.
        openUrl: shareLinkHref(code),
        printHint: t('filePanel:pdfPrintHint'),
        generatingHint: t('filePanel:pdfGenerating'),
        // DALTON-HOOK(mobile-web): pass WeChat external-browser download guidance
        wechatHint: t('filePanel:wechatOpenInBrowserHint'),
      });
    } finally {
      setPdfBusy(false);
    }
  };

  return (
    <div className="share-page">
      {/* Outside the app's shell, so nothing else hears the agent's replies here. */}
      <ThreadLifecycleFeed />
      <ShareTopBar
        name={name}
        actions={(
          <>
            {/* DALTON-HOOK(external-share-guard): hide CommentTools and version picker for external or unknown callers */}
            {canUseOwnerComments && (
              <>
                <CommentTools tabId={VIEW_ID} workspaceId={workspaceId} path={path} ui={ui} variant="bar" modeDisabled={versionView.version !== null} />
                <ArtifactVersionPicker view={versionView} variant="bar" />
              </>
            )}
            {/* DALTON-HOOK(mobile-web): fold secondary reload/PDF actions into an overflow menu on phones */}
            {isMobile ? (
              <>
                {/* DALTON-HOOK(external-share-guard): standalone stop-share entry stays enabled for shared files and historical public monitors even while identity is unknown/confirming */}
                {canShowUnshareOnly && (
                  <ShareTopBarAction icon={Link2Off} label={t('artifact.unshareOnly')} onClick={() => setDialogMode('unshare')} />
                )}
                {canShowPublish && (
                  <ShareTopBarAction icon={Link2} label={t('artifact.publish')} onClick={() => setDialogMode('publish')} disabled={publishDisabled} />
                )}
                <DropdownMenu modal={false}>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      aria-label={t('common.more', 'More')}
                      className="h-[30px] px-2 text-xs tap-target"
                      data-testid="owner-artifact-mobile-more"
                    >
                      <MoreHorizontal className="h-3.5 w-3.5" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" sideOffset={4}>
                    <DropdownMenuItem onSelect={() => setReloads((n) => n + 1)}>
                      <RefreshCw className="h-3.5 w-3.5" />
                      {t('filePanel:reloadPage')}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => void savePdf()} disabled={pdfBusy}>
                      <FileDown className="h-3.5 w-3.5" />
                      {t('filePanel:saveAsPdf')}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </>
            ) : (
              <>
                <ShareTopBarAction icon={RefreshCw} label={t('filePanel:reloadPage')} onClick={() => setReloads((n) => n + 1)} />
                <ShareTopBarAction icon={FileDown} label={t('filePanel:saveAsPdf')} onClick={() => void savePdf()} disabled={pdfBusy} />
                {/* DALTON-HOOK(external-share-guard): standalone stop-share entry stays enabled for shared files and historical public monitors even while identity is unknown/confirming */}
                {canShowUnshareOnly && (
                  <ShareTopBarAction icon={Link2Off} label={t('artifact.unshareOnly')} onClick={() => setDialogMode('unshare')} />
                )}
                {/* DALTON-HOOK(external-share-guard): publish entry follows confirmed identity (disabled while unknown/confirming) */}
                {canShowPublish && (
                  <ShareTopBarAction icon={Link2} label={t('artifact.publish')} onClick={() => setDialogMode('publish')} disabled={publishDisabled} />
                )}
              </>
            )}
          </>
        )}
      />
      {dialogMode !== null && (
        <PublishDialog
          open
          workspaceId={workspaceId}
          path={path}
          unshareOnly={dialogMode === 'unshare'}
          onClose={() => setDialogMode(null)}
        />
      )}
      <div className="share-content">
        {/* DALTON-HOOK(external-share-guard): external and unknown callers render in a read-only ViewFrame without comment layer */}
        {canUseOwnerComments ? (
          <ArtifactFrames
            // The agent's edits reach the page on a reload: no panel here reads
            // the file's bytes to notice them.
            revision={`r${reloads}`}
            fileName={name}
            workspaceId={workspaceId}
            filePath={path}
            prefix={frameBase}
            view={{ tabId: VIEW_ID, ui, active: true, versionView }}
            onOpenLink={openLink}
            anchor={anchor}
          />
        ) : (
          <HtmlViewer
            key={`ro-${reloads}`}
            content=""
            fileName={name}
            workspaceId={workspaceId}
            filePath={path}
            servePrefix={frameBase}
            anchor={anchor}
            onOpenLink={openLink}
          />
        )}
      </div>
    </div>
  );
}
