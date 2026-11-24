/**
 * Batch 2 frontend lifecycle tests: live stream projection, history replay,
 * confirm_token echoing, 4-state settlement by tool_call_id (including
 * scope_changed), and refused-resume restoration.
 */
import { describe, expect, it, vi } from 'vitest';
import type { AssistantMessage, ChatMessage } from '@/types/chat';
import {
  proposalResultFields,
  settleLiveProposalFromResult,
  settleProposalFromResult,
} from '../buckets';
import { restoreToolApprovalsPending } from '../answerBoard';
import { claimedCardFields } from '../claims';
import { projectLiveInterrupt } from '../fromLiveEvent';
import { projectHistoryInterrupt, type HistoryInterruptContext } from '../fromHistoryEvent';
import { applyHistoryToolResult } from '../../history/resumeSettlement';
import type { HistoryInterruptInfo, PairState, SSEEvent, StreamProcessorRefs } from '../../types';
import type { HistoryRuntime, StreamRuntime } from '../../runtime';

function makeAssistant(id = 'a-1'): AssistantMessage {
  return {
    id,
    role: 'assistant',
    content: '',
    contentType: 'text',
    timestamp: new Date(),
    isStreaming: true,
    contentSegments: [],
    reasoningProcesses: {},
    toolCallProcesses: {},
  };
}

function makeInterruptEvent(): SSEEvent {
  return {
    event: 'interrupt',
    interrupt_id: 'intr-cw-1',
    turn_index: 0,
    action_requests: [
      {
        type: 'create_workspace',
        tool_call_id: 'tc-cw-1',
        workspace_name: 'NVDA',
        workspace_description: 'GPU research',
        computer_id: 'comp-mac-1',
        computer_name: 'My Mac Studio',
        computer_is_own_machine: true,
        bootstrap_cloud_primary: false,
        confirm_token: 'tok-abc-123',
      },
    ],
  };
}

describe('create_workspace 4-state lifecycle: live vs history parity', () => {
  it.each([
    {
      name: 'approved (success: true)',
      content: JSON.stringify({
        success: true,
        workspace_id: 'ws-1',
        workspace_name: 'NVDA',
        computer_id: 'comp-mac-1',
        computer_name: 'My Mac Studio',
      }),
      expected: {
        status: 'approved',
        computer_name: 'My Mac Studio',
        failure_code: undefined,
        failure_error: undefined,
      },
    },
    {
      name: 'rejected (User declined)',
      content: 'User declined workspace creation.',
      expected: {
        status: 'rejected',
        failure_code: undefined,
        failure_error: undefined,
      },
    },
    {
      name: 'failed (scope_changed)',
      content: JSON.stringify({
        success: false,
        code: 'scope_changed',
        error: '情况已变化，请重新发起',
      }),
      expected: {
        status: 'failed',
        failure_code: 'scope_changed',
        failure_error: '情况已变化，请重新发起',
      },
    },
  ])('live stream and history replay settle identically for $name', ({ content, expected }) => {
    // --- Live path ---
    let liveMessages: ChatMessage[] = [makeAssistant('a-live')];
    const armed: unknown[] = [];
    const liveRt = {
      renderedInterruptIdsRef: { current: new Set<string>() },
      currentRunIdRef: { current: 'run-1' },
      pause: { arm: vi.fn((p: unknown) => armed.push(p)) },
      setMessages: (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => {
        liveMessages = typeof updater === 'function' ? updater(liveMessages) : updater;
      },
    } as unknown as StreamRuntime;
    const refs = {
      contentOrderCounterRef: { current: 0 },
    } as unknown as StreamProcessorRefs;

    projectLiveInterrupt(liveRt, makeInterruptEvent(), 'a-live', refs);
    const liveCard = (liveMessages[0] as AssistantMessage).workspaceProposals?.['intr-cw-1'];
    expect(liveCard).toMatchObject({
      workspace_name: 'NVDA',
      computer_id: 'comp-mac-1',
      computer_name: 'My Mac Studio',
      computer_is_own_machine: true,
      tool_call_id: 'tc-cw-1',
      confirm_token: 'tok-abc-123',
      status: 'pending',
    });

    // Simulate click Approve -> submitting, then tool_call_result arrives
    liveMessages = [
      {
        ...(liveMessages[0] as AssistantMessage),
        workspaceProposals: {
          'intr-cw-1': { ...liveCard!, status: 'submitting' },
        },
      },
    ];
    liveMessages = settleLiveProposalFromResult(liveMessages, 'tc-cw-1', { content });
    const settledLive = (liveMessages[0] as AssistantMessage).workspaceProposals?.['intr-cw-1'];

    // --- History replay path ---
    let histMessages: ChatMessage[] = [makeAssistant('a-hist')];
    const histRt = {
      renderedInterruptIdsRef: { current: new Set<string>() },
      setMessages: (updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => {
        histMessages = typeof updater === 'function' ? updater(histMessages) : updater;
      },
    } as unknown as HistoryRuntime;
    const pairState: PairState = {
      contentOrderCounter: 0,
      reasoningId: null,
      toolCallId: null,
      steeringBatches: 0,
    };
    const pending: HistoryInterruptInfo[] = [];
    const ctx: HistoryInterruptContext = {
      currentActivePairIndex: 0,
      assistantMessagesByPair: new Map([[0, 'a-hist']]),
      pairStateByPair: new Map([[0, pairState]]),
      pendingHistoryInterrupts: pending,
      evidence: { claims: new Map(), positional: new Map(), byAttempt: new Map() },
    };

    projectHistoryInterrupt(histRt, makeInterruptEvent(), ctx);
    expect(pending[0]?.toolCallId).toBe('tc-cw-1');

    applyHistoryToolResult({
      event: {
        event: 'tool_call_result',
        tool_call_id: 'tc-cw-1',
        content,
        content_type: 'text',
      },
      assistantMessageId: 'a-hist',
      pairState,
      pending,
      setMessages: histRt.setMessages,
    });
    const settledHist = (histMessages[0] as AssistantMessage).workspaceProposals?.['intr-cw-1'];

    expect(settledLive).toMatchObject(expected);
    expect(settledHist).toMatchObject(expected);
    expect(settledLive?.status).toBe(settledHist?.status);
    expect(settledLive?.failure_code).toBe(settledHist?.failure_code);
    expect(settledLive?.failure_error).toBe(settledHist?.failure_error);
    expect(pending).toHaveLength(0);
  });

  it('matches only the exact tool_call_id when settling multiple pending workspace proposals', () => {
    let msgs: ChatMessage[] = [
      {
        ...makeAssistant('a-1'),
        workspaceProposals: {
          'intr-1': { status: 'pending', workspace_name: 'A', tool_call_id: 'tc-1' },
          'intr-2': { status: 'pending', workspace_name: 'B', tool_call_id: 'tc-2' },
        },
      },
    ];
    const pending: HistoryInterruptInfo[] = [
      { type: 'create_workspace', assistantMessageId: 'a-1', proposalId: 'intr-1', interruptId: 'intr-1', toolCallId: 'tc-1' },
      { type: 'create_workspace', assistantMessageId: 'a-1', proposalId: 'intr-2', interruptId: 'intr-2', toolCallId: 'tc-2' },
    ];
    const setMessages = (u: ChatMessage[] | ((p: ChatMessage[]) => ChatMessage[])) => {
      msgs = typeof u === 'function' ? u(msgs) : u;
    };

    const settled = settleProposalFromResult(
      pending,
      'tc-2',
      { content: JSON.stringify({ success: false, code: 'scope_changed', error: '情况已变化，请重新发起' }) },
      setMessages,
    );
    expect(settled).toBe(true);
    const proposals = (msgs[0] as AssistantMessage).workspaceProposals!;
    expect(proposals['intr-1'].status).toBe('pending');
    expect(proposals['intr-2'].status).toBe('failed');
    expect(proposals['intr-2'].failure_code).toBe('scope_changed');
  });

  it('keeps in-flight create_workspace claims in submitting and restores refused resumes to pending', () => {
    expect(
      claimedCardFields('create_workspace', { content: '', batched: false }),
    ).toEqual({ status: 'submitting' });
    expect(
      claimedCardFields('create_workspace', { answer: null, content: '', batched: false }),
    ).toEqual({ status: 'rejected' });

    const msgs: ChatMessage[] = [
      {
        ...makeAssistant('a-1'),
        workspaceProposals: {
          'intr-1': { status: 'submitting', interruptId: 'intr-1', workspace_name: 'NVDA' },
        },
      },
    ];
    const restored = restoreToolApprovalsPending(msgs, new Set(['intr-1']));
    expect((restored[0] as AssistantMessage).workspaceProposals?.['intr-1'].status).toBe('pending');

    expect(
      proposalResultFields('create_workspace', {
        content: JSON.stringify({ success: false, error: 'Name taken' }),
      }),
    ).toEqual({ status: 'failed', failure_error: 'Name taken' });
  });
});
