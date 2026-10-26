import React, { useState, useEffect, useLayoutEffect, useRef, useMemo, memo } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, FileSearch, Info, Pencil, RefreshCw, RotateCcw, StopCircle, ThumbsDown, ThumbsUp } from 'lucide-react';
import ThumbDownModal from '../ThumbDownModal';
import FigureEightLoading, { FADE_MS } from '@/components/ui/figure-eight-loading';
import { AnimatePresence, motion } from '@/lib/framer';
import { EXIT_TWEEN } from './liveZoneTiming';
import { visibleParagraphPrefix } from '@/lib/paragraphGate';
import { useTranscriptDisplay } from '@/lib/transcriptDisplay';
import { CitationMetadataProvider } from '../CitationMetadataContext';
import { CreditPausePendingProvider } from '../CreditPausePendingContext';
import TextMessageContent, { TurnErrorCard } from '../TextMessageContent';
import type { StructuredError } from '@/utils/rateLimitError';
import { countDedupedSources, type CreditPauseState, type MessageProvenance, type ProvenanceRecord, type SubagentTaskRecord, type ToolApprovalState } from '@/types/chat';
import { TextShimmer } from '@/components/ui/text-shimmer';
import type { SelectionPreviewShape } from '../SelectionContextPreview';
import { AttachmentCard, InlineSelectionCards, InlineWidgetDeck } from './attachments';
import type { AttachmentData, WidgetChipShape } from './attachments';
import { MessageContentSegments } from './MessageContentSegments';
import { OverflowCollapse } from './OverflowCollapse';
import { useMessageActions } from './MessageActionsContext';
import { OpenThoughtContext, openThoughtLength, useArrivalQuiet, useOpenThoughts } from './useArrivalQuiet';
import { useStableHandler } from '@/hooks/useStableHandler';
import { useEditDraft, useEditDraftStore } from '../../hooks/utils/editDrafts';
import { isSteeringUserMessage } from './messagePredicates';
import { UserBubble } from './UserBubble';
import { assistantText } from './messageText';
import { TurnFileCards } from './TurnFileCards';
import type { TurnFile } from '../../utils/turnFiles';
import { projectMessageContent, streamingTextBlockIndex, type ContentProjection } from './contentProjection';
import { EMPTY_OBJ } from './types';
import type { ContentSegmentRecord, FeedbackResult, FoldState, MessageRecord, ToolCallProcessRecord } from './types';

// --- MessageBubble ---

/** Pause without new text before the streaming indicator fades back in. Longer
 *  than a token-cadence gap and the typewriter's own lag, shorter than a tool
 *  call or a thinking pause. */
const ARRIVAL_QUIET_MS = 700;

/** Collapsed bound for long user messages (~10 lines of chat text). */
const USER_MESSAGE_COLLAPSE_PX = 240;

interface MessageBubbleProps {
  message: MessageRecord;
  contentProjection?: ContentProjection;
  /** Backend turn this bubble belongs to (a steering continuation folds back
   *  into the turn it continues). Feedback is addressed by turn, not bubble.
   *  Null for a send the server refused or never got, which has no turn. */
  turnIndex: number | null;
  /** Whether this assistant bubble is the LAST VISIBLE bubble of its backend
   *  turn (steering splits one turn across several bubbles). Regenerate renders
   *  only on the tail. Computed positionally in MessageList. */
  isTurnTail: boolean;
  /** False for a turn that opened hidden, whose words a regenerate's replay
   *  would drop (see `hiddenOpenerTurns`). */
  canRegenerate?: boolean;
  /** Whether this is the transcript's newest reply. Retry re-runs the
   *  thread's latest attempt, so an older failed reply offers none. */
  isLatestReply?: boolean;
  /** The turn is still running, tool execution and gaps between model calls
   *  included. `message.isStreaming` alone drops between two model calls, and
   *  the deliverables of a running turn are not deliverables yet. */
  isTurnLive?: boolean;
  /** Files this whole turn wrote or pointed at, rendered as cards on the tail. */
  turnFiles?: TurnFile[];
  /** This turn's stored rating, or null. */
  feedback?: FeedbackResult | null;
  isLoading?: boolean;
  /** How much of the turn this bubble draws (see `FoldState`). Every bubble of
   *  a turn gets the same value; `unfolded` is the transcript as it always was. */
  fold?: FoldState;
  isSubagentView?: boolean;
  readOnly?: boolean;
  allowFiles?: boolean;
  isMobile?: boolean;
  dispatcherContext?: { threadId: string; workspaceId: string } | null;
}

/**
 * Wrapped with React.memo — safe because updateMessage() in messageHelpers.ts
 * returns the same object reference for unchanged messages. Actions come from
 * MessageActionsContext (one identity-stable object per host), so a streamed
 * chunk never re-renders settled bubbles through a handler identity.
 */
export const MessageBubble = memo(function MessageBubble({ message, contentProjection, turnIndex, isTurnTail, canRegenerate = true, isLatestReply = true, isTurnLive, turnFiles, feedback, isLoading, fold = 'unfolded', isSubagentView, readOnly, allowFiles, isMobile, dispatcherContext }: MessageBubbleProps): React.ReactElement | null {
  const { onOpenFile, onDownloadFile, onRevealFiles, onOpenSources, onEditMessage, editDrafts, onRegenerate, onRetry, onThumbUp, onThumbDown, onReportWithAgent } = useMessageActions();
  const { t } = useTranslation();
  const projection = contentProjection ?? projectMessageContent(message, isSubagentView);
  const isUser = (message.role as string) === 'user';
  const isAssistant = (message.role as string) === 'assistant';
  const isPendingDelivery = isUser && ((message.isPending as boolean) || (message.steering as boolean) || (message.queued as boolean));
  const attachments = message.attachments as AttachmentData[] | undefined;
  const hasAttachments = isUser && attachments && attachments.length > 0;
  const widgetSnapshots = message.widgetSnapshots as WidgetChipShape[] | undefined;
  const hasWidgetSnapshots = isUser && widgetSnapshots && widgetSnapshots.length > 0;
  const chartSelections = message.chartSelections as SelectionPreviewShape[] | undefined;
  const hasChartSelections = isUser && chartSelections && chartSelections.length > 0;
  const provenance = isUser ? (message.provenance as MessageProvenance | undefined) : undefined;

  // Edit mode lives in the host's draft store, so a reload that remounts the
  // bubble reopens the box with what was typed in it.
  const originalText = (message.content as string) || '';
  const draftKey = { turnIndex, original: originalText, messageId: message.id as string };
  const draftStore = useEditDraftStore(editDrafts);
  const draftText = useEditDraft(draftStore, isUser ? draftKey : null);
  const isEditing = draftText !== undefined;
  const editContent = draftText ?? '';
  const editTextareaRef = useRef<HTMLTextAreaElement>(null);

  // Copy state
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
  }, []);

  // Feedback state
  const [feedbackRating, setFeedbackRating] = useState<string | null>(null);
  const [showThumbDownModal, setShowThumbDownModal] = useState(false);

  // Seed from the turn's stored rating. Data, not a lookup callback: the old
  // `getFeedbackForMessage` identity changed on every load/submit and re-seeded
  // EVERY mounted bubble; `feedback` changes only for the turn it belongs to.
  useEffect(() => {
    if (isAssistant && feedback) setFeedbackRating(feedback.rating);
  }, [isAssistant, feedback]);

  const handleThumbUpClick = async () => {
    if (!onThumbUp || turnIndex === null) return;
    const prevRating = feedbackRating;
    const newRating = prevRating === 'thumbs_up' ? null : 'thumbs_up';
    setFeedbackRating(newRating);
    const result = await onThumbUp(turnIndex);
    if (result === null) setFeedbackRating(prevRating);
    else if (result) setFeedbackRating(result.rating);
  };

  const handleThumbDownSubmit = async (issueCategories: string[], comment: string | null, consentHumanReview: boolean) => {
    if (!onThumbDown || turnIndex === null) return;
    const prevRating = feedbackRating;
    setFeedbackRating('thumbs_down');
    setShowThumbDownModal(false);
    const result = await onThumbDown(turnIndex, issueCategories, comment, consentHumanReview);
    if (result === null) setFeedbackRating(prevRating);
  };

  // Action buttons are mounted for all normal messages (reserves layout space) and
  // only made visible after streaming settles. Keeping them mounted prevents a
  // ~32px layout jump on sibling messages when streaming ends.
  const canShowActions = !isSubagentView && !readOnly;
  const isStreaming = message.isStreaming as boolean;
  const turnLive = isTurnLive ?? isStreaming;
  // A background task outliving its stream leaves both `isStreaming` and
  // `isLoading` false while the turn is still working, and rating or
  // regenerating a reply the task can still change asks about an answer that
  // does not exist yet. `isLoading` stays in the condition on its own account:
  // it hides every other turn's controls while the session loads.
  const showActions = canShowActions && !turnLive && !isLoading;

  // The stream stamps `arrivalSeq` on every landed reply text chunk, whatever
  // the typewriter has shown of it. The streaming indicator hides while it
  // climbs and shows in the pauses. Thought text counts only while its row is
  // open: folded, it streams out of sight, and an indicator that hid for it
  // blinked off and on with every burst of thinking. Tool calls never count,
  // being written or running: the indicator keeps going under their rows.
  const { streamingMode } = useTranscriptDisplay();
  const arrivalSeq = (message.arrivalSeq as number | undefined) ?? 0;
  const [openThoughts, markOpenThought] = useOpenThoughts();
  const thoughtSeq = openThoughtLength(message.reasoningProcesses as Record<string, Record<string, unknown>> | undefined, openThoughts);
  const thoughtQuiet = useArrivalQuiet(thoughtSeq, isStreaming, ARRIVAL_QUIET_MS);
  const arrivalQuiet = useArrivalQuiet(arrivalSeq, isStreaming, ARRIVAL_QUIET_MS) && thoughtQuiet;

  // Provenance count for the Sources pill, deduped by (source_type,
  // identifier): the same URL fetched twice in one turn counts once. It counts
  // distinct sources (every result URL), so it intentionally runs ahead of the
  // panel's visible row count: the panel groups a whole web search into one
  // deck, but the pill still reports how many pages were actually read.
  const sourceCount = useMemo(() => {
    if (!isAssistant || isSubagentView) return 0;
    return countDedupedSources(message.provenanceRecords as Record<string, ProvenanceRecord> | undefined);
  }, [message.provenanceRecords, isAssistant, isSubagentView]);

  // A mid-turn bubble of a folded turn is process by definition: its prose is
  // commentary the answer superseded, and its activity is behind the fold row.
  // Once that is taken away it can have nothing left to draw, and an assistant
  // bubble with nothing in it is an avatar beside a blank column — the same
  // thing `isOrphanAssistantMessage` keeps off screen. The bubble stays in
  // state either way: turn indices are counted positionally over it.
  const foldedAway = fold === 'collapsed'
    && isAssistant
    && !isTurnTail
    && !(message.error as boolean)
    && !(message.stopped as boolean)
    && sourceCount === 0
    && !projection.hasRetained;
  // The footer row reads the message only through these, so a streaming
  // bubble can reuse its row across chunks: the message is a new object on
  // every chunk, and a row that read it would rebuild its buttons, icons and
  // labels each time while hidden. The two handlers read it when clicked.
  const messageId = message.id as string;
  const hasError = !!message.error;
  const unloaded = !!message.unloaded;
  const failure = hasError ? message.structuredError as StructuredError | undefined : undefined;
  const hasBody = !(projection.textCount === 0 && projection.blocks.every((block) => block.type === 'text'));
  const isSteering = isSteeringUserMessage(message);
  const resizeTextarea = () => {
    const el = editTextareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  };
  const handleStartEdit = useStableHandler(() => {
    draftStore.markFocus(draftKey);
    draftStore.put({ ...draftKey, text: originalText });
  });
  // A box that held the focus when a reload took it away takes it back when
  // it shows again, as does one just opened or given back, unless the user is
  // somewhere else by then. A layout effect, so its cleanup runs while the
  // box is still in the document and can tell whether it held the focus.
  useLayoutEffect(() => {
    const el = editTextareaRef.current;
    if (!isEditing || !el) return;
    resizeTextarea();
    const active = document.activeElement;
    const key = { turnIndex, original: originalText, messageId };
    if (draftStore.takeFocus(key) && (!active || active === document.body)) {
      el.focus({ preventScroll: true });
      el.setSelectionRange(el.value.length, el.value.length);
    }
    return () => {
      if (document.activeElement === el) draftStore.markFocus(key);
    };
  }, [isEditing, draftStore, turnIndex, originalText, messageId]);
  const handleCopy = useStableHandler(() => {
    const text = assistantText(message);
    // Confirm only after the write lands (it can reject when the document
    // loses focus); rapid re-copies reset the shared timer instead of
    // stacking timers that would cut the newer indicator short.
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  });

  if (foldedAway) return null;

  const handleCancelEdit = () => {
    draftStore.drop(draftKey);
  };

  // A running turn holds the chat, so the edit waits in its box until it ends.
  const handleSubmitEdit = () => {
    if (isLoading) return;
    const trimmed = editContent.trim();
    // Closed first: an edit the session cannot take yet reopens the box.
    draftStore.drop(draftKey);
    if (trimmed && trimmed !== originalText.trim()) {
      onEditMessage?.(message.id as string, trimmed);
    }
  };

  const handleEditKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmitEdit();
    } else if (e.key === 'Escape') {
      handleCancelEdit();
    }
  };

  // Whether this bubble ends in a deck. The bubble above reads it too: its own
  // bottom padding and the deck's top margin are one gap with two owners, and
  // stacking both is what put ~29px between the reply and its first card.
  const showTurnFiles = !!(
    isAssistant && isTurnTail && !turnLive && onOpenFile
    && (!readOnly || allowFiles) && turnFiles && turnFiles.length > 0
  );

  // The footer actions, built once and placed by role. An assistant bubble
  // shows them on the same line as its Sources pill, above the deliverables, so
  // the turn still ends on the deck rather than on a row of icons; a user
  // bubble keeps them underneath, where there is nothing to share a line with.
  // Always mounted (reserves space), visibility toggled: aria-hidden + inert
  // keep the buttons out of the a11y tree and tab order while opacity-0 is
  // hiding them, so screen readers don't announce "Copy, Thumbs up, ..." for
  // every streaming message.
  const actionsRow = canShowActions && !isEditing ? (
  <div
    aria-hidden={!showActions}
    inert={!showActions || undefined}
    className={`flex gap-1 transition-opacity ${
      showActions
        ? (isMobile ? 'opacity-70' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100')
        : 'opacity-0 pointer-events-none'
    } ${
      // The top margin belongs to the user placement only: under a bubble the
      // row needs its own gap, while beside the Sources pill the meta row's
      // `items-center` is what puts it on the line.
      isUser ? 'justify-end mt-0.5' : 'justify-start'
    }`}
  >
    {/* User message actions. Steering bubbles get no edit pencil:
        they're injected mid-turn and have no turn checkpoint of their
        own, so an edit fork would target the NEXT turn and leave the
        original steering text in the agent's context. */}
    {isUser && onEditMessage && !isSteering && (
      <button
        onClick={handleStartEdit}
        className="p-1 min-h-7 min-w-7 inline-flex items-center justify-center rounded transition-colors hover:bg-(--color-bg-elevated)"
        title={t('chat.actions.editMessage')}
      >
        <Pencil className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
      </button>
    )}

    {/* Copy — both roles (a user bubble falls back to message.content).
        Then assistant-only: ThumbUp -> ThumbDown -> Regenerate/Retry */}
    <button
      onClick={handleCopy}
      className="p-1 min-h-7 min-w-7 inline-flex items-center justify-center rounded transition-colors hover:bg-(--color-bg-elevated)"
      title={copied ? t('chat.actions.copied') : t('chat.actions.copyMessage')}
    >
      {copied
        ? <Check className="h-3.5 w-3.5" style={{ color: 'var(--color-profit)' }} />
        : <Copy className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
      }
    </button>
    {isAssistant && !hasError && onThumbUp && (
      <button
        onClick={handleThumbUpClick}
        className="p-1 min-h-7 min-w-7 inline-flex items-center justify-center rounded transition-colors hover:bg-(--color-bg-elevated)"
        title={feedbackRating === 'thumbs_up' ? t('chat.actions.removeRating') : t('chat.actions.goodResponse')}
      >
        <ThumbsUp
          className="h-3.5 w-3.5"
          fill={feedbackRating === 'thumbs_up' ? 'currentColor' : 'none'}
          style={{ color: feedbackRating === 'thumbs_up' ? 'var(--color-profit)' : 'var(--color-text-tertiary)' }}
        />
      </button>
    )}
    {isAssistant && !hasError && onThumbDown && (
      <button
        onClick={() => setShowThumbDownModal(true)}
        className="p-1 min-h-7 min-w-7 inline-flex items-center justify-center rounded transition-colors hover:bg-(--color-bg-elevated)"
        title={feedbackRating === 'thumbs_down' ? t('chat.actions.feedbackSubmitted') : t('chat.actions.reportIssue')}
      >
        <ThumbsDown
          className="h-3.5 w-3.5"
          fill={feedbackRating === 'thumbs_down' ? 'currentColor' : 'none'}
          style={{ color: feedbackRating === 'thumbs_down' ? 'var(--color-loss)' : 'var(--color-text-tertiary)' }}
        />
      </button>
    )}
    {/* One regenerate per backend turn, on the turn's last bubble —
        regenerating re-runs the whole turn from its input checkpoint
        (mid-run steering can't be replayed), so the affordance sits
        at the end of the full response. A turn that opened hidden has
        none: that checkpoint lacks the words its context carried. */}
    {isAssistant && (!hasError || unloaded) && onRegenerate && isTurnTail && canRegenerate && (
      <button
        onClick={() => onRegenerate(messageId)}
        className="p-1 min-h-7 min-w-7 inline-flex items-center justify-center rounded transition-colors hover:bg-(--color-bg-elevated)"
        title={t('chat.actions.regenerate')}
      >
        <RefreshCw className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
      </button>
    )}
    {isAssistant && hasError && !unloaded && onRetry && isLatestReply && (
      <button
        onClick={onRetry}
        className="p-1 min-h-7 min-w-7 inline-flex items-center justify-center rounded transition-colors hover:bg-(--color-bg-elevated)"
        title={t('chat.actions.retry')}
      >
        <RotateCcw className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
      </button>
    )}
  </div>
  ) : null;

  // A user's message goes in its bubble, a reply sits on the page itself; the
  // content is the same either way.
  const bubbleContent = (
    <>
      <OverflowCollapse enabled={isUser} maxHeight={USER_MESSAGE_COLLAPSE_PX}>
      {isPendingDelivery ? (
        <TextShimmer
          as="span"
          className="text-sm [--base-color:var(--color-text-secondary)] [--base-gradient-color:var(--color-text-primary)]"
          duration={1.5}
        >
          {(message.content as string) || ''}
        </TextShimmer>
      ) : (
      <>
      {/* Render content segments in chronological order */}
      {(message.contentSegments as ContentSegmentRecord[] | undefined) && (message.contentSegments as ContentSegmentRecord[]).length > 0 ? (
        <CitationMetadataProvider toolCallProcesses={(message.toolCallProcesses as Record<string, Record<string, unknown>>) || EMPTY_OBJ}>
        <CreditPausePendingProvider creditPauses={message.creditPauses as Record<string, CreditPauseState> | undefined}>
        <OpenThoughtContext value={markOpenThought}>
        <MessageContentSegments
          contentProjection={projection}
          segments={message.contentSegments as ContentSegmentRecord[]}
          reasoningProcesses={(message.reasoningProcesses as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          toolCallProcesses={(message.toolCallProcesses as Record<string, ToolCallProcessRecord>) || EMPTY_OBJ}
          todoListProcesses={(message.todoListProcesses as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          subagentTasks={(message.subagentTasks as Record<string, SubagentTaskRecord>) || EMPTY_OBJ}
          userQuestions={(message.userQuestions as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          workspaceProposals={(message.workspaceProposals as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          questionProposals={(message.questionProposals as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          pendingToolCallChunks={(message.pendingToolCallChunks as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          isStreaming={message.isStreaming as boolean}
          fold={fold}
          isTurnTail={isTurnTail}
          isSubagentView={isSubagentView}
          ptcAgentProposals={(message.ptcAgentProposals as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          secretaryActionProposals={(message.secretaryActionProposals as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          creditPauses={(message.creditPauses as Record<string, CreditPauseState>) || EMPTY_OBJ}
          toolApprovals={(message.toolApprovals as Record<string, ToolApprovalState>) || EMPTY_OBJ}
          htmlWidgetProcesses={(message.htmlWidgetProcesses as Record<string, Record<string, unknown>>) || EMPTY_OBJ}
          readOnly={readOnly}
          dispatcherContext={dispatcherContext}
        />
        </OpenThoughtContext>
        </CreditPausePendingProvider>
        </CitationMetadataProvider>
      ) : (
        // Fallback for messages without segments (backward compatibility) - main chat shows text only
        ((message.contentType as string) === 'text' || !(message.contentType as string)) && (
          <TextMessageContent
            content={message.content as string}
            isStreaming={message.isStreaming as boolean}
            onOpenFile={onOpenFile}
          />
        )
      )}
      </>
      )}
      </OverflowCollapse>

      {/* The failure, once, after what the turn produced, which still reads as
          its reply. A turn that produced nothing has the card for its reply. */}
      {failure && (
        <div className={hasBody ? 'mt-3' : undefined}>
          <TurnErrorCard err={failure} />
        </div>
      )}

      {/* Streaming indicator. Stays mounted for the whole stream and only
          fades: while text is landing it is invisible (the text is the
          liveness signal), in a pause it fades back in so the turn never
          looks finished. Fading rather than unmounting keeps its row, so
          the transcript bottom does not hop by a line on every pause. */}
      <AnimatePresence initial={false}>
      {isStreaming && (() => {
        const hasContent = projection.blocks.length > 0 || (message.content as string)?.trim();
        // Arriving bytes are not visible progress while paragraph delivery
        // is holding the tail back. The question is whether anything that
        // arrived is unpainted, not whether the whole bubble is blank:
        // requiring every block to be wholly invisible text went false for
        // the rest of the reply the moment the first paragraph landed, and
        // was never true at all once the turn called a tool, so the
        // indicator vanished and a reply mid-paragraph looked finished.
        // Only the block still being written is held. Asking every block
        // counted a sentence a tool call followed, which shows whole, and
        // held the indicator up through the rest of the turn.
        const streamingText = projection.blocks[streamingTextBlockIndex(projection.blocks)];
        const tail = streamingText?.type === 'text' ? streamingText.segment.content || '' : '';
        const waitingForParagraph = streamingMode === 'paragraph'
          && visibleParagraphPrefix(tail).length < tail.length;
        const quiet = arrivalQuiet || waitingForParagraph;
        const size = isMobile ? 20 : 24;
        // The gap above the glyph is padding inside the row, not a margin:
        // content that renders zero-height (text still held back, a block
        // with nothing to show yet) lets a margin collapse out through the
        // bubble's top, and the bubble then jumps up as the first visible
        // content lands. The row leaves in px, never from `auto`: an auto
        // exit inside the chat scroller forces a layout a row short and
        // clamps the scroll.
        const gap = hasContent ? 12 : 0;
        return (
          <motion.div
            key="streaming-indicator"
            className="transition-opacity"
            style={{ opacity: quiet ? 1 : 0, transitionDuration: `${FADE_MS}ms`, height: size + gap, paddingTop: gap, overflow: 'hidden' }}
            exit={{ height: 0, paddingTop: 0, opacity: 0, transition: EXIT_TWEEN }}
            aria-hidden={!quiet}
            data-testid="streaming-indicator"
            // What the welcome flow's pending mark becomes in its hand-off
            // (welcome/handoff.css), named only while that runs.
            data-handoff="agent"
            data-quiet={quiet ? 'true' : 'false'}
          >
            <FigureEightLoading active={quiet} className={`${isMobile ? 'h-[20px]' : 'h-[24px]'} text-(--color-accent-primary)`} />
          </motion.div>
        );
      })()}
      </AnimatePresence>
    </>
  );

  return (
    <div
      data-message-id={message.id as string}
      data-message-role={isUser ? 'user' : 'assistant'}
      className={`group flex items-start ${isUser ? 'justify-end' : 'justify-start'}`}
    >

      {/* Message content column -- bubble + standalone attachment cards */}
      <div className={isUser ? (isEditing && isMobile ? 'w-full' : 'max-w-[80%]') + ' flex flex-col items-end gap-2' : 'w-full min-w-0'}>

        {/* ===== EDIT MODE (user messages) ===== */}
        {isEditing && isUser ? (
          <div className="w-full flex flex-col gap-2">
            {/* Attachment preview cards -- above the edit textarea */}
            {hasAttachments && (
              <div className="flex gap-3 overflow-x-auto">
                {attachments!.map((att, idx) => (
                  <AttachmentCard key={idx} attachment={att} />
                ))}
              </div>
            )}

            {/* Bordered textarea container */}
            <div
              className="rounded-xl px-4 py-3"
              style={{
                border: '2px solid var(--color-accent-overlay)',
                backgroundColor: 'transparent',
                color: 'var(--color-text-primary)',
              }}
            >
              <textarea
                ref={editTextareaRef}
                value={editContent}
                onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => {
                  draftStore.put({ ...draftKey, text: e.target.value });
                  resizeTextarea();
                }}
                onKeyDown={handleEditKeyDown}
                className="w-full bg-transparent text-sm resize-none leading-relaxed overflow-hidden"
                style={{ color: 'var(--color-text-primary)' }}
                rows={1}
              />
            </div>

            {/* Info text + Cancel/Save row */}
            <div className={`flex gap-3 ${isMobile ? 'flex-col' : 'items-center'}`}>
              <div className="flex items-start gap-1.5 flex-1 min-w-0">
                <Info className="h-3.5 w-3.5 shrink-0 mt-0.5" style={{ color: 'var(--color-text-tertiary)' }} />
                <span className="text-xs leading-snug" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('chat.editReplacesLater')}
                </span>
              </div>
              <div className={`flex gap-2 shrink-0 ${isMobile ? 'justify-end' : ''}`}>
                <button
                  onClick={handleCancelEdit}
                  className="px-4 py-1.5 rounded-full text-sm font-medium transition-colors"
                  style={{
                    color: 'var(--color-text-primary)',
                    border: '1px solid var(--color-border-muted)',
                  }}
                >
                  Cancel
                </button>
                <button
                  onClick={handleSubmitEdit}
                  disabled={isLoading}
                  className="px-4 py-1.5 rounded-full text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{
                    color: 'var(--color-btn-primary-text)',
                    backgroundColor: 'var(--color-btn-primary-bg)',
                  }}
                >
                  Save
                </button>
              </div>
            </div>
          </div>
        ) : (
        <>
        {/* ===== NORMAL MODE ===== */}
        {isUser ? (
          <UserBubble provenance={provenance} readOnly={readOnly} isMobile={isMobile}>
            {bubbleContent}
          </UserBubble>
        ) : (
          <div
            className={`rounded-lg rounded-tl-none pl-0 pr-0 ${showTurnFiles ? 'pb-0' : isMobile ? 'pb-2' : 'pb-3'}`}
            style={{
              backgroundColor: 'transparent',
              border: 'none',
              color: 'var(--color-text-primary)',
            }}
          >
            {bubbleContent}
          </div>
        )}

        {/* The turn's deliverables, on its last bubble, directly under the
            reply. A long turn names its files somewhere in the prose or only
            inside a tool call, so the deck gathers them where the reader
            finishes reading, with nothing between. */}
        {showTurnFiles && (
          <TurnFileCards
            files={turnFiles}
            onOpenFile={onOpenFile}
            onDownloadFile={onDownloadFile}
            onReveal={() => onRevealFiles?.(message.id as string)}
          />
        )}

        {/* Per-message "⏹ Stopped" marker — the turn was hard-stopped by the
            user (live finalize or replay of a stopped turn). Quiet ink, not
            red: a stop is the user's own action, not a loss/error state. */}
        {isAssistant && (message.stopped as boolean) && (
          <div
            className="inline-flex items-center gap-1.5 self-start mt-1 text-xs"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <StopCircle className="h-3.5 w-3.5 shrink-0" />
            <span>{t('chat.stoppedChip')}</span>
          </div>
        )}

        {/* The assistant's meta row: what the turn drew on, and what you can do
            with it, on one line closing the turn.

            It comes last because the actions are hover-gated but always
            mounted, and a row that reserves space it does not fill is a hole
            wherever it sits mid-turn: between the reply and the deck it read as
            40px of nothing until the pointer arrived. At the end of the turn
            the same reserved space is just the gap before the next message.

            The Sources pill is always visible, while the actions beside it stay
            hover-gated, so the two keep their own opacity. The pill is mounted
            from the first source onward and only faded in once the turn has
            finished: a count that climbs mid-stream reads as activity, but
            unmounting it until then would hop the whole transcript by a line
            the moment the turn ends. While faded it is inert, so the invisible
            button takes neither a click nor a tab stop. Clicking it opens the
            Sources tab in the right panel. */}
        {isAssistant && ((sourceCount > 0 && !isSubagentView) || actionsRow) && (
          <div className={`flex items-center gap-2 ${showTurnFiles ? 'mt-3' : 'mt-1'}`}>
            {sourceCount > 0 && !isSubagentView && (
            <div
              className="transition-opacity duration-200"
              style={{ opacity: turnLive ? 0 : 1 }}
              inert={turnLive}
            >
              <button
                type="button"
                onClick={() => onOpenSources?.(message.id as string)}
                className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors"
                style={{
                  backgroundColor: 'var(--color-bg-elevated)',
                  color: 'var(--color-text-secondary)',
                }}
                title={t('chat.sources.title')}
              >
                <FileSearch className="h-3.5 w-3.5" />
                {t('chat.sources.pill', { count: sourceCount })}
              </button>
            </div>
            )}
            {actionsRow}
          </div>
        )}

        {/* Attachment preview cards -- standalone below the bubble */}
        {hasAttachments && (
          <div className="flex gap-3 overflow-x-auto">
            {attachments!.map((att, idx) => (
              <AttachmentCard key={idx} attachment={att} />
            ))}
          </div>
        )}

        {/* Widget context deck -- stacked chip cards below the bubble,
            mirroring the chat-input deck visuals. Read-only: chips are
            scoped to the message that attached them. */}
        {hasWidgetSnapshots && (
          <InlineWidgetDeck snapshots={widgetSnapshots!} />
        )}

        {/* Chart selection cards -- the regions / price levels the user picked
            on the chart and attached to this send. Read-only summary. */}
        {hasChartSelections && (
          <InlineSelectionCards selections={chartSelections!} />
        )}
        </>
        )}

        {isUser && actionsRow}

        {/* ThumbDown feedback modal */}
        {showThumbDownModal && (
          <ThumbDownModal
            isOpen={showThumbDownModal}
            onSubmit={handleThumbDownSubmit}
            onCancel={() => setShowThumbDownModal(false)}
            onReportWithAgent={onReportWithAgent ? (instruction: string) => {
              setShowThumbDownModal(false);
              onReportWithAgent(instruction);
            } : undefined}
          />
        )}
      </div>
    </div>
  );
});
