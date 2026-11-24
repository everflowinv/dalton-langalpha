/**
 * Batch 2 frontend lifecycle tests: live stream projection, history replay,
 * 4-state settlement by tool_call_id (including tool status="error", non-JSON
 * failure, and scope_changed), refused-resume retry with pause restoration,
 * reload-while-submitting settlement, and failed preapproved cards.
 */
import { describe, expect, it, vi } from 'vitest';
import type { AssistantMessage, ChatMessage } from '@/types/chat';
import {
  proposalResultFields,
  settleLiveProposalFromResult,
  settleProposalFromResult,
} from '../buckets';
import {
  createAnswerBoard,
  failProposalsScopeChanged,
  restoreToolApprovalsPending,
} from '../answerBoard';
import { createPauseSlot } from '../pauseSlot';
import { claimedCardFields, createApprovalEvidence, recordApprovalEvidence } from '../claims';
import { projectLiveInterrupt } from '../fromLiveEvent';
import { projectHistoryInterrupt, type HistoryInterruptContext } from '../fromHistoryEvent';
import {
  applyHistoryToolResult,
  settleResumedInterrupts,
} from '../../history/resumeSettlement';
import { preapprovedCard } from '../../../utils/preapprovedCards';
import type {
  HistoryInterruptInfo,
  PairState,
  PendingInterrupt,
  SSEEvent,
  StreamProcessorRefs,
} from '../../types';
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
      status: undefined as string | undefined,
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
      status: undefined as string | undefined,
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
      status: undefined as string | undefined,
      expected: {
        status: 'failed',
        failure_code: 'scope_changed',
        failure_error: '情况已变化，请重新发起',
      },
    },
    {
      name: 'failed (status="error" plain text from agent_change cancellation)',
      content: 'Cancelled: switched model before tool completed',
      status: 'error',
      expected: {
        status: 'failed',
        failure_error: 'Cancelled: switched model before tool completed',
      },
    },
    {
      name: 'failed (unparseable non-JSON tool result)',
      content: 'Unexpected non-JSON tool output',
      status: undefined as string | undefined,
      expected: {
        status: 'failed',
        failure_error: 'Unexpected non-JSON tool output',
      },
    },
  ])(
    'live stream and history replay settle identically for $name',
    ({ content, status, expected }) => {
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
        status: 'pending',
      });
      expect(liveCard).toBeDefined();
      expect(liveCard).not.toHaveProperty('confirm_token');

      // Simulate click Approve -> submitting, then tool_call_result arrives
      liveMessages = [
        {
          ...(liveMessages[0] as AssistantMessage),
          workspaceProposals: {
            'intr-cw-1': { ...liveCard!, status: 'submitting' },
          },
        },
      ];
      liveMessages = settleLiveProposalFromResult(liveMessages, 'tc-cw-1', { content, status });
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
          status,
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
    },
  );

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

  it('restores both card status and pause slot on refused resume, and settles scope_changed terminally', () => {
    let msgs: ChatMessage[] = [
      {
        ...makeAssistant('a-1'),
        workspaceProposals: {
          'intr-1': { status: 'submitting', interruptId: 'intr-1', workspace_name: 'NVDA', tool_call_id: 'tc-1' },
        },
      },
    ];
    const setMessages = (u: ChatMessage[] | ((p: ChatMessage[]) => ChatMessage[])) => {
      msgs = typeof u === 'function' ? u(msgs) : u;
    };
    const pendingIds = { current: new Set<string>() };
    const sessionEpoch = { current: 1 };
    let shown: PendingInterrupt | null = null;
    const board = createAnswerBoard({ pendingIds, sessionEpoch, setMessages });
    const pause = createPauseSlot((p) => { shown = p; }, board);

    const initialPause: PendingInterrupt = {
      type: 'create_workspace',
      interruptId: 'intr-1',
      assistantMessageId: 'a-1',
      proposalId: 'intr-1',
      toolCallId: 'tc-1',
    };
    pause.arm(initialPause, ['intr-1']);
    const priorPause = pause.current();
    const settleResume = board.beginResume(['intr-1']);
    pause.clear();
    expect(pause.current()).toBeNull();

    // Refused (e.g. 429 rate limit): restore card to pending AND re-arm pause so retry clicks work.
    settleResume(false);
    if (priorPause) pause.arm(priorPause, ['intr-1']);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-1'].status).toBe('pending');
    expect(pause.current()).toEqual(initialPause);
    expect(shown).toEqual(initialPause);

    // 409 scope_changed: terminal failed state, never reverted to pending.
    const failedMsgs = failProposalsScopeChanged(msgs, new Set(['intr-1']));
    expect((failedMsgs[0] as AssistantMessage).workspaceProposals?.['intr-1']).toMatchObject({
      status: 'failed',
      failure_code: 'scope_changed',
      failure_error: '情况已变化，请重新发起',
    });

    // Claimed in-flight cards stay submitting (approve) or rejected (decline).
    expect(
      claimedCardFields('create_workspace', { content: '', batched: false }),
    ).toEqual({ status: 'submitting' });
    expect(
      claimedCardFields('create_workspace', { answer: null, content: '', batched: false }),
    ).toEqual({ status: 'rejected' });

    const restored = restoreToolApprovalsPending(
      [
        {
          ...makeAssistant('a-1'),
          workspaceProposals: {
            'intr-1': { status: 'submitting', interruptId: 'intr-1', workspace_name: 'NVDA' },
          },
        },
      ],
      new Set(['intr-1']),
    );
    expect((restored[0] as AssistantMessage).workspaceProposals?.['intr-1'].status).toBe('pending');

    expect(
      proposalResultFields('create_workspace', {
        content: JSON.stringify({ success: false, error: 'Name taken' }),
      }),
    ).toEqual({ status: 'failed', failure_error: 'Name taken' });
  });

  it('reload while submitting shows submitting without clickable buttons, then settles on tool_call_result', () => {
    let msgs: ChatMessage[] = [makeAssistant('a-hist')];
    const rt = {
      renderedInterruptIdsRef: { current: new Set<string>() },
      setMessages: (u: ChatMessage[] | ((p: ChatMessage[]) => ChatMessage[])) => {
        msgs = typeof u === 'function' ? u(msgs) : u;
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

    projectHistoryInterrupt(rt, makeInterruptEvent(), ctx);
    expect(pending).toHaveLength(1);

    // Resume user_message already stamped hitl_interrupt_ids while tool_call_result has not arrived yet.
    settleResumedInterrupts(rt, pending, {
      event: 'user_message',
      content: '',
      metadata: { hitl_interrupt_ids: ['intr-cw-1'] },
    });
    expect(pending).toHaveLength(0);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']?.status).toBe('submitting');

    // Once tool_call_result arrives (or replays right after), the submitting card settles.
    applyHistoryToolResult({
      event: {
        event: 'tool_call_result',
        tool_call_id: 'tc-cw-1',
        content: JSON.stringify({
          success: false,
          code: 'scope_changed',
          error: '情况已变化，请重新发起',
        }),
        content_type: 'text',
      },
      assistantMessageId: 'a-hist',
      pairState,
      pending,
      setMessages: rt.setMessages,
    });
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']).toMatchObject({
      status: 'failed',
      failure_code: 'scope_changed',
      failure_error: '情况已变化，请重新发起',
    });
  });

  it('builds a failed create_workspace card when a preapproved create fails (scope_changed)', () => {
    const card = preapprovedCard(
      'manage_workspaces',
      { action: 'create', name: 'NVDA', description: 'GPU research' },
      {
        content: JSON.stringify({
          success: false,
          preapproved: true,
          workspace_name: 'NVDA',
          workspace_description: 'GPU research',
          computer_name: 'Cloud Primary',
          code: 'scope_changed',
          error: '情况已变化，请重新发起',
        }),
      },
      'tc-pre-1',
    );
    expect(card).toEqual({
      type: 'create_workspace',
      proposal: {
        status: 'failed',
        workspace_name: 'NVDA',
        workspace_description: 'GPU research',
        computer_name: 'Cloud Primary',
        failure_code: 'scope_changed',
        failure_error: '情况已变化，请重新发起',
      },
    });

    // Round 3 Decision 6: early returns (company_workspace_exists / coverage_suggestion)
    // with preapproved=true also produce failed cards displaying the guidance message.
    const existsCard = preapprovedCard(
      'manage_workspaces',
      { action: 'create', name: 'NVDA' },
      {
        content: JSON.stringify({
          success: false,
          preapproved: true,
          workspace_name: 'NVDA',
          workspace_description: '',
          computer_name: 'Cloud Primary',
          code: 'company_workspace_exists',
          error: '已存在同名公司工作区 NVIDIA',
        }),
      },
      'tc-pre-2',
    );
    expect(existsCard).toEqual({
      type: 'create_workspace',
      proposal: {
        status: 'failed',
        workspace_name: 'NVDA',
        workspace_description: '',
        computer_name: 'Cloud Primary',
        failure_code: 'company_workspace_exists',
        failure_error: '已存在同名公司工作区 NVIDIA',
      },
    });
  });

  it('restores create_workspace to pending on replay when a resume failed before the graph consumed the Command', () => {
    // Round 3 Decision 5: original card -> failed terminal resume record (has run_id,
    // no live claim) -> same interrupt still pending at checkpoint tip -> must restore
    // to pending & re-queue so the user can retry (never stuck in submitting).
    let msgs: ChatMessage[] = [makeAssistant('a-0'), makeAssistant('a-1')];
    const rt = {
      renderedInterruptIdsRef: { current: new Set<string>() },
      setMessages: (u: ChatMessage[] | ((p: ChatMessage[]) => ChatMessage[])) => {
        msgs = typeof u === 'function' ? u(msgs) : u;
      },
    } as unknown as HistoryRuntime;
    const pair0: PairState = { contentOrderCounter: 0, reasoningId: null, toolCallId: null, steeringBatches: 0 };
    const pair1: PairState = { contentOrderCounter: 0, reasoningId: null, toolCallId: null, steeringBatches: 0 };
    const pending: HistoryInterruptInfo[] = [];
    const evidence = createApprovalEvidence();
    const ctx: HistoryInterruptContext = {
      currentActivePairIndex: 0,
      assistantMessagesByPair: new Map([[0, 'a-0'], [1, 'a-1']]),
      pairStateByPair: new Map([[0, pair0], [1, pair1]]),
      pendingHistoryInterrupts: pending,
      evidence,
    };

    // 1. Original card on Turn 0
    projectHistoryInterrupt(rt, makeInterruptEvent(), ctx);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']?.status).toBe('pending');
    expect(pending).toHaveLength(1);

    // 2. Resume turn persisted hitl_interrupt_ids and failed before LangGraph took the Command (terminal: run_id set).
    const failedResumeUserMsg: SSEEvent = {
      event: 'user_message',
      run_id: 'run-failed-1',
      content: '',
      metadata: { hitl_interrupt_ids: ['intr-cw-1'] },
    };
    recordApprovalEvidence(evidence, failedResumeUserMsg);
    expect(evidence.claims.has('intr-cw-1')).toBe(false);
    settleResumedInterrupts(rt, pending, failedResumeUserMsg);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']?.status).toBe('submitting');
    expect(pending).toHaveLength(0);

    // 3. Tip interrupt replays (no turn_index): restores card to pending and re-queues it.
    ctx.currentActivePairIndex = 1;
    const tipEvent: SSEEvent = { ...makeInterruptEvent(), turn_index: undefined };
    projectHistoryInterrupt(rt, tipEvent, ctx);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']?.status).toBe('pending');
    expect(pending).toHaveLength(1);
    expect(pending[0]).toMatchObject({
      type: 'create_workspace',
      interruptId: 'intr-cw-1',
      proposalId: 'intr-cw-1',
      toolCallId: 'tc-cw-1',
    });

    // Contrast: when a resume is STILL RUNNING (no run_id -> live claim recorded),
    // the duplicate tip interrupt keeps the card in submitting and does not re-queue.
    const liveResumeUserMsg: SSEEvent = {
      event: 'user_message',
      content: '',
      metadata: { hitl_interrupt_ids: ['intr-cw-1'] },
    };
    recordApprovalEvidence(evidence, liveResumeUserMsg);
    expect(evidence.claims.has('intr-cw-1')).toBe(true);
    settleResumedInterrupts(rt, pending, liveResumeUserMsg);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']?.status).toBe('submitting');
    expect(pending).toHaveLength(0);
    projectHistoryInterrupt(rt, tipEvent, ctx);
    expect((msgs[0] as AssistantMessage).workspaceProposals?.['intr-cw-1']?.status).toBe('submitting');
    expect(pending).toHaveLength(0);
  });

  it('settles a pre-upgrade submitting create_workspace card without tool_call_id only on matching create_workspace result', () => {
    const preUpgradeMsgs: ChatMessage[] = [
      {
        ...makeAssistant('a-legacy'),
        toolCallProcesses: {
          'tc-bash-1': {
            toolName: 'bash',
            toolCall: { id: 'tc-bash-1', name: 'bash', args: { command: 'ls' } },
            toolCallResult: null,
            isInProgress: true,
            isComplete: false,
            order: 0,
          },
          'tc-other-create': {
            toolName: 'manage_workspaces',
            toolCall: {
              id: 'tc-other-create',
              name: 'manage_workspaces',
              args: { action: 'create', name: 'OtherWS' },
            },
            toolCallResult: null,
            isInProgress: true,
            isComplete: false,
            order: 1,
          },
          'tc-legacy-1': {
            toolName: 'manage_workspaces',
            toolCall: {
              id: 'tc-legacy-1',
              name: 'manage_workspaces',
              args: { action: 'create', name: 'LegacyWS' },
            },
            toolCallResult: null,
            isInProgress: true,
            isComplete: false,
            order: 2,
          },
        },
        workspaceProposals: {
          'intr-old': {
            status: 'submitting',
            workspace_name: 'LegacyWS',
            interruptId: 'intr-old',
          },
        },
      },
    ];

    // Unrelated tool result with success: true must NOT settle the legacy card as approved.
    const afterBash = settleLiveProposalFromResult(preUpgradeMsgs, 'tc-bash-1', {
      content: JSON.stringify({ success: true, output: 'ok' }),
    });
    expect(afterBash).toBe(preUpgradeMsgs);
    expect((afterBash[0] as AssistantMessage).workspaceProposals?.['intr-old']?.status).toBe('submitting');

    // Unregistered tool_call_id must NOT settle the legacy card.
    const afterUnknown = settleLiveProposalFromResult(preUpgradeMsgs, 'tc-unknown', {
      content: JSON.stringify({ success: true }),
    });
    expect(afterUnknown).toBe(preUpgradeMsgs);

    // A create_workspace result for a DIFFERENT workspace name must NOT settle LegacyWS.
    const afterOtherCreate = settleLiveProposalFromResult(preUpgradeMsgs, 'tc-other-create', {
      content: JSON.stringify({ success: true, workspace_name: 'OtherWS' }),
    });
    expect(afterOtherCreate).toBe(preUpgradeMsgs);
    expect((afterOtherCreate[0] as AssistantMessage).workspaceProposals?.['intr-old']?.status).toBe('submitting');

    // Now the matching manage_workspaces(action="create", name="LegacyWS") scope_changed result settles it.
    const settled = settleLiveProposalFromResult(afterOtherCreate, 'tc-legacy-1', {
      status: 'error',
      content: JSON.stringify({
        success: false,
        code: 'scope_changed',
        error: '情况已变化，请重新发起',
      }),
    });
    expect((settled[0] as AssistantMessage).workspaceProposals?.['intr-old']).toMatchObject({
      status: 'failed',
      failure_code: 'scope_changed',
      failure_error: '情况已变化，请重新发起',
    });
  });
});
