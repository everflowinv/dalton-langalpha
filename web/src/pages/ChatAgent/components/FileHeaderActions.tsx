import React, { useContext, useEffect, useRef, useState } from 'react';
import { QueryClient, QueryClientContext, useQuery, useQueryClient } from '@tanstack/react-query'; // DALTON-HOOK(mobile-web)
// DALTON-HOOK(external-share-guard): Link2Off for the external member's stop-only entry
// DALTON-HOOK(mobile-web): ArrowUp, ChevronDown, History, House, MoreHorizontal, MessageSquare, MessageSquarePlus, RefreshCw for complete mobile HTML report menu
import { ArrowUp, ChevronDown, Code2, Download, Eye, ExternalLink, FileDown, History, House, Link2, Link2Off, MessageSquare, MessageSquarePlus, MoreHorizontal, Pencil, Pin, PinOff, RefreshCw, Settings2, X, Undo2, Redo2, FileDiff, FileText, Check, Clipboard } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
} from '@/components/ui/dropdown-menu';
import { toast } from '@/components/ui/use-toast';
import { copyText } from '@/lib/clipboard';
import { useTranslation } from 'react-i18next';
import { useIsMobile } from '@/hooks/useIsMobile'; // DALTON-HOOK(mobile-web)
import { useLocale } from '@/hooks/useLocale'; // DALTON-HOOK(mobile-web)
import { ALL, refreshArtifactComments } from '@/lib/artifactCache'; // DALTON-HOOK(mobile-web)
import { usePinPageAsDashboard } from '@/lib/dashboards'; // DALTON-HOOK(mobile-web)
import { shortMonthDay } from '@/lib/format'; // DALTON-HOOK(mobile-web)
import { cn } from '@/lib/utils';
import { fileExtension, isHtmlFile } from '../utils/filePaths';
import { useDownloadState, workspaceDownloadKey } from '../utils/downloadNotice';
import { exportServedPdf, isWeChatBrowser } from './viewers/html/useHtmlActions'; // DALTON-HOOK(mobile-web)
import { useServedHtml } from './viewers/html/useServedHtml';
// DALTON-HOOK(mobile-web): sharedSeq, useArtifactComments for mobile HTML More menu
import { artifactPathKey, sharedSeq, useArtifactBinding, useArtifactByPath, useArtifactComments } from '@/hooks/useArtifact';
// DALTON-HOOK(monitors): inspect bound monitor visibility so historical public monitors show stop-share entry
import { queryKeys } from '@/lib/queryKeys';
import { getMonitorByArtifact, type MonitorSummary } from '@/pages/Monitors/api';
import { useWorkspaceFolders } from '../contexts/WorkspaceContext';
import { openComments } from './artifactComments/commentModel'; // DALTON-HOOK(mobile-web)
import { queuedBatches, refusalReason, sendBatches } from './artifactComments/commentSends'; // DALTON-HOOK(mobile-web)
import { useArtifactCommentSender } from './artifactComments/sendContext';
import { useCommentDestinations } from './artifactComments/useCommentDestinations';
import { useArtifactCrumbsState } from './filePanel/ArtifactCrumbs'; // DALTON-HOOK(mobile-web)
import type { PanelAccess } from './filePanel/types';
import type { ArtifactVersionView } from './filePanel/useArtifactVersionView'; // DALTON-HOOK(mobile-web)
import { OUTCOME_MS, summarizeSendAllOutcome } from './viewers/html/comments/CommentTools'; // DALTON-HOOK(mobile-web)
import type { CommentUi } from './viewers/html/comments/useCommentUi'; // DALTON-HOOK(mobile-web)
import type { HtmlFrameDocState } from './viewers/html/pageProtocol'; // DALTON-HOOK(mobile-web)

const PDF_SCALE_CHOICES = [0.8, 1, 1.25];
// DALTON-HOOK(monitors): inert fallback client for tests that render FileHeaderActions without QueryClientProvider
const FALLBACK_MONITOR_QUERY_CLIENT = new QueryClient({
  defaultOptions: { queries: { enabled: false, retry: false } },
});

// --- File type detection helpers ---

/** The one extension and HTML readers; re-exported so this module's callers keep one import. */
export { fileExtension as getFileExtension, isHtmlFile };

export function isMarkdownFile(filePath: string, mime: string | null): boolean {
  return fileExtension(filePath) === 'md' || (mime?.includes('markdown') ?? false);
}

export function isTextMime(mime: string | null): boolean {
  if (!mime) return false;
  if (mime.startsWith('text/')) return true;
  if (['application/json', 'application/yaml', 'application/xml', 'application/javascript', 'application/typescript'].some(t => mime.includes(t))) return true;
  if (mime.includes('markdown')) return true;
  return false;
}

// --- Props ---

interface FileHeaderActionsProps {
  selectedFile: string | null;
  isEditing: boolean;
  workspaceId: string;
  fileContent: string | null;
  fileMime: string | null;
  canEdit: boolean;
  onStartEdit: () => void;
  onOpenExportModal: () => void;
  triggerDownloadFn: (workspaceId: string, filePath: string) => Promise<void>;
  /** Whether this viewer may save the bytes. A copy-link share grants
   *  `allow_files` without `allow_download`, and every item in the menu below
   *  writes a file to disk, so the whole menu goes rather than leaving a
   *  trigger over an empty list. Copy-to-clipboard goes with it: the menu is
   *  labelled and iconed for downloading, and the viewer can still select the
   *  text it is reading. */
  canDownload?: boolean;
  readFileFullFn: (workspaceId: string, filePath: string) => Promise<{ content: string }>;
  /** Who the panel is for, which decides how an HTML file is served and opened.
   *  DALTON-HOOK(team-sharing): a readOnly panel with `mirror` (a teammate's
   *  workspace) also skips the owner's file grant inside `useServedHtml`. */
  access: PanelAccess;
  /** Opens the panel's Publish dialog on a file; left out where the panel is not the owner's. */
  openPublish?: ((path: string) => void) | null;
  /**
   * DALTON-HOOK(external-share-guard): an external member gets no publish
   * entry, but a link they already made public can still be stopped — the
   * old ShareLinkDialog allowed exactly that. Set only where the caller is
   * an external member; the button appears just for a shared record.
   */
  openUnshareOnly?: ((path: string) => void) | null;
  /**
   * The document an HTML file's frame shows, which a link inside it can make
   * another file: its menu says where that one's comments go, as the comment
   * box and toolbar do. Null while the file's viewer is not up.
   */
  artifactDoc?: string | null;
  // DALTON-HOOK(mobile-web): unified HTML frame document state machine (pending/current/bridged_root/linked/unbridged)
  htmlDocState?: HtmlFrameDocState | null;
  // DALTON-HOOK(mobile-web): open a linked in-frame HTML report as its own tab from desktop or mobile header
  onOpenDocAsTab?: ((path: string) => void) | null;
  /** An HTML file's view: its markup rather than its page, and the switch between them. */
  htmlSource?: boolean;
  onToggleHtmlSource?: (() => void) | null;
  // Edit mode callbacks
  editorRef: React.RefObject<any>;
  canUndo: boolean;
  canRedo: boolean;
  hasUnsavedChanges: boolean;
  showDiff: boolean;
  setShowDiff: (fn: (d: boolean) => boolean) => void;
  isSaving: boolean;
  saveError: string | null;
  onSave: () => void;
  onCancelEdit: () => void;
  // DALTON-HOOK(mobile-web): owner HTML tools folded into the mobile More menu so the 390px crumb row holds only path + More
  mobileHtmlTools?: MobileHtmlTools | null;
}

// DALTON-HOOK(mobile-web): contract for HTML comment, dashboard-pin, readAt, and version actions inside the mobile More menu
export interface MobileHtmlTools {
  tabId: string;
  ui: CommentUi;
  threadId?: string | null;
  versionView?: ArtifactVersionView | null;
  modeDisabled?: boolean;
  readAt?: number | null;
}

// DALTON-HOOK(mobile-web): single source of truth for header action visibility shared by
// the desktop toolbar/crumbs and the mobile HTML overflow menu (desktop has it <=> mobile has it).
interface HeaderActionFlags {
  showToggleHtmlSource: boolean;
  showUnshareOnly: boolean;
  showPublish: boolean;
  showOwnerArtifactMenu: boolean;
  showOpenUrl: boolean;
  showDownload: boolean;
  showEdit: boolean;
  showCommentTools: boolean;
  showPinDashboard: boolean;
  showArtifactCrumbs: boolean;
}

function resolveHeaderActionFlags({
  isHtml,
  access,
  onToggleHtmlSource,
  openUnshareOnly,
  unshareShared,
  openPublish,
  openUrl,
  canDownload,
  canEdit,
  mobileHtmlTools,
  // DALTON-HOOK(mobile-web): suppress file-level actions on linked/unbridged HTML frames
  suppressFileActions = false,
  isUnbridgedHtmlDoc = false,
}: {
  isHtml: boolean;
  access: PanelAccess;
  onToggleHtmlSource: (() => void) | null;
  openUnshareOnly: ((path: string) => void) | null;
  unshareShared: boolean;
  openPublish: ((path: string) => void) | null;
  openUrl: string | undefined;
  canDownload: boolean;
  canEdit: boolean;
  mobileHtmlTools: MobileHtmlTools | null;
  suppressFileActions?: boolean;
  isUnbridgedHtmlDoc?: boolean;
}): HeaderActionFlags {
  const showPublish = Boolean(openPublish) && !suppressFileActions;
  const showOwnerArtifactMenu = showPublish && isHtml && access.kind === 'owner';
  const showCommentTools = Boolean(mobileHtmlTools) && !isUnbridgedHtmlDoc;
  return {
    showToggleHtmlSource: isHtml && Boolean(onToggleHtmlSource) && !suppressFileActions,
    showUnshareOnly: Boolean(openUnshareOnly && unshareShared) && !suppressFileActions,
    showPublish,
    showOwnerArtifactMenu,
    showOpenUrl: Boolean(openUrl),
    showDownload: canDownload && !suppressFileActions,
    showEdit: canEdit && !suppressFileActions,
    // Matches useFileComments.tsx: tools renders CommentTools + PinPageButton when `path && ui`;
    // crumbs renders ArtifactCrumbs when `path && openPublish`.
    showCommentTools,
    showPinDashboard: showCommentTools && !suppressFileActions,
    showArtifactCrumbs: showCommentTools && showPublish && Boolean(mobileHtmlTools?.versionView) && !suppressFileActions,
  };
}

// --- Component ---

function FileHeaderActions({
  selectedFile,
  isEditing,
  workspaceId,
  fileContent: _fileContent,
  fileMime,
  canEdit,
  onStartEdit,
  onOpenExportModal,
  triggerDownloadFn,
  canDownload = true,
  readFileFullFn,
  access,
  openPublish = null,
  // DALTON-HOOK(external-share-guard): stop-only entry for external members
  openUnshareOnly = null,
  artifactDoc = null,
  htmlDocState = null, // DALTON-HOOK(mobile-web)
  onOpenDocAsTab = null, // DALTON-HOOK(mobile-web)
  htmlSource = false,
  onToggleHtmlSource = null,
  editorRef,
  canUndo,
  canRedo,
  hasUnsavedChanges,
  showDiff,
  setShowDiff,
  isSaving,
  saveError,
  onSave,
  onCancelEdit,
  mobileHtmlTools = null, // DALTON-HOOK(mobile-web)
}: FileHeaderActionsProps) {
  const { t } = useTranslation();
  const isMobile = useIsMobile(); // DALTON-HOOK(mobile-web)
  const downloadPending = useDownloadState(
    selectedFile ? workspaceDownloadKey(workspaceId, selectedFile) : null,
  ) !== 'idle';
  const [copied, setCopied] = useState(false);
  // Server PDF renders take seconds; ignore re-entry while one is in flight.
  const pdfInFlight = useRef(false);
  const [pdfScale, setPdfScale] = useState(1);
  const [pdfPageNumbers, setPdfPageNumbers] = useState(false);
  const [pdfBranding, setPdfBranding] = useState(true);

  const served = useServedHtml(
    workspaceId,
    selectedFile && isHtmlFile(selectedFile) ? selectedFile : null,
    access,
  );
  // DALTON-HOOK(external-share-guard): the record behind the selected file,
  // read when the stop-share entry can show (non-owners for any public share, or owners for historical public monitors).
  const contextClient = useContext(QueryClientContext);
  const monitorQueryClient = contextClient ?? FALLBACK_MONITOR_QUERY_CLIENT;
  const root = useWorkspaceFolders()?.rootDir;
  const unsharePath = access.kind === 'owner' && artifactDoc ? artifactDoc : selectedFile;
  const { data: unshareRecord } = useArtifactByPath(
    openUnshareOnly ? workspaceId : null,
    openUnshareOnly ? unsharePath : null,
    { root },
  );
  // DALTON-HOOK(monitors): also surface stop-share entry when a historical public monitor is attached even if artifact.shared is false
  const { data: unshareMonitor } = useQuery<MonitorSummary | null>(
    {
      queryKey: queryKeys.daltonMonitors.byArtifact(unshareRecord?.artifact_id ?? ''),
      queryFn: () => getMonitorByArtifact(unshareRecord!.artifact_id),
      enabled: Boolean(contextClient && openUnshareOnly && unshareRecord?.artifact_id),
      retry: false,
    },
    monitorQueryClient,
  );

  const handleExportHtmlPdf = async () => {
    if (!selectedFile || pdfInFlight.current) return;
    if (!served.plainUrl) {
      toast({ description: t('filePanel:pdfFailed') });
      return;
    }
    pdfInFlight.current = true;
    try {
      await exportServedPdf({
        filePath: selectedFile,
        servedUrl: served.plainUrl,
        openUrl: served.openUrl,
        printHint: t('filePanel:pdfPrintHint'),
        generatingHint: t('filePanel:pdfGenerating'),
        // DALTON-HOOK(mobile-web): pass WeChat external-browser download guidance
        wechatHint: t('filePanel:wechatOpenInBrowserHint'),
        scale: pdfScale,
        pageNumbers: pdfPageNumbers,
        branding: pdfBranding,
      });
    } finally {
      pdfInFlight.current = false;
    }
  };

  const handleCopy = async () => {
    if (!selectedFile) return;
    try {
      // Fetch full content to avoid copying truncated text for large files
      const { content } = await readFileFullFn(workspaceId, selectedFile);
      await copyText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ description: t('filePanel:copyFailed'), variant: 'destructive' });
    }
  };

  if (!selectedFile) return null;

  // --- Edit mode ---
  if (isEditing) {
    return (
      <>
        {saveError && (
          <span className="text-xs truncate" style={{ color: 'var(--color-icon-danger)', maxWidth: 120 }} title={saveError}>
            {saveError}
          </span>
        )}
        <button
          onClick={() => editorRef.current?.trigger('toolbar', 'undo', null)}
          className="file-panel-icon-btn"
          title={t('filePanel:undo')}
          disabled={!canUndo}
        >
          <Undo2 className="h-4 w-4" />
        </button>
        <button
          onClick={() => editorRef.current?.trigger('toolbar', 'redo', null)}
          className="file-panel-icon-btn"
          title={t('filePanel:redo')}
          disabled={!canRedo}
        >
          <Redo2 className="h-4 w-4" />
        </button>
        {hasUnsavedChanges && (
          <button
            onClick={() => setShowDiff(d => !d)}
            className={`file-panel-icon-btn ${showDiff ? 'file-panel-icon-btn-active' : ''}`}
            title={showDiff ? t('filePanel:hideDiff') : t('filePanel:showDiff')}
          >
            <FileDiff className="h-4 w-4" />
          </button>
        )}
        <button
          onClick={onSave}
          className="file-panel-icon-btn"
          title={t('filePanel:save')}
          disabled={!hasUnsavedChanges || isSaving}
        >
          <Check className={`h-4 w-4 ${isSaving ? 'animate-pulse' : ''}`} />
        </button>
        <button
          onClick={onCancelEdit}
          className="file-panel-icon-btn"
          title={t('filePanel:cancelEditing')}
        >
          <X className="h-4 w-4" />
        </button>
      </>
    );
  }

  // --- View mode ---

  // One handler for every save in this menu. Four sites carried this same body
  // before, which is four places a new one could copy without the guard above.
  // DALTON-HOOK(mobile-web): prompt to open in external browser when downloading inside WeChat built-in browser
  const download = () => {
    if (isWeChatBrowser()) {
      toast({ description: t('filePanel:wechatOpenInBrowserHint') });
      return;
    }
    triggerDownloadFn(workspaceId, selectedFile).catch((err: unknown) => {
      console.error('[FileHeaderActions] Download failed:', err);
      // A save is the whole interaction: nothing opens, nothing navigates, and the
      // browser shows no file. Without this the menu item is indistinguishable
      // from a dead one. A toast rather than an inline error, because the document
      // the reader is looking at is still fine and should stay on screen.
      toast({ description: t('filePanel:downloadFailed'), variant: 'destructive' });
    });
  };

  const isMd = isMarkdownFile(selectedFile, fileMime);
  const isHtml = isHtmlFile(selectedFile);
  const isText = isTextMime(fileMime);
  // DALTON-HOOK(mobile-web): unify desktop & mobile HTML action gating around HtmlFrameDocState
  const activeHtmlDocState = isHtml && !htmlSource ? htmlDocState : null;
  const isPendingHtmlDoc = activeHtmlDocState?.kind === 'pending';
  const isUnbridgedHtmlDoc = activeHtmlDocState?.kind === 'unbridged';
  const isLinkedHtmlDoc = activeHtmlDocState
    ? activeHtmlDocState.kind === 'linked'
    : Boolean(
        isMobile &&
        isHtml &&
        !htmlSource &&
        artifactDoc &&
        artifactPathKey(artifactDoc, root) !== artifactPathKey(selectedFile, root),
      );
  const linkedDocPath =
    activeHtmlDocState?.kind === 'linked'
      ? activeHtmlDocState.path
      : isLinkedHtmlDoc
        ? artifactDoc
        : null;
  // Never fall back to tab A's served.openUrl when the frame is linked or unbridged (currentUrl: null)
  const openUrl = !isHtml
    ? undefined
    : isUnbridgedHtmlDoc
      ? (activeHtmlDocState?.kind === 'unbridged' ? activeHtmlDocState.currentUrl ?? undefined : undefined)
      : isLinkedHtmlDoc
        ? undefined
        : served.openUrl;

  const renderDropdownItems = () => {
    if (isMd) {
      // Markdown file: Download as PDF + Download as Markdown
      return (
        <>
          <DropdownMenuItem onSelect={() => onOpenExportModal()}>
            <FileText className="h-3.5 w-3.5" />
            {t('filePanel:downloadAsPdf')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={download} disabled={downloadPending}>
            <Download className="h-3.5 w-3.5" />
            {t('filePanel:downloadAsMarkdown')}
          </DropdownMenuItem>
        </>
      );
    }

    if (isHtml) {
      // HTML file: this menu owns Download + Save-as-PDF; the view actions
      // (source, open in new tab) sit beside it in the header.
      // PDF options toggle component state via preventDefault so the menu
      // stays open while the user composes the export.
      return (
        <>
          <DropdownMenuItem onSelect={download} disabled={downloadPending}>
            <Download className="h-3.5 w-3.5" />
            {t('filePanel:download')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => void handleExportHtmlPdf()}>
            <FileDown className="h-3.5 w-3.5" />
            {t('filePanel:saveAsPdf')}
          </DropdownMenuItem>
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <Settings2 className="h-3.5 w-3.5" />
              {t('filePanel:pdfOptions')}
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  setPdfBranding((v) => !v);
                }}
              >
                <Check className={cn('h-3.5 w-3.5', !pdfBranding && 'invisible')} />
                {t('filePanel:pdfBranding')}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  setPdfPageNumbers((v) => !v);
                }}
              >
                <Check className={cn('h-3.5 w-3.5', !pdfPageNumbers && 'invisible')} />
                {t('filePanel:pdfPageNumbers')}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>{t('filePanel:pdfScale')}</DropdownMenuLabel>
              {PDF_SCALE_CHOICES.map((scale) => (
                <DropdownMenuItem
                  key={scale}
                  onSelect={(e) => {
                    e.preventDefault();
                    setPdfScale(scale);
                  }}
                >
                  <Check className={cn('h-3.5 w-3.5', pdfScale !== scale && 'invisible')} />
                  {Math.round(scale * 100)}%
                </DropdownMenuItem>
              ))}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </>
      );
    }

    if (isText) {
      // Non-markdown text file: Download + Copy to clipboard
      return (
        <>
          <DropdownMenuItem onSelect={download} disabled={downloadPending}>
            <Download className="h-3.5 w-3.5" />
            {t('filePanel:download')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={handleCopy}>
            {copied
              ? <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-success)' }} />
              : <Clipboard className="h-3.5 w-3.5" />
            }
            {copied
              ? (t('filePanel:copiedToClipboard') ?? 'Copied!')
              : (t('filePanel:copyToClipboard') ?? 'Copy to clipboard')
            }
          </DropdownMenuItem>
        </>
      );
    }

    // Binary file: Download only
    return (
      <DropdownMenuItem onSelect={download} disabled={downloadPending}>
        <Download className="h-3.5 w-3.5" />
        {t('filePanel:download')}
      </DropdownMenuItem>
    );
  };

  // DALTON-HOOK(monitors): include historical public monitors alongside active public artifact links;
  // for owner panels show the dedicated stop-share entry when artifact.shared is false but a historical public monitor exists.
  const hasHistoricalPublicMonitor =
    !unshareRecord?.shared && unshareMonitor?.visibility === 'public';
  // DALTON-HOOK(mobile-web): compute visibility flags once and share them across both desktop toolbar and mobile HTML overflow menu
  const suppressFileActions = isLinkedHtmlDoc || isUnbridgedHtmlDoc;
  const actions = resolveHeaderActionFlags({
    isHtml,
    access,
    onToggleHtmlSource,
    openUnshareOnly,
    unshareShared:
      access.kind === 'owner'
        ? hasHistoricalPublicMonitor
        : Boolean(unshareRecord?.shared || unshareMonitor?.visibility === 'public'),
    openPublish,
    openUrl,
    canDownload,
    canEdit,
    mobileHtmlTools,
    suppressFileActions,
    isUnbridgedHtmlDoc,
  });
  const hasHtmlOverflowActions =
    isMobile &&
    isHtml &&
    Boolean(
      (isLinkedHtmlDoc && linkedDocPath && onOpenDocAsTab) ||
      actions.showCommentTools ||
      actions.showPublish ||
      actions.showUnshareOnly ||
      actions.showToggleHtmlSource ||
      actions.showOpenUrl ||
      actions.showDownload ||
      actions.showEdit,
    );

  return (
    <>
      {/* DALTON-HOOK(mobile-web): forward linkedDocPath and pending state to mobile overflow menu */}
      {hasHtmlOverflowActions ? (
        <MobileHtmlOverflowMenu
          workspaceId={workspaceId}
          selectedFile={selectedFile}
          linkedDocPath={linkedDocPath}
          isPendingHtmlDoc={isPendingHtmlDoc}
          onOpenDocAsTab={onOpenDocAsTab}
          access={access}
          actions={actions}
          openPublish={openPublish}
          openUnshareOnly={openUnshareOnly}
          htmlSource={htmlSource}
          onToggleHtmlSource={onToggleHtmlSource}
          openUrl={openUrl ?? null}
          onStartEdit={onStartEdit}
          download={download}
          downloadPending={downloadPending}
          onExportHtmlPdf={handleExportHtmlPdf}
          pdfBranding={pdfBranding}
          onTogglePdfBranding={() => setPdfBranding((v) => !v)}
          pdfPageNumbers={pdfPageNumbers}
          onTogglePdfPageNumbers={() => setPdfPageNumbers((v) => !v)}
          pdfScale={pdfScale}
          onSelectPdfScale={setPdfScale}
          mobileHtmlTools={mobileHtmlTools}
        />
      ) : (
        /* DALTON-HOOK(mobile-web): desktop flat HTML action toolbar sharing the same `actions` flags as mobile */
        <>
          {/* DALTON-HOOK(mobile-web): when viewing an in-frame linked report B on desktop, offer "Open in its own tab" */}
          {isLinkedHtmlDoc && linkedDocPath && onOpenDocAsTab && (
            <button
              type="button"
              onClick={() => onOpenDocAsTab(linkedDocPath)}
              className="file-panel-icon-btn"
              title={t('filePanel:openReportInTab')}
              aria-label={t('filePanel:openReportInTab')}
              data-testid="file-header-open-doc-tab"
            >
              <ExternalLink className="h-4 w-4" />
            </button>
          )}
          {/* One switch for the two views, showing where it goes. */}
          {actions.showToggleHtmlSource && onToggleHtmlSource && (
            <button
              onClick={onToggleHtmlSource}
              className="file-panel-icon-btn"
              title={htmlSource ? t('filePanel:viewPreview') : t('filePanel:viewSource')}
              aria-label={htmlSource ? t('filePanel:viewPreview') : t('filePanel:viewSource')}
            >
              {htmlSource ? <Eye className="h-4 w-4" /> : <Code2 className="h-4 w-4" />}
            </button>
          )}
          {/* DALTON-HOOK(external-share-guard): stop an already-public link or historical public monitor. */}
          {actions.showUnshareOnly && openUnshareOnly && (
            <button
              onClick={() => openUnshareOnly(unsharePath ?? selectedFile)}
              className="file-panel-icon-btn"
              title={t('artifact.unshareOnly')}
              aria-label={t('artifact.unshareOnly')}
              disabled={isPendingHtmlDoc}
            >
              <Link2Off className="h-4 w-4" />
            </button>
          )}
          {/* An HTML file takes comments for owner panels, so its menu also says where they go. */}
          {/* DALTON-HOOK(monitors): non-owner own-workspace panels (external members) open PublishDialog directly */}
          {actions.showPublish && openPublish && (actions.showOwnerArtifactMenu ? (
            <ArtifactMenu
              workspaceId={workspaceId}
              path={artifactDoc ?? selectedFile}
              onPublish={openPublish}
              disabled={isPendingHtmlDoc}
            />
          ) : (
            <button
              onClick={() => openPublish(selectedFile)}
              className="file-panel-icon-btn"
              title={t('artifact.publish')}
              aria-label={t('artifact.publish')}
              disabled={isPendingHtmlDoc}
            >
              <Link2 className="h-4 w-4" />
            </button>
          ))}
          {/* The file's own page in a new tab: its /a/ link, as the server names
              it, for the owner; the share's served bytes on a share. Never the
              grant URL, which expires. */}
          {actions.showOpenUrl && openUrl && (
            <button
              onClick={() => window.open(openUrl, '_blank', 'noopener,noreferrer')}
              className="file-panel-icon-btn"
              title={t('filePanel:openInNewTab')}
              aria-label={t('filePanel:openInNewTab')}
              disabled={isPendingHtmlDoc}
            >
              <ExternalLink className="h-4 w-4" />
            </button>
          )}
          {actions.showDownload && (
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <button
                  // DALTON-HOOK(mobile-web): expand coarse-pointer touch target on non-HTML file header actions
                  className="file-panel-icon-btn tap-target"
                  aria-label={t('filePanel:downloadOptions') ?? 'Download options'}
                  disabled={isPendingHtmlDoc}
                >
                  <Download className="h-4 w-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" sideOffset={4}>
                {renderDropdownItems()}
              </DropdownMenuContent>
            </DropdownMenu>
          )}

          {actions.showEdit && (
            <button
              onClick={onStartEdit}
              className="file-panel-icon-btn tap-target"
              title={t('filePanel:editFile')}
              disabled={isPendingHtmlDoc}
            >
              <Pencil className="h-4 w-4" />
            </button>
          )}
        </>
      )}
      {/* DALTON-HOOK(mobile-web): end mobile HTML overflow branch */}
    </>
  );
}

// DALTON-HOOK(mobile-web): shared owner ArtifactMenu state (goesLine, bind/unbind visibility & mutations)
// consumed by both desktop ArtifactMenu and mobile MobileHtmlOverflowMenu.
function useArtifactMenuState({
  workspaceId,
  path,
  enabled = true,
  fallbackArtifact = null,
}: {
  workspaceId: string;
  path: string;
  enabled?: boolean;
  fallbackArtifact?: import('@/types/api').Artifact | null;
}) {
  const { t } = useTranslation();
  const contextClient = useContext(QueryClientContext);
  const monitorQueryClient = contextClient ?? FALLBACK_MONITOR_QUERY_CLIENT;
  const root = useWorkspaceFolders()?.rootDir;
  const { data: queriedArtifact } = useArtifactByPath(enabled ? workspaceId : null, enabled ? path : null, { root });
  const artifact = queriedArtifact ?? fallbackArtifact;
  const artifactId = artifact?.artifact_id ?? null;
  // DALTON-HOOK(monitors): read attached monitor so owner Manage label recognizes historical public monitors even when artifact.shared is false
  const { data: artifactMonitor } = useQuery<MonitorSummary | null>(
    {
      queryKey: queryKeys.daltonMonitors.byArtifact(artifactId ?? ''),
      queryFn: () => getMonitorByArtifact(artifactId!),
      enabled: Boolean(contextClient && enabled && artifactId),
      retry: false,
    },
    monitorQueryClient,
  );
  const hasPublicMonitor = artifactMonitor?.visibility === 'public';
  const sender = useArtifactCommentSender();
  const { besideThreadId } = sender;
  const { choices, canBind, targetFor } = useCommentDestinations(
    enabled ? workspaceId : '',
    artifactPathKey(path, root),
  );
  const { bind, unbind } = useArtifactBinding();

  const goesTo = choices.find((c) => c.isDefault) ?? null;
  const goesLine = !goesTo
    ? null
    : !('threadId' in goesTo.target)
      ? t('artifact.commentsGoNew')
      : goesTo.target.threadId === besideThreadId
        ? t('artifact.commentsGoHere')
        : t('artifact.commentsGoTo', { thread: goesTo.label });
  const bound = artifact?.bound_thread_id ?? null;
  // DALTON-HOOK(mobile-web): shared bind/unbind visibility and mutation handlers for desktop & mobile HTML menus
  const showBind = Boolean(canBind && besideThreadId);
  const showUnbind = Boolean(bound && artifactId);

  const onBind = () => {
    if (!besideThreadId) return;
    bind.mutate(
      { workspaceId, path, threadId: besideThreadId },
      { onError: () => toast({ description: t('pageViewer:comments.bindFailed'), variant: 'destructive' }) },
    );
  };
  const onUnbind = () => {
    if (!artifactId) return;
    unbind.mutate(artifactId, {
      onError: () => toast({ description: t('artifact.unbindFailed'), variant: 'destructive' }),
    });
  };

  return {
    artifact,
    artifactId,
    hasPublicMonitor,
    sender,
    targetFor,
    goesLine,
    showBind,
    showUnbind,
    onBind,
    onUnbind,
  };
}

/**
 * An HTML file's Publish, and where its comments go: the default the comment
 * box would send to (`useCommentDestinations`), said once the server has
 * answered for the file. Binding to the thread beside makes it the file's
 * destination from anywhere, the standalone view included, until it is
 * unbound. `path` is the document the frame shows, so after a link inside it
 * the menu is about the file being commented on, not the one the tab opened.
 */
function ArtifactMenu({ workspaceId, path, onPublish, disabled = false }: {
  workspaceId: string;
  path: string;
  onPublish: (path: string) => void;
  // DALTON-HOOK(mobile-web): disable publish/bind menu trigger while framed HTML doc identity is pending
  disabled?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  // DALTON-HOOK(mobile-web): reuse shared ArtifactMenu state hook with MobileHtmlOverflowMenu
  const menu = useArtifactMenuState({ workspaceId, path });

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button className="file-panel-icon-btn" title={t('artifact.publish')} aria-label={t('artifact.publish')} disabled={disabled}>
          <Link2 className="h-4 w-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={4} className="min-w-56 max-w-72">
        <DropdownMenuItem onSelect={() => onPublish(path)}>
          <Link2 className="h-3.5 w-3.5" />
          {/* DALTON-HOOK(monitors): show Manage label for active shares and historical public monitors */}
          {menu.artifact?.shared || menu.hasPublicMonitor ? t('artifact.manage') : t('artifact.publishEllipsis')}
        </DropdownMenuItem>
        {(menu.goesLine || menu.showBind || menu.showUnbind) && <DropdownMenuSeparator />}
        {menu.goesLine && (
          <DropdownMenuLabel className="truncate text-xs font-normal" style={{ color: 'var(--color-text-tertiary)' }}>
            {menu.goesLine}
          </DropdownMenuLabel>
        )}
        {menu.showBind && (
          <DropdownMenuItem onSelect={menu.onBind}>
            <Pin className="h-3.5 w-3.5" />
            {t('pageViewer:comments.bindHere')}
          </DropdownMenuItem>
        )}
        {menu.showUnbind && (
          <DropdownMenuItem onSelect={menu.onUnbind}>
            <PinOff className="h-3.5 w-3.5" />
            {t('artifact.unbind')}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// DALTON-HOOK(mobile-web): complete mobile HTML overflow menu carrying owner ArtifactMenu, comment/send-all, dashboard pin, versions/update, and file actions
const MOBILE_LIVE_VERSION = 'live';

function MobileHtmlOverflowMenu({
  workspaceId,
  selectedFile,
  linkedDocPath,
  isPendingHtmlDoc,
  onOpenDocAsTab,
  access,
  actions,
  openPublish,
  openUnshareOnly,
  htmlSource,
  onToggleHtmlSource,
  openUrl,
  onStartEdit,
  download,
  downloadPending,
  onExportHtmlPdf,
  pdfBranding,
  onTogglePdfBranding,
  pdfPageNumbers,
  onTogglePdfPageNumbers,
  pdfScale,
  onSelectPdfScale,
  mobileHtmlTools,
}: {
  workspaceId: string;
  selectedFile: string;
  // DALTON-HOOK(mobile-web): linked document path and pending flag derived from HtmlFrameDocState
  linkedDocPath: string | null;
  isPendingHtmlDoc: boolean;
  onOpenDocAsTab: ((path: string) => void) | null;
  access: PanelAccess;
  actions: HeaderActionFlags;
  openPublish: ((path: string) => void) | null;
  openUnshareOnly: ((path: string) => void) | null;
  htmlSource: boolean;
  onToggleHtmlSource: (() => void) | null;
  openUrl: string | null;
  onStartEdit: () => void;
  download: () => void;
  downloadPending: boolean;
  onExportHtmlPdf: () => Promise<void>;
  pdfBranding: boolean;
  onTogglePdfBranding: () => void;
  pdfPageNumbers: boolean;
  onTogglePdfPageNumbers: () => void;
  pdfScale: number;
  onSelectPdfScale: (scale: number) => void;
  mobileHtmlTools: MobileHtmlTools | null;
}): React.ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const root = useWorkspaceFolders()?.rootDir;
  const isOwner = access.kind === 'owner';
  // DALTON-HOOK(mobile-web): reuse linkedDocPath resolved from unified HtmlFrameDocState
  const isLinkedDoc = Boolean(linkedDocPath);
  const commentDocPath = isLinkedDoc && linkedDocPath ? linkedDocPath : selectedFile;
  const commentDocKey = artifactPathKey(commentDocPath, root);

  const versionView = !isLinkedDoc ? (mobileHtmlTools?.versionView ?? null) : null;
  const menu = useArtifactMenuState({
    workspaceId,
    path: selectedFile,
    enabled: isOwner && !isLinkedDoc,
    fallbackArtifact: versionView?.record ?? null,
  });
  const { data: linkedArtifact } = useArtifactByPath(
    isOwner && isLinkedDoc ? workspaceId : null,
    isLinkedDoc ? commentDocKey : null,
    { root },
  );
  const artifact = !isLinkedDoc ? menu.artifact : null;
  const commentArtifactId = isLinkedDoc ? (linkedArtifact?.artifact_id ?? null) : menu.artifactId;
  const { targetFor: commentTargetFor } = useCommentDestinations(
    isOwner && isLinkedDoc ? workspaceId : '',
    commentDocKey,
  );
  const commentsQuery = useArtifactComments(mobileHtmlTools ? commentArtifactId : null);
  const comments = commentsQuery.data ?? [];
  const openCount = openComments(comments).length;
  const queued = comments.filter((c) => c.status === 'open' && c.delivery === 'queued' && !c.thread_hidden);
  const [sendingAll, setSendingAll] = useState(false);
  const [sendAllNotice, setSendAllNotice] = useState('');
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [pdfOptionsOpen, setPdfOptionsOpen] = useState(false);
  const pickedVersionRef = useRef(false);

  useEffect(() => {
    if (!sendAllNotice) return;
    const timer = window.setTimeout(() => setSendAllNotice(''), OUTCOME_MS);
    return () => window.clearTimeout(timer);
  }, [sendAllNotice]);

  const pin = usePinPageAsDashboard(
    selectedFile,
    { workspaceId, threadId: mobileHtmlTools?.threadId ?? null },
    root,
  );

  // DALTON-HOOK(mobile-web): reuse desktop ArtifactCrumbs state & in-place Update handler verbatim
  const viewingVersion = versionView?.version ?? null;
  const updatedTo = versionView?.updatedTo ?? null;
  const announcing = updatedTo !== null;
  const crumbs = useArtifactCrumbsState({
    artifact,
    version: viewingVersion,
    onReview: () => openPublish?.(selectedFile),
    readAt: mobileHtmlTools?.readAt ?? null,
    enabled: Boolean(!isLinkedDoc && actions.showArtifactCrumbs && versionView),
    updateDisabled: isPendingHtmlDoc,
  });
  const latestSeq = crumbs.latest;
  const shared = crumbs.shared;
  const visitorsAt = sharedSeq(artifact);
  const refused = crumbs.refused;
  const changed = crumbs.changed;
  const versionList = versionView?.versions ?? [];

  const versionFaceLabel = viewingVersion === null
    ? visitorsAt > 0
      ? t('artifact.publishedVersion', { seq: visitorsAt })
      : shared ? t('publish:published') : t('artifact.unsharedVersion', { seq: latestSeq })
    : announcing
      ? t('filePanel:pageUpdated', { seq: viewingVersion })
      : t('filePanel:pageVersion', { seq: viewingVersion });
  const baseVersionAria = viewingVersion === null
    ? t('artifact.liveStatusName', { status: versionFaceLabel })
    : announcing
      ? versionFaceLabel
      : t('filePanel:pageVersionName', { seq: viewingVersion });
  const versionStatusNote = refused
    ? t('artifact.legacyRefused')
    : changed
      ? t('artifact.changedSince')
      : null;
  const versionStatusFullNote = refused
    ? t('publish:legacyRefused')
    : changed
      ? t('publish:changedSince', { seq: visitorsAt > 0 ? visitorsAt : latestSeq })
      : null;
  const versionTriggerAriaLabel = [
    t('filePanel:pageVersions'),
    baseVersionAria,
    versionStatusNote,
  ].filter(Boolean).join(' · ');

  // DALTON-HOOK(mobile-web): comment mode, list state, and Send All handler with live-region announcement
  const commentMode = mobileHtmlTools ? mobileHtmlTools.ui.modeTab === mobileHtmlTools.tabId : false;
  const commentListOpen = mobileHtmlTools ? mobileHtmlTools.ui.listTab === mobileHtmlTools.tabId : false;
  const handleSendAll = () => {
    if (sendingAll || !mobileHtmlTools || !commentArtifactId) return;
    const waiting = queued
      .filter((c) => mobileHtmlTools.ui.sends.get(c.comment_id)?.phase !== 'sending')
      .map((c) => ({ ...c, workspace_id: workspaceId }));
    const batches = queuedBatches(waiting, isLinkedDoc ? commentTargetFor : menu.targetFor);
    if (batches.length === 0) return;
    setSendingAll(true);
    setSendAllNotice('');
    void sendBatches(batches, {
      sender: menu.sender,
      sends: mobileHtmlTools.ui.sends,
      reasonOf: (err) => refusalReason(t, err, true),
    }).then((outcomes) => {
      setSendingAll(false);
      const summary = summarizeSendAllOutcome(t, locale, outcomes, menu.sender.besideThreadId);
      setSendAllNotice([summary.sent, summary.refused].filter(Boolean).join(' · '));
      refreshArtifactComments(queryClient, [{ artifactId: commentArtifactId, workspaceId }], ALL);
    });
  };

  const hasVersionSection = Boolean(!isLinkedDoc && versionView && crumbs.visible);
  const hasOwnerPublishSection = Boolean(!isLinkedDoc && (actions.showPublish || actions.showUnshareOnly));
  const hasSecondarySection = Boolean(!isLinkedDoc && (actions.showToggleHtmlSource || actions.showOpenUrl || actions.showEdit || actions.showDownload));

  return (
    <>
      {/* DALTON-HOOK(mobile-web): keep ArtifactCrumbs UpdateReview mounted outside DropdownMenuContent so closing the menu on click still finishes in-place publish */}
      {crumbs.reviewNode}
      {/* DALTON-HOOK(mobile-web): persistent sr-only status announcements for Send All outcome and version updates */}
      <span className="sr-only" role="status" data-testid="file-header-mobile-send-all-status">{sendAllNotice}</span>
      <span className="sr-only" role="status" data-testid="file-header-mobile-version-status">{announcing ? versionFaceLabel : ''}</span>
      <DropdownMenu
        modal={false}
        onOpenChange={(open) => {
          if (!open) {
            setVersionsOpen(false);
            setPdfOptionsOpen(false);
          }
        }}
      >
        <DropdownMenuTrigger asChild>
          {/* DALTON-HOOK(mobile-web): single mobile HTML More trigger with data-artifact-version-picker so opening Versions keeps an active comment draft */}
          <button
            type="button"
            className="file-panel-icon-btn tap-target"
            aria-label={t('common.more')}
            title={t('common.more')}
            data-testid="file-header-mobile-more"
            data-artifact-version-picker={mobileHtmlTools ? '' : undefined}
          >
            <MoreHorizontal className="h-4 w-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          sideOffset={4}
          className="min-w-56 max-w-[min(18rem,calc(100vw-1rem))] max-h-[min(70dvh,30rem)] overflow-y-auto"
          data-testid="file-header-mobile-more-menu"
          data-artifact-version-picker={mobileHtmlTools ? '' : undefined}
          onCloseAutoFocus={(e) => {
            if (!pickedVersionRef.current) return;
            pickedVersionRef.current = false;
            const field = document.querySelector<HTMLTextAreaElement>('textarea[data-page-draft]');
            if (!field) return;
            e.preventDefault();
            field.focus({ preventScroll: true });
            field.setSelectionRange(field.value.length, field.value.length);
          }}
        >
          {/* DALTON-HOOK(mobile-web): when viewing an in-frame linked report B inside tab A, offer only "Open in its own tab" + comments */}
          {isLinkedDoc && linkedDocPath && (
            <>
              {onOpenDocAsTab && (
                <DropdownMenuItem
                  onSelect={() => onOpenDocAsTab(linkedDocPath)}
                  data-testid="file-header-mobile-open-doc-tab"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  <span className="flex-1">{t('filePanel:openReportInTab')}</span>
                </DropdownMenuItem>
              )}
              {onOpenDocAsTab && actions.showCommentTools && mobileHtmlTools && <DropdownMenuSeparator />}
            </>
          )}

          {actions.showCommentTools && mobileHtmlTools && (
            <>
              <DropdownMenuItem
                disabled={mobileHtmlTools.modeDisabled}
                onSelect={() => mobileHtmlTools.ui.setMode(mobileHtmlTools.tabId, !commentMode)}
                data-testid="file-header-mobile-comment"
              >
                <MessageSquarePlus className="h-3.5 w-3.5" />
                <span className="flex-1">{t('pageViewer:comments.comment')}</span>
                {commentMode && <Check className="h-3.5 w-3.5" />}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => mobileHtmlTools.ui.setList(mobileHtmlTools.tabId, !commentListOpen)}
                data-testid="file-header-mobile-comment-list"
              >
                <MessageSquare className="h-3.5 w-3.5" />
                <span className="flex-1">
                  {openCount > 0 ? `${t('pageViewer:comments.listTitle')} (${openCount})` : t('pageViewer:comments.listTitle')}
                </span>
                {commentListOpen && <Check className="h-3.5 w-3.5" />}
              </DropdownMenuItem>
              {/* DALTON-HOOK(mobile-web): batch-send queued comments from mobile More menu */}
              {queued.length > 0 && (
                <DropdownMenuItem
                  disabled={sendingAll}
                  onSelect={handleSendAll}
                  data-testid="file-header-mobile-send-all"
                >
                  <ArrowUp className="h-3.5 w-3.5" />
                  <span className="flex-1">
                    {sendingAll
                      ? t('pageViewer:comments.sending')
                      : `${t('pageViewer:comments.sendAll')} (${queued.length})`}
                  </span>
                </DropdownMenuItem>
              )}
              {!isLinkedDoc && actions.showPinDashboard && (
                <DropdownMenuItem
                  disabled={pin.pending || isPendingHtmlDoc}
                  onSelect={pin.pinIt}
                  data-testid="file-header-mobile-pin-dashboard"
                >
                  <House className="h-3.5 w-3.5" style={pin.pinned ? { color: 'var(--color-accent-primary)' } : undefined} />
                  <span className="flex-1">
                    {pin.pinned ? t('customDashboard:isDashboard') : t('customDashboard:setAsDashboard')}
                  </span>
                  {pin.pinned && <Check className="h-3.5 w-3.5" />}
                </DropdownMenuItem>
              )}
              {(hasVersionSection || hasOwnerPublishSection || hasSecondarySection) && <DropdownMenuSeparator />}
            </>
          )}

          {/* DALTON-HOOK(mobile-web): inline-expandable version picker & desktop-shared Update action inside mobile More menu (never overflows 390px) */}
          {hasVersionSection && versionView && (
            <>
              {crumbs.showUpdate && openPublish && (
                <DropdownMenuItem
                  disabled={crumbs.updatePending || isPendingHtmlDoc}
                  onSelect={isPendingHtmlDoc ? undefined : crumbs.onUpdate}
                  data-testid="file-header-mobile-update"
                  title={versionStatusFullNote ?? undefined}
                >
                  <RefreshCw className="h-3.5 w-3.5 shrink-0" style={refused ? { color: 'var(--color-warning)' } : undefined} />
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span>{t('artifact.update')}</span>
                    {versionStatusNote && (
                      <span
                        className="truncate text-xs"
                        style={{ color: refused ? 'var(--color-warning)' : 'var(--color-text-tertiary)' }}
                      >
                        {versionStatusNote}
                      </span>
                    )}
                  </span>
                </DropdownMenuItem>
              )}
              {crumbs.showVersions && (
                <>
                  <DropdownMenuItem
                    data-testid="file-header-mobile-versions"
                    data-artifact-version-picker=""
                    aria-expanded={versionsOpen}
                    aria-label={versionTriggerAriaLabel}
                    title={versionStatusFullNote ?? baseVersionAria}
                    onSelect={(e) => {
                      e.preventDefault();
                      setVersionsOpen((open) => !open);
                    }}
                  >
                    <History className="h-3.5 w-3.5 shrink-0" />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate">{t('filePanel:pageVersions')}</span>
                      <span className="truncate text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                        {versionFaceLabel}
                        {versionStatusNote ? ` · ${versionStatusNote}` : ''}
                      </span>
                    </span>
                    <ChevronDown
                      className={cn('h-3.5 w-3.5 shrink-0 transition-transform', versionsOpen && 'rotate-180')}
                      aria-hidden="true"
                    />
                  </DropdownMenuItem>
                  {versionsOpen && (
                    <div
                      data-testid="file-header-mobile-versions-list"
                      data-artifact-version-picker=""
                      className="my-1 ml-2 border-l pl-1"
                      style={{ borderColor: 'var(--color-border-muted)' }}
                    >
                      <DropdownMenuRadioGroup
                        value={viewingVersion === null ? MOBILE_LIVE_VERSION : String(viewingVersion)}
                        onValueChange={(v) => {
                          pickedVersionRef.current = true;
                          versionView.pick(v === MOBILE_LIVE_VERSION ? null : Number(v));
                        }}
                      >
                        <DropdownMenuRadioItem value={MOBILE_LIVE_VERSION} className="items-start">
                          <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" style={{ opacity: viewingVersion === null ? 1 : 0 }} aria-hidden="true" />
                          <span className="flex min-w-0 flex-col">
                            <span>{t('artifact.live')}</span>
                            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('artifact.liveHint')}</span>
                          </span>
                        </DropdownMenuRadioItem>
                        {versionList.length > 0 && <DropdownMenuSeparator />}
                        {versionList.map((v) => (
                          <DropdownMenuRadioItem
                            key={v.seq}
                            value={String(v.seq)}
                            className="items-start"
                          >
                            <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" style={{ opacity: v.seq === viewingVersion ? 1 : 0 }} aria-hidden="true" />
                            <span className="flex min-w-0 flex-1 flex-col">
                              <span className="flex flex-wrap items-center gap-1.5">
                                <span>{t('filePanel:pageVersion', { seq: v.seq })}</span>
                                {v.seq === latestSeq && (
                                  <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('filePanel:pageLatest')}</span>
                                )}
                                {v.seq === visitorsAt && visitorsAt !== latestSeq && (
                                  <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('filePanel:pageShared')}</span>
                                )}
                                <span className="ml-auto pl-2 text-xs tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
                                  {shortMonthDay(new Date(v.published_at), locale)}
                                </span>
                              </span>
                              {v.label && (
                                <span className="truncate text-xs" style={{ color: 'var(--color-text-tertiary)' }} title={v.label}>{v.label}</span>
                              )}
                            </span>
                          </DropdownMenuRadioItem>
                        ))}
                      </DropdownMenuRadioGroup>
                    </div>
                  )}
                </>
              )}
              {(hasOwnerPublishSection || hasSecondarySection) && <DropdownMenuSeparator />}
            </>
          )}

          {/* DALTON-HOOK(mobile-web): owner ArtifactMenu publish & thread binding/unbinding (same visibility flags & state as desktop) */}
          {!isLinkedDoc && actions.showPublish && openPublish && (
            <DropdownMenuItem disabled={isPendingHtmlDoc} onSelect={() => openPublish(selectedFile)} data-testid="file-header-mobile-publish">
              <Link2 className="h-3.5 w-3.5" />
              {actions.showOwnerArtifactMenu && (menu.artifact?.shared || menu.hasPublicMonitor) ? t('artifact.manage') : t('artifact.publishEllipsis')}
            </DropdownMenuItem>
          )}
          {!isLinkedDoc && actions.showOwnerArtifactMenu && menu.goesLine && (
            <DropdownMenuLabel className="truncate text-xs font-normal" style={{ color: 'var(--color-text-tertiary)' }}>
              {menu.goesLine}
            </DropdownMenuLabel>
          )}
          {!isLinkedDoc && actions.showOwnerArtifactMenu && menu.showBind && (
            <DropdownMenuItem
              disabled={isPendingHtmlDoc}
              data-testid="file-header-mobile-bind"
              onSelect={menu.onBind}
            >
              <Pin className="h-3.5 w-3.5" />
              {t('pageViewer:comments.bindHere')}
            </DropdownMenuItem>
          )}
          {!isLinkedDoc && actions.showOwnerArtifactMenu && menu.showUnbind && (
            <DropdownMenuItem
              disabled={isPendingHtmlDoc}
              data-testid="file-header-mobile-unbind"
              onSelect={menu.onUnbind}
            >
              <PinOff className="h-3.5 w-3.5" />
              {t('artifact.unbind')}
            </DropdownMenuItem>
          )}
          {!isLinkedDoc && actions.showUnshareOnly && openUnshareOnly && (
            <DropdownMenuItem disabled={isPendingHtmlDoc} onSelect={() => openUnshareOnly(selectedFile)} data-testid="file-header-mobile-unshare">
              <Link2Off className="h-3.5 w-3.5" />
              {t('artifact.unshareOnly')}
            </DropdownMenuItem>
          )}
          {hasOwnerPublishSection && hasSecondarySection && <DropdownMenuSeparator />}

          {/* DALTON-HOOK(mobile-web): secondary HTML actions (same visibility flags as desktop) */}
          {!isLinkedDoc && actions.showToggleHtmlSource && onToggleHtmlSource && (
            <DropdownMenuItem onSelect={onToggleHtmlSource} data-testid="file-header-mobile-source">
              {htmlSource ? <Eye className="h-3.5 w-3.5" /> : <Code2 className="h-3.5 w-3.5" />}
              {htmlSource ? t('filePanel:viewPreview') : t('filePanel:viewSource')}
            </DropdownMenuItem>
          )}
          {!isLinkedDoc && actions.showOpenUrl && openUrl && (
            <DropdownMenuItem
              disabled={isPendingHtmlDoc}
              onSelect={() => window.open(openUrl, '_blank', 'noopener,noreferrer')}
              data-testid="file-header-mobile-open-url"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t('filePanel:openInNewTab')}
            </DropdownMenuItem>
          )}
          {!isLinkedDoc && actions.showEdit && (
            <DropdownMenuItem disabled={isPendingHtmlDoc} onSelect={onStartEdit} data-testid="file-header-mobile-edit">
              <Pencil className="h-3.5 w-3.5" />
              {t('filePanel:editFile')}
            </DropdownMenuItem>
          )}
          {!isLinkedDoc && actions.showDownload && (actions.showToggleHtmlSource || actions.showOpenUrl || actions.showEdit) && <DropdownMenuSeparator />}
          {!isLinkedDoc && actions.showDownload && (
            <>
              {/* DALTON-HOOK(mobile-web): disable download and PDF export while HTML frame doc state is pending */}
              <DropdownMenuItem onSelect={download} disabled={downloadPending || isPendingHtmlDoc} data-testid="file-header-mobile-download">
                <Download className="h-3.5 w-3.5" />
                {t('filePanel:download')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={isPendingHtmlDoc} onSelect={() => void onExportHtmlPdf()} data-testid="file-header-mobile-save-pdf">
                <FileDown className="h-3.5 w-3.5" />
                {t('filePanel:saveAsPdf')}
              </DropdownMenuItem>
              <DropdownMenuItem
                data-testid="file-header-mobile-pdf-options"
                aria-expanded={pdfOptionsOpen}
                onSelect={(e) => {
                  e.preventDefault();
                  setPdfOptionsOpen((open) => !open);
                }}
              >
                <Settings2 className="h-3.5 w-3.5 shrink-0" />
                <span className="flex-1">{t('filePanel:pdfOptions')}</span>
                <ChevronDown
                  className={cn('h-3.5 w-3.5 shrink-0 transition-transform', pdfOptionsOpen && 'rotate-180')}
                  aria-hidden="true"
                />
              </DropdownMenuItem>
              {pdfOptionsOpen && (
                <div
                  data-testid="file-header-mobile-pdf-options-list"
                  className="my-1 ml-2 border-l pl-1"
                  style={{ borderColor: 'var(--color-border-muted)' }}
                >
                  <DropdownMenuItem
                    onSelect={(e) => {
                      e.preventDefault();
                      onTogglePdfBranding();
                    }}
                  >
                    <Check className={cn('h-3.5 w-3.5', !pdfBranding && 'invisible')} />
                    {t('filePanel:pdfBranding')}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={(e) => {
                      e.preventDefault();
                      onTogglePdfPageNumbers();
                    }}
                  >
                    <Check className={cn('h-3.5 w-3.5', !pdfPageNumbers && 'invisible')} />
                    {t('filePanel:pdfPageNumbers')}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuLabel>{t('filePanel:pdfScale')}</DropdownMenuLabel>
                  {PDF_SCALE_CHOICES.map((scale) => (
                    <DropdownMenuItem
                      key={scale}
                      onSelect={(e) => {
                        e.preventDefault();
                        onSelectPdfScale(scale);
                      }}
                    >
                      <Check className={cn('h-3.5 w-3.5', pdfScale !== scale && 'invisible')} />
                      {Math.round(scale * 100)}%
                    </DropdownMenuItem>
                  ))}
                </div>
              )}
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}

export default FileHeaderActions;
