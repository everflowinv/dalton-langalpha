import { useMemo } from 'react';
import type { RefObject } from 'react';
import { useStableActions } from '@/hooks/useStableActions';
import type { ChatInputHandle } from '@/components/ui/chat-input';
import type { MessageActions } from '../messageList/MessageActionsContext';
import type { EditDraftStore } from '../../hooks/utils/editDrafts';
import type { WidgetPrompts } from '../../utils/widgetPrompt';
import type { ModelOptions } from './types';
import type { ArtifactCommentOpeners, ArtifactCommentRef } from '../artifactComments/artifactCommentReply';

type Action<K extends keyof MessageActions> = NonNullable<MessageActions[K]>;

/**
 * A host's raw handlers, before their identities are pinned. One a host
 * leaves out is absent from its bundle, so the transcript offers nothing for
 * it: the market panel opens no files and no other thread.
 */
export interface MessageActionSources {
  reportThreadId?: string;
  feedbackLoading?: boolean;
  onOpenThread?: Action<'onOpenThread'>;
  onOpenFile?: Action<'onOpenFile'>;
  onDownloadFile?: Action<'onDownloadFile'>;
  onPublishFile: Action<'onPublishFile'>;
<<<<<<< HEAD
  /**
   * DALTON-HOOK(external-share-guard)/(team-sharing): whether this chat's
   * file cards may offer Publish at all — the workspace's own owner only. A
   * colleague (rw or ro), an external member, or a read-only thread gets no
   * entry; the server refuses them regardless.
   */
  publishAllowed?: boolean;
  downloadKeyFor: Action<'downloadKeyFor'>;
  onRevealFiles: Action<'onRevealFiles'>;
=======
  downloadKeyFor?: Action<'downloadKeyFor'>;
  onRevealFiles?: Action<'onRevealFiles'>;
>>>>>>> db284da5a
  onRevealMessage: Action<'onRevealMessage'>;
  onOpenSources?: Action<'onOpenSources'>;
  onToolCallDetailClick: Action<'onToolCallDetailClick'>;
  onOpenChart?: Action<'onOpenChart'>;
  /** One opener for both kinds, a file's in the panel and a chart's in its
   *  tab, or openers by kind that are already identity-stable. */
  onOpenArtifactComment: ((comment: ArtifactCommentRef) => void) | ArtifactCommentOpeners | undefined;
  onOpenSubagentTask: Action<'onOpenSubagentTask'>;
  onOpenDispatcher?: Action<'onOpenDispatcher'>;
  onAnswerQuestion: Action<'onAnswerQuestion'>;
  onSkipQuestion: Action<'onSkipQuestion'>;
  onApproveCreateWorkspace: Action<'onApproveCreateWorkspace'>;
  onRejectCreateWorkspace: Action<'onRejectCreateWorkspace'>;
  onApproveStartQuestion: Action<'onApproveStartQuestion'>;
  onRejectStartQuestion: Action<'onRejectStartQuestion'>;
  onApprovePTCAgent: Action<'onApprovePTCAgent'>;
  onRejectPTCAgent: Action<'onRejectPTCAgent'>;
  onApproveSecretaryAction: Action<'onApproveSecretaryAction'>;
  onRejectSecretaryAction: Action<'onRejectSecretaryAction'>;
  // DALTON-HOOK(watchlist): optional watchlist confirmation handlers
  onApproveWatchlistAction?: Action<'onApproveWatchlistAction'>;
  onRejectWatchlistAction?: Action<'onRejectWatchlistAction'>;
  onResumeCreditPause: Action<'onResumeCreditPause'>;
  onApproveToolCall: Action<'onApproveToolCall'>;
  onRejectToolCall: Action<'onRejectToolCall'>;
  onThumbUp: Action<'onThumbUp'>;
  onThumbDown: Action<'onThumbDown'>;
  onThumbDownFollowUp: Action<'onThumbDownFollowUp'>;
  /** Already identity-stable (`useWidgetPromptActions`). */
  widgetPrompts: WidgetPrompts;
  /** These three read the composer's current model options at call time. */
  onEditMessage: (messageId: string, content: string, modelOptions?: ModelOptions) => unknown;
  onRegenerate: (messageId: string, modelOptions?: ModelOptions) => unknown;
  onRetry: (modelOptions?: ModelOptions) => unknown;
  onForkFromTurn?: Action<'onForkFromTurn'>;
  /** Created once by the session, so already identity-stable. */
  editDrafts: EditDraftStore;
  chatInputRef: RefObject<ChatInputHandle | null>;
  // DALTON-HOOK(team-sharing): withhold mutation/interactive actions in read-only mode
  readOnly?: boolean;
}

/**
 * Stable handler identities for the memoized message tree. MessageBubble is
 * memo'd, but the handlers a host hands in are recreated upstream on every
 * streamed chunk (useChatMessages/useRightPanel re-render per chunk); passing
 * them straight through would re-render every settled bubble on every chunk.
 * Each identity is pinned while it always invokes the freshest closure, which
 * is exactly what edit/regenerate need: their turn-index math must read the
 * current messages array, never a memoized snapshot. Feedback handlers close
 * over the stored ratings, so their raw identity churns on every load/submit.
 */
export function useMessageActionBundles(src: MessageActionSources): {
  messageActions: MessageActions;
  subagentMessageActions: MessageActions;
} {
  const {
    reportThreadId, feedbackLoading, editDrafts, widgetPrompts, chatInputRef,
    onOpenArtifactComment, onEditMessage, onRegenerate, onRetry, ...handlers
  } = src;
  const modelOptions = () => chatInputRef.current?.getModelOptions?.();
  const stable = useStableActions({
    ...handlers,
    onOpenArtifactComment: typeof onOpenArtifactComment === 'function' ? onOpenArtifactComment : undefined,
    onEditMessage: (id: string, content: string) => { onEditMessage(id, content, modelOptions()); },
    onRegenerate: (id: string) => { onRegenerate(id, modelOptions()); },
    onRetry: () => { onRetry(modelOptions()); },
  });
  const openComment = stable.onOpenArtifactComment;
  const openArtifactComment = useMemo(
    () => (openComment ? { file: openComment, chart: openComment } : undefined),
    [openComment],
  );
<<<<<<< HEAD
  const stableOpenSubagentTask = useStableHandler(src.onOpenSubagentTask);
  const stableOpenDispatcher = useStableHandler(src.onOpenDispatcher);
  const stableOpenThread = useStableHandler(src.onOpenThread);
  const stableAnswerQuestion = useStableHandler(src.onAnswerQuestion);
  const stableSkipQuestion = useStableHandler(src.onSkipQuestion);
  const stableApproveCreateWorkspace = useStableHandler(src.onApproveCreateWorkspace);
  const stableRejectCreateWorkspace = useStableHandler(src.onRejectCreateWorkspace);
  const stableApproveStartQuestion = useStableHandler(src.onApproveStartQuestion);
  const stableRejectStartQuestion = useStableHandler(src.onRejectStartQuestion);
  const stableApprovePTCAgent = useStableHandler(src.onApprovePTCAgent);
  const stableRejectPTCAgent = useStableHandler(src.onRejectPTCAgent);
  const stableApproveSecretaryAction = useStableHandler(src.onApproveSecretaryAction);
  const stableRejectSecretaryAction = useStableHandler(src.onRejectSecretaryAction);
  // DALTON-HOOK(watchlist): pin watchlist confirmation callbacks
  const stableApproveWatchlistAction = useStableHandler((proposalData: Record<string, unknown>) =>
    src.onApproveWatchlistAction?.(proposalData));
  const stableRejectWatchlistAction = useStableHandler((proposalData: Record<string, unknown>) =>
    src.onRejectWatchlistAction?.(proposalData));
  const stableResumeCreditPause = useStableHandler(src.onResumeCreditPause);
  const stableApproveToolCall = useStableHandler(src.onApproveToolCall);
  const stableRejectToolCall = useStableHandler(src.onRejectToolCall);
  const stableEditMessage = useStableHandler((id: string, content: string) =>
    src.onEditMessage(id, content, src.chatInputRef.current?.getModelOptions?.()));
  const stableRegenerate = useStableHandler((id: string) =>
    src.onRegenerate(id, src.chatInputRef.current?.getModelOptions?.()));
  // DALTON-HOOK(automation-effort): read retry options without auto-sending saved composer reasoningEffort
  const stableRetry = useStableHandler(() =>
    src.onRetry(
      src.chatInputRef.current?.getRetryModelOptions?.()
      ?? src.chatInputRef.current?.getModelOptions?.(),
    ));
  const stableForkFromTurn = useStableHandler(src.onForkFromTurn);
  const editDrafts = src.editDrafts;
  // Feedback handlers close over the stored ratings, so their raw identity
  // churns on every load/submit; wrap them or a single thumbs click would
  // re-render the whole transcript through the context value.
  const stableThumbUp = useStableHandler(src.onThumbUp);
  const stableThumbDown = useStableHandler(src.onThumbDown);
  const stableThumbDownFollowUp = useStableHandler(src.onThumbDownFollowUp);
  const stableSendMessage = src.onWidgetSendPrompt;

  // The main transcript's action surface. Every member is identity-stable, so
  // this object is built once and never re-renders the memoized message tree.
  // DALTON-HOOK(team-sharing): in read-only mode, expose only navigation actions
  const readOnly = Boolean(src.readOnly);
  const publishAllowed = src.publishAllowed !== false && !readOnly;
  const messageActions = useMemo<MessageActions>(() => (
    readOnly
      ? {
          onOpenFile: stableOpenFile,
          onDownloadFile: stableDownloadFile,
          downloadKeyFor: stableDownloadKeyFor,
          onRevealFiles: stableRevealFiles,
          onRevealMessage: stableRevealMessage,
          onOpenSources: stableOpenSources,
          onToolCallDetailClick: stableToolCallDetail,
          onOpenChart: stableOpenChart,
          onOpenSubagentTask: stableOpenSubagentTask,
          onOpenDispatcher: stableOpenDispatcher,
          onOpenThread: stableOpenThread,
        }
      : {
          onOpenFile: stableOpenFile,
          onDownloadFile: stableDownloadFile,
          ...(publishAllowed ? { onPublishFile: stablePublishFile } : {}),
          downloadKeyFor: stableDownloadKeyFor,
          onRevealFiles: stableRevealFiles,
          onRevealMessage: stableRevealMessage,
          onOpenSources: stableOpenSources,
          onToolCallDetailClick: stableToolCallDetail,
          onOpenChart: stableOpenChart,
          onOpenArtifactComment: openArtifactComment,
          onOpenSubagentTask: stableOpenSubagentTask,
          onOpenDispatcher: stableOpenDispatcher,
          onOpenThread: stableOpenThread,
          onAnswerQuestion: stableAnswerQuestion,
          onSkipQuestion: stableSkipQuestion,
          onApproveCreateWorkspace: stableApproveCreateWorkspace,
          onRejectCreateWorkspace: stableRejectCreateWorkspace,
          onApproveStartQuestion: stableApproveStartQuestion,
          onRejectStartQuestion: stableRejectStartQuestion,
          onApprovePTCAgent: stableApprovePTCAgent,
          onRejectPTCAgent: stableRejectPTCAgent,
          onApproveSecretaryAction: stableApproveSecretaryAction,
          onRejectSecretaryAction: stableRejectSecretaryAction,
          // DALTON-HOOK(watchlist): expose watchlist confirmation handlers when provided
          ...(src.onApproveWatchlistAction ? { onApproveWatchlistAction: stableApproveWatchlistAction } : {}),
          ...(src.onRejectWatchlistAction ? { onRejectWatchlistAction: stableRejectWatchlistAction } : {}),
          onResumeCreditPause: stableResumeCreditPause,
          onApproveToolCall: stableApproveToolCall,
          onRejectToolCall: stableRejectToolCall,
          onEditMessage: stableEditMessage,
          onRegenerate: stableRegenerate,
          onRetry: stableRetry,
          onForkFromTurn: stableForkFromTurn,
          editDrafts,
          onThumbUp: stableThumbUp,
          onThumbDown: stableThumbDown,
          onThumbDownFollowUp: stableThumbDownFollowUp,
          onWidgetSendPrompt: stableSendMessage,
          reportThreadId: src.reportThreadId,
          feedbackLoading: src.feedbackLoading,
        }
  ), [
    readOnly,
    stableOpenFile, stableDownloadFile, stablePublishFile, stableDownloadKeyFor, stableRevealFiles, stableRevealMessage, stableOpenSources, stableToolCallDetail,
    stableOpenChart, openArtifactComment, stableOpenSubagentTask, stableOpenDispatcher, stableOpenThread, stableAnswerQuestion, stableSkipQuestion, stableApproveCreateWorkspace,
    stableRejectCreateWorkspace, stableApproveStartQuestion, stableRejectStartQuestion,
    stableApprovePTCAgent, stableRejectPTCAgent, stableApproveSecretaryAction,
    stableRejectSecretaryAction, stableApproveWatchlistAction, stableRejectWatchlistAction,
    src.onApproveWatchlistAction, src.onRejectWatchlistAction, // DALTON-HOOK(watchlist)
    stableResumeCreditPause, stableApproveToolCall, stableRejectToolCall,
    stableEditMessage, stableRegenerate, stableRetry, stableForkFromTurn, editDrafts,
    stableThumbUp, stableThumbDown, stableThumbDownFollowUp, stableSendMessage, publishAllowed, src.reportThreadId, src.feedbackLoading, // DALTON-HOOK(external-share-guard)
  ]);
=======
  const commentOpeners = typeof onOpenArtifactComment === 'function' ? openArtifactComment : onOpenArtifactComment;

  // The main transcript's action surface. Every member is identity-stable, so
  // this object is built once and never re-renders the memoized message tree.
  const messageActions = useMemo<MessageActions>(() => ({
    ...stable,
    onOpenArtifactComment: commentOpeners,
    editDrafts,
    widgetPrompts,
    reportThreadId,
    feedbackLoading,
  }), [stable, commentOpeners, editDrafts, widgetPrompts, reportThreadId, feedbackLoading]);
>>>>>>> db284da5a

  // The subagent transcript is a DIFFERENT surface: its cards belong to a task,
  // not to the main thread's turn, so the main thread's approve/reject/edit
  // handlers must not be reachable from it. Navigation only.
  // No onRevealFiles: it moves the main transcript, and the settle-aware
  // observer that follows an unfolding deck is only attached there.
  const subagentMessageActions = useMemo<MessageActions>(() => ({
<<<<<<< HEAD
    onOpenFile: stableOpenFile,
    onDownloadFile: stableDownloadFile,
    ...(publishAllowed ? { onPublishFile: stablePublishFile } : {}), // DALTON-HOOK(external-share-guard)
    downloadKeyFor: stableDownloadKeyFor,
    onToolCallDetailClick: stableToolCallDetail,
    onOpenChart: stableOpenChart,
  }), [stableOpenFile, stableDownloadFile, stablePublishFile, stableDownloadKeyFor, stableToolCallDetail, stableOpenChart, publishAllowed]);
=======
    onOpenFile: stable.onOpenFile,
    onDownloadFile: stable.onDownloadFile,
    onPublishFile: stable.onPublishFile,
    downloadKeyFor: stable.downloadKeyFor,
    onToolCallDetailClick: stable.onToolCallDetailClick,
    onOpenChart: stable.onOpenChart,
  }), [stable]);
>>>>>>> db284da5a

  return { messageActions, subagentMessageActions };
}
