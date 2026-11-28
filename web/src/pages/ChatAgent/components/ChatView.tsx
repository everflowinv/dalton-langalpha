import { RuleRow } from '@/components/organization/RuleRow';
import { othersTurnWords } from '@/lib/turnRefusal';
import { SharedThreadContext } from './messageList/SharedThreadContext';
import { useSharedThreadView } from '../hooks/useSharedThreadView';
import { useSharedEntry, useWorkspaceCapabilities } from '@/hooks/useSharedWorkspaces';
import React, { Suspense, useEffect, useEffectEvent, useId, useLayoutEffect, useRef, useState, useCallback, useMemo } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { MessageSelectionQuote } from './selectionQuote/MessageSelectionQuote';
import { ArrowLeft, PanelRight, Menu, Users } from 'lucide-react';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useCapabilities } from '@/hooks/useCapabilities'; // DALTON-HOOK(mobile-web)
import { useCallerIdentity } from '@/hooks/useCallerIdentity'; // DALTON-HOOK(external-share-guard)
import { useStableHandler } from '@/hooks/useStableHandler';
import { useLatestRef } from '@/hooks/useLatestRef';
import { ScrollArea } from '../../../components/ui/scroll-area';
import { usePreferences } from '@/hooks/usePreferences';
// DALTON-HOOK(antigravity-series): gate Dalton-only turn-pick & effort rollback behavior to Dalton builds
import { isDaltonBuild } from '@/config/brand';
import { attachComposerModelPickSeq } from '@/lib/dalton/threadModelDalton';
// DALTON-HOOK(antigravity-series): clear local thread effort on Antigravity switch without extra PATCH
import { useAllModels } from '@/hooks/useAllModels';
import { inspectSavedAntigravityModel } from '@/lib/antigravitySeries';
import { usePlanReader } from '@/hooks/useClaudeCodePlan';
import { planActionText } from '@/lib/claudeCodePlan';
import { readTurnEndScroll } from '@/lib/turnEndScroll';
import {
  readStreamingMode,
  readToolCallLayout,
  readTurnDisplay,
  TranscriptDisplayContext,
  type TranscriptDisplay,
} from '@/lib/transcriptDisplay';
import { useFeatureEnabled } from '@/hooks/useFeatures';
import { isHomeWorkspace } from '@/lib/homeWorkspace';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { cn } from '@/lib/utils';
import { invalidateSubagents } from '@/hooks/useSubagents';
import { markHandoffReady } from '@/pages/Onboarding/welcome/handoff';
import { ChatAppsPromptCard } from '@/pages/Onboarding/ChatAppsPromptCard';
import { cardDownloadKey, trackPending } from '../utils/downloadNotice';
import { summarizeThread, offloadThread, cancelSubagentTask, triggerFileDownload, resolveWorkspaceFile, type NextSummary } from '../utils/api';
import { downloadTarget } from '../utils/fileRefResolver';
import { toast } from '@/components/ui/use-toast';
import { mergeWarmingDisplay } from '../utils/warmWorkspace';
import { useChatMessages } from '../hooks/useChatMessages';
import { BROWSER_FEATURE } from '../hooks/useBrowserHere';
import { useThreadModel } from '../hooks/useThreadModel';
import { useThreadSubagents } from '../hooks/useThreadSubagents';
import { useThreadTuning } from '../hooks/useThreadTuning';
import { useForeignRunCatchUp } from '../hooks/useForeignRunCatchUp';
import { useComputerFolders } from '../hooks/useComputerFolders';
import { useComputerFoldersReady } from '../hooks/useComputerFoldersReady'; // DALTON-HOOK(channel-links): wait for folder queries before consuming rooted ?file= deep links
import { useComputer } from '../hooks/useComputers';
import { saveChatSession, getChatSession, clearChatSession } from '../hooks/utils/chatSessionRestore';
import type { PreviewData } from '../hooks/utils/types';
import { useCardState } from '../hooks/useCardState';
import { useWorkspace } from '@/hooks/useWorkspace';
import { classifyAgentPath, isSubagentFilePath } from '../utils/agentPaths';
import { fileArtifactPath } from '../utils/fileArtifact';
import type { FileOperationArtifactPayload, ThreadForkCut } from '@/types/api';
import { taskIdFromAgentId } from '../utils/agentId';
import {
  routeStopAction,
  compactionErrorCode,
  isUserStoppedCompaction,
  shouldClearCompactingFlag,
  isManualCompactionInFlight,
} from '../utils/compactionControl';
import './FilePanel.css';
import ChatInput, { type ChatInputHandle } from '../../../components/ui/chat-input';
import { MAX_FILES } from '../../../components/ui/chat-input.helpers'; // DALTON-HOOK(chat-attachment-count)
import type { ComposerSend, ContextPayload } from '../../../components/ui/chat-input.types';
import { attachmentSendParts, countContextAttachments, widgetSnapshotsToContexts } from '../utils/fileUpload';
import MessageList, { LiveMessageList, normalizeSubagentText } from './MessageList';
import { MessageActionsProvider } from './messageList/MessageActionsContext';
import { UserBubble } from './messageList/UserBubble';
import { MAIN_AGENT_PROVENANCE } from '../session/provenance';
import { SubagentTelemetryContext } from './SubagentTelemetryContext';
import { WorkflowRunContext } from './WorkflowRunContext';
import WorkflowRunDetail from './WorkflowRunDetail';
import { WORKFLOW_TASK_TYPE } from '../session/subagents/workflowRunState';
import { deriveSubagentStatus, isTerminalStatus } from '../session/subagents/subagentStatus';
import Markdown from './Markdown';
import ChatMinimap from './ChatMinimap';
import { DispatchStatusProvider } from '../hooks/usePTCDispatchStatus';
import JumpToLatestPill from './JumpToLatestPill';
import ShareButton from './ShareButton';
import { WorkspaceProvider } from '../contexts/WorkspaceContext';
import { ChartComposerContext } from '@/components/chart/chartComposer';
import { LinkActionsContext } from '../contexts/LinkActionsContext';
import type { BrowserUse } from '../session/stream/browserUse';
import { RouteLeaveGuardContext } from '../contexts/RouteLeaveGuardContext';
import SubagentStatusBar from './SubagentStatusBar';
import { ComposerTray, ComposerTrayStrip } from './chatView/ComposerTray';
import { ReconnectingChip } from './chatView/trayChips';
import { TrayDock } from './chatView/trayLiquid';
import { readComposerTray } from '@/lib/composerTray';
import { useThreadStatusValue } from './chatView/useThreadStatusValue';
import { ThreadStatusContext } from './chatView/threadStatus';
import { ThreadQuotesProvider } from './chatView/threadQuotes';
import { useHistoryBackfill } from './chatView/useHistoryBackfill';
import { useFocusTurn } from './chatView/useFocusTurn';
import { PaletteSearchButton } from '@/components/nav/PaletteSearchButton';
import { OlderHistorySkeleton } from './chatView/OlderHistorySkeleton';
import { useThreadCommands } from '../hooks/useCommands';
import { ThreadCommandsContext } from './commands/threadCommandsContext';
import { ChatCommentSendsProvider, useChatCommentSends } from './artifactComments/useChatCommentSends';
import { DockableComposer } from './chatView/composerDock';
import { ComposerDockContext } from './chatView/useComposerDock';
import { useChatCover, useChatCoverReads } from './chatView/useChatCover';
import { useComposerGiveBack } from './chatView/useComposerGiveBack';
import { usePublishFile } from './chatView/usePublishFile';
import { DockTurnStatus } from './chatView/DockTurnStatus';
import { QueuedCommentDeck } from './artifactComments/QueuedCommentDeck';
import { useTrayFold } from './chatView/useTrayFold';
import { ErrorBanner } from '@/components/ui/error-banner';
// DALTON-HOOK(mobile-web): PanInfo no longer needed after disabling content-area drag-to-close on mobile FilePanel
import { motion, AnimatePresence } from '@/lib/framer';
import { DURATION, EASE_COLLAPSE, SPRING_FOLD, TWEEN } from '@/lib/motion';
import { MobileBottomSheet } from '@/components/ui/mobile-bottom-sheet';
import { preloadableLazy } from '@/lib/lazyWithMotion';



// Preloadable: a lazy panel suspends on its first render even with its code
// in, and its boundary's 300ms reveal throttle left a preloaded panel blank.
// DALTON-HOOK(monitors): FilePanel reaches PublishDialog ('monitors')
const FilePanel = preloadableLazy(withCopy(() => import('./FilePanel'), ['monitors', 'publish', 'vault']));
const loadFilePanel = FilePanel.preload;
// The panel opens on the fold spring, which leaves the chat's edge almost
// where it was for the first frames, and shuts on the collapse curve, as a
// fold does, the fade outrunning it.
const PANEL_OPEN = { width: SPRING_FOLD, opacity: TWEEN.quick };
const PANEL_EXIT = { width: 0, opacity: 0, transition: { width: { duration: DURATION.fold, ease: EASE_COLLAPSE }, opacity: TWEEN.exit } };
const DetailPanel = React.lazy(() => import('./DetailPanel'));
const PreviewViewer = React.lazy(() => import('./viewers/PreviewViewer'));

import type { SendOptions } from '../session/types';
import type {
  MessageRecord, LocationState,
  ActionCommand,
  ChatViewProps,
} from './chatView/types';
import { TranscriptStatus, type ThreadNotice } from './chatView/TranscriptStatus';
import { CompactingLine } from './chatView/CompactingLine';
import { compactionOf } from './chatView/compaction';
import { newestTurnUnstarted } from './messageList/turnProjection';
import { useLiveSelect } from '../session/stream/liveMessages';
import { useComposerNotices } from './chatView/useComposerNotices';
import { useNextSummary } from '../hooks/useNextSummaryHint';
import { ChatDiskWarning } from './chatView/ChatDiskWarning';
import { HomePlacementNotice } from './chatView/HomePlacementNotice';
import { ChatRestoreOffer } from './chatView/ChatRestoreOffer';
import { useToolCallAnnouncer } from './chatView/useToolCallAnnouncer';
import { MobileNavDrawer } from './chatView/MobileNavDrawer';
import { useChatScroll } from './chatView/useChatScroll';
import { useStreamFollow } from './chatView/transcriptFollow';
import { OlderHistoryRow } from './chatView/OlderHistoryRow';
import { isTurnOpen, useTranscriptFollow } from './chatView/useTranscriptFollow';
import { useSubagentTabs } from './chatView/useSubagentTabs';
import { publishSidebarAgents, clearSidebarAgents } from './sidebarAgentsBridge';
import { useRightPanel } from './chatView/useRightPanel';
import { handChatStrips, releaseChatStrips } from './filePanel/freshStrips';
import { handOffToggleFocus } from './filePanel/panelToggleFocus';
import { PanelLoadingStrip } from './filePanel/PanelLoadingStrip';
import { usePanelFiles } from './chatView/usePanelFiles';
import { usePanelChart } from './chatView/usePanelChart';
import { SelectionChips } from '@/pages/MarketView/components/SelectionChips';
// DALTON-HOOK(channel-deep-links): resolve ?chart=&layer= and ?widget= deep links in ChatView
import { useChannelDeepLinks } from './chatView/useChannelDeepLinks';
import { useMessageActionBundles } from './chatView/useMessageActionBundles';
// DALTON-HOOK(team-sharing): read-only state, banner, and disabled composer for colleague threads
import {
  TeamReadOnlyBanner,
  TeamReadOnlyComposer,
  useTeamReadOnly,
} from './dalton/TeamSharing';
import { useUser } from '@/hooks/useUser';
// DALTON-HOOK(coverage-tracking): company workspace auto-tracking toggle in thread top bar
import { WorkspaceTrackingToggle } from './dalton/WorkspaceTrackingToggle';
// DALTON-HOOK(workspace-sharing-settings): per-workspace sharing settings modal in thread top bar
import { WorkspaceSharingModal } from './dalton/WorkspaceSharingModal';
import { useForkThread } from './chatView/useForkThread';
import { ForkedFromLink } from './chatView/ForkedFromLink';
import { ThreadPrivacyRow } from './chatView/threadPrivacy';
import { useThreadPrivacy } from './chatView/useThreadPrivacy';
import { withCopy } from '@/i18n';


// Clears the composer floating over the transcript's bottom edge (its height
// is published as --composer-h), plus a small gap above it. The content
// re-declares the variable so a change stops here: inherited, it restyled the
// whole transcript on every frame the composer resized (1.3ms a change on a
// three-turn thread, against 0.1ms, and growing with the thread).
const TRANSCRIPT_BOTTOM_PAD = 'pb-[calc(var(--composer-h,0px)+0.5rem)] *:[--composer-h:initial]';
// The transcript column's width beside the minimap. Inline rather than a
// second max-w utility, which cn() would merge away with max-w-3xl: that class
// is the column's mark, which the scroll code (resolveScrollContent) and the
// e2e probes find it by.
const BESIDE_MINIMAP: React.CSSProperties = { maxWidth: 'min(48rem, calc(100% - 3rem))' };
function ChatView({ workspaceId, threadId, initialTaskId, onBack, workspaceName: initialWorkspaceName, isActive = true, onThreadResolved, warmingState = false }: ChatViewProps): React.ReactElement | null {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const containerRef = useRef<HTMLDivElement>(null);
  const chatInputRef = useRef<ChatInputHandle>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const { preferences } = usePreferences();
  const marketWatchEnabled = useFeatureEnabled('market_watch');
  const queryClient = useQueryClient();
  const initialMessageSentRef = useRef(false);
  const state = location.state as LocationState | null;
  // The workspace row drives the header title and names Home. It
  // has to come from the shared detail query rather than a mount-time snapshot:
  // a rename invalidates that key, and this view outlives the rename (it is
  // kept mounted by the ChatView LRU, whose own copy of the name never
  // refreshes). The prop is only the pre-fetch seed.
  const { data: workspaceRecord } = useWorkspace(workspaceId);

  // Home, named by its row. Home runs the Chief of Staff as a full agent,
  // and it dispatches work into other workspaces: PTC cards open the threads
  // it started, report-backs land here, and file links reach into the
  // workspaces that wrote them.
  const isHome = isHomeWorkspace(workspaceRecord);
  // Home is named for what it stands for.
  const workspaceName = (isHome ? t('agents.chiefOfStaff') : workspaceRecord?.name) || initialWorkspaceName || '';
  // DALTON-HOOK(team-sharing): detect read-only colleague workspace/thread
  const teamReadOnly = useTeamReadOnly(workspaceId, workspaceRecord);
  // DALTON-HOOK(external-share-guard): unified caller identity for publish and file panel gating; fail closed to unknown while confirming
  const identity = useCallerIdentity();
  const resolvedCallerTier = identity.isPlatform
    ? null
    : (identity.status === 'ready' && !identity.isConfirming ? identity.tier : 'unknown');
  // DALTON-HOOK(mobile-web): gate WorkspaceSharingModal entry when either colleague_sharing or conversation_archive is enabled
  const { hasCapability } = useCapabilities();
  const workspaceSharingEntryReady =
    hasCapability('colleague_sharing') || hasCapability('conversation_archive');
  // DALTON-HOOK(colleague-dispatch): current user for thread ownership check
  const { user: currentUser } = useUser({ enabled: Boolean(workspaceId) });

  // The model a navigation's first message goes out with, captured at mount:
  // ChatAgent keeps up to five ChatViews rendered at once (display:none, not
  // unmounted) and they all read the same current location, and the
  // auto-send below clears the route state.
  const [navModel] = useState(() => (typeof state?.model === 'string' && state.model ? state.model : null));
  // The navigation bringing a new thread's first message, with the landing
  // composer's Subagents, effort and fast-mode picks. Read per navigation
  // rather than frozen: a new chat's view stays cached while unsent, and the
  // next one takes it up.
  const landingNav =
    isActive && threadId === '__default__' && (state?.initialMessage || state?.hiddenOpener) ? state : null;
  const subagentsLanding = landingNav
    ? { key: location.key, allowed: typeof landingNav.subagentsAllowed === 'boolean' ? landingNav.subagentsAllowed : null }
    : null;
  const tuningLanding = landingNav
    ? {
      key: location.key,
      reasoningEffort: typeof landingNav.reasoningEffortOverride === 'string' ? landingNav.reasoningEffortOverride : null,
      fastMode: typeof landingNav.fastModeOverride === 'boolean' ? landingNav.fastModeOverride : null,
    }
    : null;
  // The thread the welcome flow opens, captured at mount for the same reason.
  // The flow hands its conversation over as a view transition
  // (welcome/handoff.ts), which lands on this view's parts and waits for it to
  // draw the agent at work.
  const [kickoffView] = useState(() => isActive && state?.handoff === true);



  // Active agent in main view (default: 'main', or from URL taskId)
  const [activeAgentId, setActiveAgentId] = useState(
    initialTaskId ? `task:${initialTaskId}` : 'main'
  );
  // The turns the main list has drawn running. Kept here, one view per
  // thread, since that list unmounts while a subagent tab is open.
  const [mainRanHere] = useState(() => new Set<number>());
  // Show system files in FilePanel (.agents/, code/, tools/, etc.)
  const [showSystemFiles, setShowSystemFiles] = useState(
    () => localStorage.getItem('filePanel.showSystemFiles') === 'true'
  );
  // Track whether the user hard-stopped the current turn (drives the
  // "⏹ Stopped" marker + placeholder). Cleared on the next send.
  const [wasStopped, setWasStopped] = useState(false);
  // Track intentional back navigation (skip session save on unmount)
  const intentionalExitRef = useRef(false);
  // Ref mirrors isActive prop for use in unmount cleanup closures (R1). Written
  // here rather than through useLatestRef so the hooks lint can see it carries a
  // value, not a node, where the unmount cleanup reads it.
  const isActiveRef = useRef(isActive);
  useLayoutEffect(() => {
    isActiveRef.current = isActive;
  });

  // The mobile nav drawer. Desktop navigation is the app-shell AppSidebar.
  const [navPanelVisible, setNavPanelVisible] = useState(false);
  const handleNavMinimize = useCallback(() => setNavPanelVisible(false), []);

  // Subagent cards. Called before useChatMessages, which writes them.
  const {
    cards,
    updateSubagentCard,
    clearSubagentCards,
  } = useCardState();

  // Every subagent's own messages, so a tool row clicked in one of their
  // transcripts resolves to its live record the way a main-thread row does.
  const subagentTranscripts = useMemo(
    () => Object.values(cards).flatMap((card) => (card.subagentData?.messages ? [card.subagentData.messages] : [])),
    [cards],
  );

  // Navigate to a newly created workspace with an optional starter question
  const handleWorkspaceCreated = useCallback(({ workspaceId: newWsId, question }: { workspaceId?: string; question?: string }) => {
    if (!newWsId) return;
    const path = `/chat/t/__default__`;
    const navState = { workspaceId: newWsId, ...(question ? { initialMessage: question } : {}) };
    navigate(path, { state: navState });
  }, [navigate]);

  // Workspace files, shared between FilePanel and ChatInput, and declared
  // before useChatMessages so the agent's writes can refresh them.
  const {
    override: filePanelWorkspaceId,
    setOverride: setFilePanelWorkspaceId,
    shownWorkspaceId: panelWorkspaceId,
    files: workspaceFiles,
    loading: filesLoading,
    error: filesError,
    refresh: refreshFiles,
    refreshOwn: refreshOwnFiles,
    mentionFiles,
    panelAccess: filePanelAccess,
  } = usePanelFiles({
    isHome,
    workspaceId,
    workspaceName,
    includeSystem: showSystemFiles,
    // DALTON-HOOK(workspace-sharing-settings): allow file write for rw colleagues, keep colleague identity separate
    readOnly: !teamReadOnly.canWriteFiles,
    isColleague: teamReadOnly.isColleague,
    // DALTON-HOOK(external-share-guard): external and unknown-tier members cannot create public file share links
    callerTier: resolvedCallerTier,
  });
  // A path names a sibling by its folder from the workspace it was written
  // in: the chat's for the transcript, the panel's for the files it shows.
  const chatFolders = useComputerFolders(workspaceId);
  const chatFoldersReady = useComputerFoldersReady(workspaceId); // DALTON-HOOK(channel-links): track folder query settlement for ?file= deep link routing
  const otherPanelFolders = useComputerFolders(panelWorkspaceId === workspaceId ? null : panelWorkspaceId);
  const panelFolders = panelWorkspaceId === workspaceId ? chatFolders : otherPanelFolders;
  const chatRootDir = chatFolders?.rootDir;

  // Set when the agent writes the user's profile (onboarding saves it there,
  // `user.json` included), and read at turn end.
  const profileWrittenRef = useRef(false);

  // When the agent writes to a memory- or memo-tier path, invalidate the
  // matching queries so the Memory / Memo tab reflects the new content
  // without a manual refresh. classifyAgentPath is the single source of
  // truth — same logic the chat row click routing uses.
  const handleFileArtifact = useCallback((event: { payload?: Record<string, unknown> }) => {
    refreshOwnFiles();
    const filePath = fileArtifactPath(event?.payload as FileOperationArtifactPayload | undefined);
    if (!filePath) return;
    const info = classifyAgentPath(filePath, chatRootDir);
    // The agent writes its own memory, whichever workspace the panel shows.
    if (info.kind === 'memory') {
      if (info.tier === 'user') {
        queryClient.invalidateQueries({ queryKey: queryKeys.memory.user() });
      } else if (workspaceId) {
        queryClient.invalidateQueries({
          queryKey: queryKeys.memory.workspace(workspaceId),
        });
      }
    } else if (info.kind === 'memo') {
      queryClient.invalidateQueries({ queryKey: queryKeys.memo.all });
    } else if (info.kind === 'user-data' && info.entity !== 'automations') {
      // Re-read at turn end rather than now: the profile files are rows the
      // server writes on save, and the turn may write several.
      profileWrittenRef.current = true;
    }
    // The Subagents pane is a page away, so this only marks its list stale
    // for the way back.
    if (isSubagentFilePath(filePath, chatRootDir)) invalidateSubagents(queryClient);
  }, [refreshOwnFiles, queryClient, workspaceId, chatRootDir]);

  // Stable ref-based callback for opening preview URLs from SSE events.
  // Defined here so it can be passed to useChatMessages; assigned after
  // clampPanelWidth/pushPanelHistory are defined further down.
  const openPreviewRef = useRef<(data: PreviewData) => void>(() => {});
  const handleOpenPreviewFromStream = useCallback((data: PreviewData) => {
    openPreviewRef.current(data);
  }, []);
  const browserUseRef = useRef<(use: BrowserUse) => void>(() => {});
  const handleBrowserUseFromStream = useCallback((use: BrowserUse) => {
    browserUseRef.current(use);
  }, []);

  // Chat messages management - writes the subagent cards above.
  // Send, edit, regenerate and retry come from useTranscriptFollow below.
  // DALTON-HOOK(antigravity-series): attach active manual modelPickSeq when reading composer options for HITL resumes
  const threadModelSeqRef = useRef<{ sendModel: string | null; sendModelSeq: number | null }>({ sendModel: null, sendModelSeq: null });
  const readComposerOptions = useCallback(() => attachComposerModelPickSeq(chatInputRef.current?.getModelOptions?.(), threadModelSeqRef.current), []);
  // Filled by useChatCover below. A send that fails at a file's foot comes
  // back to the box; in the chat it stays on screen with its Retry.
  const coverReads = useChatCoverReads();
  const chat = useChatMessages(workspaceId, threadId, {
    updateSubagentCard,
    onFileArtifact: handleFileArtifact, onPreviewUrl: handleOpenPreviewFromStream, onBrowserUse: handleBrowserUseFromStream, clearSubagentCards,
    onWorkspaceCreated: handleWorkspaceCreated, platform: 'web', readComposerOptions, returnsUnsent: coverReads.docked,
  });
  const {
    messages,
    liveMessages,
    isLoading,
    hasActiveSubagents,
    awaitingReportBack,
    reportBackOwed,
    reportBackOwedThreads,
    reportBackOwedKinds,
    turnStatus,
    isCompacting,
    setIsCompacting,
    compactionTokens,
    queuedSend,
    isLoadingHistory,
    historyLoadFailed,
    isLoadingThread,
    olderHistory: { snapshot: olderHistory, load: loadOlderHistory, backfill: backfillOlderHistory },
    isReconnecting,
    modelStatus,
    messageError,
    watchCommentSends,
    editDrafts,
    stopWorkflow,
    stopCompaction,
    pendingInterrupt,
    handleAnswerQuestion,
    handleSkipQuestion,
    handleApproveCreateWorkspace,
    handleRejectCreateWorkspace,
    handleApproveStartQuestion,
    handleRejectStartQuestion,
    handleApprovePTCAgent,
    handleRejectPTCAgent,
    handleApproveSecretaryAction,
    handleRejectSecretaryAction,
    // DALTON-HOOK(watchlist): personal watchlist confirmation card handlers
    handleApproveWatchlistAction,
    handleRejectWatchlistAction,
    handleResumeCreditPause,
    handleApproveToolCall,
    handleRejectToolCall,
    tokenUsage,
    forThread,
    threadId: currentThreadId,
    threadModels,
    marketWatch,
    isShared: threadIsShared,
    handleThumbUp,
    handleThumbDown,
    handleThumbDownFollowUp,
    feedbackByTurn,
    feedbackLoading,
    reconnectIfStaleRun,
    isOwnRun,
    getSubagentHistory,
    getTaskRuns,
    putTaskRun,
    subagentHistoryEntries,
    resolveSubagentIdToAgentId,
    hydrateTaskTranscript,
    sendSubagentInstruction,
  } = chat;
  const { view: sharedThreadView, othersTurn } = useSharedThreadView(workspaceId, messages, isLoading, pendingInterrupt);
  const capabilities = useWorkspaceCapabilities(workspaceId);
  const sharedWorkspace = useSharedEntry(workspaceId) != null;
  // Who sees the conversation, in a shared workspace; null in one's own.
  const { privacy, sendOptions: privacySendOptions, chip: privacyChip, dialog: shareDialog } = useThreadPrivacy(currentThreadId, workspaceId);

  const activeThreadId =
    currentThreadId && currentThreadId !== '__default__' ? currentThreadId : threadId;
  // DALTON-HOOK(antigravity-series): pass workspaceId, activeTurnModel, and createdThreadId for shared guard, queued-send safety, and creation binding
  // DALTON-HOOK(model-picker-races): always forward activeTurnModel so first-turn pre-admission failures preserve picks in Core builds too
  const threadModel = useThreadModel({
    threadId: activeThreadId,
    isLoading,
    initialModel: navModel,
    chiefOfStaff: isHome,
    workspaceId,
    activeTurnModel: chat.activeTurnModel,
    ...(isDaltonBuild ? { createdThreadId: chat.createdThreadId } : {}),
  });
  threadModelSeqRef.current = threadModel;
  const { pickModel: pickThreadModel, thread: threadRecord } = threadModel;
  // DALTON-HOOK(colleague-dispatch): determine whether the active thread is read-only for the caller
  const effectiveThreadRecord = useMemo(() => {
    if (threadRecord) return threadRecord as Record<string, unknown>;
    if (!activeThreadId || activeThreadId === '__default__') return null;
    const cachedDetail = queryClient.getQueryData<Record<string, unknown>>(
      queryKeys.threads.detail(activeThreadId),
    );
    if (cachedDetail) return cachedDetail;
    if (workspaceId) {
      const entries = queryClient.getQueriesData<unknown>({
        queryKey: queryKeys.threads.byWorkspace(workspaceId),
      });
      for (const [, data] of entries) {
        if (!data || typeof data !== 'object') continue;
        const directThreads = (data as { threads?: Array<Record<string, unknown>> }).threads;
        if (Array.isArray(directThreads)) {
          const match = directThreads.find(
            (t) => (t.thread_id || t.conversation_thread_id) === activeThreadId,
          );
          if (match) return match;
        }
        const pages = (
          data as { pages?: Array<{ threads?: Array<Record<string, unknown>> }> }
        ).pages;
        if (Array.isArray(pages)) {
          for (const page of pages) {
            const match = page?.threads?.find(
              (t) => (t.thread_id || t.conversation_thread_id) === activeThreadId,
            );
            if (match) return match;
          }
        }
      }
    }
    return null;
  }, [threadRecord, activeThreadId, workspaceId, queryClient]);
  const workspaceOwnerId =
    (effectiveThreadRecord?.workspace_owner_id as string | null | undefined) ??
    (workspaceRecord?.user_id as string | null | undefined) ??
    null;
  const currentUserId =
    currentUser?.user_id ?? (!teamReadOnly.isColleague ? workspaceOwnerId : null);
  const threadUserId =
    (effectiveThreadRecord?.thread_user_id as string | null | undefined) ??
    (effectiveThreadRecord?.user_id as string | null | undefined) ??
    null;
  const rawMetadata =
    (effectiveThreadRecord?.metadata as Record<string, unknown> | null | undefined) ?? null;
  const dispatchedBy =
    ((effectiveThreadRecord?.dispatched_by ?? rawMetadata?.dispatched_by) as
      | { user_id?: string; name?: string | null; channel?: string }
      | null
      | undefined) ?? null;
  const isForeignThreadOwner = Boolean(
    (threadUserId && currentUserId && threadUserId !== currentUserId) ||
      (dispatchedBy?.user_id &&
        (!currentUserId || dispatchedBy.user_id !== currentUserId)) ||
      (!teamReadOnly.isColleague &&
        threadUserId &&
        workspaceOwnerId &&
        threadUserId !== workspaceOwnerId),
  );
  const isOwnThreadInRwColleagueWs =
    teamReadOnly.isColleague &&
    teamReadOnly.colleagueDispatchEnabled &&
    !teamReadOnly.isReadOnly &&
    (threadId === '__default__' ||
      Boolean(threadUserId && currentUserId && threadUserId === currentUserId));
  const isThreadReadOnly =
    isForeignThreadOwner || (teamReadOnly.isColleague && !isOwnThreadInRwColleagueWs);
  const dispatchedByName = isForeignThreadOwner
    ? dispatchedBy?.name || dispatchedBy?.user_id || teamReadOnly.ownerName || '同事'
    : null;
  const threadSubagents = useThreadSubagents({
    threadId: currentThreadId,
    landing: subagentsLanding,
  });
  const { toSend: subagentsToSend } = threadSubagents;
  // DALTON-HOOK(antigravity-series): pass workspaceId & createdThreadId to useThreadTuning for shared local-only mode and null-origin handle binding
  // DALTON-HOOK(model-picker-races): forward createdTuning baseline for conditional post-create tuning repatch (both Dalton and Core builds)
  const threadTuning = useThreadTuning({
    threadId: currentThreadId,
    landing: tuningLanding,
    workspaceId,
    createdTuning: chat.createdThreadTuning,
    ...(isDaltonBuild ? { createdThreadId: chat.createdThreadId } : {}),
  });
  const { toSend: tuningToSend, clearLocalEffort } = threadTuning;
  const { seriesCatalog, customModels, metadata: modelMetadata, rawApiResponse } = useAllModels();
  // DALTON-HOOK(antigravity-series): clear local effort on Antigravity pick, confirming on success and restoring to pre-save value on immediate or deferred failure in Dalton builds
  const handlePickThreadModel = useCallback((nextModel: string) => {
    const rawMeta = rawApiResponse?.model_metadata ?? modelMetadata;
    const isAntigravity = Boolean(inspectSavedAntigravityModel(nextModel, { seriesCatalog, rawMetadata: rawMeta, customModels }));
    if (!isDaltonBuild) {
      if (isAntigravity) clearLocalEffort();
      return pickThreadModel(nextModel);
    }
    const effortClear = isAntigravity ? clearLocalEffort() : null;
    return pickThreadModel(nextModel, effortClear ? {
      onSettled: (saved) => {
        if (saved) effortClear.confirm();
        else effortClear();
      },
    } : undefined).catch((err: unknown) => {
      effortClear?.();
      throw err;
    });
  }, [clearLocalEffort, customModels, modelMetadata, pickThreadModel, rawApiResponse, seriesCatalog]);

  // The route's `__default__` stands for a chat with no thread yet; handing it
  // to the panel would give every unsent chat in a workspace one shared strip.
  const liveThreadId = currentThreadId || threadId;
  const panelThreadId = liveThreadId && liveThreadId !== '__default__' ? liveThreadId : undefined;
  // The host keys this view by chat instance, so this lives as long as the
  // chat, across the panel's own remounts. What the chat held goes to its
  // thread once it has one, whether or not the panel is open, and the rest
  // goes with the chat.
  const panelChatKey = useId();
  useEffect(() => () => releaseChatStrips(panelChatKey), [panelChatKey]);
  useEffect(() => { if (panelThreadId) handChatStrips(panelChatKey, panelThreadId); }, [panelChatKey, panelThreadId]);

  // Spinner state merges the in-conversation signal (chat SSE `workspace_status`
  // events, set when this client's message owns the start) with the entry-time
  // warming signal (the /events stream, which sees the start even when a
  // background warm owns it). 'archived' from either source wins so the slow-
  // restore copy survives a plain 'starting' from the other.
  const displayTurnStatus = {
    ...turnStatus,
    workspaceStarting: mergeWarmingDisplay(turnStatus.workspaceStarting, warmingState),
  };
  // The running turn held before its model has done anything: a compaction
  // ahead of its first call, or the send waiting behind one. A compaction
  // there summarizes the thread up to the turn's message, so its line goes
  // above that message, where the divider its end leaves goes too.
  const turnUnstarted = useLiveSelect(liveMessages, newestTurnUnstarted);
  const heldBeforeWork = isLoading && turnUnstarted && (!!isCompacting || displayTurnStatus.turnHeld !== null);
  const compaction = compactionOf(isCompacting, compactionTokens, displayTurnStatus.turnHeld);
  const compactionAbove = heldBeforeWork ? compaction : null;

  const chatPlaceholder = useMemo(() => {
    if (wasStopped && !isLoading && !pendingInterrupt)
      return t('chat.placeholderStopped');
    if (isLoading) return t('chat.placeholderLoading');
    if (hasActiveSubagents) return t('chat.placeholderSubagentsRunning');
    // The input's own default, which knows a shared workspace has no skills.
    return undefined;
  }, [wasStopped, isLoading, pendingInterrupt, hasActiveSubagents, t]);

  // A market watch is running, so a watch call's row opens the watch's live
  // Status tab (useRightPanel). The tray reads the same snapshot for its chip.
  const showWatchChip = (marketWatch?.symbols?.length ?? 0) > 0;

  useComposerGiveBack(chatInputRef, chat);

  // Read by the unmount cleanup, whose own closure holds a stale thread id
  const readCurrentThreadId = useEffectEvent(() => currentThreadId);
  // The resolved thread ID, handed to useSubagentTabs so its callbacks read the
  // current one without closing over it.
  const resolvedThreadIdRef = useLatestRef(currentThreadId || threadId);

  const isStreaming = isTurnOpen(chat);
  // Chat transcript scroll controller + tab scroll memory (chatView/useChatScroll).
  const scroll = useChatScroll({
    activeAgentId,
    messages,
    isActive,
    isActiveRef,
    isLoadingHistory,
    historyLoadFailed,
    isStreaming,
    currentThreadId,
    threadId,
  });
  const {
    scrollAreaRef,
    transcriptFloorRef,
    subagentScrollRoot,
    subagentPane,
    takeTabPlace,
    getScrollContainer,
    pinToBottom,
    saveScrollPosition,
    jumpPill,
    activeAgentIdRef,
    isNearBottomRef,
    restoredForThreadRef,
    pinToMessage,
    revealFiles,
    revealMessage,
    holdPlace,
    toTop,
    entryRestored,
    pinTargetRef,
  } = scroll;
  // What the next send summarizes first, which every turn the reader starts carries.
  const { summary: summaryHint, starters: summarized } = useNextSummary(chat);
  // What the composer says about the next send, each on the control it is about.
  const composerNotices = useComposerNotices({
    threadId: currentThreadId,
    threadModel,
    subagents: threadSubagents,
    chat,
    summary: summaryHint,
    workspaceId,
    chiefOfStaff: isHome,
  });
  const { handleSendMessage, handleEditMessage, handleRegenerate, handleRetry } = useTranscriptFollow(
    { ...chat, ...summarized },
    scroll.follow,
    () => getScrollContainer(scrollAreaRef),
    readTurnEndScroll(preferences),
  );
  // Older turns of a paged thread, asked for as the reader nears the top and
  // landed under a hold on what they are reading.
  const getMainScrollRoot = useCallback(() => getScrollContainer(scrollAreaRef), [getScrollContainer, scrollAreaRef]);
  const loadOlderPage = useCallback(() => {
    void loadOlderHistory(holdPlace);
  }, [loadOlderHistory, holdPlace]);
  // What a newest page cut short on its bytes is owed, landed the same way.
  useHistoryBackfill({
    snapshot: olderHistory,
    ready: isActive && entryRestored,
    backfill: backfillOlderHistory,
    place: holdPlace,
  });

  // One value for both transcripts below (main thread and subagent tab), so a
  // flip in Settings reaches them together and neither re-renders on the other's
  // account.
  const transcriptDisplay = useMemo<TranscriptDisplay>(
    () => ({
      turnDisplay: readTurnDisplay(preferences),
      streamingMode: readStreamingMode(preferences),
      toolCallLayout: readToolCallLayout(preferences),
    }),
    [preferences],
  );

  // Subagent tab registry + card refresh (chatView/useSubagentTabs).
  const {
    sidebarAgentRows,
    activeAgent,
    handleSelectAgent,
    handleOpenSubagentTask,
    handleRemoveAgent,
    handleSubagentInstruction,
    resolveSubagentTelemetry,
    resolveWorkflowRun,
  } = useSubagentTabs({
    threadId,
    workspaceId,
    initialTaskId,
    isLoadingHistory,
    activeAgentId,
    setActiveAgentId,
    cards,
    updateSubagentCard,
    sendSubagentInstruction,
    getSubagentHistory,
    getTaskRuns,
    putTaskRun,
    resolveSubagentIdToAgentId,
    hydrateTaskTranscript,
    saveScrollPosition,
    activeAgentIdRef,
    resolvedThreadIdRef,
  });
  const subagentTelemetry = useMemo(
    () => ({ resolve: resolveSubagentTelemetry, followsRuns: true }),
    [resolveSubagentTelemetry],
  );

  // A navigation naming a turn (the command palette's "In conversations" hit)
  // opens the thread there, once its entry has landed.
  const showMainTranscript = useCallback(() => handleSelectAgent('main'), [handleSelectAgent]);
  const focusNewest = useCallback(() => pinToBottom('auto'), [pinToBottom]);
  useFocusTurn({
    isActive,
    ready: entryRestored,
    onMain: activeAgentId === 'main',
    showMain: showMainTranscript,
    messages,
    status: olderHistory.status,
    load: loadOlderHistory,
    place: holdPlace,
    reveal: revealMessage,
    toNewest: focusNewest,
    scrollRoot: getMainScrollRoot,
  });

  // Whether the open subagent's turn is still running. A bubble's own
  // `isStreaming` goes false between two model calls of one agent loop, so a
  // transcript that judged liveness by the bubble alone would fold a working
  // task behind a "Worked for" summary and then unfold it. The task's own
  // status is the turn-length signal, read through the same derivation the
  // status indicator above the transcript uses.
  const subagentTurnLive = activeAgent
    ? !isTerminalStatus(deriveSubagentStatus({ status: activeAgent.status, messages: activeAgent.messages }))
    : false;
  // The subagent transcript's follow: the market panel's, gliding while that
  // turn runs, and opening where the reader left the tab, or at its end. Its
  // pane is gone once main is shown, though its state clears a commit later.
  // A tab opened onto a transcript still being fetched loads until its first
  // message, which can land long after the opening's hold has lapsed.
  const subagentShown = activeAgentId === 'main' ? null : subagentPane;
  const { toTop: subagentToTop } = useStreamFollow(subagentShown?.scroller ?? null, subagentShown?.content ?? null, {
    live: subagentTurnLive,
    loading: !!activeAgent && (activeAgent.messages?.length ?? 0) === 0,
    view: activeAgentId,
    openAt: () => takeTabPlace(activeAgentId),
  });

  // Publish this view's subagent registry to the global AppSidebar while it is
  // the visible ChatView. Same thread key as the drawer's `currentThreadId`
  // prop below, and the same row array + select/remove closures — the sidebar
  // tree renders from identical inputs, so the two stay in lockstep. Every dep
  // here is identity-stable across streamed chunks (rows keep their identity
  // while no rendered field changes; the handlers are the hook's stable
  // wrappers), so this effect re-runs only on genuine sidebar changes.
  const sidebarAgentsKey = currentThreadId || threadId;
  useEffect(() => {
    if (!isActive || !sidebarAgentsKey || sidebarAgentsKey === '__default__') return;
    publishSidebarAgents({
      threadId: sidebarAgentsKey,
      agents: sidebarAgentRows,
      activeAgentId,
      onSelectAgent: handleSelectAgent,
      onRemoveAgent: handleRemoveAgent,
    });
    return () => clearSidebarAgents(sidebarAgentsKey);
  }, [isActive, sidebarAgentsKey, sidebarAgentRows, activeAgentId, handleSelectAgent, handleRemoveAgent]);

  // The same inputs the AppSidebar receives over the bridge, so the drawer's
  // tree and the desktop tree render identically.
  const navAgentsSlice = useMemo(() => ({
    agents: sidebarAgentRows,
    activeAgentId,
    onSelectAgent: handleSelectAgent,
    onRemoveAgent: handleRemoveAgent,
  }), [sidebarAgentRows, activeAgentId, handleSelectAgent, handleRemoveAgent]);


  // Save chat session on unmount for cross-tab restoration (workspace + thread only).
  // Only the active view saves — evicted hidden views must not overwrite (R1).
  useEffect(() => {
    return () => {
      if (!isActiveRef.current) return;
      if (intentionalExitRef.current) {
        saveChatSession({ workspaceId });
        return;
      }
      saveChatSession({
        workspaceId,
        threadId: readCurrentThreadId(),
      });
    };
  }, [workspaceId]);

  // Consume saved session on mount so it doesn't interfere with future navigations.
  // One-shot: fires once per instance, never re-fires on isActive changes (R5).
  const sessionConsumedRef = useRef(false);
  useEffect(() => {
    if (sessionConsumedRef.current) return;
    sessionConsumedRef.current = true;
    const session = getChatSession();
    if (session && session.workspaceId === workspaceId) {
      clearChatSession();
    }
  }, [workspaceId]);

  // useChatMessages recreates its send/stop functions on every render — i.e.
  // every streamed chunk. The composer callbacks below route through these
  // stable wrappers instead of depending on the functions directly, so the
  // memoized ChatInput keeps identity-stable props while a turn streams. The
  // wrappers re-point post-commit; the callbacks only fire from user events,
  // which always run after commit, so they never see a stale function.
  const stableSendMessage = useStableHandler(handleSendMessage);
  // What every send from this chat carries beside its own.
  // DALTON-HOOK(antigravity-series): tag outgoing Dalton sends with the active manual model pick sequence so queued sends never clear a newer unsent pick
  const composerSendOptions = useStableHandler((options: SendOptions): SendOptions => {
    const modelPickSeq = isDaltonBuild && options.model && options.model === threadModel.sendModel
      ? threadModel.sendModelSeq
      : undefined;
    return {
      ...options,
      ...(modelPickSeq != null ? { modelPickSeq } : {}),
      subagentsAllowed: subagentsToSend,
      ...tuningToSend,
      ...privacySendOptions,
    };
  });
  // The file comments queued for this thread's next send, shown above the
  // text box and carried by its send, and the sender the file panel uses.
  const commentSends = useChatCommentSends({
    threadId: panelThreadId, running: isLoading, watch: watchCommentSends, chatCovered: coverReads.covered,
    sendToChat: (artifactComments) => stableSendMessage('', null, null, composerSendOptions({ ...readComposerOptions(), artifactComments })),
  });
  const { deck: commentDeck, take: takeQueuedComments } = commentSends;
  // A rerun carries what the composer shows, as a send does, so a pick whose
  // save has not landed is not lost to the row the server would read.
  const stableEditMessage = useStableHandler((id: string, content: string) =>
    handleEditMessage(id, content, chatInputRef.current?.getModelOptions?.()));
  const stableRegenerate = useStableHandler((id: string) =>
    handleRegenerate(id, chatInputRef.current?.getModelOptions?.()));
  // DALTON-HOOK(automation-effort): identify the currently retryable failed reply so prior unsent effort picks do not carry into a later retry
  const retryTargetId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (m.role === 'assistant') {
        return (m as { error?: unknown }).error ? m.id : null;
      }
    }
    return null;
  }, [messages]);
  // DALTON-HOOK(automation-effort): forward retry model options from action bundle and prefer getRetryModelOptions
  const stableRetry = useStableHandler((opts?: Parameters<typeof handleRetry>[0]) =>
    handleRetry(
      opts
      ?? chatInputRef.current?.getRetryModelOptions?.()
      ?? chatInputRef.current?.getModelOptions?.(),
    ));
  const stableStopWorkflow = useStableHandler(stopWorkflow);
  const stableStopCompaction = useStableHandler(stopCompaction);

  // Hard-stop handler: terminates the current turn immediately (main agent +
  // all subagents) while preserving state. The hook's stopWorkflow aborts the
  // client reader, finalizes the open message, and POSTs /cancel; we flip the
  // "⏹ Stopped" marker here.
  const handleStop = useCallback(() => {
    setWasStopped(true);
    void stableStopWorkflow();
  }, [stableStopWorkflow]);

  // Set when the user stops a MANUAL compaction so handleAction's .catch
  // (the summarize/offload request rejects once the backend cancels it) shows a
  // "stopped" notice instead of an error banner. Reset at the start of each new
  // compaction in handleAction.
  const userStoppedCompactionRef = useRef(false);

  // Monotonic token: each manual compaction trigger bumps it, so a late
  // resolution/rejection from a superseded compaction can detect it is stale
  // and skip flipping isCompacting (RT#2). Without this, a rapid
  // /compact→Stop→/compact lets the first request's late .catch clear the flag
  // and unmask the input while the second compaction is still running.
  const compactionGenerationRef = useRef(0);

  // Single stop control reused by the chat-input Stop button. A manual
  // compaction has isLoading=false (no streaming turn) so it routes to
  // stopCompaction; otherwise (a running turn, including an auto Tier-2
  // summarize) it tears down the turn via stopWorkflow.
  const handleStopButton = useCallback(() => {
    if (routeStopAction({ isCompacting, isLoading }) === 'compaction') {
      userStoppedCompactionRef.current = true;
      void stableStopCompaction();
    } else {
      handleStop();
    }
  }, [isCompacting, isLoading, handleStop, stableStopCompaction]);

  const panelChart = usePanelChart(isActive, chatInputRef);
  // DALTON-HOOK(chat-attachment-count): panel chart region screenshots count toward MAX_FILES
  const panelChartImageCount = useMemo(
    () => panelChart.chips.filter((s) => Boolean(s.croppedImage)).length,
    [panelChart.chips],
  );
  const prevPanelChartImageCountRef = useRef(panelChartImageCount);
  useEffect(() => {
    const prev = prevPanelChartImageCountRef.current;
    prevPanelChartImageCountRef.current = panelChartImageCount;
    if (panelChartImageCount > prev && (chatInputRef.current?.getAttachmentCount() ?? panelChartImageCount) > MAX_FILES) {
      toast({
        title: t('chat.attachmentsTooMany', { max: MAX_FILES }),
        variant: 'destructive',
      });
    }
  }, [panelChartImageCount, t]);

  // Wrapper: converts ChatInput's send into handleSendMessage(message,
  // additionalContext, attachmentMeta), the send itself the draft it gives back.
  const handleSendWithAttachments = useCallback((send: ComposerSend) => {
    const { message, files, commands: slashCommands, modelOptions } = send;
    // DALTON-HOOK(chat-large-upload): attachmentSendParts carries both upstream staged attachment_id and Dalton chat_upload upload_id
    const attached = attachmentSendParts(files);
    const contexts: Record<string, unknown>[] = [...attached.contexts];
    const meta = [...attached.meta];

    // Skill contexts from slash commands
    for (const cmd of slashCommands) {
      if (cmd.type === 'skill') {
        contexts.push({ type: 'skills', name: cmd.skillName });
      } else if (cmd.type === 'subagent') {
        contexts.push({ type: 'directive', content: 'User wishes you to complete this task using subagents.' });
      }
    }

    // Watch toggle activates the market-watch skill (re-sent every turn while
    // on, as MarketView's sends carry the chart-annotation skill). Dedup against a manually-typed
    // /market-watch pill added by the loop above (SkillsMiddleware also dedups
    // the body, but this keeps the context list clean).
    if (modelOptions.marketWatch && marketWatchEnabled && !contexts.some((c) => c.type === 'skills' && c.name === 'market-watch')) {
      contexts.push({
        type: 'skills',
        name: 'market-watch',
        instruction: 'Market watch mode is on for this message. If the central tickers are not yet registered, register them with watch_market.',
      });
    }

    // Widget context snapshots from the deck rail. Each snapshot becomes one
    // `{type:"widget"}` item plus an optional sibling `{type:"image"}` item
    // (the existing MultimodalContext channel handles vision-vs-text-only
    // routing). The same snapshots are also forwarded to handleSendMessage so
    // the user message renders chip cards inline below its bubble.
    if (modelOptions.widgetSnapshots && modelOptions.widgetSnapshots.length > 0) {
      contexts.push(...widgetSnapshotsToContexts(modelOptions.widgetSnapshots));
    }

    // DALTON-HOOK(chat-attachment-count): guard total attachments (files + widget/chart screenshots)
    // before panelChart.takeForSend clears chart selections or dispatches a doomed request.
    if (countContextAttachments(contexts) + panelChartImageCount > MAX_FILES) {
      toast({
        title: t('chat.attachmentsTooMany', { max: MAX_FILES }),
        variant: 'destructive',
      });
      return false;
    }

    // Regions and price levels picked on a panel chart tab, and what each chart shows.
    const { picked, chartViews } = panelChart.takeForSend(message);
    if (picked) {
      contexts.push(...picked.contexts);
      meta.push(...picked.attachments);
    }

    const additionalContext = contexts.length > 0 ? contexts : null;
    const attachmentMeta = meta.length > 0 ? meta : null;
    const outgoing = picked?.outgoingMessage ?? message;
    // The queued file comments go with every send, the text as their note.
    const artifactComments = takeQueuedComments(outgoing);
    // DALTON-HOOK(composer-drafts): forward send result so ChatInput preserves the draft when sending fails
    return stableSendMessage(
      outgoing,
      additionalContext,
      attachmentMeta,
      composerSendOptions({
        ...modelOptions,
        ...(picked ? { chartSelections: picked.snapshots } : {}),
        ...(artifactComments ? { artifactComments } : {}),
        ...(chartViews.length > 0 ? { chartViews } : {}),
        // DALTON-HOOK(composer-drafts): the box as the person wrote it — a
        // send the server refuses while the transcript is out of view goes
        // back to the composer whole (attachments, mentions and commands too)
        // instead of leaving a failed reply nobody reads.
        draft: send,
      }),
    );
  }, [marketWatchEnabled, stableSendMessage, panelChart, panelChartImageCount, takeQueuedComments, composerSendOptions, t]);

  const planReader = useLatestRef(usePlanReader());

  // Handle action-type slash commands (e.g. /compact, /compaction, /offload)
  const handleAction = useCallback((cmd: ActionCommand) => {
    const tid = currentThreadId || threadId;
    if (!tid || tid === '__default__') return;
    const thread = forThread(tid);

    // Surface backend errors from /compact + /offload. Backend may return
    // detail as a structured object ({code, verb, message}) — the 409
    // "workflow_active" case comes through this path when the user fires
    // /compact mid-stream, and we upgrade it to a warning banner.
    const surfaceActionError = (err: unknown, fallbackKey: string) => {
      const resp = (err as { response?: { status?: number; data?: unknown } } | undefined)?.response;
      const data = (resp?.data ?? undefined) as { detail?: unknown } | undefined;
      const detail = data?.detail;
      // `typeof null === 'object'` and arrays are objects in JS, so guard both.
      if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
        const obj = detail as { code?: string; message?: string };
        if (obj.code === 'workflow_active') {
          thread.insertNotification(t('chat.compactBusy'), 'warning');
          return;
        }
        const planWords = planActionText(t, detail as Record<string, unknown>, { ...planReader.current, now: Date.now() });
        if (planWords) {
          thread.insertNotification(planWords, 'warning');
          return;
        }
        if (typeof obj.message === 'string' && obj.message.length > 0) {
          thread.insertNotification(obj.message, 'warning');
          return;
        }
        thread.insertNotification(t(fallbackKey), 'warning');
        return;
      }
      if (typeof detail === 'string' && detail.length > 0) {
        thread.insertNotification(detail, 'warning');
        return;
      }
      thread.insertNotification(t(fallbackKey), 'warning');
    };

    // A user Stop while this compaction runs cancels the backend call, which
    // rejects the request below. Treat that as a clean stop (not an error).
    // The backend's shared cancellation wrapper tags any user-cancelled request
    // with a structured detail (409 {code: "request_cancelled"}); honor that
    // even when the local ref was already consumed — a rapid stop→retrigger
    // resets the ref before this rejection lands, which would otherwise mislabel
    // the stop as a failure.
    const handleActionError = (err: unknown) => {
      const code = compactionErrorCode(err);
      if (isUserStoppedCompaction({ userStopped: userStoppedCompactionRef.current, errorCode: code })) {
        userStoppedCompactionRef.current = false;
        thread.insertNotification(t('chat.compactionStopped'), 'info');
        return;
      }
      surfaceActionError(err, 'chat.compactionError');
    };

    // Snapshot the generation BEFORE the await so a superseded compaction's late
    // settlement leaves the active one's isCompacting flag alone (RT#2).
    const clearIfCurrent = (myGeneration: number) => {
      if (shouldClearCompactingFlag(myGeneration, compactionGenerationRef.current)) {
        setIsCompacting(false);
      }
    };

    // Refuse a duplicate /compact or /offload while a manual compaction is
    // already running (#1). The duplicate would 409 ("compaction_in_progress")
    // on the backend, but it first bumps the generation token — which would
    // strand isCompacting, since the real (earlier-generation) compaction's
    // completion could then no longer clear the flag. Block it before it enters
    // the generation protocol. (An auto Tier-2 summarize has isLoading=true, so
    // this guard does not fire there.)
    if (
      (cmd.name === 'compact' || cmd.name === 'offload') &&
      isManualCompactionInFlight({ isCompacting, isLoading })
    ) {
      thread.insertNotification(t('chat.compactBusy'), 'warning');
      return;
    }

    if (cmd.name === 'compact') {
      // SSE wire action value "summarize" is preserved as a protocol contract.
      userStoppedCompactionRef.current = false;
      const myGeneration = ++compactionGenerationRef.current;
      setIsCompacting('summarize');
      summarizeThread(tid)
        .then((data: Record<string, unknown>) => {
          clearIfCurrent(myGeneration);
          thread.landCompaction({
            notice: t('chat.compactedNotification', { from: data.original_message_count }),
            detail: (data.summary_text as string | undefined) || undefined,
          });
        })
        .catch((err: unknown) => {
          console.error('[ChatView] Compaction failed:', err);
          handleActionError(err);
          clearIfCurrent(myGeneration);
        })
        .finally(() => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.features.claudeCode() });
        });
    } else if (cmd.name === 'offload') {
      userStoppedCompactionRef.current = false;
      const myGeneration = ++compactionGenerationRef.current;
      setIsCompacting('offload');
      offloadThread(tid)
        .then((data: Record<string, unknown>) => {
          clearIfCurrent(myGeneration);
          thread.landCompaction({
            notice: t('chat.offloadedNotification', {
              args: (data.offloaded_args as number) || 0,
              reads: (data.offloaded_reads as number) || 0,
            }),
            nextSummary: (data.next_summary as NextSummary | null | undefined) ?? null,
          });
        })
        .catch((err: unknown) => {
          console.error('[ChatView] Offload failed:', err);
          handleActionError(err);
          clearIfCurrent(myGeneration);
        });
    }
  }, [currentThreadId, threadId, setIsCompacting, forThread, isCompacting, isLoading, t, planReader, queryClient]);

  // Show sidebar at the start of each backend response (streaming)
  // Auto-refresh workspace files when agent finishes (isLoading transitions true→false)
  const prevLoadingRef = useRef(false);
  useEffect(() => {
    const wasLoading = prevLoadingRef.current;
    prevLoadingRef.current = isLoading;
    if (isLoading && !wasLoading) {
      setWasStopped(false);
    }
    if (!isLoading && wasLoading) {
      refreshOwnFiles();
      // Whether onboarding is done is the profile's to say, and the app reads
      // it off the user row. A turn that wrote the profile re-reads that row
      // and the preferences. So does any Home turn while onboarding is still
      // open, or not known to be done, because a profile written through Bash
      // or code reports no file.
      const me = queryClient.getQueryData<{ onboarding_completed?: boolean }>(queryKeys.user.me());
      const onboardingOpen = isHome && me?.onboarding_completed !== true;
      if (profileWrittenRef.current || onboardingOpen) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      }
      profileWrittenRef.current = false;
    }
  }, [isLoading, refreshOwnFiles, queryClient, isHome]);









  // Right-panel controller (chatView/useRightPanel).
  const { data: chatComputer } = useComputer(workspaceRecord?.computer_id ?? null);
  const browserOn = useFeatureEnabled(BROWSER_FEATURE);
  const {
    panelTarget,
    handleTargetHandled,
    handleTargetMemoryHandled,
    handleTargetMemoHandled,
    rightPanelType,
    setRightPanelType,
    rightPanelWidth,
    panelExpanded,
    handleTogglePanelExpanded,
    previewData,
    panelWrapperRef,
    isDragging,
    dragJustEnded,
    handleDividerMouseDown,
    popPanelHistory,
    handleOpenFileFromChat,
    handleOpenSourcesFromChat,
    handleOpenStatusFromChat,
    handleToolCallDetailClick,
    handleCloseDetailPanel,
    handleClosePreview,
    handleRefreshPreview,
    handleToggleFilePanel,
    handleFilesLeaveGuardChange,
    handleActiveTabKindChange,
    // DALTON-HOOK(mobile-web): activeTabKind no longer needed for mobile FilePanel drag toggle
    leaveFiles,
    handleOpenPreview,
    handleBrowserUse,
    browserViewFor,
    linkActions,
    handleOpenArtifactComment,
    handleOpenChart,
    handleOpenInMarketView,
    detailToolCall,
    transcript,
    getRecentWritePaths,
  } = useRightPanel({
    isMobile,
    workspaceId,
    folders: chatFolders,
    foldersReady: chatFoldersReady, // DALTON-HOOK(channel-links): defer rooted ?file= deep link consumption until folder queries settle
    threadId: panelThreadId,
    isActive,
    containerRef,
    setFilePanelWorkspaceId,
    filePanelWorkspaceId,
    messages,
    subagentTranscripts,
    watching: showWatchChip,
    computer: chatComputer ?? null,
    shared: sharedWorkspace,
    browserOn,
    // DALTON-HOOK(workspace-sharing-settings): suppress preview sandbox startup for colleagues (ro and rw)
    readOnly: teamReadOnly.isColleague,
    isColleague: teamReadOnly.isColleague,
  });

  // Whether the file panel covers the chat, and the composer that moves to the
  // file's foot while it does (chatView/useChatCover).
  const { dockOpen, dock: composerDock, panelInnerRef, onSheetLanded } = useChatCover({
    isMobile, filePanel: rightPanelType === 'file', panelExpanded, panelWidth: rightPanelWidth, threadId: panelThreadId,
    mainAgent: activeAgentId === 'main', queuedComments: commentDeck.comments, reads: coverReads,
  });
  // Files that hold the send keep the comments waiting with them; the
  // composer takes focus, where the hint beside Send says why.
  const addCommentsToTurn = useStableHandler(() => {
    const input = chatInputRef.current;
    if (input && !input.send() && input.holdsSend()) input.focus();
  });
  const focusComposer = useStableHandler(() => { chatInputRef.current?.focus(); });
  // The queued comments ride above the text box and count as something to
  // send. Folded as the sheet starts over the chat, so it docks folded.
  const composerDeck = {
    hasExternalContext: panelChart.chips.length > 0 || commentDeck.ready.length > 0,
    placeholder: commentDeck.placeholder ?? chatPlaceholder,
    // DALTON-HOOK(team-sharing): a colleague's workspace or a read-only
    // thread shows no docked comment box — comments are sent into the thread
    // from the owner's own chat.
    deck: teamReadOnly.isColleague || isThreadReadOnly ? null : <QueuedCommentDeck deck={commentDeck} running={isLoading} compact={isMobile && dockOpen} onOpen={handleOpenArtifactComment}
      onAddToTurn={addCommentsToTurn} focusComposer={focusComposer} />,
  };

  // On desktop the header's files toggle gives way to the panel's own the
  // moment it is pressed, so a panel still fetching its code would leave
  // neither on screen; on a phone a panel opening on its code's fetch slides
  // in blank. Fetched once the view settles, or as the pointer nears the
  // toggle, it is there before the click.
  useEffect(() => {
    const timer = setTimeout(() => { void loadFilePanel(); }, 1500);
    return () => clearTimeout(timer);
  }, []);

  // DALTON-HOOK(channel-deep-links): parse ?chart=&layer= (open chart panel & focus layer) and ?widget= (scroll/highlight widget)
  useChannelDeepLinks({
    active: isActive,
    workspaceId,
    onOpenChart: handleOpenChart,
    messagesDeps: messages,
  });
  // The file panel's props are one compiled scope, so this handler's per-chunk
  // identity would re-create every inline prop beside it and re-render the panel.
  const openSubagentTaskFromPanel = useStableHandler(handleOpenSubagentTask);

  // The dispatcher's deep-link context for PTC-agent proposal cards. Memoized:
  // a fresh object per render would defeat the bubble memo in Home.
  const dispatcherContext = useMemo(
    () => (isHome && currentThreadId ? { threadId: currentThreadId, workspaceId } : null),
    [isHome, currentThreadId, workspaceId],
  );

  // The thread's background commands, read once here for the tray and the
  // tool cards, which watch this read in the cache.
  const commands = useThreadCommands(panelThreadId, isActive);
  // On a phone the file panel covers the chat, so it gives way to the end of
  // the chat it would otherwise scroll out of sight.
  const jumpToLatest = () => {
    if (isMobile && rightPanelType === 'file') {
      setRightPanelType(null);
      popPanelHistory();
    }
    pinToBottom('smooth');
  };
  const { status: threadStatus, plansUnderMessages } = useThreadStatusValue({
    marketWatch, commands, threadId: panelThreadId ?? null, isActive, cards, taskHistory: subagentHistoryEntries,
    messages, isLoading: !!isLoading, hasActiveSubagents, reportBackOwed, owedThreads: reportBackOwedThreads,
    owedKinds: reportBackOwedKinds, isHome, awaitingAnswer: !!pendingInterrupt, from: dispatcherContext,
    onSelectAgent: handleSelectAgent, onOpenSubagentTask: handleOpenSubagentTask, onOpenCommand: handleToolCallDetailClick,
    onOpenStatusPanel: handleOpenStatusFromChat, onJumpToLatest: jumpToLatest,
  });
  const trayContents = threadStatus.contents;
  // The composer tray, and its fold: while the reader is up in the history the
  // tray folds into the composer's toolbar, on the jump-to-latest pill's
  // signal and until the pill's glide to the end has landed. Clicking the
  // folded dock pours it back out where the reader is, until they scroll on.
  // Kept inside the composer as its top strip, the tray has nothing scrolling
  // under it and nowhere to fold, so the fold sees no tray.
  const trayInside = readComposerTray(preferences) === 'inside';
  const floatingTray = trayInside ? null : trayContents;
  const { phase: trayPhase, trayRef, dockRef: trayDockRef, peek: peekTray, settle: settleTray } = useTrayFold({
    scrolledUp: jumpPill.visible,
    jumping: scroll.jumping,
    contents: floatingTray,
    getScroller: getMainScrollRoot,
    docked: composerDock.docked,
    covered: !!othersTurn,
  });
  // Where the tray sits: above the composer with its dock in the toolbar, or
  // as the composer's strip.
  const tray = floatingTray
    ? {
      above: <ComposerTray phase={trayPhase} trayRef={trayRef} dockRef={trayDockRef} onSettled={settleTray} />,
      dock: <TrayDock ref={trayDockRef} phase={trayPhase} contents={floatingTray} onOpen={peekTray} />,
      strip: null,
    }
    : { above: null, dock: null, strip: trayInside && <ComposerTrayStrip /> };

  // Keep the ref in sync so SSE events (via handleOpenPreviewFromStream) use the latest closure
  useLayoutEffect(() => {
    openPreviewRef.current = handleOpenPreview;
    browserUseRef.current = handleBrowserUse;
  });

  // A deliverable card names its own workspace only for a cross-workspace ref;
  // otherwise the file belongs to the thread's own workspace, which the card
  // has no way to know.
  const downloadKeyFor = useCallback(
    (path: string, targetWorkspaceId?: string) => cardDownloadKey(targetWorkspaceId ?? workspaceId, path),
    [workspaceId],
  );
  const handleDownloadFileFromChat = useCallback((path: string, targetWorkspaceId?: string) => {
    const wsId = targetWorkspaceId ?? workspaceId;
    if (!wsId) return;
    return trackPending(downloadKeyFor(path, targetWorkspaceId), async () => {
      try {
        // This thread's writes break ties between namesakes, and they only name
        // files in its own workspace, so a card pointing elsewhere resolves
        // without them.
        const writes = wsId === workspaceId ? getRecentWritePaths() : [];
        const target = await downloadTarget(
          path,
          (candidates, recentWrites) => resolveWorkspaceFile(wsId, candidates, recentWrites),
          writes,
        );
        // The lookup found namesakes and could not pick one, so there is no file
        // to save and a fetch of the reference as written would 404 in silence.
        // Open already asks which one the reader meant, so the click goes there.
        if (!target.placed) {
          handleOpenFileFromChat(path, targetWorkspaceId);
          return false;
        }
        await triggerFileDownload(wsId, target.path);
        return true;
      } catch (err: unknown) {
        console.error('[ChatView] Download failed:', err);
        // A card's Download is the whole interaction: nothing opens, nothing
        // navigates, and the browser shows no save. Without this the click is
        // indistinguishable from a dead button.
        toast({ description: t('filePanel:downloadFailed'), variant: 'destructive' });
        return false;
      }
    });
  }, [workspaceId, downloadKeyFor, getRecentWritePaths, handleOpenFileFromChat, t]);

  // The thread that handed this one its turn, reached the same way from the
  // back button and from the label over a hand-off in the transcript: Home's.
  // The workspace rides along when this thread was opened from that one;
  // otherwise the route looks it up.
  const fromThreadId = state?.fromThreadId;
  const fromWorkspaceId = state?.fromWorkspaceId;
  // Every exit to another thread marks itself intentional as it goes, and
  // asks the file panel about unsaved drafts first (`leaveToThread`).
  const goToThread = useCallback((targetThreadId: string, navState: LocationState) => {
    intentionalExitRef.current = true;
    navigate(`/chat/t/${targetThreadId}`, { state: navState });
  }, [navigate]);
  const leaveToThread = useCallback((targetThreadId: string, navState: LocationState) => {
    leaveFiles(() => goToThread(targetThreadId, navState));
  }, [leaveFiles, goToThread]);
  const openDispatcher = useCallback((dispatcherThreadId: string) => {
    leaveToThread(
      dispatcherThreadId,
      dispatcherThreadId === fromThreadId && fromWorkspaceId ? { workspaceId: fromWorkspaceId } : {},
    );
  }, [leaveToThread, fromThreadId, fromWorkspaceId]);

  // "Fork from here" and "Edit in new chat": a copy of this thread up to a
  // turn, opened as a new chat while this view is still the one in front. A
  // view kept mounted after the user moved on leaves the fork in the list.
  // The file panel is asked before the copy is made, so declining leaves no
  // fork behind, and asked again on the way out unless the user already
  // agreed to discard: a draft typed while the copy ran is asked about too.
  // A copy that waits on its dialog runs without the button's pending state,
  // which ends when the dialog opens.
  const forkDiscardAgreedRef = useRef(false);
  const openFork = useCallback((forkThreadId: string, navState: LocationState) => {
    if (!isActiveRef.current) return;
    if (forkDiscardAgreedRef.current) goToThread(forkThreadId, navState);
    else leaveToThread(forkThreadId, navState);
  }, [goToThread, leaveToThread]);
  const forkFromTurn = useForkThread({ threadId: currentThreadId, open: openFork });
  const handleForkFromTurn = useCallback(
    (turnIndex: number, cut: ThreadForkCut) => new Promise<void>((resolve) => {
      // The guard runs this at once when nothing was unsaved, and later only
      // once the user agreed in its dialog.
      let inline = true;
      let went = false;
      leaveFiles(() => {
        went = true;
        forkDiscardAgreedRef.current = !inline;
        void forkFromTurn(turnIndex, cut).finally(resolve);
      });
      inline = false;
      if (!went) resolve();
    }),
    [leaveFiles, forkFromTurn],
  );

  // The file cards' Publish, one dialog for the chat.
  const { onPublishFile, dialog: publishDialog } = usePublishFile(workspaceId || null);

  // Identity-stable action bundles for the memoized message tree
  // (chatView/useMessageActionBundles).
  const { messageActions, subagentMessageActions } = useMessageActionBundles({
    reportThreadId: currentThreadId && currentThreadId !== '__default__' ? currentThreadId : undefined,
    feedbackLoading,
    onOpenFile: handleOpenFileFromChat,
    onDownloadFile: handleDownloadFileFromChat,
    onPublishFile,
    // DALTON-HOOK(external-share-guard)/(team-sharing): the transcript's file
    // cards offer Publish to the workspace's own owner only — a colleague
    // (rw or ro), an external/unknown member, or a read-only thread gets no entry.
    publishAllowed:
      !teamReadOnly.isColleague
      && identity.canActAsOwner
      && !identity.isConfirming
      && !isThreadReadOnly,
    downloadKeyFor,
    onRevealFiles: revealFiles,
    onRevealMessage: revealMessage,
    onOpenSources: handleOpenSourcesFromChat,
    onToolCallDetailClick: handleToolCallDetailClick,
    onOpenChart: handleOpenChart,
    onOpenArtifactComment: handleOpenArtifactComment,
    onOpenSubagentTask: handleOpenSubagentTask,
    onOpenDispatcher: openDispatcher,
    onAnswerQuestion: handleAnswerQuestion,
    onSkipQuestion: handleSkipQuestion,
    onApproveCreateWorkspace: handleApproveCreateWorkspace,
    onRejectCreateWorkspace: handleRejectCreateWorkspace,
    onApproveStartQuestion: handleApproveStartQuestion,
    onRejectStartQuestion: handleRejectStartQuestion,
    onApprovePTCAgent: handleApprovePTCAgent,
    onRejectPTCAgent: handleRejectPTCAgent,
    onApproveSecretaryAction: handleApproveSecretaryAction,
    onRejectSecretaryAction: handleRejectSecretaryAction,
    // DALTON-HOOK(watchlist): wire personal watchlist confirmation handlers
    onApproveWatchlistAction: handleApproveWatchlistAction,
    onRejectWatchlistAction: handleRejectWatchlistAction,
    onResumeCreditPause: handleResumeCreditPause,
    onApproveToolCall: handleApproveToolCall,
    onRejectToolCall: handleRejectToolCall,
    onThumbUp: handleThumbUp,
    onThumbDown: handleThumbDown,
    onThumbDownFollowUp: handleThumbDownFollowUp,
    onWidgetSendPrompt: stableSendMessage,
    onEditMessage: stableEditMessage,
    onRegenerate: stableRegenerate,
    onRetry: stableRetry,
    onForkFromTurn: handleForkFromTurn,
    editDrafts,
    onSendMessage: handleSendMessage,
    chatInputRef,
    // DALTON-HOOK(workspace-sharing-settings): withhold mutation/interactive callbacks for colleagues
    // DALTON-HOOK(colleague-dispatch): also withhold mutation/interactive callbacks on foreign-owned threads
    readOnly: isThreadReadOnly,
  });


  // Open a file in the right panel from chat tool calls
  // --- Mobile back-button integration for panels ---






















  // Add context from FilePanel or message selection to ChatInput
  const handleAddContext = useCallback((ctx: ContextPayload) => {
    chatInputRef.current?.addContext(ctx);
  }, []);

  const msgAreaRef = useRef<HTMLDivElement>(null);
  // Collapse avatars when the messages column is too narrow to comfortably
  // accommodate them (mobile, side panels, etc.). 640px matches the visual
  // breakpoint where avatar gutters start crowding the message bubble.

  const scrollThreadToTop = useCallback(() => {
    if (activeAgentId === 'main') toTop();
    else subagentToTop();
  }, [activeAgentId, toTop, subagentToTop]);

  // Mobile: tap top bar to scroll chat to top
  const handleTopBarTap = useCallback((e: React.MouseEvent) => {
    if (!isMobile) return;
    if ((e.target as HTMLElement).closest('button, a')) return;
    scrollThreadToTop();
  }, [isMobile, scrollThreadToTop]);









  // Update URL when thread ID changes (e.g., when __default__ becomes actual thread ID)
  // Hidden views notify parent via onThreadResolved but skip URL navigate.
  useEffect(() => {
    if (currentThreadId && currentThreadId !== '__default__' && currentThreadId !== threadId && workspaceId) {
      // Notify parent so the cache key updates in-place (preserves instanceId)
      onThreadResolved?.(threadId, currentThreadId);
      if (isActive) {
        const activeTid = activeAgentIdRef.current !== 'main'
          ? taskIdFromAgentId(activeAgentIdRef.current) ?? activeAgentIdRef.current
          : null;
        const path = activeTid
          ? `/chat/t/${currentThreadId}/${activeTid}`
          : `/chat/t/${currentThreadId}`;
        navigate(path, { replace: true, state: { workspaceId } });
      }
      // Invalidate thread cache so navigation panel picks up the new thread
      queryClient.invalidateQueries({ queryKey: queryKeys.threads.byWorkspace(workspaceId) });
    }
  }, [currentThreadId, threadId, workspaceId, navigate, queryClient, isActive, onThreadResolved, activeAgentIdRef]);

  // Auto-send initial message from navigation state (e.g., from Dashboard)
  useEffect(() => {
    // Hidden views must not send initial messages (R7 — all views share useLocation)
    if (!isActive) return;
    // DALTON-HOOK(workspace-sharing-settings): never auto-send initialMessage or onboarding kickoff on read-only colleague workspaces
    // DALTON-HOOK(colleague-dispatch): allow auto-send when colleague starts a new thread on a web_access === 'rw' workspace
    if (isThreadReadOnly) return;
    // Only proceed if we have the required IDs
    if (!workspaceId || !threadId) {
      return;
    }

    // Handle regular message flow. The welcome flow's opening turn goes this
    // way too, and when it asks the agent where to start it has no message of
    // its own: a hidden opener, sent with an empty one.
    if ((location.state?.initialMessage || location.state?.hiddenOpener) && !initialMessageSentRef.current) {
      // Merge state.skills (names) into additionalContext as skill entries,
      // so hidden skills preloaded upstream (e.g. chart-annotation from
      // MarketView) stay active on the PTC side.
      const mergeSkills = (
        context: Record<string, unknown>[] | null | undefined,
        skills: unknown,
      ): Record<string, unknown>[] | null => {
        const base = Array.isArray(context) ? [...context] : [];
        if (Array.isArray(skills)) {
          for (const name of skills) {
            if (typeof name !== 'string' || !name) continue;
            if (base.some((c) => c?.type === 'skills' && c?.name === name)) continue;
            base.push({ type: 'skills', name });
          }
        }
        return base.length > 0 ? base : null;
      };

      // For new threads (__default__), send immediately without waiting for history
      // For existing threads, wait for history to finish loading
      // This ensures we don't send duplicate messages
      if (threadId !== '__default__' && (isLoadingHistory || isLoading)) return;
      initialMessageSentRef.current = true;
      // Capture state values before clearing (navigate may update location ref)
      const {
        initialMessage,
        additionalContext,
        attachmentMeta,
        model,
        reasoningEffort,
        fastMode,
        widgetSnapshots,
        chartSelections,
        chartViews,
        skills,
        hiddenOpener,
        threadTitle,
        handoff,
        sharedWithWorkspace: landingShared,
      } = location.state;
      const mergedContext = mergeSkills(additionalContext, skills);
      // Clear navigation state to prevent re-sending on re-renders
      navigate(location.pathname, { replace: true, state: {} });
      const send = () => {
        handleSendMessage(initialMessage ?? '', mergedContext, attachmentMeta || null, {
          model,
          reasoningEffort,
          fastMode,
          widgetSnapshots,
          chartSelections,
          chartViews,
          subagentsAllowed: subagentsToSend,
          ...tuningToSend,
          sharedWithWorkspace: landingShared,
          hiddenOpener,
          threadTitle,
        });
      };
      // The welcome flow's hand-off is sent at once: the view is mounted by
      // the time its effects run, and the hand-off into it holds the page
      // still until the turn is drawn. Anything else waits a small delay to
      // ensure the component is fully mounted.
      if (handoff) send();
      else setTimeout(send, 100);
    }
  }, [location.state, workspaceId, threadId, isLoading, isLoadingHistory, handleSendMessage, navigate, location.pathname, isActive, subagentsToSend, tuningToSend, isThreadReadOnly]); // DALTON-HOOK(colleague-dispatch): never auto-send on read-only threads

  // The welcome's thread is never an empty chat: until its opening turn goes
  // out it draws nothing where the prompt to type would be. The route state
  // carrying the turn is cleared as it goes, so one that could not go out
  // leaves the prompt. Drawn, the turn tells the hand-off it can land.
  const awaitingKickoff = kickoffView && messages.length === 0 && state?.handoff === true;
  const kickoffShown = kickoffView && messages.length > 0;
  useLayoutEffect(() => {
    if (kickoffShown) markHandoffReady();
  }, [kickoffShown]);

  // Re-seed the composer from navigation state when there's no
  // initialMessage (the auto-send branch above already consumes them inline).
  // Used by the ContextOverflowPill click handoff: dashboard → /chat with
  // queued widget cards but no auto-send. And by Plugins → Subagents' "Create
  // with the agent", which hands it a sentence to finish rather than send.
  // And by the Chief of Staff's starter questions.
  // Once per navigation, not once per view: an unsent __default__ view is
  // kept while the user goes back to the gallery, and the next starter lands
  // on the same one. Only the view on screen takes it, since every cached
  // view reads the same location.
  const reseededKeyRef = useRef<string | null>(null);
  useEffect(() => {
    // DALTON-HOOK(team-sharing): do not reseed composer in read-only mode
    // DALTON-HOOK(colleague-dispatch): also skip reseeding on foreign-owned threads
    if (isThreadReadOnly) return;
    if (!isActive || reseededKeyRef.current === location.key) return;
    const navState = location.state as LocationState | null;
    const snaps = navState?.widgetSnapshots;
    const draft = navState?.draftMessage;
    if ((!snaps?.length && !draft) || navState?.initialMessage) return;
    reseededKeyRef.current = location.key;
    snaps?.forEach((s) => chatInputRef.current?.addWidgetSnapshot(s));
    if (draft && navState?.draftAppend) chatInputRef.current?.giveBack(draft);
    else if (draft) chatInputRef.current?.setValue(draft, navState?.draftCommands);
    navigate(location.pathname, { replace: true, state: { ...navState, widgetSnapshots: undefined, draftMessage: undefined, draftCommands: undefined, draftAppend: undefined } });
  }, [isActive, location.key, location.state, location.pathname, navigate, isThreadReadOnly]); // DALTON-HOOK(colleague-dispatch): re-check when read-only changes





  // Screen-reader announcements for tool-call completions (polite live region).
  const recentlyCompletedAnnouncement = useToolCallAnnouncer(messages, chatRootDir);


  // When this view becomes active (thread switch or new thread):
  // 1. Close the nav drawer: a cached view was left however it stood, and the
  //    drawer covers the thread this view is here to show
  // 2. Scroll to bottom — while hidden (display:none) auto-scroll is a no-op
  // A run that started or ended while it was hidden is useForeignRunCatchUp's.
  const prevIsActiveRef = useRef(false);
  useEffect(() => {
    if (isActive && !prevIsActiveRef.current) {
      handleNavMinimize();

      const tidNow = currentThreadId || threadId;
      requestAnimationFrame(() => {
        // First-mount restore is owned by the entry-restore effect. Here we only
        // catch up a cached re-entry to the bottom if the user left it at bottom;
        // otherwise the DOM scroll position preserved under display:none stands.
        if (restoredForThreadRef.current === tidNow && isNearBottomRef.current) {
          pinToBottom('auto');
        }
      });
    }
    prevIsActiveRef.current = isActive;
  }, [isActive, getScrollContainer, currentThreadId, threadId, pinToBottom, handleNavMinimize, isNearBottomRef, restoredForThreadRef]);

  const feedThreadId = currentThreadId || threadId;
  useForeignRunCatchUp({
    threadId: feedThreadId,
    isActive,
    busy: isLoading || isLoadingHistory || isLoadingThread,
    awaitingReportBack,
    isOwnRun,
    catchUp: reconnectIfStaleRun,
  });

  const publishComposerHeight = useCallback((el: HTMLDivElement | null) => {
    const column = el?.parentElement;
    if (!el || !column) return;
    // A cached thread view is display:none and measures 0. Keep the last real
    // height so it comes back with the right padding: publishing 0 there let
    // the reactivated view pin to the bottom with the last message under the
    // composer, then jump once the real height landed.
    const publish = () => {
      if (el.offsetHeight) column.style.setProperty('--composer-h', `${el.offsetHeight}px`);
    };
    publish();
    const ro = new ResizeObserver(publish);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The minimap's turn rail: desktop only, on the main thread, with no panel
  // beside the chat. The transcript keeps its lines clear of it.
  const minimapShown = isActive && !isMobile && !rightPanelType && activeAgentId === 'main';

  // About the thread and its computer rather than the next send, so they sit
  // under the last reply and stay there when the reader scrolls up, instead of
  // floating over the history with the composer. At a file's foot, where the
  // transcript is out of view, they come along above the composer. Keyed, so
  // the transcript can close a row that a condition here takes away.
  const threadNotices: ThreadNotice[] = [];
  // A notice is not about the running turn, so it need not wait for it to end.
  if (messageError && (!isLoading || (typeof messageError === 'object' && messageError.kind === 'notice'))) {
    threadNotices.push({
      key: 'error',
      node: (
        <ErrorBanner
          error={messageError}
          reportThreadId={currentThreadId && currentThreadId !== '__default__' ? currentThreadId : undefined}
        />
      ),
    });
  }
  if (isHome) threadNotices.push({ key: 'home', node: <HomePlacementNotice /> });
  if (workspaceRecord?.computer_id && capabilities.restoreOffer) {
    threadNotices.push({
      key: 'restore',
      node: <ChatRestoreOffer workspaceId={workspaceId} computerId={workspaceRecord.computer_id} listed={!filesLoading} />,
    });
  }
  if (workspaceRecord?.computer_id) {
    threadNotices.push({ key: 'disk', node: <ChatDiskWarning computerId={workspaceRecord.computer_id} /> });
  }

  // Early return if workspaceId or threadId is missing
  if (!workspaceId || !threadId) {
    return (
      <div className="flex items-center justify-center h-full" style={{ backgroundColor: 'var(--color-bg-page)' }}>
        <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('chat.missingWorkspaceOrThread')}
        </p>
      </div>
    );
  }

  return (
    <ChartComposerContext value={panelChart.composer}>
    <ThreadStatusContext value={threadStatus}>
    {/* The tickers the tray follows, priced live for the tray and the panel. */}
    <ThreadQuotesProvider contents={trayContents} active={isActive}>
    <WorkspaceProvider workspaceId={workspaceId} downloadFile={null} folders={chatFolders}>
    {/* A link in the transcript that changes the route unmounts the file
        panel beside it, so it leaves through the panel's guard like the
        panel's own exits do. */}
    <RouteLeaveGuardContext value={leaveFiles}>
    <LinkActionsContext value={linkActions}>
    <ThreadCommandsContext value={panelThreadId ?? null}>
    <ChatCommentSendsProvider value={commentSends}>
    <ComposerDockContext value={composerDock.context}>
    {/* `h-full`, never `h-screen`: this fills the shell's content column, which
        is the viewport only when nothing else is in it. Pinning it to 100vh
        pushes it out of its own box the moment anything is (the offline
        banner). It carried a 10px entrance slide too, which for a box that
        exactly fills its scroll parent hung 10px past the bottom for the length
        of the animation and flashed a scrollbar on every mount; the route-level
        fade in Main.tsx already covers the transition. */}
    <div
      ref={containerRef}
      data-handoff-scope={kickoffView ? '' : undefined}
      className="relative flex w-full overflow-hidden h-full"
      style={{
        backgroundColor: 'var(--color-bg-page)',
      }}
    >
      {/* Polite aria-live region for screen-reader announcements when tool
          calls reach a terminal state. Visually hidden via sr-only. */}
      <div aria-live="polite" aria-atomic="false" className="sr-only">
        {recentlyCompletedAnnouncement}
      </div>
      {/* Left Side: Topbar + Sidebar + Chat Window. Inert under an expanded
          panel, so focus and a screen reader stay on what is shown, and
          isolated, so the chat's own raised layers (the jump-to-latest pill)
          stay under it rather than stacking against it. */}
      <div className={cn('flex flex-col flex-1 min-w-0', panelExpanded && 'isolate')} inert={panelExpanded}>
        {/* Top bar */}
        {/* `drag` makes this the window's drag band inside the desktop shell.
            It already spans the titlebar and is mostly empty, so the shell gets
            a full-width band at no layout cost; the buttons in it opt out
            through the global no-drag rule. */}
        {/* DALTON-HOOK(mobile-web): tighten horizontal padding/gaps on phones while keeping adjacent 44px tap-targets non-overlapping */}
        <div data-chrome="drag" data-handoff="header" className={cn('flex items-center justify-between py-2 border-b min-w-0 shrink-0', isMobile ? 'px-2 gap-2' : 'px-4')} style={{ borderColor: 'var(--color-border-muted)', cursor: isMobile ? 'pointer' : undefined }} onClick={handleTopBarTap}>
          <div className={cn('flex items-center min-w-0', isMobile ? 'gap-2 flex-1' : 'gap-4 shrink')}>
            {/* DALTON-HOOK(mobile-web): 12px gap between 32px Back & Drawer buttons keeps their 44px tap-targets edge-to-edge without overlap */}
            <div className={cn('flex items-center shrink-0', isMobile && 'gap-3')}>
              <button
                onClick={() => {
                  if (activeAgentId !== 'main') {
                    // Workflow-owned children step up to their run's view;
                    // everything else returns to the main chat.
                    handleSelectAgent(activeAgent?.ownerTaskId ?? 'main');
                  } else if (state?.fromThreadId) {
                    // Back to the thread that dispatched this PTC thread.
                    openDispatcher(state.fromThreadId);
                  } else {
                    intentionalExitRef.current = true;
                    onBack();
                  }
                }}
                className={cn('rounded-md transition-colors shrink-0', isMobile ? 'p-1.5 tap-target' : 'p-2')}
                style={{ color: 'var(--color-text-primary)' }}
                title={
                  activeAgentId !== 'main'
                    ? activeAgent?.ownerTaskId
                      ? t('chat.backToWorkflow', 'Back to workflow')
                      : t('chat.backToMain', 'Back to main')
                    : state?.fromThreadId
                      ? t('agents.backToChiefOfStaff')
                      : t('workspace.backToThreads')
                }
                onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'; }}
                onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = ''; }}
              >
                <ArrowLeft className="h-5 w-5" />
              </button>
              {isMobile && (
                <button
                  onClick={() => setNavPanelVisible(true)}
                  className="p-1.5 tap-target rounded-md transition-colors shrink-0"
                  style={{ color: 'var(--color-text-primary)' }}
                  title={t('sidebar.menu')}
                >
                  <Menu className="h-5 w-5" />
                </button>
              )}
            </div>
            <h1 className="title-font text-[length:var(--cs-text-heading,1rem)] leading-6 font-semibold tracking-[var(--cs-title-track,-0.01em)] whitespace-nowrap truncate min-w-0" style={{ color: 'var(--color-text-primary)' }}>
              {/* The bar's tap is a pointer's shortcut, and a bar holding buttons
                  cannot be one. The title is the same action as a real control,
                  named by the words on it, with what it does as its description. */}
              {isMobile ? (
                <button type="button" className="block max-w-full truncate text-left" title={t('chat.scrollToTop')} onClick={scrollThreadToTop}>
                  {workspaceName || t('thread.workspace')}
                </button>
              ) : (workspaceName || t('thread.workspace'))}
            </h1>
            {/* DALTON-HOOK(workspace-sharing-settings): display colleague access banner in top bar */}
            {/* DALTON-HOOK(mobile-web): move TeamReadOnlyBanner below top bar on mobile */}
            {teamReadOnly.isColleague && !isMobile && (
              <TeamReadOnlyBanner ownerName={teamReadOnly.ownerName} access={teamReadOnly.access} />
            )}
            {activeAgentId === 'main' && (
              <ForkedFromLink threadId={currentThreadId} onOpen={leaveToThread} />
            )}
            {isLoadingHistory ? (
              <span className="text-xs whitespace-nowrap" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('chat.loadingHistory')}
              </span>
            ) : null}
          </div>

          <div className={cn('flex items-center', isMobile ? 'gap-2 shrink-0' : 'gap-2')}>
            {/* DALTON-HOOK(coverage-tracking): company workspace auto-tracking toggle in thread top bar */}
            {/* DALTON-HOOK(workspace-sharing-settings): company workspace sharing settings modal in thread top bar */}
            {/* DALTON-HOOK(mobile-web): desktop uses a Fragment (zero empty wrapper gap); phones group compact controls with gap-2.5 empty:hidden and gate sharing on confirmed colleague_sharing or conversation_archive */}
            {!isHome && workspaceId && (
              isMobile ? (
                <div className="flex items-center shrink-0 gap-2.5 empty:hidden">
                  <WorkspaceTrackingToggle
                    workspaceId={workspaceId}
                    readOnly={teamReadOnly.isColleague}
                    compact
                  />
                  {workspaceSharingEntryReady && (
                    <WorkspaceSharingModal
                      workspaceId={workspaceId}
                      readOnly={teamReadOnly.isColleague}
                      compact
                    />
                  )}
                </div>
              ) : (
                <>
                  <WorkspaceTrackingToggle
                    workspaceId={workspaceId}
                    readOnly={teamReadOnly.isColleague}
                  />
                  {workspaceSharingEntryReady && (
                    <WorkspaceSharingModal
                      workspaceId={workspaceId}
                      readOnly={teamReadOnly.isColleague}
                    />
                  )}
                </>
              )
            )}
            {/* A phone has no sidebar search row and, mostly, no ⌘K. */}
            {isMobile && <PaletteSearchButton />}
            {/* DALTON-HOOK(workspace-sharing-settings): hide ShareButton for colleagues */}
            {/* DALTON-HOOK(colleague-dispatch): hide ShareButton on read-only foreign threads */}
            {/* DALTON-HOOK(external-share-guard): ShareButton reads unified caller identity directly */}
            {currentThreadId && currentThreadId !== '__default__' && !isThreadReadOnly && !teamReadOnly.isColleague && (
              <ShareButton threadId={currentThreadId} initialIsShared={threadIsShared} workspaceId={workspaceId} />
            )}
            {/* On desktop the open panel carries this toggle in the same
                corner, so it is sized and placed as the panel's own buttons
                are and the icon does not move when the panel opens. */}
            {(isMobile || rightPanelType !== 'file') && (
              <button
                type="button"
                data-panel-toggle="header"
                onClick={(event) => { handleToggleFilePanel(); if (!isMobile) handOffToggleFocus(event, 'panel'); }}
                onPointerEnter={() => { void loadFilePanel(); }}
                onFocus={() => { void loadFilePanel(); }}
                aria-pressed={rightPanelType === 'file'}
                className={cn('transition-colors hover:bg-(--color-border-muted)', isMobile ? 'p-1.5 tap-target rounded-md' : 'p-1.5 -mr-2 rounded-[4px]')}
                style={{ color: 'var(--color-text-primary)' }}
                title={t('chat.workspaceFiles')}
                aria-label={t('chat.workspaceFiles')}
              >
                <PanelRight className={isMobile ? 'h-5 w-5' : 'h-4 w-4'} />
              </button>
            )}
          </div>
        </div>

        {/* DALTON-HOOK(mobile-web): dedicated sub-row for colleague access banner on phones */}
        {isMobile && teamReadOnly.isColleague && (
          <div className="px-3 py-1.5 border-b min-w-0 shrink-0" style={{ borderColor: 'var(--color-border-muted)' }}>
            <TeamReadOnlyBanner ownerName={teamReadOnly.ownerName} access={teamReadOnly.access} compact />
          </div>
        )}

        {privacy && activeAgentId === 'main' && <ThreadPrivacyRow privacy={privacy} isMobile={isMobile} />}
        {shareDialog}

        {/* Content area: Chat Window (+ nav drawer on mobile — desktop nav
            lives in the app-shell AppSidebar now) */}
        <div className="flex-1 flex overflow-hidden" style={{ position: 'relative', containerType: 'inline-size' }}>
          {isMobile && (
            <MobileNavDrawer
              visible={navPanelVisible}
              onMinimize={handleNavMinimize}
              isActive={isActive}
              workspaceId={workspaceId}
              threadId={sidebarAgentsKey}
              agents={navAgentsSlice}
              workspaceName={workspaceName}
            />
          )}

          {/* Chat Window — full width; the mobile nav drawer overlays (never pushes) */}
          <div className="flex-1 flex flex-col overflow-hidden min-w-0 relative">
            {/* Messages Area - Fixed height, scrollable */}
            {/* Subscribe inline subagent cards directly to live telemetry. The
                resolver identity changes whenever a card or the subagent
                history does, not per main-agent token, and only context
                consumers re-render: MessageBubble / MessageContentSegments
                stay React.memo'd. */}
            <SubagentTelemetryContext value={subagentTelemetry}>
            <WorkflowRunContext value={resolveWorkflowRun}>
            <SharedThreadContext value={sharedThreadView}>
            <TranscriptDisplayContext value={transcriptDisplay}>
            <div
              ref={msgAreaRef}
              className="flex-1 overflow-hidden"
              style={{
                minHeight: 0,
                height: 0, // Force flex-1 to work properly
                position: 'relative',
              }}
            >
              {/* Message selection quote (#676) replaces Dalton's selection tooltip. */}
              {/* DALTON-HOOK(workspace-sharing-settings): no selecting text to quote for colleagues on a read-only thread */}
              {/* DALTON-HOOK(colleague-dispatch): no selecting text to quote on read-only foreign threads */}
              <MessageSelectionQuote
                key={`${threadId}:${activeAgentId}:${isActive}`}
                surfaceRef={msgAreaRef}
                active={!!isActive && activeAgentId === 'main' && !isThreadReadOnly}
                agentName={isHome ? t('agents.chiefOfStaff') : t('agents.analyst')}
                onAddContext={handleAddContext}
              />
              {activeAgentId === 'main' ? (
                <ScrollArea ref={scrollAreaRef} className={`h-full w-full${!isMobile && !rightPanelType ? ' chat-scroll-hide-scrollbar' : ''}`}>
                  {/* The floor a pinned send holds open (chatView/turnPin); a transition would
                      hand the pin last frame's floor. A thread opened mid-way sits at the
                      bottom under a skeleton, and back on top while a pin holds room. */}
                  <div
                    ref={transcriptFloorRef}
                    data-transcript-floor=""
                    className={cn(
                      'group/floor transition-none',
                      olderHistory.aligned && 'flex min-h-full flex-col justify-end data-[pinned]:justify-start',
                    )}
                  >
                    {olderHistory.aligned && (olderHistory.status === 'idle' || olderHistory.status === 'loading') && (
                      <OlderHistorySkeleton isMobile={isMobile} />
                    )}
                    <div className={`${isMobile ? 'px-3 pt-3' : 'px-6 pt-4'} ${TRANSCRIPT_BOTTOM_PAD} flex justify-center`}>
                      {/* clip, not hidden: hidden makes overflow-y auto, and a sent bubble's
                          rise would overflow this column and flash a scrollbar. */}
                      {/* Beside the minimap the column keeps 48px off each
                          edge, the rail's 12px inset and 32px width and a
                          4px gap, so no line runs under its ticks; kept even
                          on both sides, it stays centred over the composer.
                          Only a column narrow enough to reach the rail
                          gives anything up. */}
                      <div
                        data-transcript-content=""
                        className="w-full max-w-3xl overflow-x-clip"
                        style={minimapShown ? BESIDE_MINIMAP : undefined}
                      >
                        <OlderHistoryRow
                          getRoot={getMainScrollRoot}
                          status={olderHistory.status}
                          canLoad={isActive && entryRestored && olderHistory.status === 'idle'}
                          onLoad={loadOlderPage}
                        />
                        <MessageActionsProvider actions={messageActions}>
                          {/* Above the list's chunk boundary: one batched
                              dispatch-liveness query for every PTC card in the
                              list, which a streamed frame does not re-render. */}
                          <DispatchStatusProvider>
                            <LiveMessageList
                              store={liveMessages}
                              isLoading={isLoading}
                              plans={plansUnderMessages}
                              isLoadingHistory={isLoadingHistory}
                              starting={awaitingKickoff}
                              feedbackByTurn={feedbackByTurn}
                              dispatcherContext={dispatcherContext}
                              ranHere={mainRanHere}
                              // DALTON-HOOK(workspace-sharing-settings): render message cards in read-only mode for colleagues while preserving file cards
                              // DALTON-HOOK(colleague-dispatch): also render message cards read-only on foreign-owned threads
                              readOnly={isThreadReadOnly}
                              allowFiles={isThreadReadOnly}
                              holding={heldBeforeWork}
                              holdLine={compactionAbove && <CompactingLine {...compactionAbove} />}
                            />
                          </DispatchStatusProvider>
                        </MessageActionsProvider>
                        <TranscriptStatus
                          modelStatus={modelStatus}
                          isLoading={isLoading}
                          retired={threadModel.retired}
                          turnStatus={displayTurnStatus}
                          model={threadModel.model}
                          compaction={compactionAbove ? null : compaction}
                          queuedSend={queuedSend}
                          threadId={feedThreadId}
                          active={isActive}
                          notices={composerDock.docked ? [] : threadNotices}
                          noticesDocked={composerDock.docked}
                        />
                        {/* Under a finished reply (hosted only, in one thread, on screen). */}
                        {currentThreadId && currentThreadId !== '__default__' && (
                          <ChatAppsPromptCard
                            threadId={currentThreadId}
                            active={isActive}
                            idle={!isLoading && !isStreaming}
                            replied={messages.some((m) => m.role === 'assistant')}
                          />
                        )}
                      </div>
                    </div>
                  </div>
                </ScrollArea>
              ) : activeAgent && activeAgent.type === WORKFLOW_TASK_TYPE ? (
                // Workflow run detail — a run has no transcript of its own;
                // its progress console replaces the generic subagent layout.
                <ScrollArea ref={subagentScrollRoot} className="h-full w-full">
                  <div className={`${isMobile ? 'px-3 pt-3' : 'px-6 pt-4'} ${TRANSCRIPT_BOTTOM_PAD} flex justify-center`}>
                    <div className="w-full max-w-3xl">
                      <WorkflowRunDetail
                        // Keyed per run: the detail owns local stop state, and
                        // an unkeyed switch between two runs would carry it over.
                        key={activeAgent.id}
                        agent={activeAgent}
                        onOpenChild={handleOpenSubagentTask}
                        resolveChildTelemetry={resolveSubagentTelemetry}
                        onStop={
                          // DALTON-HOOK(workspace-sharing-settings): disable workflow/subagent stop for colleagues
                          // DALTON-HOOK(colleague-dispatch): also disable stop on foreign-owned threads
                          !isThreadReadOnly && currentThreadId && currentThreadId !== '__default__'
                            ? () => cancelSubagentTask(currentThreadId, taskIdFromAgentId(activeAgent.id) ?? activeAgent.id)
                            : undefined
                        }
                      />
                    </div>
                  </div>
                </ScrollArea>
              ) : activeAgent ? (
                <ScrollArea ref={subagentScrollRoot} className="h-full w-full">
                  <div className={`${isMobile ? 'px-3 pt-3' : 'px-6 pt-4'} ${TRANSCRIPT_BOTTOM_PAD} flex justify-center`}>
                    <div className="w-full max-w-3xl space-y-2.5">
                      {/* Task description as header */}
                      {activeAgent.description && (
                        <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.8125rem', fontWeight: 500 }}>
                          {activeAgent.description}
                        </div>
                      )}
                      {/* The prompt as a user bubble, the one the transcript draws.
                          Only until the transcript's own epoch-opening user bubble
                          arrives: new task streams open with the run's instruction as
                          a user_message, so rendering both would duplicate it. Legacy
                          tasks (no opener) keep the static bubble. */}
                      {activeAgent.prompt && activeAgent.messages?.[0]?.role !== 'user' && (
                        <div className="flex justify-end">
                          <UserBubble provenance={MAIN_AGENT_PROVENANCE} isMobile={isMobile} className="max-w-[80%]">
                            <Markdown
                              variant="chat"
                              content={normalizeSubagentText(activeAgent.prompt)}
                              className="text-sm leading-relaxed"
                            />
                          </UserBubble>
                        </div>
                      )}
                      {/* Messages — reuse MessageList */}
                      {(activeAgent.messages?.length ?? 0) > 0 && (
                        <div style={{ borderTop: '0.5px solid var(--color-border-muted)', paddingTop: '8px' }}>
                          <MessageActionsProvider actions={subagentMessageActions}>
                            {/* Keyed per agent: which folds are open is one
                                transcript's state, and every subagent numbers
                                its turns from 0. */}
                            <DispatchStatusProvider key={activeAgentId}>
                              <MessageList
                                messages={activeAgent.messages as MessageRecord[]}
                                isSubagentView={true}
                                isLoading={subagentTurnLive}
                                // DALTON-HOOK(workspace-sharing-settings): render subagent message cards in read-only mode for colleagues while preserving file cards
                                // DALTON-HOOK(colleague-dispatch): also render subagent message cards read-only on foreign-owned threads
                                readOnly={isThreadReadOnly}
                                allowFiles={isThreadReadOnly}
                              />
                            </DispatchStatusProvider>
                          </MessageActionsProvider>
                        </div>
                      )}
                    </div>
                  </div>
                </ScrollArea>
              ) : (
                // Active agent not found (may have been removed) - fallback
                <div className="flex items-center justify-center h-full">
                  <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('chat.agentNotFound')}
                  </p>
                </div>
              )}
              {/* Minimap TOC — desktop only, when no right panel open */}
              {minimapShown && (
                <ChatMinimap
                  store={liveMessages}
                  scrollAreaRef={scrollAreaRef}
                  turnInFlight={isLoading}
                  pinToMessage={pinToMessage}
                  pinTargetRef={pinTargetRef}
                />
              )}
              {/* Jump-to-latest pill — coexists with the minimap (centered vs
                  right edge): the pill is the quick way down + new-message
                  count, the minimap is navigation. */}
              {activeAgentId === 'main' && (
                <JumpToLatestPill
                  visible={jumpPill.visible}
                  hasNew={jumpPill.hasNew}
                  newCount={jumpPill.newCount}
                  onJump={() => pinToBottom('smooth')}
                />
              )}
            </div>
            </TranscriptDisplayContext>
            </SharedThreadContext>
            </WorkflowRunContext>
            </SubagentTelemetryContext>

            {/* Input Area */}
            {/* Floats over the message list, which runs the full column height, so
                the padding, corners and gaps around the composer are transparent
                and message text shows through. The wrapper's height is published
                as --composer-h for the list's bottom padding and the jump pill. */}
            <div
              ref={publishComposerHeight}
              // DALTON-HOOK(mobile-web): respect iPhone Home Indicator safe-area inset on mobile floating composer
              className={`absolute inset-x-0 bottom-0 z-10 pointer-events-none ${isMobile ? 'px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]' : 'p-4'} flex justify-center`}
            >
              {/* sibling-space-y, not space-y: SelectionChips' own margin would
                  otherwise win. */}
              {/* Re-declares --composer-h for the same reason the transcript
                  content does (TRANSCRIPT_BOTTOM_PAD): nothing in the stack
                  reads it. */}
              {/* The stack moves to the foot of a file tab while the chat is
                  out of view (chatView/useComposerDock), as the same elements. */}
              {/* The rows here that draw a surface (the thread's notices, at a
                  file's foot) take the composer's corner, 16 unless
                  the card style says otherwise (--cs-composer-*,
                  styles/cardStyle.css), and set their text in where Soft's
                  rounder corner needs it. The same rows elsewhere keep their own. */}
              <DockableComposer home={composerDock.home} host={composerDock.host}>
              <div className="w-full max-w-3xl sibling-space-y-3 relative pointer-events-auto [--composer-h:initial] [--stack-row-radius:var(--cs-composer-stack-radius,16px)] [--stack-row-pad:var(--cs-composer-row-pad)]">
                {/* DALTON-HOOK(workspace-sharing-settings): replace writable composer with disabled colleague composer */}
                {/* DALTON-HOOK(colleague-dispatch): also show disabled composer on foreign-owned threads or revoked workspaces */}
                {isThreadReadOnly ? (
                  <TeamReadOnlyComposer
                    ownerName={teamReadOnly.ownerName}
                    access={teamReadOnly.access}
                    colleagueDispatchEnabled={teamReadOnly.colleagueDispatchEnabled}
                    dispatchedByName={dispatchedByName}
                    revoked={teamReadOnly.isThreadOnly}
                  />
                ) : activeAgentId === 'main' ? (
                  <>
                    {/* Only rows with a surface of their own sit here: the
                        transcript scrolls beneath this stack, and a bare line
                        of text over it reads as part of whatever is under it.
                        The thread's status lines are under the last reply
                        instead (data-transcript-status), and what it waits on
                        is chips in the tray. */}
                    {tray.above}
                    {/* Each a row of the stack; one that renders nothing leaves
                        no element, so it takes no gap. */}
                    {composerDock.docked && threadNotices.map(({ key, node }) => <React.Fragment key={key}>{node}</React.Fragment>)}
                    <SelectionChips chips={panelChart.chips} />
                    {/* At a file's foot the transcript is out of view, so the
                        turn's progress comes along with the composer. */}
                    {composerDock.docked && <DockTurnStatus running={isLoading} messages={messages} />}
                    {/* One row: the drawer is a tab tucked under the composer's top
                        edge, so no status row may come between them, and the
                        parent's sibling gap must not either. */}
                    {othersTurn && (
                      <RuleRow
                        icon={Users}
                        title={othersTurnWords(t, othersTurn.name, othersTurn.waiting)}
                      />
                    )}
                    <div data-handoff="composer">
                      <ChatInput
                        ref={chatInputRef}
                        // DALTON-HOOK(composer-drafts): persist draft per thread or per workspace new chat
                        draftKey={panelThreadId ? `thread:${panelThreadId}` : (workspaceId ? `ws:${workspaceId}:new` : undefined)}
                        // DALTON-HOOK(automation-effort): scope explicit retry effort picks to the active failed reply
                        retryTargetId={retryTargetId}
                        onSend={handleSendWithAttachments}
                        {...composerDeck}
                        disabled={isLoadingHistory || !workspaceId || !!pendingInterrupt}
                        sendHeld={!!othersTurn}
                        onStop={handleStopButton}
                        isLoading={isLoading}
                        isCompacting={!!isCompacting}
                        files={mentionFiles}
                        tokenUsage={tokenUsage}
                        tokenUsagePending={isLoadingHistory}
                        threadId={panelThreadId}
                        onAction={handleAction}
                        model={threadModel.model}
                        sendModel={threadModel.sendModel}
                        // DALTON-HOOK(antigravity-series): pass sendModelSeq so real ChatInput.getModelOptions() carries modelPickSeq
                        sendModelSeq={threadModel.sendModelSeq}
                        // DALTON-HOOK(antigravity-series): clear local effort alongside Antigravity model switch
                        onPickModel={handlePickThreadModel}
                        threadModels={threadModels}
                        subagentsAllowed={threadSubagents.allowed}
                        onToggleSubagents={threadSubagents.setAllowed}
                        tuning={threadTuning.tuning}
                        onPickReasoningEffort={threadTuning.setReasoningEffort}
                        onPickFastMode={threadTuning.setFastMode}
                        selectedWorkspaceId={workspaceId}
                        inHome={isHome}
                        // DALTON-HOOK(chat-attachment-count): include panel chart captures in composer attachment cap
                        externalAttachmentCount={panelChartImageCount}
                        header={tray.strip}
                        toolbarStatus={(privacyChip || tray.dock) && <>{privacyChip}{tray.dock}</>}
                        notices={composerNotices}
                      />
                    </div>
                    {/* Last in the stack: the parent's sibling gap gives every
                        row after the first a top margin, so one mounted ahead
                        of the others would shift them by it. */}
                    {isReconnecting && <ReconnectingChip />}
                  </>
                ) : activeAgent && activeAgent.type !== WORKFLOW_TASK_TYPE ? (
                  // Workflow runs are script-driven and take no steering input —
                  // the run detail above is their whole surface.
                  <SubagentStatusBar agent={activeAgent} onSendInstruction={handleSubagentInstruction} />
                ) : null}
              </div>
              </DockableComposer>
            </div>
          </div>
        </div>
      </div>

      {/* Mobile detail bottom sheet — always rendered so exit animation works */}
      {isMobile && (
        <MobileBottomSheet
          open={rightPanelType === 'detail' && !!detailToolCall}
          onClose={handleCloseDetailPanel}
          sizing="fixed"
          style={{ paddingBottom: 'calc(var(--bottom-tab-height, 0px) + 16px)' }}
        >
          <Suspense fallback={null}>
            <DetailPanel
              toolCallProcess={detailToolCall}
              onOpenFile={handleOpenFileFromChat}
              onOpenSubagentTask={handleOpenSubagentTask}
            />
          </Suspense>
        </MobileBottomSheet>
      )}

      {/* Mobile preview bottom sheet. Desktop shows a running app as a tab in
          the file panel instead; the sheet has no tab strip to land in. */}
      {isMobile && (
        <MobileBottomSheet
          open={rightPanelType === 'preview' && !!previewData}
          onClose={handleClosePreview}
          sizing="fixed"
          height="75vh"
          className="px-0! overflow-hidden!"
        >
          <Suspense fallback={null}>
            <PreviewViewer
              url={previewData?.url ?? ''}
              port={previewData?.port ?? 0}
              title={previewData?.title}
              path={previewData?.path}
              workspaceId={previewData?.workspaceId}
              loading={previewData?.loading}
              error={previewData?.error}
              onClose={handleClosePreview}
              onRefresh={handleRefreshPreview}
              reloadToken={previewData?.reloadToken}
            />
          </Suspense>
        </MobileBottomSheet>
      )}

      {/* Right Side: File panel (mobile overlay) or split panel (desktop) */}
      {isMobile ? (
        /* Mobile: no AnimatePresence — avoids exit animation restart when React Router
           re-renders mid-exit (popstate triggers RR location change during framer-motion
           exit, causing the panel to briefly re-appear and slide out again).
           Entry animation + drag-to-dismiss still work via motion.div. */
        rightPanelType === 'file' && (
          <motion.div
            key="file"
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            transition={TWEEN.enter}
            onAnimationComplete={onSheetLanded}
            // DALTON-HOOK(mobile-web): disable content-area horizontal drag-to-close so wide tables, PDFs, HTML reports and browser views pan freely; close via top back button or browser back
            className="flex overflow-hidden mobile-panel-overlay"
            style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 30, backgroundColor: 'var(--color-bg-page)' }}
          >
            <div className="shrink-0 h-full" style={{ width: '100%' }}>
              <Suspense fallback={null}>
                <WorkspaceProvider workspaceId={panelWorkspaceId} downloadFile={null} folders={panelFolders}>
                <FilePanel
                  workspaceId={panelWorkspaceId}
                  threadId={panelThreadId}
                  chatKey={panelChatKey}
                  isActive={isActive}
                  onClose={() => { setRightPanelType(null); popPanelHistory(); }}
                  onLeaveGuardChange={handleFilesLeaveGuardChange}
                  onActiveTabKindChange={handleActiveTabKindChange}
                  target={panelTarget}
                  onTargetHandled={handleTargetHandled}
                  onTargetMemoryHandled={handleTargetMemoryHandled}
                  onTargetMemoHandled={handleTargetMemoHandled}
                  onOpenInMarketView={handleOpenInMarketView}
                  onOpenSubagentTask={openSubagentTaskFromPanel}
                  transcript={transcript}
                  onOpenFile={handleOpenFileFromChat}
                  getRecentWritePaths={getRecentWritePaths}
                  files={workspaceFiles}
                  filesLoading={filesLoading}
                  filesError={filesError}
                  onRefreshFiles={refreshFiles}
                  onAddContext={!isThreadReadOnly && activeAgentId === 'main' ? handleAddContext : null} // DALTON-HOOK(workspace-sharing-settings)
                  // DALTON-HOOK(team-sharing): no comment layer/docked comment box on a read-only thread
                  threadReadOnly={isThreadReadOnly}
                  agentName={isHome ? t('agents.chiefOfStaff') : t('agents.analyst')}
                  showSystemFiles={showSystemFiles}
                  onToggleSystemFiles={() => {
                    setShowSystemFiles((v) => {
                      localStorage.setItem('filePanel.showSystemFiles', String(!v));
                      return !v;
                    });
                  }}
                  {...filePanelAccess}
                  browserView={browserViewFor(panelWorkspaceId)}
                />
                </WorkspaceProvider>
              </Suspense>
            </div>
          </motion.div>
        )
      ) : (
        <>
        {/* Resize divider — outside overflow-hidden panel so its wide hover zone isn't clipped */}
        {rightPanelType && !panelExpanded && (
          <div
            className={`chat-split-divider${isDragging ? ' dragging' : ''}`}
            onMouseDown={handleDividerMouseDown}
          />
        )}
        <AnimatePresence>
          {rightPanelType && (
            <motion.div
              ref={panelWrapperRef}
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: rightPanelWidth, opacity: 1 }}
              exit={PANEL_EXIT}
              transition={(isDragging || dragJustEnded)
                ? { duration: 0 }
                : PANEL_OPEN
              }
              className="flex shrink-0 overflow-hidden"
            >
              {/* Expanded, the panel lies over the whole view while the
                  wrapper keeps its width in the row: the chat underneath
                  keeps its layout and scroll, and the panel stays the same
                  element, so a running app's iframe does not reload. */}
              <div
                ref={panelInnerRef}
                data-panel-inner
                className={panelExpanded ? 'absolute inset-0 z-10' : 'shrink-0 h-full'}
                style={panelExpanded ? undefined : { width: rightPanelWidth }}
              >
                <Suspense fallback={rightPanelType === 'file' ? <PanelLoadingStrip onClose={() => { setRightPanelType(null); popPanelHistory(); }} /> : null}>
                  {rightPanelType === 'file' ? (
                    <WorkspaceProvider workspaceId={panelWorkspaceId} downloadFile={null} folders={panelFolders}>
                    <FilePanel
                      workspaceId={panelWorkspaceId}
                      threadId={panelThreadId}
                      chatKey={panelChatKey}
                      isActive={isActive}
                      onClose={() => { setRightPanelType(null); popPanelHistory(); }}
                      expanded={panelExpanded}
                      onToggleExpanded={handleTogglePanelExpanded}
                      panelToggle
                      onLeaveGuardChange={handleFilesLeaveGuardChange}
                      onActiveTabKindChange={handleActiveTabKindChange}
                      target={panelTarget}
                      onTargetHandled={handleTargetHandled}
                      onTargetMemoryHandled={handleTargetMemoryHandled}
                      onTargetMemoHandled={handleTargetMemoHandled}
                      onOpenInMarketView={handleOpenInMarketView}
                      onOpenSubagentTask={openSubagentTaskFromPanel}
                      transcript={transcript}
                      onOpenFile={handleOpenFileFromChat}
                      getRecentWritePaths={getRecentWritePaths}
                      files={workspaceFiles}
                      filesLoading={filesLoading}
                      filesError={filesError}
                      onRefreshFiles={refreshFiles}
                      onAddContext={!isThreadReadOnly && activeAgentId === 'main' ? handleAddContext : null} // DALTON-HOOK(workspace-sharing-settings)
                      // DALTON-HOOK(team-sharing): no comment layer/docked comment box on a read-only thread
                      threadReadOnly={isThreadReadOnly}
                      agentName={isHome ? t('agents.chiefOfStaff') : t('agents.analyst')}
                      showSystemFiles={showSystemFiles}
                      onToggleSystemFiles={() => {
                        setShowSystemFiles((v) => {
                          localStorage.setItem('filePanel.showSystemFiles', String(!v));
                          return !v;
                        });
                      }}
                      {...filePanelAccess}
                      browserView={browserViewFor(panelWorkspaceId)}
                    />
                    </WorkspaceProvider>
                  ) : null}
                </Suspense>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
        </>
      )}

      {publishDialog}
    </div>
    </ComposerDockContext>
    </ChatCommentSendsProvider>
    </ThreadCommandsContext>
    </LinkActionsContext>
    </RouteLeaveGuardContext>
    </WorkspaceProvider>
    </ThreadQuotesProvider>
    </ThreadStatusContext>
    </ChartComposerContext>
  );
}

export default ChatView;
