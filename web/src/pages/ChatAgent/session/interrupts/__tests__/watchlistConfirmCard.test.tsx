import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { AssistantMessage, ChatMessage, WatchlistProposalState } from '@/types/chat';
import {
  CARD_BUCKET_FOR_TYPE,
  INTERRUPT_CARD_BUCKETS,
  PROPOSAL_DATA_KEY_MAP,
  PROPOSAL_INTERRUPT_TYPES,
  settleLiveProposalFromResult,
  settleProposalFromResult,
} from '../buckets';
import { projectLiveInterrupt } from '../fromLiveEvent';
import { projectHistoryInterrupt } from '../fromHistoryEvent';
import { claimedCardFields, createApprovalEvidence } from '../claims';
import { useInterruptAnswers } from '../useInterruptAnswers';
import { buildRenderBlocks } from '../../../components/messageList/buildRenderBlocks';
import * as watchlistLib from '@/lib/dalton/watchlist';
import WatchlistConfirmCard from '../../../components/WatchlistConfirmCard';
import type { HistoryInterruptInfo, SSEEvent, StreamProcessorRefs } from '../../types';

function makeAssistantMessage(id = 'msg-1'): AssistantMessage {
  return {
    id,
    role: 'assistant',
    content: '',
    contentType: 'text',
    timestamp: new Date('2026-11-26T10:00:00Z'),
    isStreaming: true,
    contentSegments: [],
    reasoningProcesses: {},
    toolCallProcesses: {},
  };
}

function makeWatchlistInterruptEvent(overrides: Record<string, unknown> = {}): SSEEvent {
  return {
    event: 'interrupt',
    interrupt_id: 'intr-wl-1',
    turn_index: 0,
    action_requests: [
      {
        type: 'watchlist_set_tier',
        tool_call_id: 'tc-wl-1',
        owner_user_id: 'user-alice',
        raw_action: 'set_tier',
        name: '确认调整 1 只标的至 雷达',
        description: '调整 NVDA 至 雷达，影响 1 个已有研究工作区的受管任务。',
        target_tickers: ['NVDA'],
        target_tier: 'radar',
        affected_tickers: [
          { ticker: 'NVDA', name: 'NVIDIA', old_tier: 'core', new_tier: 'radar', has_workspace: true },
        ],
        affected_workspaces: [
          {
            ticker: 'NVDA',
            workspace_id: 'ws-nvda',
            old_tier: 'core',
            new_tier: 'radar',
            task_changes: [{ name: 'dalton-nvda-watch', change: 'pause' }],
          },
        ],
        task_summary: { pause_count: 1 },
        ...overrides,
      },
    ],
  } as unknown as SSEEvent;
}

describe('WatchlistConfirmCard & interrupt bucket 4-state lifecycle', () => {
  it('registers all 4 watchlist action types in interrupt bucket tables', () => {
    for (const actionType of [
      'watchlist_batch_add',
      'watchlist_set_tier',
      'watchlist_remove',
      'watchlist_tier_policy',
    ]) {
      expect(PROPOSAL_INTERRUPT_TYPES.has(actionType)).toBe(true);
      expect(PROPOSAL_DATA_KEY_MAP[actionType]).toBe('watchlistProposals');
      expect(CARD_BUCKET_FOR_TYPE[actionType]).toBe('watchlistProposals');
    }
    expect(INTERRUPT_CARD_BUCKETS).toContain('watchlistProposals');
  });

  it('projects live interrupt and settles approved / rejected / failed (scope_changed) by exact tool_call_id', () => {
    let messages: ChatMessage[] = [makeAssistantMessage()];
    const armed: unknown[] = [];
    const rt = {
      setMessages: (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
        messages = updater(messages);
      },
      renderedInterruptIdsRef: { current: new Set<string>() },
      currentRunIdRef: { current: 'run-1' },
      pause: { arm: (p: unknown) => armed.push(p) },
    } as never;
    const refs = { contentOrderCounterRef: { current: 0 } } as StreamProcessorRefs;

    projectLiveInterrupt(rt, makeWatchlistInterruptEvent(), 'msg-1', refs);
    expect(armed).toHaveLength(1);
    const liveMsg = messages[0] as AssistantMessage;
    expect(liveMsg.watchlistProposals?.['intr-wl-1']?.status).toBe('pending');

    // Wrong tool_call_id must NOT settle the card
    const untouched = settleLiveProposalFromResult(messages, 'tc-other', {
      content: JSON.stringify({ success: true }),
    });
    expect(untouched).toBe(messages);

    // Exact tool_call_id settles to approved with reconcile_status
    const approvedMsgs = settleLiveProposalFromResult(messages, 'tc-wl-1', {
      content: JSON.stringify({ success: true, reconcile_status: 'completed', job_id: 'job-1' }),
    });
    expect((approvedMsgs[0] as AssistantMessage).watchlistProposals?.['intr-wl-1']).toMatchObject({
      status: 'approved',
      reconcile_status: 'completed',
      job_id: 'job-1',
    });

    // Declined settles to rejected
    const rejectedMsgs = settleLiveProposalFromResult(messages, 'tc-wl-1', {
      content: 'User declined this action.',
    });
    expect((rejectedMsgs[0] as AssistantMessage).watchlistProposals?.['intr-wl-1']?.status).toBe('rejected');

    // scope_changed error settles to failed with failure_code
    const failedMsgs = settleLiveProposalFromResult(messages, 'tc-wl-1', {
      status: 'error',
      content: JSON.stringify({ success: false, code: 'scope_changed', error: '情况已变化，请重新发起' }),
    });
    expect((failedMsgs[0] as AssistantMessage).watchlistProposals?.['intr-wl-1']).toMatchObject({
      status: 'failed',
      failure_code: 'scope_changed',
    });
  });

  it('replays history interrupt, builds render block, and settles via claims and tool results', () => {
    let messages: ChatMessage[] = [makeAssistantMessage()];
    const rt = {
      setMessages: (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
        messages = updater(messages);
      },
      renderedInterruptIdsRef: { current: new Set<string>() },
    } as never;
    const pending: HistoryInterruptInfo[] = [];
    const ctx = {
      currentActivePairIndex: 0,
      assistantMessagesByPair: new Map([[0, 'msg-1']]),
      pairStateByPair: new Map([[0, { contentOrderCounter: 0 } as never]]),
      pendingHistoryInterrupts: pending,
      evidence: createApprovalEvidence(),
    };

    projectHistoryInterrupt(rt, makeWatchlistInterruptEvent(), ctx);
    expect(pending).toHaveLength(1);
    expect(pending[0]).toMatchObject({ type: 'watchlist_set_tier', toolCallId: 'tc-wl-1' });

    // buildRenderBlocks emits a dedicated watchlist_set_tier block
    const msg = messages[0] as AssistantMessage;
    const { blocks } = buildRenderBlocks(msg.contentSegments as never[], {
      reasoningProcesses: {},
      toolCallProcesses: {},
    });
    expect(blocks.some((b) => b.type === 'watchlist_set_tier')).toBe(true);

    // Mid-resume claim keeps card in submitting (never premature approved)
    expect(claimedCardFields('watchlist_set_tier', { answer: 'ok', content: '', batched: false })).toEqual({
      status: 'submitting',
    });
    expect(claimedCardFields('watchlist_set_tier', { answer: null, content: '', batched: false })).toEqual({
      status: 'rejected',
    });

    // History tool result with exact tool_call_id settles card
    const settled = settleProposalFromResult(
      pending,
      'tc-wl-1',
      { content: JSON.stringify({ success: false, code: 'scope_changed', error: '情况已变化，请重新发起' }) },
      (updater) => {
        messages = typeof updater === 'function' ? updater(messages) : updater;
      },
    );
    expect(settled).toBe(true);
    expect((messages[0] as AssistantMessage).watchlistProposals?.['intr-wl-1']).toMatchObject({
      status: 'failed',
      failure_code: 'scope_changed',
    });
  });

  it('renders WatchlistConfirmCard across all 4 states with affected tickers and 盘中盯盘 task label', () => {
    const base: WatchlistProposalState = {
      status: 'pending',
      actionType: 'watchlist_set_tier',
      name: '确认调整 1 只标的至 雷达',
      description: '调整 NVDA 至 雷达',
      affected_tickers: [{ ticker: 'NVDA', old_tier: 'core', new_tier: 'radar' }],
      affected_workspaces: [
        {
          ticker: 'NVDA',
          workspace_id: 'ws-nvda',
          task_changes: [{ name: 'dalton-nvda-watch', change: 'pause' }],
        },
      ],
      task_summary: { pause_count: 1 },
      tool_call_id: 'tc-wl-1',
    };

    const onApprove = vi.fn();
    const onReject = vi.fn();
    const { rerender } = render(
      <WatchlistConfirmCard proposalData={base} onApprove={onApprove} onReject={onReject} />,
    );

    // 1) Pending: shows ticker, task label (盘中盯盘 / Intraday Watch), and action buttons
    expect(screen.getAllByText('NVDA').length).toBeGreaterThan(0);
    expect(screen.getByText(/盘中盯盘|Intraday Watch/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /确认执行|Confirm/i }));
    expect(onApprove).toHaveBeenCalledTimes(1);

    // 2) Approved
    rerender(<WatchlistConfirmCard proposalData={{ ...base, status: 'approved' }} />);
    expect(screen.getByTestId('proposal-settled').textContent).toMatch(/个人观察名单已更新|Personal watchlist updated/);

    // 3) Rejected
    rerender(<WatchlistConfirmCard proposalData={{ ...base, status: 'rejected' }} />);
    expect(screen.getByTestId('proposal-settled').textContent).toMatch(/已取消|cancelled/);

    // 4) Failed (scope_changed)
    rerender(
      <WatchlistConfirmCard
        proposalData={{ ...base, status: 'failed', failure_code: 'scope_changed', failure_error: '情况已变化，请重新发起' }}
      />,
    );
    expect(screen.getByTestId('proposal-settled').textContent).toMatch(/情况已变化|state changed/);
  });

  it('r4: settles both English and legacy Chinese decline as rejected, routes mixed HITL via answerBoard, and checks retry errors', async () => {
    // 1. Legacy Chinese cancel message AND new English decline both settle as rejected (never failed!)
    let messages: ChatMessage[] = [makeAssistantMessage()];
    const rt = {
      setMessages: (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
        messages = updater(messages);
      },
      renderedInterruptIdsRef: { current: new Set<string>() },
      currentRunIdRef: { current: 'run-1' },
      pause: { arm: vi.fn() },
    } as never;
    projectLiveInterrupt(rt, makeWatchlistInterruptEvent(), 'msg-1', { contentOrderCounterRef: { current: 0 } } as StreamProcessorRefs);

    const legacyDeclined = settleLiveProposalFromResult(messages, 'tc-wl-1', {
      content: '用户取消了个人观察名单变更。',
    });
    expect((legacyDeclined[0] as AssistantMessage).watchlistProposals?.['intr-wl-1']?.status).toBe('rejected');

    const englishDeclined = settleLiveProposalFromResult(messages, 'tc-wl-1', {
      content: 'User declined personal watchlist update.',
    });
    expect((englishDeclined[0] as AssistantMessage).watchlistProposals?.['intr-wl-1']?.status).toBe('rejected');

    // 2. Mixed HITL: pendingInterrupt holds ask_user_question, but clicking watchlist card still answers via answerBoard by interruptId
    const boardAnswer = vi.fn(() => ({ 'intr-wl-1': { decisions: [{ type: 'approve' }] } }));
    const resumeMock = vi.fn();
    const { result } = renderHook(() =>
      useInterruptAnswers({
        pendingInterrupt: {
          type: 'ask_user_question',
          interruptId: 'intr-q-9',
          assistantMessageId: 'msg-1',
          proposalId: 'intr-q-9',
        } as never,
        setMessages: (updater) => {
          messages = typeof updater === 'function' ? updater(messages) : updater;
        },
        resumeWithHitlResponse: resumeMock,
        answerBoard: { answer: boardAnswer } as never,
        pendingPTCBackfillRef: { current: new Map() },
        armReportBackWatch: vi.fn(),
        threadIdRef: { current: 't-1' },
      }),
    );

    result.current.handleApproveWatchlistAction({ interruptId: 'intr-wl-1' });
    expect(boardAnswer).toHaveBeenCalledWith('intr-wl-1', { decisions: [{ type: 'approve' }] });
    expect(resumeMock).toHaveBeenCalledWith({ 'intr-wl-1': { decisions: [{ type: 'approve' }] } });

    // 3. WatchlistConfirmCard retry keeps Retry button visible on partial/error and clears only on completed
    const spyReconcile = vi.spyOn(watchlistLib, 'reconcileWatchlist');
    spyReconcile.mockResolvedValueOnce({ ok: false, status: 'partial' });
    render(
      <WatchlistConfirmCard
        proposalData={{
          status: 'approved',
          actionType: 'watchlist_set_tier',
          reconcile_status: 'partial',
          job_id: 'job-partial',
          affected_tickers: [{ ticker: 'NVDA', old_tier: 'core', new_tier: 'radar' }],
          affected_workspaces: [
            { ticker: 'NVDA', workspace_id: 'ws-nvda', task_changes: [{ name: 'dalton-nvda-watch', change: 'pause' }] },
          ],
        }}
      />,
    );
    // Expand settled card to inspect localized task verbs and Retry button
    fireEvent.click(
      screen.getByRole('button', { name: /Watchlist updated|已更新观察名单/i }),
    );
    // Task change verb is localized (not raw '(pause)')
    expect(screen.queryByText(/\(pause\)/)).toBeNull();
    expect(screen.getByText(/\(暂停\)|\(Pause\)/)).toBeTruthy();
    const retryBtn = screen.getByRole('button', { name: /一键补齐同步|Retry sync/i });
    fireEvent.click(retryBtn);
    await waitFor(() => {
      expect(screen.getByText(/仍有部分任务未完成同步|still pending sync/i)).toBeTruthy();
    });
    // Retry button stays visible when still partial
    expect(screen.getByRole('button', { name: /一键补齐同步|Retry sync/i })).toBeTruthy();

    // Second click succeeds -> Retry button disappears & settled label updates
    spyReconcile.mockResolvedValueOnce({ ok: true, status: 'completed' });
    fireEvent.click(screen.getByRole('button', { name: /一键补齐同步|Retry sync/i }));
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /一键补齐同步|Retry sync/i })).toBeNull();
    });
    spyReconcile.mockRestore();
  });
});
