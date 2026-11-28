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
  /**
   * DALTON-HOOK(external-share-guard)/(team-sharing): whether this chat's
   * file cards may offer Publish at all — the workspace's own owner only. A
   * colleague (rw or ro), an external member, or a read-only thread gets no
   * entry; the server refuses them regardless.
   */
  publishAllowed?: boolean;
  downloadKeyFor?: Action<'downloadKeyFor'>;
  onRevealFiles?: Action<'onRevealFiles'>;
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
    readOnly: readOnlyProp, publishAllowed: publishAllowedProp, // DALTON-HOOK(team-sharing) DALTON-HOOK(external-share-guard)
    onOpenArtifactComment, onEditMessage, onRegenerate, onRetry, ...handlers
  } = src;
  const modelOptions = () => chatInputRef.current?.getModelOptions?.();
  const stable = useStableActions({
    ...handlers,
    onOpenArtifactComment: typeof onOpenArtifactComment === 'function' ? onOpenArtifactComment : undefined,
    onEditMessage: (id: string, content: string) => { onEditMessage(id, content, modelOptions()); },
    onRegenerate: (id: string) => { onRegenerate(id, modelOptions()); },
    // DALTON-HOOK(automation-effort): read retry options without auto-sending saved composer reasoningEffort
    onRetry: () => {
      onRetry(chatInputRef.current?.getRetryModelOptions?.() ?? modelOptions());
    },
  });
  const openComment = stable.onOpenArtifactComment;
  const openArtifactComment = useMemo(
    () => (openComment ? { file: openComment, chart: openComment } : undefined),
    [openComment],
  );
  const commentOpeners = typeof onOpenArtifactComment === 'function' ? openArtifactComment : onOpenArtifactComment;

  // The main transcript's action surface. Every member is identity-stable, so
  // this object is built once and never re-renders the memoized message tree.
  // DALTON-HOOK(team-sharing): in read-only mode, expose only navigation actions
  // DALTON-HOOK(external-share-guard): withhold onPublishFile when publishAllowed is false
  const readOnly = Boolean(readOnlyProp);
  const publishAllowed = publishAllowedProp !== false && !readOnly;
  const messageActions = useMemo<MessageActions>(() => {
    if (readOnly) {
      return {
        onOpenFile: stable.onOpenFile,
        onDownloadFile: stable.onDownloadFile,
        downloadKeyFor: stable.downloadKeyFor,
        onRevealFiles: stable.onRevealFiles,
        onRevealMessage: stable.onRevealMessage,
        onOpenSources: stable.onOpenSources,
        onToolCallDetailClick: stable.onToolCallDetailClick,
        onOpenChart: stable.onOpenChart,
        onOpenSubagentTask: stable.onOpenSubagentTask,
        onOpenDispatcher: stable.onOpenDispatcher,
        onOpenThread: stable.onOpenThread,
      };
    }
    const { onPublishFile: stablePublishFile, ...restStable } = stable;
    return {
      ...restStable,
      ...(publishAllowed ? { onPublishFile: stablePublishFile } : {}),
      onOpenArtifactComment: commentOpeners,
      editDrafts,
      widgetPrompts,
      reportThreadId,
      feedbackLoading,
    };
  }, [readOnly, publishAllowed, stable, commentOpeners, editDrafts, widgetPrompts, reportThreadId, feedbackLoading]);

  // The subagent transcript is a DIFFERENT surface: its cards belong to a task,
  // not to the main thread's turn, so the main thread's approve/reject/edit
  // handlers must not be reachable from it. Navigation only.
  // No onRevealFiles: it moves the main transcript, and the settle-aware
  // observer that follows an unfolding deck is only attached there.
  const subagentMessageActions = useMemo<MessageActions>(() => ({
    onOpenFile: stable.onOpenFile,
    onDownloadFile: stable.onDownloadFile,
    ...(publishAllowed ? { onPublishFile: stable.onPublishFile } : {}), // DALTON-HOOK(external-share-guard)
    downloadKeyFor: stable.downloadKeyFor,
    onToolCallDetailClick: stable.onToolCallDetailClick,
    onOpenChart: stable.onOpenChart,
  }), [stable, publishAllowed]);

  return { messageActions, subagentMessageActions };
}
