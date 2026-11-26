/**
 * Live-stream interrupt projection: renders the HITL card for an `interrupt`
 * SSE event onto the streaming assistant bubble and arms the pending-interrupt
 * state. Stream-lifecycle effects (loading flag, ownership release) stay with
 * the router; history replay has its own projection in fromHistoryEvent.
 */

import type { AssistantMessage, WatchlistProposalActionType } from '@/types/chat'; // DALTON-HOOK(watchlist)
import { updateMessage } from '../../hooks/utils/messageHelpers';
import { setCardStatus, WATCHLIST_INTERRUPT_TYPES } from './buckets'; // DALTON-HOOK(watchlist)
import { buildCreditPauseState } from './creditPauseCard';
import { isToolApprovalInterrupt, toolApprovalCards } from './toolApprovalCard';
import type { PendingInterrupt, SSEEvent, StreamProcessorRefs } from '../types';
import type { StreamRuntime } from '../runtime';
import { eventOrder } from '../eventOrder';

export function projectLiveInterrupt(
  rt: StreamRuntime,
  event: SSEEvent,
  assistantMessageId: string,
  refs: StreamProcessorRefs,
): void {
  const actionRequests = event.action_requests || [];
  const actionType = actionRequests[0]?.type as string | undefined;

  // A still-pending interrupt re-raised after a HITL resume streams into a
  // fresh `assistant-hitl-*` bubble with the same interrupt_id. Suppress the
  // duplicate CARD (the segment push) while keeping the map write + pending
  // tracking below, so the original card stays answerable and the resume's
  // pending set (cleared at resume start) still re-tracks it.
  const interruptAlreadyRendered = event.interrupt_id
    ? rt.renderedInterruptIdsRef.current.has(event.interrupt_id)
    : false;
  if (event.interrupt_id) rt.renderedInterruptIdsRef.current.add(event.interrupt_id);
  // Every branch below MUST push its card segment through this helper so
  // the re-raise suppression can't be forgotten on a future interrupt type.
  const appendCardSegment = (
    segs: AssistantMessage['contentSegments'] | undefined,
    seg: AssistantMessage['contentSegments'][number],
  ): AssistantMessage['contentSegments'] =>
    interruptAlreadyRendered ? (segs || []) : [...(segs || []), seg];
  // The run streaming now raised the pause.
  const arm = (pending: PendingInterrupt): void => {
    rt.pause.arm({ ...pending, runId: rt.currentRunIdRef.current }, [event.interrupt_id]);
  };

  if (actionType === 'ask_user_question') {
    // --- User question interrupt ---
    const questionId = event.interrupt_id || `question-${Date.now()}`;
    const questionData = actionRequests[0];
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);

    rt.setMessages((prev) =>
      updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
        ...msg,
        contentSegments: appendCardSegment(msg.contentSegments, { type: 'user_question', questionId, order }),
        userQuestions: {
          ...(msg.userQuestions || {}),
          [questionId]: {
            question: questionData.question,
            options: questionData.options || [],
            allow_multiple: questionData.allow_multiple || false,
            interruptId: event.interrupt_id,
            status: 'pending',
            answer: null,
          },
        },
        isStreaming: false,
      }; })
    );

    arm({
      type: 'ask_user_question',
      interruptId: event.interrupt_id,
      assistantMessageId,
      questionId,
    });
  } else if (actionType === 'create_workspace') {
    // --- Create workspace interrupt ---
    const proposalId = event.interrupt_id || `workspace-${Date.now()}`;
    const proposalData = actionRequests[0];
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);
    // DALTON-HOOK(cos-settings-tools): pre-upgrade create_workspace cards without tool_call_id settle directly as scope_changed
    const hasToolCallId = typeof proposalData.tool_call_id === 'string' && proposalData.tool_call_id.length > 0;

    // --- DALTON-HOOK(cos-settings-tools): begin ---
    // A re-raised create_workspace interrupt (e.g. failed resume + /retry)
    // means the pause was never consumed while the original card is stuck in
    // `submitting`. Restore the originally rendered card by interrupt_id back
    // to `pending` and continue suppressing duplicate rendering on this bubble.
    rt.setMessages((prev) =>
      interruptAlreadyRendered
        ? (hasToolCallId ? setCardStatus(prev, 'workspaceProposals', proposalId, 'pending') : prev)
        : updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
            ...msg,
            contentSegments: appendCardSegment(msg.contentSegments, { type: 'create_workspace', proposalId, order }),
            workspaceProposals: {
              ...(msg.workspaceProposals || {}),
              [proposalId]: {
                workspace_name: proposalData.workspace_name,
                workspace_description: proposalData.workspace_description,
                // DALTON-HOOK(workspace-duplicate-guard): name the workspace this create would duplicate
                duplicate_workspace_id: proposalData.duplicate_workspace_id,
                duplicate_workspace_name: proposalData.duplicate_workspace_name,
                duplicate_check_unavailable: proposalData.duplicate_check_unavailable,
                // DALTON-HOOK(cos-settings-tools): confirmation snapshot fields, tool_call_id, and legacy scope_changed terminal state
                computer_id: proposalData.computer_id,
                computer_name: proposalData.computer_name,
                computer_is_own_machine: proposalData.computer_is_own_machine,
                bootstrap_cloud_primary: proposalData.bootstrap_cloud_primary,
                tool_call_id: proposalData.tool_call_id,
                interruptId: event.interrupt_id,
                status: hasToolCallId ? 'pending' : 'failed',
                ...(hasToolCallId
                  ? {}
                  : {
                      failure_code: 'scope_changed',
                      failure_error: '情况已变化，请重新发起',
                    }),
              },
            },
            isStreaming: false,
          }; })
    );
    // --- DALTON-HOOK(cos-settings-tools): end ---

    // DALTON-HOOK(cos-settings-tools): do not arm legacy cards lacking tool_call_id
    if (hasToolCallId) {
      arm({
        type: 'create_workspace',
        interruptId: event.interrupt_id,
        assistantMessageId,
        proposalId,
        toolCallId: proposalData.tool_call_id,
      });
    }
  } else if (actionType === 'start_question') {
    // --- Start question interrupt ---
    const proposalId = event.interrupt_id || `question-start-${Date.now()}`;
    const proposalData = actionRequests[0];
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);

    rt.setMessages((prev) =>
      updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
        ...msg,
        contentSegments: appendCardSegment(msg.contentSegments, { type: 'start_question', proposalId, order }),
        questionProposals: {
          ...(msg.questionProposals || {}),
          [proposalId]: {
            workspace_id: proposalData.workspace_id,
            question: proposalData.question,
            interruptId: event.interrupt_id,
            status: 'pending',
          },
        },
        isStreaming: false,
      }; })
    );

    arm({
      type: 'start_question',
      interruptId: event.interrupt_id,
      assistantMessageId,
      proposalId,
    });
  } else if (actionType === 'ptc_agent') {
    // --- PTC agent interrupt ---
    const proposalId = event.interrupt_id || `ptc-agent-${Date.now()}`;
    const proposalData = actionRequests[0];
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);

    rt.setMessages((prev) =>
      updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
        ...msg,
        contentSegments: appendCardSegment(msg.contentSegments, { type: 'ptc_agent' as const, proposalId, order }),
        ptcAgentProposals: {
          ...(msg.ptcAgentProposals || {}),
          [proposalId]: {
            workspace_id: proposalData.workspace_id,
            workspace_name: proposalData.workspace_name,
            question: proposalData.question ?? '',
            report_back: proposalData.report_back ?? true,
            model: proposalData.model ?? null,
            effort: proposalData.effort ?? null,
            interruptId: event.interrupt_id,
            // Persist tool_call_id ON the proposal so the clicked card
            // self-identifies for backfill — never read from
            // `pendingInterrupt`, which N parallel dispatches overwrite.
            tool_call_id: proposalData.tool_call_id,
            status: 'pending',
          },
        },
        isStreaming: false,
      }; })
    );

    arm({
      type: 'ptc_agent',
      interruptId: event.interrupt_id,
      assistantMessageId,
      proposalId,
      toolCallId: proposalData.tool_call_id,
    });
  } else if (actionType === 'delete_workspace' || actionType === 'stop_workspace' || actionType === 'delete_thread') {
    // --- Secretary action interrupt ---
    const proposalId = event.interrupt_id || `secretary-${actionType}-${Date.now()}`;
    const proposalData = actionRequests[0];
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);

    rt.setMessages((prev) =>
      updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
        ...msg,
        contentSegments: appendCardSegment(msg.contentSegments, { type: actionType as 'delete_workspace' | 'stop_workspace' | 'delete_thread', proposalId, order }),
        secretaryActionProposals: {
          ...(msg.secretaryActionProposals || {}),
          [proposalId]: {
            actionType: actionType as 'delete_workspace' | 'stop_workspace' | 'delete_thread',
            workspace_id: proposalData.workspace_id,
            workspace_name: proposalData.workspace_name,
            thread_id: proposalData.thread_id,
            // DALTON-HOOK(cos-settings-tools): carry tool_call_id for secretary cards
            tool_call_id: proposalData.tool_call_id,
            interruptId: event.interrupt_id,
            status: 'pending',
          },
        },
        isStreaming: false,
      }; })
    );

    arm({
      type: actionType,
      interruptId: event.interrupt_id,
      assistantMessageId,
      proposalId,
      toolCallId: proposalData.tool_call_id,
    });
  // --- DALTON-HOOK(watchlist): begin ---
  } else if (actionType && WATCHLIST_INTERRUPT_TYPES.has(actionType)) {
    const proposalId = event.interrupt_id || `watchlist-${actionType}-${Date.now()}`;
    const proposalData = actionRequests[0] as Record<string, unknown>;
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);
    const toolCallId = typeof proposalData.tool_call_id === 'string' ? proposalData.tool_call_id : '';
    const hasToolCallId = toolCallId.length > 0;

    rt.setMessages((prev) =>
      interruptAlreadyRendered
        ? (hasToolCallId ? setCardStatus(prev, 'watchlistProposals', proposalId, 'pending') : prev)
        : updateMessage(prev, assistantMessageId, (m) => {
            if (m.role !== 'assistant') return m;
            const msg = m as AssistantMessage;
            return {
              ...msg,
              contentSegments: appendCardSegment(msg.contentSegments, {
                type: actionType as WatchlistProposalActionType,
                proposalId,
                order,
              }),
              watchlistProposals: {
                ...(msg.watchlistProposals || {}),
                [proposalId]: {
                  actionType: actionType as WatchlistProposalActionType,
                  raw_action: typeof proposalData.raw_action === 'string' ? proposalData.raw_action : undefined,
                  name: typeof proposalData.name === 'string' ? proposalData.name : undefined,
                  description: typeof proposalData.description === 'string' ? proposalData.description : undefined,
                  owner_user_id: typeof proposalData.owner_user_id === 'string' ? proposalData.owner_user_id : undefined,
                  expected_roster_hash: typeof proposalData.expected_roster_hash === 'string' ? proposalData.expected_roster_hash : undefined,
                  expected_policy_versions: (proposalData.expected_policy_versions as Record<string, number> | undefined),
                  target_tickers: Array.isArray(proposalData.target_tickers) ? (proposalData.target_tickers as string[]) : undefined,
                  target_tier: typeof proposalData.target_tier === 'string' ? proposalData.target_tier : null,
                  affected_tickers: Array.isArray(proposalData.affected_tickers) ? (proposalData.affected_tickers as never[]) : undefined,
                  affected_workspaces: Array.isArray(proposalData.affected_workspaces) ? (proposalData.affected_workspaces as never[]) : undefined,
                  task_summary: (proposalData.task_summary as Record<string, number> | undefined),
                  preserve_workspace_notice: typeof proposalData.preserve_workspace_notice === 'string' ? proposalData.preserve_workspace_notice : null,
                  group_id: typeof proposalData.group_id === 'string' ? proposalData.group_id : null,
                  group_label: typeof proposalData.group_label === 'string' ? proposalData.group_label : null,
                  tool_call_id: toolCallId || undefined,
                  interruptId: event.interrupt_id,
                  status: hasToolCallId ? 'pending' : 'failed',
                  ...(hasToolCallId
                    ? {}
                    : {
                        failure_code: 'scope_changed',
                        failure_error: '情况已变化，请重新发起',
                      }),
                },
              },
              isStreaming: false,
            };
          }),
    );

    if (hasToolCallId) {
      arm({
        type: actionType,
        interruptId: event.interrupt_id,
        assistantMessageId,
        proposalId,
        toolCallId,
      });
    }
  // --- DALTON-HOOK(watchlist): end ---
  } else if (actionType === 'credit_pause') {
    // --- Credit pause interrupt ---
    const proposalId = event.interrupt_id!;
    const pauseState = buildCreditPauseState(actionRequests[0], event.interrupt_id!);
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);

    // A re-raise is the pause saying it was never consumed, whatever the resume
    // looked like from the client: admission opens a run either way, so the card
    // was already flipped to `resumed`. Put that card back rather than writing a
    // pending entry onto this bubble, which the suppression above leaves with no
    // segment to render it — the pause would otherwise sit unanswerable, and the
    // status is what history replays.
    rt.setMessages((prev) =>
      interruptAlreadyRendered
        ? setCardStatus(prev, 'creditPauses', proposalId, 'pending')
        : updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
            ...msg,
            contentSegments: appendCardSegment(msg.contentSegments, { type: 'credit_pause', proposalId, order }),
            creditPauses: { ...(msg.creditPauses || {}), [proposalId]: pauseState },
            isStreaming: false,
          }; })
    );

    arm({
      type: 'credit_pause',
      interruptId: event.interrupt_id,
      assistantMessageId,
      proposalId,
    });
  } else if (isToolApprovalInterrupt(event.kind, actionRequests[0])) {
    // --- Direct MCP tool approval interrupt ---
    const cards = toolApprovalCards(
      actionRequests,
      event.interrupt_id,
      `tool-approval-${Date.now()}`,
    );
    const order = eventOrder(event, () => ++refs.contentOrderCounterRef.current);

    // A re-raise is the backend saying the calls are still stopped, whatever
    // the resume looked like from the client, and the click already settled
    // the cards to approved/rejected. Put them back rather than writing fresh
    // entries onto this bubble, which the suppression above leaves with no
    // segment to render them: the calls would otherwise sit unanswerable, and
    // the status is what history replays.
    rt.setMessages((prev) =>
      interruptAlreadyRendered
        ? cards.reduce((msgs, card) => setCardStatus(msgs, 'toolApprovals', card.proposalId, 'pending'), prev)
        : updateMessage(prev,assistantMessageId, (m) => { if (m.role !== 'assistant') return m; const msg = m as AssistantMessage; return {
            ...msg,
            contentSegments: cards.reduce(
              (segments, card, i) =>
                appendCardSegment(segments, { type: 'tool_approval', proposalId: card.proposalId, order: order + i }),
              msg.contentSegments,
            ),
            toolApprovals: {
              ...(msg.toolApprovals || {}),
              ...Object.fromEntries(cards.map((c) => [c.proposalId, c.state])),
            },
            isStreaming: false,
          }; })
    );

    arm({
      type: 'tool_approval',
      interruptId: event.interrupt_id,
      assistantMessageId,
      proposalId: cards[0].proposalId,
    });
  }
  // Any other interrupt gets no card and no pending state. The only one left
  // in the wild is a SubmitPlan review on a thread paused before the agent
  // stopped raising it, and nothing can resume it now: arming it would hold the
  // composer shut behind a card with no answer, while leaving it lets the next
  // message start a fresh turn.
}
