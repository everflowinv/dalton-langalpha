import React from 'react';
import { motion } from '@/lib/framer';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useStableArray } from '@/hooks/useStableArray';
import { NotificationDivider } from './messageList/NotificationDivider';
import { WakeupRow } from './messageList/WakeupRow';
import { QuietChecksRow } from './messageList/QuietChecksRow';
import { MessageBubble } from './messageList/MessageBubble';
import { TurnFold } from './messageList/TurnFold';
import { projectMessageContent, visibleProjection } from './messageList/contentProjection';
import { useMessageActions } from './messageList/MessageActionsContext';
import { isReportRow, opensTurn } from './messageList/messagePredicates';
import { ReportRow, type ReportProvenance } from './messageList/ReportRow';
import { HandoffPlacesContext, placeHandoffs, samePlaces } from './analystHandoff/reportLinks';
import {
  computeTurnTails, hasTurn, hiddenOpenerTurns, liveTurns, messageTimeMs, projectTurns,
} from './messageList/turnProjection';
import { planQuietChecks } from './messageList/quietChecks';
import { useLiveMessages, type LiveMessages } from '../session/stream/liveMessages';
import type { ChatMessage, WakeupArrival } from '@/types/chat';
import { turnFilesByTurn } from '../utils/turnFiles';
import { useWorkspaceFolders } from '../contexts/WorkspaceContext';
import type { FeedbackResult, FoldState, MessageRecord } from './messageList/types';

/** What the fold row for one backend turn needs to know about it. */
interface TurnFoldInfo {
  /** Index into the visible list of the turn's FIRST assistant bubble: the row
   *  goes immediately above it, below the message that asked for the turn. */
  rowPos: number;
  startedAt: number;
  completedAt?: number;
  isLive: boolean;
  /** Nothing stands for the turn once folded, so it settles to `process`. */
  noAnswer: boolean;
}

/** The activity accordion's fold spring, so the two levels move alike. */
const FOLD_ROW_ENTER = { type: 'spring' as const, stiffness: 260, damping: 30 };

// --- MessageList ---

interface MessageListProps {
  messages: MessageRecord[];
  isLoading?: boolean;
  isLoadingHistory?: boolean;
  isSubagentView?: boolean;
  readOnly?: boolean;
  allowFiles?: boolean;
  /** Feedback keyed by BACKEND TURN — the projection below maps each bubble to
   *  its turn, so a steering continuation shows the continued turn's rating. */
  feedbackByTurn?: Record<number, FeedbackResult>;
  dispatcherContext?: { threadId: string; workspaceId: string } | null;
  /** A first turn is about to land (the welcome flow's), so an empty list is
   *  not an empty chat and draws no prompt to type. */
  starting?: boolean;
}

function MessageList({ messages, isLoading, isLoadingHistory, isSubagentView, readOnly, allowFiles, feedbackByTurn, dispatcherContext, starting }: MessageListProps): React.ReactElement | null {
  const isMobile = useIsMobile();
  const { onOpenFile } = useMessageActions();
  // The deck keeps the workspace's own notes file out on every surface; its
  // folders, when the host knows them, let it read a path that names the
  // folder, and place one that names a sibling's.
  const folders = useWorkspaceFolders();

  // Session memory, never auto-cleared: a fold the reader opened stays open
  // until they close it or reload. Only the turn they touched is addressed, so
  // a new turn arriving cannot snap an older one shut under them.
  const [expandedTurns, setExpandedTurns] = React.useState<Set<number>>(() => new Set());
  const toggleTurn = React.useCallback((turnIndex: number) => {
    setExpandedTurns((prev) => {
      const next = new Set(prev);
      if (next.has(turnIndex)) next.delete(turnIndex);
      else next.add(turnIndex);
      return next;
    });
  }, []);

  // ONE raw projection pass carries the turn semantics (edit/regenerate/
  // feedback all address backend turns); orphan filtering and the regenerate
  // tail then run over the VISIBLE list, so a hidden bubble never steals an
  // affordance from a painted one.
  const projected = React.useMemo(() => projectTurns(messages), [messages]);
  const visible = React.useMemo(
    () => visibleProjection(projected, isSubagentView, folders?.rootDir),
    [projected, isSubagentView, folders?.rootDir],
  );
  const turnTails = React.useMemo(() => computeTurnTails(visible), [visible]);
  // The bubbles of the server's turns. A send it never stored is painted
  // like any other but has no turn to time, fold, rate or list files under.
  const turned = React.useMemo(() => projected.filter(hasTurn), [projected]);
  // A turn that opened hidden has no message a regenerate could run again.
  const hiddenTurns = React.useMemo(() => hiddenOpenerTurns(turned), [turned]);
  // Retry re-runs the thread's latest attempt and replaces everything from
  // its reply on, so only a reply nothing was sent after offers it.
  const latestReplyId = React.useMemo(() => {
    const last = visible.findLast(({ message }) => message.role !== 'notification')?.message;
    return last?.role === 'assistant' ? last.id : undefined;
  }, [visible]);
  // Each hand-off on screen with the report that answers it, so its slip can
  // link down to that report rather than away to the thread. Kept while it
  // says the same: the list rebuilds on every streamed frame, and a new map
  // each time would redraw every slip with it.
  const placeEntries = useStableArray(React.useMemo(() => placeHandoffs(visible), [visible]), samePlaces);
  const places = React.useMemo(() => new Map(placeEntries), [placeEntries]);

  // The deliverables strip reads the RAW projection: a turn's files are named
  // across its whole span, including a bubble the list never paints.
  const filesByTurn = React.useMemo(
    () => turnFilesByTurn(turned, folders),
    [turned, folders],
  );

  // Only the newest turn can still be running. A bubble's own `isStreaming`
  // is per model call, not per turn: the text handler drops it on every
  // content-free finish_reason and the next chunk re-arms it, so between two
  // model calls of one agent loop it reads false. The session's `isLoading`
  // spans the whole turn, tool execution included, and is the truth here.
  // Read from the raw projection: in that same gap a bubble whose blocks so
  // far are all hidden tools is not painted, and the visible list would hand
  // the running turn's liveness to the one before it.
  const newestTurn = React.useMemo(() => {
    let newest = -1;
    for (const { message, turnIndex } of turned) {
      if ((message.role as string) === 'assistant' && turnIndex > newest) newest = turnIndex;
    }
    return newest;
  }, [turned]);

  const live = React.useMemo(
    () => liveTurns(turned, newestTurn, !!isLoading, (message) => projectMessageContent(message, isSubagentView, folders?.rootDir).hasPinnedLive),
    [turned, newestTurn, isLoading, isSubagentView, folders?.rootDir],
  );

  // Both display preferences fold finished turns; only live reasoning differs.
  // The same pass times every settled turn, whose reply shows when it ended.
  const { turnFolds, turnEnds } = React.useMemo(() => {
    const folds = new Map<number, TurnFoldInfo>();
    const ends = new Map<number, number>();

    const startedAt = new Map<number, number>();
    const firstReplyAt = new Map<number, number>();
    const rowPos = new Map<number, number>();
    const assistantBubbles = new Map<number, MessageRecord[]>();

    // The turn's end is stamped on its tail bubble, and that bubble is not
    // always painted: a steering continuation that settled empty is dropped
    // from `visible` by `isOrphanAssistantMessage` while still carrying the
    // stamp stream finalization and both replay paths wrote to it. Read from
    // the raw list, so a turn that ended on one is still timed. Last write
    // wins, which is the latest stamped bubble of the turn.
    const endStamps = new Map<number, MessageRecord>();
    for (const { message, turnIndex } of turned) {
      if ((message.role as string) !== 'assistant') continue;
      if (message.completedAt !== undefined || message.completionObservedAt !== undefined) {
        endStamps.set(turnIndex, message);
      }
    }

    for (let i = 0; i < visible.length; i++) {
      const { message, turnIndex } = visible[i];
      if (turnIndex === null) continue;
      const role = message.role as string;
      if (role === 'user') {
        // A steering bubble is projected onto the NEXT turn (it lands after the
        // opener already advanced the counter), and on replay its timestamp is
        // not history time. Only a turn's own initiator can start its clock.
        if (!opensTurn(message)) continue;
        // Of two openers on one count the later is the turn's: the row and
        // the clock start over there.
        const ts = messageTimeMs(message.timestamp);
        if (ts !== undefined) startedAt.set(turnIndex, ts);
        else startedAt.delete(turnIndex);
        rowPos.delete(turnIndex);
        firstReplyAt.delete(turnIndex);
        assistantBubbles.delete(turnIndex);
        continue;
      }
      if (role !== 'assistant') continue;
      if (!rowPos.has(turnIndex)) rowPos.set(turnIndex, i);
      if (!firstReplyAt.has(turnIndex)) {
        const ts = messageTimeMs(message.timestamp);
        if (ts !== undefined) firstReplyAt.set(turnIndex, ts);
      }
      const bubbles = assistantBubbles.get(turnIndex);
      if (bubbles) bubbles.push(message);
      else assistantBubbles.set(turnIndex, [message]);
    }

    // A turn with no row of its own to take its clock from (a report-back
    // whose row names no source, or one attached live) starts at its first
    // reply, the nearest thing.
    for (const [turnIndex, ts] of firstReplyAt) {
      if (!startedAt.has(turnIndex)) startedAt.set(turnIndex, ts);
    }

    for (let i = 0; i < visible.length; i++) {
      // The turn's end is stamped on its tail bubble, which is also the only
      // bubble that can be streaming once the turn has several.
      if (!turnTails[i]) continue;
      const { message, turnIndex } = visible[i];
      if (turnIndex === null) continue;
      const contents = (assistantBubbles.get(turnIndex) ?? []).map((bubble) => projectMessageContent(bubble, isSubagentView, folders?.rootDir));
      const isLive = live.has(turnIndex);
      // A pinned call outlives the stream that launched it, so the stream-close
      // stamp was taken before the turn's work was done and a fold reading it
      // counts backwards, from `Working for 2m` to `Worked for 5s`. The server's
      // own stamp still wins outright; this only sharpens the local fallback.
      const endStamp = endStamps.get(turnIndex) ?? message;
      const observedEnd = messageTimeMs(endStamp.completionObservedAt);
      const localEnd = contents.reduce<number | undefined>(
        (end, content) => (content.pinnedSettledAt !== null && (end === undefined || content.pinnedSettledAt > end)
          ? content.pinnedSettledAt
          : end),
        observedEnd,
      );
      const completedAt = isLive ? undefined : messageTimeMs(endStamp.completedAt) ?? localEnd;
      if (completedAt !== undefined) ends.set(turnIndex, completedAt);
      const start = startedAt.get(turnIndex);
      const pos = rowPos.get(turnIndex);
      if (start === undefined || pos === undefined) continue;
      if (!contents.some((content) => content.hasProcess) && contents.reduce((sum, content) => sum + content.textCount, 0) < 2) continue;
      const hasFiles = !!onOpenFile && (!readOnly || allowFiles) && (filesByTurn.get(turnIndex)?.length ?? 0) > 0;
      folds.set(turnIndex, {
        noAnswer: !(contents.at(-1)?.lastTextKey || contents.some((content) => content.hasRetained) || hasFiles),
        rowPos: pos,
        startedAt: start,
        completedAt,
        isLive,
      });
    }
    return { turnFolds: folds, turnEnds: ends };
  }, [visible, turned, turnTails, live, isSubagentView, folders?.rootDir, filesByTurn, onOpenFile, readOnly, allowFiles]);

  const items = React.useMemo(
    () => planQuietChecks(visible, turned, live, isSubagentView),
    [visible, turned, live, isSubagentView],
  );

  // Empty state - show when no messages exist (hidden in subagent view)
  if (messages.length === 0) {
    if (isSubagentView || starting) return null;
    if (isLoadingHistory) {
      return (
        <div className="space-y-6 py-4 animate-pulse">
          {/* User message skeleton */}
          <div className="flex justify-end">
            <div className="rounded-2xl" style={{ background: 'var(--color-border-muted)', width: '55%', height: 40 }} />
          </div>
          {/* Assistant message skeleton */}
          <div className="flex">
            <div className="flex-1 space-y-3">
              <div className="rounded" style={{ background: 'var(--color-border-muted)', width: '80%', height: 14 }} />
              <div className="rounded" style={{ background: 'var(--color-border-muted)', width: '65%', height: 14 }} />
              <div className="rounded" style={{ background: 'var(--color-border-muted)', width: '40%', height: 14 }} />
            </div>
          </div>
          {/* Second user message skeleton */}
          <div className="flex justify-end">
            <div className="rounded-2xl" style={{ background: 'var(--color-border-muted)', width: '40%', height: 40 }} />
          </div>
          {/* Second assistant skeleton */}
          <div className="flex">
            <div className="flex-1 space-y-3">
              <div className="rounded" style={{ background: 'var(--color-border-muted)', width: '90%', height: 14 }} />
              <div className="rounded" style={{ background: 'var(--color-border-muted)', width: '70%', height: 14 }} />
            </div>
          </div>
        </div>
      );
    }
    return (
      <div className="flex flex-col items-center justify-center min-h-full py-12">
        <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
          Start a conversation by typing a message below
        </p>
      </div>
    );
  }

  // The host renders DispatchStatusProvider above the list, so every
  // hand-off in it shares one batched dispatch-liveness query and timer.
  // Here it re-rendered, and re-ran its query hook, on every streamed frame.
  // sibling-space-y, not space-y: NotificationDivider's own my-1 would
  // otherwise replace the gap below it.
  const list = (
    <div className={`font-content ${isMobile ? 'sibling-space-y-4' : 'sibling-space-y-6'}`}>
      {items.map((item) => {
        if (item.kind === 'quiet') {
          return <QuietChecksRow key={`quiet-checks-${item.checks[0].turnIndex}`} checks={item.checks} />;
        }
        const i = item.index;
        const { message, turnIndex } = visible[i];
        if ((message.role as string) === 'notification') {
          const wakeup = message.wakeup as WakeupArrival | undefined;
          return wakeup
            ? <WakeupRow key={message.id as string} wakeup={wakeup} />
            : <NotificationDivider key={message.id as string} message={message} />;
        }
        if (isReportRow(message)) {
          const prev = visible[i - 1]?.message;
          return (
            <ReportRow
              key={message.id as string}
              id={message.id as string}
              provenance={message.provenance as ReportProvenance}
              together={!!prev && isReportRow(prev)}
              readOnly={readOnly}
              dispatcherContext={dispatcherContext}
            />
          );
        }
        // A bubble only ever folds when its turn shows the row that unfolds it:
        // a turn with no fold row (no initiating message, nothing foldable yet)
        // keeps the verbose shape rather than hiding prose behind nothing.
        const info = turnIndex === null ? undefined : turnFolds.get(turnIndex);
        const fold: FoldState = !info
          ? 'unfolded'
          : info.isLive
            ? 'live'
            : turnIndex !== null && expandedTurns.has(turnIndex) ? 'expanded' : info.noAnswer ? 'process' : 'collapsed';
        const bubbleProps = {
          message,
          contentProjection: projectMessageContent(message, isSubagentView, folders?.rootDir),
          turnIndex,
          isTurnTail: turnTails[i],
          canRegenerate: turnIndex === null || !hiddenTurns.has(turnIndex),
          isLatestReply: message.id === latestReplyId,
          // The fold already decided whether this turn is working, and that
          // decision now includes a background task outliving its stream.
          // Recomputing the older expression here let the bubble call the
          // turn settled while the row above it still read `Working for`,
          // which published sources, deliverables and the rating controls
          // over results the running task can still change.
          isTurnLive: info
            ? info.isLive
            : (turnIndex !== null && live.has(turnIndex)) || !!message.isStreaming,
          turnFiles: turnTails[i] && turnIndex !== null ? filesByTurn.get(turnIndex) : undefined,
          completedAt: turnTails[i] && turnIndex !== null ? turnEnds.get(turnIndex) : undefined,
          feedback: turnIndex === null ? null : feedbackByTurn?.[turnIndex] ?? null,
          isLoading,
          fold,
          isSubagentView,
          readOnly,
          allowFiles,
          isMobile,
          dispatcherContext,
        };
        // Every bubble gets the wrapper from its first paint: the fold row
        // only exists once the turn has something foldable, and switching a
        // streaming bubble from bare to wrapped at the same key would remount it.
        // The row and the answer it heads are one item of the list's rhythm, so
        // the gap between them is the row's own margin, not the list's.
        return (
          <div key={message.id as string}>
            {info && turnIndex !== null && info.rowPos === i && (
              /* A row that arrives over a turn already streaming unfolds
                 rather than pushing the reply down a line in one paint. A
                 settled turn's row was there before the reader was. */
              <motion.div
                initial={info.isLive ? { height: 0, opacity: 0 } : false}
                animate={{ height: 'auto', opacity: 1 }}
                transition={FOLD_ROW_ENTER}
                style={{ overflow: 'hidden' }}
              >
                <TurnFold
                  state={fold === 'live' ? 'live' : fold === 'collapsed' ? 'collapsed' : fold === 'process' ? 'process' : 'expanded'}
                  startedAt={info.startedAt}
                  completedAt={info.completedAt}
                  onToggle={() => toggleTurn(turnIndex)}
                />
              </motion.div>
            )}
            <MessageBubble {...bubbleProps} />
          </div>
        );
      })}
    </div>
  );
  return <HandoffPlacesContext value={places}>{list}</HandoffPlacesContext>;
}

/**
 * The list as of the latest streamed chunk. A component of its own so that a
 * chunk renders the list and not the view that placed it.
 */
export function LiveMessageList({ store, ...props }: Omit<MessageListProps, 'messages'> & { store: LiveMessages<ChatMessage[]> }) {
  const messages = useLiveMessages(store);
  // The session types its messages; the list reads them as records.
  return <MessageList messages={messages} {...props} />;
}

export default MessageList;
export { MessageContentSegments } from './messageList/MessageContentSegments';
// eslint-disable-next-line react-refresh/only-export-components
export { normalizeSubagentText } from './messageList/normalizeSubagentText';
// eslint-disable-next-line react-refresh/only-export-components
export { isOrphanAssistantMessage } from './messageList/contentProjection';
