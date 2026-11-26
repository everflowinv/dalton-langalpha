/**
 * Personal Watchlist HITL interrupt projection & settlement helpers (Dalton-owned).
 * Keeps upstream `buckets.ts`, `fromLiveEvent.ts`, and `fromHistoryEvent.ts` to thin 1-5 line hooks.
 */

import type { AssistantMessage } from '@/types/chat';
import type {
  WatchlistProposalActionType,
  WatchlistProposalState,
} from '@/lib/dalton/watchlistChatTypes';

export const WATCHLIST_INTERRUPT_TYPES = new Set<string>([
  'watchlist_batch_add',
  'watchlist_set_tier',
  'watchlist_remove',
  'watchlist_tier_policy',
]);

export function isWatchlistInterruptType(actionType: string | undefined): actionType is WatchlistProposalActionType {
  return Boolean(actionType && WATCHLIST_INTERRUPT_TYPES.has(actionType));
}

export function buildWatchlistProposalState(
  actionType: WatchlistProposalActionType,
  proposalData: Record<string, unknown>,
  interruptId?: string,
): { state: WatchlistProposalState; toolCallId: string; hasToolCallId: boolean } {
  const toolCallId = typeof proposalData.tool_call_id === 'string' ? proposalData.tool_call_id : '';
  const hasToolCallId = toolCallId.length > 0;
  const state: WatchlistProposalState = {
    actionType,
    raw_action: typeof proposalData.raw_action === 'string' ? proposalData.raw_action : undefined,
    name: typeof proposalData.name === 'string' ? proposalData.name : undefined,
    description: typeof proposalData.description === 'string' ? proposalData.description : undefined,
    owner_user_id: typeof proposalData.owner_user_id === 'string' ? proposalData.owner_user_id : undefined,
    expected_roster_hash:
      typeof proposalData.expected_roster_hash === 'string' ? proposalData.expected_roster_hash : undefined,
    expected_policy_versions: proposalData.expected_policy_versions as Record<string, number> | undefined,
    target_tickers: Array.isArray(proposalData.target_tickers)
      ? (proposalData.target_tickers as string[])
      : undefined,
    target_tier: typeof proposalData.target_tier === 'string' ? proposalData.target_tier : null,
    affected_tickers: Array.isArray(proposalData.affected_tickers)
      ? (proposalData.affected_tickers as WatchlistProposalState['affected_tickers'])
      : undefined,
    affected_workspaces: Array.isArray(proposalData.affected_workspaces)
      ? (proposalData.affected_workspaces as WatchlistProposalState['affected_workspaces'])
      : undefined,
    task_summary: proposalData.task_summary as WatchlistProposalState['task_summary'],
    preserve_workspace_notice:
      typeof proposalData.preserve_workspace_notice === 'string'
        ? proposalData.preserve_workspace_notice
        : null,
    group_id: typeof proposalData.group_id === 'string' ? proposalData.group_id : null,
    group_label: typeof proposalData.group_label === 'string' ? proposalData.group_label : null,
    tool_call_id: toolCallId || undefined,
    interruptId,
    status: hasToolCallId ? 'pending' : 'failed',
    ...(hasToolCallId
      ? {}
      : {
          failure_code: 'scope_changed',
          failure_error: '情况已变化，请重新发起',
        }),
  };
  return { state, toolCallId, hasToolCallId };
}

export function extractWatchlistOutcomeExtras(
  baseOutcome: { status: string; failure_code?: string; failure_error?: string },
  content: unknown,
): Record<string, unknown> {
  if (baseOutcome.status !== 'approved' || typeof content !== 'string') {
    return baseOutcome;
  }
  try {
    const parsed: unknown = JSON.parse(content);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const obj = parsed as Record<string, unknown>;
      return {
        ...baseOutcome,
        ...(typeof obj.reconcile_status === 'string' ? { reconcile_status: obj.reconcile_status } : {}),
        ...(typeof obj.job_id === 'string' ? { job_id: obj.job_id } : {}),
      };
    }
  } catch {
    // Keep base outcome.
  }
  return baseOutcome;
}

export function settleWatchlistProposalsBucket<TResult>(
  proposals: Record<string, WatchlistProposalState> | undefined,
  toolCallId: string,
  result: TResult,
  resolveFields: (actionType: string, res: TResult) => Partial<WatchlistProposalState>,
): { next: Record<string, WatchlistProposalState> | undefined; changed: boolean } {
  if (!proposals) return { next: proposals, changed: false };
  let changed = false;
  const updated: Record<string, WatchlistProposalState> = { ...proposals };
  for (const [cardId, card] of Object.entries(proposals)) {
    if (
      card &&
      card.tool_call_id === toolCallId &&
      (card.status === 'submitting' || card.status === 'pending')
    ) {
      updated[cardId] = {
        ...card,
        ...resolveFields(card.actionType, result),
      };
      changed = true;
    }
  }
  return { next: changed ? updated : proposals, changed };
}

export function applyWatchlistProposalToMessage(
  msg: AssistantMessage,
  actionType: WatchlistProposalActionType,
  proposalId: string,
  order: number,
  state: WatchlistProposalState,
  appendSegment: (
    segments: AssistantMessage['contentSegments'],
    seg: { type: WatchlistProposalActionType; proposalId: string; order: number },
  ) => AssistantMessage['contentSegments'],
  extraFlags?: Partial<AssistantMessage>,
): AssistantMessage {
  return {
    ...msg,
    contentSegments: appendSegment(msg.contentSegments, {
      type: actionType,
      proposalId,
      order,
    }),
    watchlistProposals: {
      ...(msg.watchlistProposals || {}),
      [proposalId]: state,
    },
    ...(extraFlags || {}),
  };
}
