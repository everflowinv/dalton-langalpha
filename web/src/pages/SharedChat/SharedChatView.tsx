import React, { useState, useEffect, useRef, useCallback, useMemo, useContext } from 'react'; // DALTON-HOOK(share-redaction)
import { Link } from 'react-router';
import { QueryClientContext } from '@tanstack/react-query'; // DALTON-HOOK(share-redaction)
import { BRAND_NAME } from '@/config/brand'; // DALTON-HOOK(brand-name)
import { ArrowLeft, FolderOpen, PanelRight } from 'lucide-react';
// DALTON-HOOK(mobile-web): PanInfo no longer needed after disabling content-area drag-to-close on mobile FilePanel
import { motion } from '@/lib/framer';
import { TWEEN } from '@/lib/motion';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Loader } from '@/components/ui/loader';
import { cn } from '@/lib/utils';
import MessageList from '../ChatAgent/components/MessageList';
import {
  MessageActionsProvider,
  READ_ONLY_MESSAGE_ACTIONS,
  type MessageActions,
} from '../ChatAgent/components/messageList/MessageActionsContext';
import FilePanel, { type PanelTarget } from '../ChatAgent/components/FilePanel';
import { dirTarget, fileTarget, stampTarget, type ChartTabSpec } from '../ChatAgent/components/filePanel/types';
import type { FileTab } from '../ChatAgent/components/filePanel/useFileTabs';
import { handOffToggleFocus } from '../ChatAgent/components/filePanel/panelToggleFocus';
import { CHART_SURFACE_MIN_WIDTH } from '@/pages/MarketView/components/chartSurfaceLayout';
import { WorkspaceProvider } from '../ChatAgent/contexts/WorkspaceContext';
import { useAuth } from '../../contexts/AuthContext';
import { useTranslation } from 'react-i18next';
import { toast } from '@/components/ui/use-toast';
import {
  handleHistoryUserMessage,
  handleHistorySteeringDelivered, // DALTON-HOOK(share-redaction): replay steering follow-ups and notices in shared chats
  handleHistoryReasoningSignal,
  handleHistoryReasoningContent,
  handleHistoryTextContent,
  handleHistoryToolCalls,
  handleHistoryToolCallResult,
  handleHistoryTaskArtifactStatus,
  handleHistoryHtmlWidget, // DALTON-HOOK(shared-widgets): replay inline widgets in shared chats
} from '../ChatAgent/hooks/utils/historyEventHandlers';
import type { PairState } from '../ChatAgent/hooks/utils/historyEventHandlers';
// DALTON-HOOK(share-redaction): permission-downgrade detection, shared image/file cache revocation, and redaction context
import { apiErrorStatus } from '@/api/errors';
import { SharedRedactionProvider } from '@/components/dalton/RedactedSharePlaceholders';
import { buildSharedImageScope, revokeSharedImageScope } from '@/lib/dalton/sharedImageCache';
import {
  classifyShareForbidden,
  clearSharedFileBodies,
  createShareFileEpoch,
  isShareDowngradeActive,
  isShareDownloadHandled,
  runGuardedShareCardDownload,
  runGuardedShareFileRead,
  type ShareDowngradeRecord,
  type ShareFileEpoch,
  type ShareForbiddenSource,
} from '@/lib/dalton/shareFileGuard';
import {
  replaySharedThread,
  getSharedFiles,
  readSharedFile,
  resolveSharedFile,
  downloadSharedFile,
  servedBytes,
  servedObjectUrl,
  sharedServePrefix,
} from './api';
import type { SharedThreadMetadata, SSEEvent } from './api';
import ShareUnavailable from './ShareUnavailable';
import type { TextSegment } from '@/types/chat';
import { isTaskAgentId } from '../ChatAgent/utils/agentId';
import type { FileLocation } from '../ChatAgent/utils/fileLocation';
import { computeAgentArtifactRouting } from '../ChatAgent/utils/agentArtifactRouting';
import { computerFolders, type ComputerFolders } from '../ChatAgent/utils/agentPaths';
import { collectRecentWritePaths, downloadTarget, type TurnMessage } from '../ChatAgent/utils/fileRefResolver';
import { useStableHandler } from '@/hooks/useStableHandler';
import { DispatchStatusProvider } from '../ChatAgent/hooks/usePTCDispatchStatus';
import { useIsMobile } from '@/hooks/useIsMobile';
import type { HtmlWidgetData, MessageRecord, SetMessages } from '../ChatAgent/hooks/utils/types';
import { applyPlanSnapshot, readPlanSteps } from '@/pages/ChatAgent/session/planUpdate';
import { eventOrder } from '@/pages/ChatAgent/session/eventOrder';
import { threadPlans } from '../ChatAgent/components/messageList/requestPlans';


/** The divider's bounds: a floor any tab fits, and a share of the window the thread keeps. */
const PANEL_MIN_WIDTH = 280;
const PANEL_MAX_RATIO = 0.6;
/** A chart opens wide, as it does beside the owner's chat, so its toolbar has room. */
const CHART_MAX_RATIO = 0.92;

/**
 * SharedChatView — Public read-only view of a shared conversation.
 * Mirrors ChatView layout exactly, with interactive operations disabled.
 * Accessible at /s/:shareToken without authentication.
 */
interface SharedChatViewProps {
  shareToken: string;
  /** What the replay and file routes are asked with: a member's grant, which
   *  the page renews, or the link's own token. */
  access?: string;
  /** Resolved by `SharePage`; the replay starts from it. */
  metadata: SharedThreadMetadata;
  // DALTON-HOOK(share-redaction): metadata refresh stamp for allow_files=true serve URLs and 403 revalidation hook
  refreshStamp?: number;
  onPermissionForbidden?: () => void;
}

// DALTON-HOOK(share-redaction): replay one shared history page (initial or beforeTurn) into an isolated message list
async function replaySharedPage(
  token: string,
  opts: { beforeTurn?: number; signal?: AbortSignal } = {},
): Promise<{ messages: MessageRecord[]; firstTurnIndex: number | null; hasMore: boolean }> {
  let pageMessages: MessageRecord[] = [];
  let firstTurnIndex: number | null = null;
  let hasMore = false;
  const assistantMessagesByPair = new Map<number, string>();
  const pairStateByPair = new Map<number, PairState>();
  const runSettledAtByPair = new Map<number, number>();
  const sharedRefs = {
    recentlySentTracker: { isRecentlySent: (_content: string) => false },
    currentMessageRef: { current: null as string | null },
    newMessagesStartIndexRef: { current: 0 },
  };
  const setPageMessages: SetMessages = (updater) => {
    pageMessages = updater(pageMessages);
  };
  const stampSettledTurns = () => {
    if (runSettledAtByPair.size === 0) return;
    const settledAtByMessageId = new Map<string, number>();
    for (const [pairIndex, settledAt] of runSettledAtByPair) {
      const tailId = assistantMessagesByPair.get(pairIndex);
      if (tailId) settledAtByMessageId.set(tailId, settledAt);
    }
    if (settledAtByMessageId.size === 0) return;
    pageMessages = pageMessages.map((msg) => {
      const settledAt = settledAtByMessageId.get(msg.id as string);
      return msg.role === 'assistant' && settledAt !== undefined ? { ...msg, completedAt: settledAt } : msg;
    });
  };

  await replaySharedThread(token, (event: SSEEvent) => {
    if (opts.signal?.aborted) return;
    const eventType = event.event as string | undefined;
    const contentType = event.content_type as string | undefined;
    const hasRole = event.role !== undefined;
    const hasPairIndex = event.turn_index !== undefined;

    // DALTON-HOOK(share-redaction): track history_page pagination cursor
    if (eventType === 'history_page') {
      if (typeof event.first_turn_index === 'number') firstTurnIndex = event.first_turn_index;
      hasMore = event.has_more === true;
      return;
    }

    if (eventType === 'replay_done') {
      stampSettledTurns();
      return;
    }

    if (isTaskAgentId(event.agent)) return;

    if (eventType === 'user_message' && hasPairIndex) {
      if (typeof event.run_completed_at === 'string') {
        const settledAt = Date.parse(event.run_completed_at);
        if (Number.isFinite(settledAt)) runSettledAtByPair.set(event.turn_index as number, settledAt);
      }
      handleHistoryUserMessage({
        event,
        pairIndex: event.turn_index as number,
        assistantMessagesByPair,
        pairStateByPair,
        refs: sharedRefs,
        setMessages: setPageMessages,
      });
      return;
    }

    // DALTON-HOOK(share-redaction): replay steering_delivered follow-ups and notices in shared view
    if (eventType === 'steering_delivered' && hasPairIndex) {
      handleHistorySteeringDelivered({
        event,
        pairIndex: event.turn_index as number,
        assistantMessagesByPair,
        pairStateByPair,
        refs: sharedRefs,
        setMessages: setPageMessages,
      });
      return;
    }

    if (eventType === 'message_chunk' && hasRole && event.role === 'assistant' && hasPairIndex) {
      const pairIndex = event.turn_index as number;
      const currentAssistantMessageId = assistantMessagesByPair.get(pairIndex);
      const pairState = pairStateByPair.get(pairIndex);
      if (!currentAssistantMessageId || !pairState) return;

      if (contentType === 'reasoning_signal') {
        handleHistoryReasoningSignal({
          assistantMessageId: currentAssistantMessageId,
          signalContent: (event.content as string) || '',
          pairIndex,
          pairState,
          setMessages: setPageMessages,
          elapsedMs: typeof event.elapsed_ms === 'number' ? event.elapsed_ms : undefined,
        });
        return;
      }

      if (contentType === 'reasoning' && event.content) {
        handleHistoryReasoningContent({
          assistantMessageId: currentAssistantMessageId,
          content: event.content as string,
          pairState,
          setMessages: setPageMessages,
        });
        return;
      }

      if (contentType === 'text' && event.content) {
        handleHistoryTextContent({
          assistantMessageId: currentAssistantMessageId,
          content: event.content as string,
          finishReason: event.finish_reason as string | undefined,
          pairState,
          setMessages: setPageMessages,
          phase: event.phase as TextSegment['phase'],
        });
        return;
      }

      if (event.finish_reason) {
        handleHistoryTextContent({
          assistantMessageId: currentAssistantMessageId,
          content: '',
          finishReason: event.finish_reason as string,
          pairState,
          setMessages: setPageMessages,
        });
        return;
      }
    }

    if (eventType === 'tool_call_chunks') return;

    const steps = eventType === 'artifact' ? readPlanSteps(event.artifact_type, event.payload) : null;
    if (steps && hasPairIndex) {
      const pairIndex = event.turn_index as number;
      const currentAssistantMessageId = assistantMessagesByPair.get(pairIndex);
      const pairState = pairStateByPair.get(pairIndex);
      if (currentAssistantMessageId && pairState) {
        applyPlanSnapshot(setPageMessages, currentAssistantMessageId, steps, eventOrder(event, () => ++pairState.contentOrderCounter));
      }
      return;
    }

    if (eventType === 'artifact' && event.artifact_type === 'task') {
      const payload = (event.payload as Record<string, unknown>) || {};
      handleHistoryTaskArtifactStatus({
        toolCallId: event.tool_call_id as string | undefined,
        taskId: payload.task_id as string | undefined,
        status: payload.status,
        setMessages: setPageMessages,
      });
      return;
    }

    // DALTON-HOOK(shared-widgets): replay inline html_widget artifacts (including redacted placeholders)
    if (eventType === 'artifact' && event.artifact_type === 'html_widget' && hasPairIndex) {
      const pairIndex = event.turn_index as number;
      const currentAssistantMessageId = assistantMessagesByPair.get(pairIndex);
      const pairState = pairStateByPair.get(pairIndex);
      if (!currentAssistantMessageId || !pairState) return;
      handleHistoryHtmlWidget({
        assistantMessageId: currentAssistantMessageId,
        artifactType: 'html_widget',
        artifactId: event.artifact_id as string,
        payload: (event.payload as HtmlWidgetData | null) ?? null,
        pairState,
        setMessages: setPageMessages,
        eventId: null,
      });
      return;
    }

    if (eventType === 'tool_calls' && hasPairIndex) {
      const pairIndex = event.turn_index as number;
      const currentAssistantMessageId = assistantMessagesByPair.get(pairIndex);
      const pairState = pairStateByPair.get(pairIndex);
      if (!currentAssistantMessageId || !pairState) return;
      handleHistoryToolCalls({
        assistantMessageId: currentAssistantMessageId,
        toolCalls: event.tool_calls as Array<Record<string, unknown>>,
        pairState,
        setMessages: setPageMessages,
      });
      return;
    }

    if (eventType === 'tool_call_result' && hasPairIndex) {
      const pairIndex = event.turn_index as number;
      const currentAssistantMessageId = assistantMessagesByPair.get(pairIndex);
      const pairState = pairStateByPair.get(pairIndex);
      if (!currentAssistantMessageId || !pairState) return;
      handleHistoryToolCallResult({
        assistantMessageId: currentAssistantMessageId,
        toolCallId: event.tool_call_id as string,
        result: {
          content: event.content,
          content_type: event.content_type as string,
          tool_call_id: event.tool_call_id as string,
          artifact: event.artifact,
          status: event.status,
        },
        pairState,
        setMessages: setPageMessages,
      });
    }
  }, opts);

  stampSettledTurns();
  return {
    messages: pageMessages.map((m) => ({ ...m, isStreaming: false })),
    firstTurnIndex,
    hasMore,
  };
}

export default function SharedChatView({
  shareToken,
  access = shareToken,
  metadata,
  refreshStamp,
  onPermissionForbidden,
}: SharedChatViewProps) {
  const { t } = useTranslation();
  // The latest credential, read by the loads keyed on the share itself, so a
  // renewed grant serves the next request without replaying the thread.
  const accessRef = useRef(access);
  useEffect(() => {
    accessRef.current = access;
  }, [access]);

  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // File panel (right side, matching ChatView's rightPanelType === 'file')
  const [showFilePanel, setShowFilePanel] = useState(false);
  const [files, setFiles] = useState<string[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  // Where the one listing fetch stands, so a second opener (or a re-render
  // while it is in flight) does not ask again. A failed fetch goes back to
  // idle so the next open retries it.
  // Which share the listing belongs to rides along, so a different token starts over.
  const filesFetchRef = useRef<{ token: string | undefined; state: 'idle' | 'loading' | 'loaded' }>({ token: undefined, state: 'idle' });
  const [filePanelTarget, setFilePanelTarget] = useState<PanelTarget | null>(null);
  const filePanelTargetSeq = useRef(0);
  const { isLoggedIn, isInitialized: authInitialized } = useAuth();
  const isMobile = useIsMobile();
  const [rightPanelWidth, setRightPanelWidth] = useState(750);
  // What the panel has in front: a chart holds the divider at its floor.
  const [panelTabKind, setPanelTabKind] = useState<FileTab['kind'] | null>(null);
  // Expanded, the panel lies over the whole view, as it does in the owner's.
  // Closing the panel ends it, so the next open starts beside the chat, and
  // so does the mobile layout, which offers no toggle.
  const [expanded, setExpanded] = useState(false);
  if (expanded && (!showFilePanel || isMobile)) setExpanded(false);
  const panelExpanded = expanded && !isMobile;
  const handleTogglePanelExpanded = useCallback(() => setExpanded((v) => !v), []);
  const panelMinWidth = panelTabKind === 'chart' ? CHART_SURFACE_MIN_WIDTH : PANEL_MIN_WIDTH;
  const panelMaxRatio = panelTabKind === 'chart' ? CHART_MAX_RATIO : PANEL_MAX_RATIO;
  useEffect(() => {
    setRightPanelWidth((w) => Math.max(w, Math.min(panelMinWidth, window.innerWidth * panelMaxRatio)));
  }, [panelMinWidth, panelMaxRatio]);
  const isDraggingRef = useRef(false);
  // Armed for the duration of a divider drag; unmount mid-drag would otherwise
  // strand document listeners and app-wide col-resize/no-select body styles.
  const dragCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => dragCleanupRef.current?.(), []);

  const scrollAreaRef = useRef<HTMLDivElement>(null);
  // DALTON-HOOK(share-redaction): pagination, downgrade revocation, file-read epoch, and 403 fail-closed state
  const queryClient = useContext(QueryClientContext);
  const [historyPage, setHistoryPage] = useState<{ firstTurnIndex: number | null; hasMore: boolean }>({
    firstTurnIndex: null,
    hasMore: false,
  });
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [downgrade, setDowngrade] = useState<ShareDowngradeRecord | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const replayAbortRef = useRef<AbortController | null>(null);
  const olderAbortRef = useRef<AbortController | null>(null);
  const filesAbortRef = useRef<AbortController | null>(null);
  const fileEpochRef = useRef<ShareFileEpoch>(createShareFileEpoch());
  const rotateFileEpoch = useCallback(() => {
    fileEpochRef.current.revoke();
    fileEpochRef.current = createShareFileEpoch();
  }, []);

  // Permissions (fail-closed: anything other than explicit true is false)
  const permissions = (metadata.permissions || {}) as Record<string, unknown>;
  const rawAllowFiles = permissions.allow_files === true;
  const downgradeActive = isShareDowngradeActive(downgrade, shareToken, refreshStamp, rawAllowFiles);
  const canBrowseFiles = rawAllowFiles && !downgradeActive;
  const canDownload = canBrowseFiles && permissions.allow_download === true;
  const sharedImageScope = useMemo(
    () => buildSharedImageScope(shareToken, canBrowseFiles),
    [shareToken, canBrowseFiles],
  );

  // DALTON-HOOK(share-redaction): clear stale 403 downgrade record once fresher metadata restores allow_files=true
  useEffect(() => {
    if (downgrade !== null && !downgradeActive) {
      setDowngrade(null);
    }
  }, [downgrade, downgradeActive]);

  const handleForbiddenDowngrade = useStableHandler(() => {
    rotateFileEpoch();
    setDowngrade((prev) =>
      prev?.token === shareToken && prev.stamp === refreshStamp ? prev : { token: shareToken, stamp: refreshStamp },
    );
    onPermissionForbidden?.();
  });

  const handleDownloadForbidden = useStableHandler(() => {
    onPermissionForbidden?.();
    toast({ description: t('share.downloadNotPermitted'), variant: 'destructive' });
  });

  const guardFileRead = useCallback(
    <T,>(source: ShareForbiddenSource, read: (signal: AbortSignal) => Promise<T>, onDiscard?: (value: T) => void) =>
      runGuardedShareFileRead({
        epoch: fileEpochRef.current,
        source,
        read,
        onFilesForbidden: handleForbiddenDowngrade,
        onDownloadForbidden: handleDownloadForbidden,
        onDiscard,
      }),
    [handleForbiddenDowngrade, handleDownloadForbidden],
  );

  // DALTON-HOOK(share-redaction): on allow_files downgrade (true -> false), abort in-flight loads, rotate file epoch, revoke caches, and clear state before redacted replay
  const prevPermRef = useRef<{ token: string; allowFiles: boolean }>({ token: shareToken, allowFiles: canBrowseFiles });
  useEffect(() => {
    const prev = prevPermRef.current;
    prevPermRef.current = { token: shareToken, allowFiles: canBrowseFiles };
    if (prev.token === shareToken && prev.allowFiles && !canBrowseFiles) {
      replayAbortRef.current?.abort();
      olderAbortRef.current?.abort();
      filesAbortRef.current?.abort();
      rotateFileEpoch();
      revokeSharedImageScope(shareToken);
      if (accessRef.current !== shareToken) revokeSharedImageScope(accessRef.current);
      void clearSharedFileBodies(queryClient);
      setMessages([]);
      setFiles([]);
      setShowFilePanel(false);
      setFilePanelTarget(null);
      setHistoryPage({ firstTurnIndex: null, hasMore: false });
      setLoadingOlder(false);
      setLoading(true);
      filesFetchRef.current = { token: undefined, state: 'idle' };
    }
  }, [shareToken, canBrowseFiles, queryClient, rotateFileEpoch]);

  // DALTON-HOOK(share-redaction): abort all in-flight loads and revoke image + file-body caches on unmount
  useEffect(() => {
    if (!fileEpochRef.current.isCurrent()) {
      fileEpochRef.current = createShareFileEpoch();
    }
    return () => {
      replayAbortRef.current?.abort();
      olderAbortRef.current?.abort();
      filesAbortRef.current?.abort();
      fileEpochRef.current.revoke();
      revokeSharedImageScope(shareToken);
      if (accessRef.current !== shareToken) revokeSharedImageScope(accessRef.current);
      void clearSharedFileBodies(queryClient);
    };
  }, [shareToken, queryClient]);

  // Replay the conversation (re-runs on shareToken change or allow_files downgrade)
  // DALTON-HOOK(share-redaction): abortable paginated replay with history_page & steering_delivered support
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    replayAbortRef.current?.abort();
    replayAbortRef.current = controller;
    setLoading(true);
    setError(null);

    (async () => {
      try {
        const result = await replaySharedPage(accessRef.current, { signal: controller.signal });
        if (cancelled || controller.signal.aborted) return;
        shouldAutoScrollRef.current = true;
        setMessages(result.messages);
        setHistoryPage({ firstTurnIndex: result.firstTurnIndex, hasMore: result.hasMore });
        setLoading(false);
      } catch (e: unknown) {
        if (!cancelled && !controller.signal.aborted && (e as { name?: string })?.name !== 'AbortError') {
          setError((e as Error).message);
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      olderAbortRef.current?.abort();
    };
  }, [shareToken, canBrowseFiles]);

  // DALTON-HOOK(share-redaction): load an earlier history page via beforeTurn and prepend it
  const handleLoadOlder = useCallback(async () => {
    if (loadingOlder || !historyPage.hasMore || historyPage.firstTurnIndex == null) return;
    const controller = new AbortController();
    olderAbortRef.current?.abort();
    olderAbortRef.current = controller;
    setLoadingOlder(true);
    try {
      const older = await replaySharedPage(accessRef.current, {
        beforeTurn: historyPage.firstTurnIndex,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      shouldAutoScrollRef.current = false;
      setMessages((prev) => {
        const seen = new Set(prev.map((m) => m.id));
        const freshOlder = older.messages.filter((m) => !seen.has(m.id));
        return [...freshOlder, ...prev];
      });
      setHistoryPage({ firstTurnIndex: older.firstTurnIndex, hasMore: older.hasMore });
    } catch (e: unknown) {
      if (!controller.signal.aborted && (e as { name?: string })?.name !== 'AbortError') {
        toast({ description: (e as Error).message, variant: 'destructive' });
      }
    } finally {
      if (!controller.signal.aborted) setLoadingOlder(false);
    }
  }, [loadingOlder, historyPage]);

  // Auto-scroll on initial load (skip when prepending older history)
  useEffect(() => {
    if (!shouldAutoScrollRef.current) return;
    if (scrollAreaRef.current) {
      const el = scrollAreaRef.current.querySelector('[data-radix-scroll-area-viewport]');
      if (el) el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  // The route keeps this view mounted from one share to the next, and the panel
  // and its listing belong to the share they were opened on.
  useEffect(() => {
    setFiles([]);
    setShowFilePanel(false);
    setFilePanelTarget(null);
  }, [shareToken]);

  // The listing loads whenever the panel is showing on a share that permits
  // browsing, whichever entry opened it. A chart card opens the panel too, and
  // a tree that never loaded reads as an empty workspace once its tab closes.
  useEffect(() => {
    if (!showFilePanel || !canBrowseFiles) return;
    const fetchState = filesFetchRef.current;
    if (fetchState.token === shareToken && fetchState.state !== 'idle') return;
    let cancelled = false;
    // DALTON-HOOK(share-redaction): abortable file listing fetch that downgrades on HTTP 403
    const controller = new AbortController();
    filesAbortRef.current?.abort();
    filesAbortRef.current = controller;
    filesFetchRef.current = { token: shareToken, state: 'loading' };
    setFilesLoading(true);
    (async () => {
      try {
        const result = await getSharedFiles(accessRef.current, '.', { signal: controller.signal });
        if (cancelled || controller.signal.aborted) return;
        const listed = result.files || [];
        setFiles(listed);
        // An empty listing is asked again on the next open, as it was before
        // the load moved here: files can land after the share was made.
        filesFetchRef.current = { token: shareToken, state: listed.length ? 'loaded' : 'idle' };
      } catch (err: unknown) {
        if (!cancelled && !controller.signal.aborted) {
          // DALTON-HOOK(share-redaction): classify list 403 as file-permission revocation
          const status = apiErrorStatus(err) ?? (err as { status?: number })?.status;
          if (status === 403 && classifyShareForbidden('list') === 'files') handleForbiddenDowngrade();
          filesFetchRef.current = { token: shareToken, state: 'idle' };
        }
      }
      if (!cancelled && !controller.signal.aborted) setFilesLoading(false);
    })();
    return () => {
      cancelled = true;
      controller.abort();
      // Unmount mid-fetch: the result is dropped, so the state must not claim it.
      if (filesFetchRef.current.state === 'loading') filesFetchRef.current = { token: shareToken, state: 'idle' };
    };
  }, [showFilePanel, canBrowseFiles, shareToken, handleForbiddenDowngrade]);

  // File panel handlers
  const handleToggleFilePanel = useCallback(() => {
    if (!canBrowseFiles) return;
    setShowFilePanel((prev) => !prev);
  }, [canBrowseFiles]);

  // The panel's adapter over the public endpoints. The serve prefix stands in
  // for the workspace, which a share never names.
  // DALTON-HOOK(share-redaction): bind public file/preview/download reads to file epoch and source-classified 403 handling
  const servePrefix = sharedServePrefix(access);
  const revokeLateObjectUrl = useCallback((url: string) => {
    if (typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
  }, []);
  const fileApiAdapter = useMemo(() => ({
    readFile: (path: string) => guardFileRead('read', (signal) => readSharedFile(access, path, { signal })),
    // HTML files read their full source here; the public read endpoint caps at
    // its own line limit, and the preview renders via the served URL regardless.
    readFileFull: (path: string) => guardFileRead('read', (signal) => readSharedFile(access, path, { signal })),
    // Previews read under the serve prefix (allow_files), matching the rendered
    // report; only the explicit save uses the download endpoint (allow_download).
    downloadFile: (path: string) =>
      guardFileRead('serve', (signal) => servedObjectUrl(servePrefix, path, { refreshStamp, signal }), revokeLateObjectUrl),
    downloadFileAsArrayBuffer: (path: string) =>
      guardFileRead('serve', (signal) => servedBytes(servePrefix, path, { refreshStamp, signal })),
    triggerDownload: (path: string) => guardFileRead('download', (signal) => downloadSharedFile(access, path, { signal })),
    servePrefix,
    resolveFile: (candidates: string[], recentWrites: string[]) =>
      guardFileRead('resolve', (signal) => resolveSharedFile(access, candidates, recentWrites, { signal })),
  }), [access, servePrefix, refreshStamp, guardFileRead, revokeLateObjectUrl]);

  const imageDownloader = useCallback(
    (path: string) =>
      guardFileRead('serve', (signal) => servedObjectUrl(servePrefix, path, { refreshStamp, signal }), revokeLateObjectUrl),
    [servePrefix, refreshStamp, guardFileRead, revokeLateObjectUrl],
  );

  // A share is a finished thread: nothing runs, and nothing will come back.
  const plans = useMemo(() => threadPlans(messages, false, false).underMessages, [messages]);
  // The workspace's folder, so its absolute paths read as workspace paths here
  // as they do in the owner's view, a turn from before a move included. Its
  // siblings are not the viewer's, nor are the owner's other computers.
  const { root_dir: rootDir, dir_name: dirName, previous_root_dirs: previousRootDirs } = metadata;
  const folders = useMemo<ComputerFolders | null>(
    () => (rootDir || dirName
      ? computerFolders({ workspace_id: '', root_dir: rootDir, dir_name: dirName, previous_root_dirs: previousRootDirs }, null)
      : null),
    [rootDir, dirName, previousRootDirs],
  );

  // Open file from chat (tool call artifacts, file mention cards)
  const handleOpenFile = useCallback((filePath: string, _workspaceId?: string, location?: FileLocation, opts?: { pin?: boolean }) => {
    if (!canBrowseFiles) return;
    setShowFilePanel(true);
    const dest = computeAgentArtifactRouting(filePath, undefined, folders);
    const dir = dest.kind === 'panel' && 'dir' in dest.target ? dest.target.dir : null;
    // The panel reads a request off the counter rather than off a changed
    // string, so the same folder clicked twice arrives twice here too.
    const seq = ++filePanelTargetSeq.current;
    setFilePanelTarget(dir == null ? fileTarget(filePath, { location, pin: opts?.pin }, seq) : dirTarget(dir, seq));
  }, [canBrowseFiles, folders]);

  // A chart card opens the symbol as a chart tab here, prices only. The card
  // names the owner's workspace, whose drawings and events the share does not
  // reach, so the tab is opened with none: the surface then asks nothing of
  // any workspace. No file permission is needed for a chart, so the panel
  // opens for it either way and, without files to browse, holds just this tab.
  // Market data is served per account, so a signed-out reader would get a
  // chart that never loads; they are told why and stay on the thread. Until
  // the session has resolved, signed-out is not yet known, so the tab opens
  // and the chart surface itself fails soft if the data is refused.
  const handleOpenChart = useCallback(({ symbol, timeframe }: ChartTabSpec) => {
    if (authInitialized && !isLoggedIn) {
      toast({ description: t('share.signInForChart') });
      return;
    }
    setShowFilePanel(true);
    setFilePanelTarget(stampTarget({ kind: 'chart', symbol, timeframe }, ++filePanelTargetSeq.current));
  }, [authInitialized, isLoggedIn, t]);

  // Clears only the ask it names, so one landed in between is kept.
  const handleTargetHandled = useCallback((seq?: number) => {
    setFilePanelTarget((prev) => (prev?.seq === seq ? null : prev));
  }, []);

  // This thread's writes break ties between namesakes, the same tiebreak the
  // owner view passes. Identity-stable, so the actions memo below does not
  // rebuild on every replayed message.
  const getRecentWritePaths = useStableHandler(() => collectRecentWritePaths(messages as TurnMessage[], folders));

  // Read-only adapter for the transcript's action surface. The shared view has
  // no turn to edit/regenerate/rate and no interrupt to approve. Subagent-task
  // opening isn't declared here — MessageContentSegments strips it on readOnly
  // surfaces, so a value would be dead weight pretending to be the gate.
  const readOnlyActions = useMemo<MessageActions>(() => ({
    ...READ_ONLY_MESSAGE_ACTIONS,
    // DALTON-HOOK(shared-widgets): no new tab or PDF (they run the widget off its
    // sandbox on the app's origin); the HTML download only where the share allows it.
    widgetExports: canDownload ? 'download' : 'none',
    onOpenFile: handleOpenFile,
    onOpenChart: handleOpenChart,
    // A copy-link share grants allow_files without allow_download, so the
    // deliverable card offers Download only where the share actually permits
    // saving the bytes.
    onDownloadFile: canDownload
      ? async (path: string, fileWorkspaceId?: string) => {
          // A card relayed from the worker names the workspace holding it, and
          // the share token authorizes this thread's workspace alone. Resolving
          // it here would look the name up in the wrong place and save whatever
          // namesake it found, so the click goes where an unplaceable one goes.
          if (fileWorkspaceId) return void handleOpenFile(path, fileWorkspaceId);
          // DALTON-HOOK(share-redaction): capture one file epoch across resolve and save so unmount/downgrade aborts both steps
          const epoch = fileEpochRef.current;
          try {
            const outcome = await runGuardedShareCardDownload({
              epoch,
              path,
              recentWrites: getRecentWritePaths(),
              resolveTarget: downloadTarget,
              resolveFile: (candidates, recentWrites, signal) =>
                resolveSharedFile(access, candidates, recentWrites, { signal }),
              downloadFile: (resolvedPath, signal) => downloadSharedFile(access, resolvedPath, { signal }),
              onFilesForbidden: handleForbiddenDowngrade,
              onDownloadForbidden: handleDownloadForbidden,
            });
            // Namesakes the lookup could not pick between leave nothing to
            // save, so the click lands on the panel that asks, exactly as Open
            // does with the same answer.
            if (outcome === 'unplaced') return void handleOpenFile(path, fileWorkspaceId);
          } catch (err: unknown) {
            if (isShareDownloadHandled(err) || apiErrorStatus(err) === 403) return;
            console.error('[SharedChatView] Download failed:', err);
            // A reader on a shared link has no other way to learn the save did
            // not happen: there is no panel error to fall back on here.
            toast({ description: t('filePanel:downloadFailed'), variant: 'destructive' });
          }
        }
      : undefined,
  }), [handleOpenFile, handleOpenChart, canDownload, access, getRecentWritePaths, handleForbiddenDowngrade, handleDownloadForbidden, t]);

  // Deep link: `?file=<path>` opens that report directly once metadata + file
  // permission are known. One-shot — the share-link target from §1.3b.
  const fileDeepLinkConsumedRef = useRef(false);
  useEffect(() => {
    if (fileDeepLinkConsumedRef.current || !canBrowseFiles) return;
    const fileParam = new URLSearchParams(window.location.search).get('file');
    if (!fileParam) return;
    fileDeepLinkConsumedRef.current = true;
    handleOpenFile(fileParam);
  }, [metadata, canBrowseFiles, handleOpenFile]);

  // Drag-to-resize file panel (matches ChatView)
  const handleDividerMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingRef.current = true;
    const startX = e.clientX;
    const startWidth = rightPanelWidth;

    const onMouseMove = (moveEvent: MouseEvent) => {
      if (!isDraggingRef.current) return;
      const delta = startX - moveEvent.clientX;
      // The floor goes in before the cap so a narrow window still wins.
      const newWidth = Math.max(PANEL_MIN_WIDTH, Math.min(Math.max(startWidth + delta, panelMinWidth), window.innerWidth * panelMaxRatio));
      setRightPanelWidth(newWidth);
    };

    const onMouseUp = () => teardown();
    // Doubles as the unmount cleanup — unhooks listeners and restores body styles.
    const teardown = () => {
      dragCleanupRef.current = null;
      isDraggingRef.current = false;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };

    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    dragCleanupRef.current = teardown;
  }, [rightPanelWidth, panelMinWidth, panelMaxRatio]);

  // One panel for both layouts; only its container differs by viewport.
  const filePanel = (
    <WorkspaceProvider workspaceId={null} downloadFile={null} folders={folders}>
      <FilePanel
        readOnly
        canDownload={canDownload}
        workspaceId=""
        apiAdapter={fileApiAdapter}
        // No files to browse means no tree.
        singleFileMode={!canBrowseFiles}
        onActiveTabKindChange={setPanelTabKind}
        expanded={panelExpanded}
        onToggleExpanded={isMobile ? null : handleTogglePanelExpanded}
        onClose={() => setShowFilePanel(false)}
        // A panel the header did not open (a file link, with nothing to
        // browse) has no toggle to stand in for, so it keeps its close.
        panelToggle={canBrowseFiles}
        files={files}
        filesLoading={filesLoading}
        target={filePanelTarget}
        onTargetHandled={handleTargetHandled}
        onOpenFile={handleOpenFile}
        getRecentWritePaths={getRecentWritePaths}
      />
    </WorkspaceProvider>
  );

  // The metadata resolved, so a failure here is the replay's, not the link's.
  if (error) {
    return <ShareUnavailable variant="failed" onRetry={() => window.location.reload()} />;
  }

  return (
    <div
      // DALTON-HOOK(mobile-web): use dynamic viewport height on phones so Safari bottom toolbar never covers the footer
      className={cn('pf-shell relative flex w-full overflow-hidden', isMobile ? 'h-dvh' : 'h-screen')}
      style={{ backgroundColor: 'var(--color-bg-page)' }}
    >
      {/* Left Side: Topbar + Chat Window — identical structure to ChatView,
          inert and isolated under an expanded panel as there. */}
      <div className={cn('flex flex-col flex-1 min-w-0', panelExpanded && 'isolate')} inert={panelExpanded}>
        {/* Top bar — matches ChatView's top bar exactly */}
        <div className={cn('flex items-center justify-between py-2 border-b min-w-0 shrink-0', isMobile ? 'px-2.5 gap-2' : 'px-4')} style={{ borderColor: 'var(--color-border-muted)' }}>
          <div className={cn('flex items-center min-w-0', isMobile ? 'gap-2 flex-1' : 'gap-4 shrink')}>
            <Link
              to="/"
              className="p-2 rounded-md transition-colors shrink-0"
              style={{ color: 'var(--color-text-primary)' }}
              title={`Back to ${BRAND_NAME}`} // DALTON-HOOK(brand-name)
              onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = ''; }}
            >
              <ArrowLeft className="h-5 w-5" />
            </Link>
            {/* DALTON-HOOK(mobile-web): min-w-0 allows long workspace titles to truncate without pushing the file button off-screen */}
            <h1 className="title-font text-[length:var(--cs-text-heading,1rem)] leading-6 font-semibold tracking-[var(--cs-title-track,-0.01em)] whitespace-nowrap truncate min-w-0" style={{ color: 'var(--color-text-primary)' }}>
              {metadata.workspace_name || metadata.title || 'Shared Conversation'}
            </h1>
            {loading && (
              <span className="text-xs whitespace-nowrap" style={{ color: 'var(--color-text-tertiary)' }}>
                Loading history...
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {/* Covered by the open panel on desktop, whose corner carries it. */}
            {canBrowseFiles && (isMobile || !showFilePanel) && (
              <button
                type="button"
                data-panel-toggle="header"
                onClick={(event) => { handleToggleFilePanel(); if (!isMobile) handOffToggleFocus(event, 'panel'); }}
                aria-pressed={showFilePanel}
                className={cn('transition-colors hover:bg-(--color-border-muted)', isMobile ? 'p-2 rounded-md' : 'p-1.5 -mr-2 rounded-[4px]')}
                style={{ color: 'var(--color-text-primary)' }}
                title="Workspace Files"
                aria-label="Workspace Files"
              >
                <PanelRight className={isMobile ? 'h-5 w-5' : 'h-4 w-4'} />
              </button>
            )}
          </div>
        </div>

        {/* Content area: Chat Window */}
        <div className="flex-1 flex overflow-hidden">
          {/* Chat Window */}
          <div className="flex-1 flex flex-col overflow-hidden min-w-0">
            {/* Messages Area - Fixed height, scrollable */}
            <div
              className="flex-1 overflow-hidden"
              style={{ minHeight: 0, height: 0 }}
            >
              <ScrollArea ref={scrollAreaRef} className="h-full w-full">
                {/* DALTON-HOOK(mobile-web): tighter horizontal padding and phone-only overflow-x-clip */}
                <div className="px-3 md:px-6 py-4 flex justify-center max-md:overflow-x-clip">
                  <div className="w-full max-w-3xl min-w-0">
                    {/* Until the replay is whole: it writes the transcript an
                        event at a time, and drawn as it went a finished turn
                        showed its work open, then folded it away. */}
                    {loading ? (
                      <div className="flex items-center justify-center py-20">
                        <span aria-hidden="true" className="shrink-0">
                          <Loader size={20} className="text-(--color-text-tertiary)" />
                        </span>
                        <span className="ml-2 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>Loading conversation...</span>
                      </div>
                    ) : (
                      <WorkspaceProvider
                        workspaceId={null}
                        downloadFile={canBrowseFiles ? imageDownloader : null}
                        folders={folders}
                        imageScope={sharedImageScope}
                      >
                      <MessageActionsProvider actions={readOnlyActions}>
                      <DispatchStatusProvider>
                      {/* DALTON-HOOK(share-redaction): propagate allowFiles/allowDiagramRender context and render older-history pagination trigger */}
                      <SharedRedactionProvider
                        allowFiles={canBrowseFiles}
                        allowDiagramRender={canBrowseFiles}
                        shareToken={shareToken}
                        imageScope={sharedImageScope}
                        onForbidden={handleForbiddenDowngrade}
                      >
                        {historyPage.hasMore && (
                          <div className="flex justify-center pb-4">
                            <button
                              type="button"
                              onClick={() => void handleLoadOlder()}
                              disabled={loadingOlder}
                              className="text-xs px-3 py-1.5 rounded-full border transition-colors hover:bg-(--color-border-muted) disabled:opacity-50"
                              style={{
                                borderColor: 'var(--color-border-muted)',
                                color: 'var(--color-text-secondary)',
                              }}
                            >
                              {loadingOlder ? t('chat.loadingOlderHistory') : t('share.loadEarlierMessages')}
                            </button>
                          </div>
                        )}
                        <MessageList
                          messages={messages}
                          readOnly={true}
                          allowFiles={canBrowseFiles}
                          plans={plans}
                        />
                      {/* DALTON-HOOK(share-redaction): end redaction context */}
                      </SharedRedactionProvider>
                      </DispatchStatusProvider>
                      </MessageActionsProvider>
                      </WorkspaceProvider>
                    )}
                  </div>
                </div>
              </ScrollArea>
            </div>

            {/* Input Area — matches ChatView's input area styling */}
            {/* DALTON-HOOK(mobile-web): respect bottom safe-area inset and narrower padding on phones */}
            <div className="shrink-0 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] md:p-4 flex justify-center">
              <div className="w-full max-w-3xl space-y-3">
                <div
                  className="flex flex-col items-stretch rounded-2xl border"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    backgroundColor: 'var(--color-bg-card)',
                  }}
                >
                  <div className="flex flex-col px-3 pt-3 pb-2 gap-2">
                    <div
                      className="text-sm px-1 py-2"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      This is a read-only shared conversation
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-xs px-1" style={{ color: 'var(--color-text-tertiary)' }}>
                        Powered by{' '}
                        <Link to="/" className="hover:underline" style={{ color: 'var(--color-accent-primary)' }}>
                          {BRAND_NAME} {/* DALTON-HOOK(brand-name) */}
                        </Link>
                      </span>
                      <button
                        disabled
                        className="flex items-center justify-center w-8 h-8 rounded-lg"
                        style={{
                          backgroundColor: 'var(--color-accent-disabled)',
                          color: 'var(--color-text-tertiary)',
                          cursor: 'not-allowed',
                        }}
                      >
                        <ArrowLeft className="h-4 w-4 rotate-180" />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Right Side: File Panel — reuses real FilePanel in readOnly mode. Only
          a permitted file open or a chart card sets it showing, so the flag
          alone is the gate. A phone has no room beside the thread, so there the
          panel is a full-screen sheet over it, as the owner's view does, and
          closing it returns to the chat. */}
      {showFilePanel && (isMobile ? (
        <motion.div
          key="file"
          initial={{ x: '100%' }}
          animate={{ x: 0 }}
          transition={TWEEN.enter}
          // DALTON-HOOK(mobile-web): disable content-area horizontal drag-to-close so wide tables, PDFs and HTML reports pan freely; close via top back button
          className="flex overflow-hidden mobile-panel-overlay"
          style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 30, backgroundColor: 'var(--color-bg-page)' }}
        >
          <div className="shrink-0 h-full" style={{ width: '100%' }}>
            {filePanel}
          </div>
        </motion.div>
      ) : (
        <>
          {!panelExpanded && <div className="chat-split-divider" onMouseDown={handleDividerMouseDown} />}
          <div
            className={panelExpanded ? 'absolute inset-0 z-10' : 'shrink-0'}
            style={panelExpanded ? undefined : { width: rightPanelWidth }}
          >
            {filePanel}
          </div>
        </>
      ))}
    </div>
  );
}
