/**
 * Shared interrupt descriptors — the one place the HITL card vocabulary is
 * declared. Both projections (live stream and history replay) key off these
 * tables; the dedup rebuild in messageFinalizers walks INTERRUPT_CARD_BUCKETS.
 * A new interrupt type registers here first, then adds its projection branches.
 */

// --- DALTON-HOOK(cos-settings-tools): begin ---
import type {
  AssistantMessage,
  ChatMessage,
  SecretaryActionProposalState,
  WatchlistProposalState, // DALTON-HOOK(watchlist)
  WorkspaceProposalState,
} from '@/types/chat';
// --- DALTON-HOOK(cos-settings-tools): end ---
import { updateMessage } from '../../hooks/utils/messageHelpers';
import { dispatchResultFields, type ToolResultLike } from '../../utils/dispatchResult';
import type { HistoryInterruptInfo, SetMessages } from '../types';

import {
  WATCHLIST_INTERRUPT_TYPES,
  extractWatchlistOutcomeExtras,
  isWatchlistDeclineContent,
  settleWatchlistProposalsBucket,
} from '@/lib/dalton/watchlistInterrupts'; // DALTON-HOOK(watchlist)

/** Interrupt types that map to proposal-based HITL cards (workspace, question, ptc, secretary). */
const PROPOSAL_INTERRUPT_TYPES = new Set([
  'create_workspace', 'start_question', 'ptc_agent',
  'delete_workspace', 'stop_workspace', 'delete_thread',
  // DALTON-HOOK(watchlist): personal watchlist mutation cards
  'watchlist_batch_add', 'watchlist_set_tier', 'watchlist_remove', 'watchlist_tier_policy',
]);

/** Maps interrupt types to their proposal bucket key on AssistantMessage. */
const PROPOSAL_DATA_KEY_MAP: Record<string, keyof AssistantMessage> = {
  create_workspace: 'workspaceProposals',
  start_question: 'questionProposals',
  ptc_agent: 'ptcAgentProposals',
  delete_workspace: 'secretaryActionProposals',
  stop_workspace: 'secretaryActionProposals',
  delete_thread: 'secretaryActionProposals',
  // DALTON-HOOK(watchlist): map watchlist confirmation interrupts to watchlistProposals bucket
  watchlist_batch_add: 'watchlistProposals',
  watchlist_set_tier: 'watchlistProposals',
  watchlist_remove: 'watchlistProposals',
  watchlist_tier_policy: 'watchlistProposals',
};

/**
 * The card bucket each pending interrupt settles into, keyed by the type its
 * pending entry carries — which, unlike the action request's `type`, names a
 * tool approval too, so there is no fallback case to get wrong. Typed against
 * `AssistantMessage` so a renamed bucket fails here rather than writing a card
 * under a name nothing renders.
 */
const CARD_BUCKET_FOR_TYPE: Record<string, keyof AssistantMessage> = {
  ask_user_question: 'userQuestions',
  credit_pause: 'creditPauses',
  tool_approval: 'toolApprovals',
  ...PROPOSAL_DATA_KEY_MAP,
};

/**
 * Message-map buckets whose entries carry an `interruptId` (rendered HITL
 * cards). `satisfies keyof AssistantMessage` makes a renamed/added bucket a
 * compile error here instead of a silently-wrong dedup rebuild.
 */
const INTERRUPT_CARD_BUCKETS = [
  'userQuestions', 'workspaceProposals', 'questionProposals',
  'ptcAgentProposals', 'secretaryActionProposals',
  'watchlistProposals', // DALTON-HOOK(watchlist)
  'creditPauses', 'toolApprovals',
] as const satisfies readonly (keyof AssistantMessage)[];

/**
 * Merge fields into one card wherever it lives. A deduped re-raise can put the
 * card on a bubble other than the one the answer came from, so every assistant
 * message is searched rather than the one that was clicked.
 */
function setCardFields(
  messages: ChatMessage[],
  bucket: string,
  cardId: string,
  fields: Record<string, unknown>,
): ChatMessage[] {
  return messages.map((m) => {
    if (m.role !== 'assistant') return m;
    const cards = (m as unknown as Record<string, Record<string, Record<string, unknown>>>)[bucket];
    if (!cards?.[cardId]) return m;
    return { ...(m as AssistantMessage), [bucket]: { ...cards, [cardId]: { ...cards[cardId], ...fields } } };
  });
}

/** Flip one card's status wherever it lives. */
function setCardStatus(
  messages: ChatMessage[],
  bucket: string,
  cardId: string,
  status: string,
): ChatMessage[] {
  return setCardFields(messages, bucket, cardId, { status });
}

/**
 * Drop the history copies of these cards from every bubble, so a reconnect
 * stream's redelivery of the same interrupt is the only copy on screen.
 *
 * By card id, never by the bubble a pending entry names: a re-raised interrupt
 * restores its cards where they were first rendered while its entries are
 * queued against the turn that re-raised them, and a bubble-scoped strip would
 * leave those cards standing next to the redelivered pair.
 */
function stripHistoryInterruptCards(
  messages: ChatMessage[],
  strips: HistoryInterruptInfo[],
): ChatMessage[] {
  // Question and proposal ids come from distinct interrupts, so one set can
  // hold both: only card segments carry either id.
  const ids = new Set(strips.map(historyCardKey).filter((id): id is string => !!id));
  if (ids.size === 0) return messages;
  return messages.map((m) => {
    if (m.role !== 'assistant') return m;
    const msg = m as AssistantMessage;
    const next: AssistantMessage = {
      ...msg,
      contentSegments: (msg.contentSegments || []).filter((seg) => {
        const { questionId, proposalId } = seg as { questionId?: string; proposalId?: string };
        const id = questionId ?? proposalId;
        return !id || !ids.has(id);
      }),
    };
    for (const key of INTERRUPT_CARD_BUCKETS) {
      const bucket = msg[key];
      if (!bucket) continue;
      const map = { ...(bucket as Record<string, unknown>) };
      for (const id of ids) delete map[id];
      (next as unknown as Record<string, unknown>)[key] = map;
    }
    return next;
  });
}

/**
 * The card id a pending history interrupt settles under. Each family fills
 * exactly one of these at queue time, so the first one present is its key.
 */
function historyCardKey(info: HistoryInterruptInfo): string | undefined {
  return info.proposalId ?? info.questionId;
}

/** Which card a resolved history interrupt writes to, and what it writes. */
interface HistoryCardPatch {
  bucket: string;
  key: string;
  fields: Record<string, unknown>;
}

/**
 * Settle the first pending history interrupt `match` accepts: merge its patch
 * into the card and drop it from the pending list, so what remains at the end
 * of replay is exactly what the user still owes an answer.
 */
function resolvePendingHistoryInterrupt(
  pending: HistoryInterruptInfo[],
  match: (p: HistoryInterruptInfo) => boolean,
  toPatch: (matched: HistoryInterruptInfo) => HistoryCardPatch | null,
  setMessages: SetMessages,
): boolean {
  const idx = pending.findIndex(match);
  if (idx === -1) return false;
  const matched = pending[idx];
  // A caller that matches an entry but declines to patch it leaves the card
  // pending and the entry queued, so replay still ends holding what is owed.
  const patch = toPatch(matched);
  if (!patch) return false;
  const { bucket, key, fields } = patch;
  setMessages((prev) =>
    updateMessage(prev, matched.assistantMessageId, (m) => {
      if (m.role !== 'assistant') return m;
      const msg = m as AssistantMessage;
      const cards = ((msg as unknown as Record<string, unknown>)[bucket] || {}) as Record<string, Record<string, unknown>>;
      return { ...msg, [bucket]: { ...cards, [key]: { ...(cards[key] || {}), ...fields } } };
    })
  );
  pending.splice(idx, 1);
  return true;
}

// --- DALTON-HOOK(cos-settings-tools): begin ---
type ProposalOutcomeFields = {
  status: 'approved' | 'rejected' | 'failed';
  failure_code?: string;
  failure_error?: string;
};

type WorkspaceProposalOutcomeFields = {
  status: 'approved' | 'rejected' | 'failed';
  failure_code?: string;
  failure_error?: string;
  computer_name?: string;
  bootstrap_cloud_primary?: boolean;
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function proposalOutcomeFields(
  type: string,
  result: ToolResultLike & { status?: unknown },
): ProposalOutcomeFields {
  const { content, status: toolStatus } = result;
  // DALTON-HOOK(watchlist): also recognize legacy Chinese watchlist cancel text as rejected
  if (typeof content === 'string' && (content.startsWith('User declined') || isWatchlistDeclineContent(content))) {
    return { status: 'rejected', failure_code: undefined, failure_error: undefined };
  }
  const isToolError = toolStatus === 'error';
  // DALTON-HOOK(watchlist): personal watchlist confirmation cards require explicit JSON success/error settlement
  const strictJsonOutcome =
    type === 'create_workspace' || type === 'archive_workspace' || WATCHLIST_INTERRUPT_TYPES.has(type);
  if (typeof content !== 'string') {
    return isToolError || strictJsonOutcome
      ? { status: 'failed' }
      : { status: 'approved', failure_code: undefined, failure_error: undefined };
  }
  const trimmed = content.trim();
  try {
    const parsed: unknown = JSON.parse(content);
    if (isPlainObject(parsed)) {
      if (parsed.success === false || isToolError) {
        return {
          status: 'failed',
          ...(typeof parsed.code === 'string' && parsed.code ? { failure_code: parsed.code } : {}),
          ...(typeof parsed.error === 'string' && parsed.error
            ? { failure_error: parsed.error }
            : isToolError && trimmed
              ? { failure_error: trimmed }
              : {}),
        };
      }
      if (parsed.success === true) {
        return { status: 'approved', failure_code: undefined, failure_error: undefined };
      }
      if (strictJsonOutcome) {
        return {
          status: 'failed',
          ...(typeof parsed.code === 'string' && parsed.code ? { failure_code: parsed.code } : {}),
          ...(typeof parsed.error === 'string' && parsed.error ? { failure_error: parsed.error } : {}),
        };
      }
    }
  } catch {
    if (isToolError || strictJsonOutcome) {
      return {
        status: 'failed',
        ...(trimmed ? { failure_error: trimmed } : {}),
      };
    }
  }
  if (isToolError || strictJsonOutcome) {
    return {
      status: 'failed',
      ...(trimmed ? { failure_error: trimmed } : {}),
    };
  }
  return { status: 'approved', failure_code: undefined, failure_error: undefined };
}

function workspaceProposalResultFields(
  result: ToolResultLike & { status?: unknown },
): WorkspaceProposalOutcomeFields {
  const outcome = proposalOutcomeFields('create_workspace', result);
  if (outcome.status !== 'approved' || typeof result.content !== 'string') {
    return outcome;
  }
  try {
    const parsed: unknown = JSON.parse(result.content);
    if (isPlainObject(parsed)) {
      return {
        ...outcome,
        ...(typeof parsed.computer_name === 'string' && parsed.computer_name
          ? { computer_name: parsed.computer_name }
          : {}),
        ...(parsed.bootstrap_cloud_primary === true ? { bootstrap_cloud_primary: true } : {}),
      };
    }
  } catch {
    // Already parsed successfully in proposalOutcomeFields; keep outcome as-is.
  }
  return outcome;
}

/** Derive settled card fields (approved / rejected / failed) from a tool result.
 *  Shared by both live stream settlement and history replay so both views agree.
 *  A tool result with `status === 'error'` or non-JSON / unparseable payload on
 *  workspace/archive confirmation cards settles as `failed`, never `approved`. */
function proposalResultFields(
  type: string,
  result: ToolResultLike & { status?: unknown },
): Record<string, unknown> {
  if (type === 'ptc_agent') {
    if (typeof result.content === 'string' && result.content.startsWith('User declined')) {
      return { status: 'rejected', failure_code: undefined, failure_error: undefined };
    }
    return { status: 'approved', ...dispatchResultFields(result) };
  }
  if (type === 'create_workspace') {
    return workspaceProposalResultFields(result);
  }
  // DALTON-HOOK(watchlist): enrich watchlist confirmation outcome with reconcile_status / job_id
  if (WATCHLIST_INTERRUPT_TYPES.has(type)) {
    return extractWatchlistOutcomeExtras(proposalOutcomeFields(type, result), result.content);
  }
  return proposalOutcomeFields(type, result);
}

/** Settle a live submitting/pending proposal card by exact ``tool_call_id``.
 *  Returns ``messages`` unchanged (same reference) when no card matches. */
function settleLiveProposalFromResult(
  messages: ChatMessage[],
  toolCallId: string | undefined,
  result: ToolResultLike & { status?: unknown },
): ChatMessage[] {
  if (!toolCallId || (typeof result.content !== 'string' && result.status !== 'error')) return messages;
  let changed = false;
  const next: ChatMessage[] = messages.map((m): ChatMessage => {
    if (m.role !== 'assistant') return m;
    let msgChanged = false;
    let nextWorkspaceProposals = m.workspaceProposals;
    if (m.workspaceProposals) {
      let bucketChanged = false;
      const updatedCards: Record<string, WorkspaceProposalState> = { ...m.workspaceProposals };
      for (const [cardId, card] of Object.entries(m.workspaceProposals)) {
        if (
          card
          && card.tool_call_id === toolCallId
          && (card.status === 'submitting' || card.status === 'pending')
        ) {
          updatedCards[cardId] = { ...card, ...workspaceProposalResultFields(result) };
          bucketChanged = true;
        }
      }
      if (bucketChanged) {
        nextWorkspaceProposals = updatedCards;
        msgChanged = true;
      }
    }
    let nextSecretaryProposals = m.secretaryActionProposals;
    if (m.secretaryActionProposals) {
      let bucketChanged = false;
      const updatedCards: Record<string, SecretaryActionProposalState> = { ...m.secretaryActionProposals };
      for (const [cardId, card] of Object.entries(m.secretaryActionProposals)) {
        if (
          card
          && card.tool_call_id === toolCallId
          && (card.status === 'submitting' || card.status === 'pending')
        ) {
          updatedCards[cardId] = {
            ...card,
            ...proposalOutcomeFields(card.actionType || 'delete_workspace', result),
          };
          bucketChanged = true;
        }
      }
      if (bucketChanged) {
        nextSecretaryProposals = updatedCards;
        msgChanged = true;
      }
    }
    // DALTON-HOOK(watchlist): settle matching personal watchlist confirmation cards by tool_call_id
    const wlSettled = settleWatchlistProposalsBucket(
      m.watchlistProposals,
      toolCallId,
      result,
      (t, r) => proposalResultFields(t || 'watchlist_set_tier', r) as Partial<WatchlistProposalState>,
    );
    const nextWatchlistProposals = wlSettled.next;
    if (wlSettled.changed) msgChanged = true;
    if (!msgChanged) return m;
    changed = true;
    const updatedMsg: AssistantMessage = {
      ...m,
      ...(nextWorkspaceProposals ? { workspaceProposals: nextWorkspaceProposals } : {}),
      ...(nextSecretaryProposals ? { secretaryActionProposals: nextSecretaryProposals } : {}),
      ...(nextWatchlistProposals ? { watchlistProposals: nextWatchlistProposals } : {}), // DALTON-HOOK(watchlist)
    };
    return updatedMsg;
  });
  return changed ? next : messages;
}
// --- DALTON-HOOK(cos-settings-tools): end ---

/**
 * Settle the pending proposal a tool result answers. A decline rejects it, a
 * result with `success: false` on any card but a hand-off's marks it failed
 * (preserving `failure_code` / `failure_error` such as `scope_changed`);
 * anything else approves it. One resume can answer several interrupts, and
 * their results need not arrive in the order the cards did, so an interrupt
 * that named its tool call is settled only by that call's result; the rest
 * take the next result in order.
 */
function settleProposalFromResult(
  pending: HistoryInterruptInfo[],
  toolCallId: string | undefined,
  result: ToolResultLike,
  setMessages: SetMessages,
): boolean {
  const { content } = result;
  if (typeof content !== 'string') return false;
  const named = !!toolCallId && pending.some((p) => p.toolCallId === toolCallId);
  return resolvePendingHistoryInterrupt(
    pending,
    // DALTON-HOOK(cos-settings-tools): create_workspace settles only by exact tool_call_id (never positional fallback)
    // DALTON-HOOK(watchlist): watchlist confirmation cards also settle strictly by exact tool_call_id
    (p) => PROPOSAL_INTERRUPT_TYPES.has(p.type) && (
      p.type === 'create_workspace' || WATCHLIST_INTERRUPT_TYPES.has(p.type)
        ? (!!toolCallId && p.toolCallId === toolCallId)
        : (named ? p.toolCallId === toolCallId : !toolCallId || !p.toolCallId)
    ),
    // Parsed here so a result pays for it only when it settles a proposal.
    (m) => ({
      bucket: PROPOSAL_DATA_KEY_MAP[m.type] || 'questionProposals',
      key: m.proposalId!,
      // DALTON-HOOK(cos-settings-tools): share 4-state settlement (approved / rejected / failed)
      fields: proposalResultFields(m.type, result),
    }),
    setMessages,
  );
}

export {
  WATCHLIST_INTERRUPT_TYPES, // DALTON-HOOK(watchlist)
  PROPOSAL_INTERRUPT_TYPES, PROPOSAL_DATA_KEY_MAP,
  INTERRUPT_CARD_BUCKETS, CARD_BUCKET_FOR_TYPE,
  setCardStatus, setCardFields, resolvePendingHistoryInterrupt, historyCardKey,
  proposalResultFields, settleProposalFromResult, settleLiveProposalFromResult,
  stripHistoryInterruptCards,
};
