/**
 * An HITL resume reads its callbacks and runtime from the current render, not
 * the one that raised the interrupt, and runs what the composer shows now.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';
import { settleMountEffect } from './chatHookHarness';

vi.mock('react-i18next', () => {
  const t = (k: string) => k;
  return { useTranslation: () => ({ t }) };
});

vi.mock('@/lib/supabase', () => ({ supabase: null }));

// DALTON-HOOK(antigravity-series): enable Dalton build flag for activeTurnModel HITL admission tests
const brandMock = vi.hoisted(() => ({ isDaltonBuild: false }));
vi.mock('@/config/brand', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/brand')>()),
  get isDaltonBuild() {
    return brandMock.isDaltonBuild;
  },
}));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', async () => (await import('./chatHookHarness')).apiMockModule());

import { getHandoffLiveness, getWorkflowStatus, reconnectToWorkflowStream, replayThreadHistory, sendChatMessageStream, sendHitlResponse } from '../../utils/api';
import { postSSEStream } from '../../utils/api/transport';
import { useChatMessages } from '../useChatMessages';
import type { ModelOptions } from '../../session/types';

const mockSendStream = sendChatMessageStream as Mock;
// DALTON-HOOK(antigravity-series): constrain HITL resume mock to the real sendHitlResponse signature
const mockSendHitl = vi.mocked(sendHitlResponse);
const mockGetWorkflowStatus = getWorkflowStatus as Mock;
const mockGetHandoffLiveness = getHandoffLiveness as Mock;
const mockReconnectStream = reconnectToWorkflowStream as Mock;
const mockReplayHistory = replayThreadHistory as Mock;

const questionInterrupt = {
  event: 'interrupt',
  interrupt_id: 'int-ask',
  action_requests: [{ type: 'ask_user_question', question: 'Which tickers?', options: [], allow_multiple: false }],
};

const fileArtifact = {
  event: 'artifact',
  agent: 'main',
  artifact_type: 'file_operation',
  artifact_id: 'file-1',
  payload: { operation: 'write_file', file_path: 'report.md' },
};

function sendRaises(interrupt: Record<string, unknown>) {
  mockSendStream.mockImplementation(async (...args: unknown[]) => {
    (args[3] as { onEvent: (e: Record<string, unknown>) => void }).onEvent(interrupt);
    return { disconnected: false };
  });
}

/** The hook as ChatView mounts it, with the input a test flips. */
function renderChat(initial: {
  onFileArtifact: (e: unknown) => void;
  readComposerOptions?: () => ModelOptions | undefined;
  onRender?: (state: ReturnType<typeof useChatMessages>) => void;
}) {
  let props = initial;
  const rendered = renderHookWithProviders(() => {
    const state = useChatMessages('ws-test', 'thread-1', { onFileArtifact: props.onFileArtifact, readComposerOptions: props.readComposerOptions ?? null });
    props.onRender?.(state);
    return state;
  });
  const flip = (next: Partial<typeof initial>) => {
    props = { ...props, ...next };
    rendered.rerender();
  };
  return { ...rendered, flip };
}

describe('useChatMessages: HITL resume freshness', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    brandMock.isDaltonBuild = false;
    mockSendHitl.mockResolvedValue({ disconnected: false, aborted: false, contentLocation: null });
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: false, status: 'completed' });
    mockGetHandoffLiveness.mockResolvedValue({ liveness: [], runs: [], steers: [] });
    mockReconnectStream.mockResolvedValue({ disconnected: false, aborted: false, contentLocation: null });
    mockReplayHistory.mockResolvedValue(undefined);
  });

  it('routes the resumed stream to the callbacks of the current render', async () => {
    sendRaises(questionInterrupt);
    const armedWith = vi.fn();
    const current = vi.fn();
    const { result, flip } = renderChat({ onFileArtifact: armedWith });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names');
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    flip({ onFileArtifact: current });
    mockSendHitl.mockImplementation(async (_hitl, _ws, _tid, opts = {}) => {
      opts.onEvent?.(fileArtifact);
      return { disconnected: false, aborted: false, contentLocation: null };
    });
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA and AMD', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));
    expect(current).toHaveBeenCalledWith(expect.objectContaining({ artifact_id: 'file-1' }));
    expect(armedWith).not.toHaveBeenCalled();
  });

  it("runs the composer's picks as they are now, not the asking send's", async () => {
    sendRaises(questionInterrupt);
    let shown: ModelOptions = { model: 'model-a', reasoningEffort: 'high', fastMode: true };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    // The thread's level and speed changed while the question was open.
    shown = { model: 'model-a', reasoningEffort: 'low', fastMode: false };
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA and AMD', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));
    expect(mockSendHitl.mock.calls[0][3]).toMatchObject({ llmModel: 'model-a', reasoningEffort: 'low', fastMode: false });
  });

  // DALTON-HOOK(antigravity-series): R2 — a stale_fork refusal frame after onRunIdResolved must not leave activeTurnModel marked admitted
  it('does not mark activeTurnModel admitted when a paused resume receives a stale_fork frame after onRunIdResolved', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 7, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    let resolveStream!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockSendHitl.mockImplementation(async (_hitl, _ws, _tid, opts = {}) => {
      opts.onRunIdResolved?.('run-stale', 'thread-1');
      opts.onEvent?.({
        event: 'error',
        thread_id: 'thread-1',
        error: 'the branch moved',
        type: 'workflow_error',
        error_type: 'admission_conflict',
        code: 'stale_fork',
      });
      return new Promise((res) => { resolveStream = res; });
    });

    act(() => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));
    expect(result.current.activeTurnModel?.admitted).toBe(false);

    await act(async () => {
      resolveStream({ disconnected: false, aborted: false, contentLocation: null });
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.activeTurnModel).toBeNull();
  });

  // DALTON-HOOK(antigravity-series): R2 — START refusal frames (running, workspace_moving) stay unadmitted and re-arm pause
  it.each([
    ['running', 'another turn is running'],
    ['workspace_moving', 'workspace is moving'],
  ])('keeps activeTurnModel unadmitted and restores pause on START %s refusal frame', async (code, message) => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 11, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    let resolveStream!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockSendHitl.mockImplementation(async (_hitl, _ws, _tid, opts = {}) => {
      opts.onEvent?.({
        event: 'error',
        thread_id: 'thread-1',
        error: message,
        type: 'workflow_error',
        error_type: 'admission_conflict',
        code,
      });
      return new Promise((res) => { resolveStream = res; });
    });

    act(() => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));
    expect(result.current.activeTurnModel).toEqual({
      model: 'gemini-4-neon-high-oauth',
      pickSeq: 11,
      admitted: false,
    });

    await act(async () => {
      resolveStream({ disconnected: false, aborted: false, contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-refused' });
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.pendingInterrupt).not.toBeNull();
    // DALTON-HOOK(antigravity-series): R2 — refused START bubble must be marked unadmitted so the next successful resume takes the right turnIndex
    const refusedBubble = result.current.messages.filter((m) => m.role === 'assistant').at(-1);
    expect(refusedBubble).toMatchObject({ unadmitted: true });

    mockSendHitl.mockImplementationOnce(async (_hitl, _ws, _tid, opts = {}) => {
      opts.onRunIdResolved?.('run-after-refusal', 'thread-1');
      opts.onEvent?.({ event: 'metadata', thread_id: 'thread-1', run_id: 'run-after-refusal' });
      opts.onEvent?.({ event: 'message_chunk', content: 'resumed ok', content_type: 'text', finish_reason: 'stop' });
      return { disconnected: false, aborted: false, contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-after-refusal' };
    });
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const resumedBubble = result.current.messages.filter((m) => m.role === 'assistant').at(-1);
    expect(resumedBubble?.unadmitted).toBeFalsy();
    expect(resumedBubble?.turnIndex).toBe(1);
  });

  // DALTON-HOOK(antigravity-series): R2 — real SSE retry/resend frame without run_id through postSSEStream must not consume requestKey, must not settle pause card, and must mark placeholder unadmitted
  it('keeps requestKey unspent and pause card pending when real SSE transport receives retry/resend without run_id', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 21, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    const rawRetrySse = [
      'event: retry',
      `data: ${JSON.stringify({
        message: 'Temporary error occurred, you can retry or resume the turn',
        thread_id: 'thread-1',
        auto_retry: true,
        error_type: 'database_error',
        error_class: 'OperationalError',
        retry_count: 1,
        max_retries: 3,
        recovery: 'resend',
      })}`,
      '',
      '',
    ].join('\n');
    const originalFetch = globalThis.fetch;
    const fetchSpy = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({
        'Content-Location': '/api/v1/threads/thread-1/messages/stream?run_id=run-pre-db-fail',
      }),
      body: {
        getReader: () => {
          let sent = false;
          const bytes = new TextEncoder().encode(rawRetrySse);
          return {
            read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: bytes })),
          };
        },
      },
    });
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    const observedAdmitted: Array<boolean | undefined> = [];
    const capturedKeys: Array<string | null | undefined> = [];
    mockSendHitl.mockImplementationOnce(async (_hitl, _ws, tid, opts = {}) => {
      capturedKeys.push(opts.requestKey);
      return await postSSEStream(`/api/v1/threads/${tid}/messages`, {}, {
        onEvent: opts.onEvent ?? (() => {}),
        onRunIdResolved: opts.onRunIdResolved ?? null,
        signal: opts.signal ?? null,
      });
    });

    try {
      await act(async () => {
        result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
        await new Promise((r) => setTimeout(r, 0));
        observedAdmitted.push(result.current.activeTurnModel?.admitted);
      });
      await waitFor(() => expect(result.current.isLoading).toBe(false));
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(observedAdmitted.every((v) => v !== true)).toBe(true);
    expect(result.current.pendingInterrupt).not.toBeNull();
    const unstartedBubble = result.current.messages.filter((m) => m.role === 'assistant').at(-1);
    expect(unstartedBubble).toMatchObject({ unadmitted: true });

    // Clicking Answer again must reuse the EXACT same requestKey (proving transport did not consume it).
    mockSendHitl.mockImplementationOnce(async (_hitl, _ws, _tid, opts = {}) => {
      capturedKeys.push(opts.requestKey);
      opts.onRunIdResolved?.('run-retry-ok', 'thread-1');
      opts.onEvent?.({ event: 'metadata', thread_id: 'thread-1', run_id: 'run-retry-ok' });
      return { disconnected: false, aborted: false, contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-retry-ok' };
    });
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(capturedKeys[0]).toBeTruthy();
    expect(capturedKeys[1]).toBe(capturedKeys[0]);
    expect(result.current.pendingInterrupt).toBeNull();
  });

  // DALTON-HOOK(antigravity-series): R2 — pre-first-frame disconnect & empty-body close verify against this request's run_id (live, terminal completed, proven untouched pause, and indeterminate reload)
  it('verifies pre-first-frame disconnect and empty-body close by run_id: confirms terminal/live runs, re-arms proven untouched pause, and reloads without confirming when indeterminate', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 12, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    // 1) Proven untouched pause: disconnect before first frame; /status still shows interrupted at turn 0 and liveness has no run -> unadmitted & re-arms pause.
    const observedDuringUnstarted: Array<boolean | undefined> = [];
    mockGetWorkflowStatus.mockResolvedValueOnce({ can_reconnect: false, status: 'interrupted', latest_turn_index: 0 });
    mockGetHandoffLiveness.mockResolvedValueOnce({ liveness: [], runs: [], steers: [] });
    mockSendHitl.mockImplementationOnce(async () => ({
      disconnected: true,
      aborted: false,
      contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-unstarted',
    }));

    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
      observedDuringUnstarted.push(result.current.activeTurnModel?.admitted);
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(observedDuringUnstarted.every((v) => v !== true)).toBe(true);
    expect(result.current.pendingInterrupt).not.toBeNull();

    // 2) Empty-body close where run is already live -> verifyDisconnect confirms this run_id and reconnects.
    let resolveEmptyLive!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: true, run_id: 'run-empty-live', status: 'running', active_tasks: [] });
    mockReconnectStream.mockImplementationOnce(() => new Promise((res) => { resolveEmptyLive = res; }));
    mockSendHitl.mockImplementationOnce(async () => ({
      disconnected: false,
      aborted: false,
      contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-empty-live',
    }));
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect(result.current.activeTurnModel).toEqual({
      model: 'gemini-4-neon-high-oauth',
      pickSeq: 12,
      admitted: true,
    }));
    await act(async () => {
      resolveEmptyLive({ disconnected: false, aborted: false, contentLocation: null });
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
  });

  // DALTON-HOOK(antigravity-series): R2 — already-completed resume turn (disconnect or empty close) is confirmed via run_id liveness and reloads history instead of falsely re-arming pause
  it.each([
    ['pre-first-frame disconnect', true],
    ['empty-body clean close', false],
  ])('confirms an already-completed HITL resume on %s via run_id liveness and triggers history reload', async (_label, disconnected) => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 14, reasoningEffort: null, fastMode: false };
    const admittedSnapshots: Array<boolean | undefined> = [];
    const { result } = renderChat({
      onFileArtifact: vi.fn(),
      readComposerOptions: () => shown,
      onRender: (s) => { if (s.activeTurnModel) admittedSnapshots.push(s.activeTurnModel.admitted); },
    });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());
    admittedSnapshots.length = 0;

    const replaysBefore = mockReplayHistory.mock.calls.length;
    // /status reports terminal (run_id: null, can_reconnect: false) because the resume already finished;
    // /dispatches/liveness confirms run-already-done exists in terminal state.
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: false, run_id: null, status: 'completed', latest_turn_index: 1 });
    mockGetHandoffLiveness.mockResolvedValueOnce({
      liveness: [],
      runs: [{ run_id: 'run-already-done', thread_id: 'thread-1', status: 'completed' }],
      steers: [],
    });
    let resolveHitl!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockSendHitl.mockImplementationOnce(() => new Promise((res) => { resolveHitl = res; }));

    act(() => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
    });
    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveHitl({
        disconnected,
        aborted: false,
        contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-already-done',
      });
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(mockReplayHistory.mock.calls.length).toBeGreaterThan(replaysBefore));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(mockGetHandoffLiveness).toHaveBeenCalledWith({ threads: [], runs: ['run-already-done'], steers: [] });
    expect(admittedSnapshots).toContain(true);
    expect(result.current.pendingInterrupt).toBeNull();
  });

  // DALTON-HOOK(antigravity-series): R2 — indeterminate terminal status keeps history reload while keeping activeTurnModel unconfirmed
  it.each([
    ['pre-first-frame disconnect', true],
    ['empty-body clean close', false],
  ])('preserves history reload and keeps model pick unconfirmed on %s when status cannot prove outcome', async (_label, disconnected) => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 15, reasoningEffort: null, fastMode: false };
    const observedAdmitted: Array<boolean | undefined> = [];
    const { result } = renderChat({
      onFileArtifact: vi.fn(),
      readComposerOptions: () => shown,
      onRender: (s) => { if (s.activeTurnModel) observedAdmitted.push(s.activeTurnModel.admitted); },
    });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());
    observedAdmitted.length = 0;

    const replaysBefore = mockReplayHistory.mock.calls.length;
    // /status reports completed with no run_id, and liveness does not contain the run -> indeterminate.
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: false, run_id: null, status: 'completed' });
    mockGetHandoffLiveness.mockResolvedValueOnce({ liveness: [], runs: [], steers: [] });
    mockSendHitl.mockImplementationOnce(async () => ({
      disconnected,
      aborted: false,
      contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-unknown',
    }));

    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(mockReplayHistory.mock.calls.length).toBeGreaterThan(replaysBefore));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(observedAdmitted).not.toContain(true);
  });

  // DALTON-HOOK(antigravity-series): R2 — normal completion with real admission signal marks activeTurnModel admitted: true
  it('marks activeTurnModel admitted on normal HITL resume completion after real admission signal', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 13, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    let resolveStream!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockSendHitl.mockImplementationOnce(async (_hitl, _ws, _tid, opts = {}) => {
      opts.onRunIdResolved?.('run-ok', 'thread-1');
      opts.onEvent?.({ event: 'metadata', thread_id: 'thread-1', run_id: 'run-ok' });
      opts.onEvent?.({ event: 'message_chunk', content: 'done', content_type: 'text', finish_reason: 'stop' });
      return new Promise((res) => { resolveStream = res; });
    });

    act(() => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(result.current.activeTurnModel).toEqual({
      model: 'gemini-4-neon-high-oauth',
      pickSeq: 13,
      admitted: true,
    }));

    await act(async () => {
      resolveStream({ disconnected: false, aborted: false, contentLocation: '/api/v1/threads/thread-1/messages/stream?run_id=run-ok' });
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.pendingInterrupt).toBeNull();
  });

  // DALTON-HOOK(antigravity-series): R5b — two HITL submissions with different models under the same requestKey must confirm the first-submitted old run's model on 409, not the newly read pick
  it('confirms the first-submitted model and pickSeq (not the newly read pick) when a second HITL resume under the same requestKey adopts a 409 duplicate run', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    let shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 9, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    // Submission 1: user submits High (seq 9); server accepts it, but client loses the response.
    mockSendHitl.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    // User switches composer to Low (seq 10) before retrying the same answer.
    shown = { model: 'gemini-4-neon-low-oauth', modelPickSeq: 10, reasoningEffort: null, fastMode: false };

    // Submission 2: reuses the same requestKey; server returns 409 duplicate_request pointing to the old High run.
    let resolveReconnect!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: true, run_id: 'run-dup-hitl', status: 'in_progress', active_tasks: [] });
    mockReconnectStream.mockImplementationOnce(() => new Promise((res) => { resolveReconnect = res; }));
    const dupErr = Object.assign(new Error('This request was already accepted'), {
      status: 409,
      errorInfo: {
        code: 'duplicate_request',
        run_id: 'run-dup-hitl',
        thread_id: 'thread-1',
        run_status: 'in_progress',
      },
    });
    mockSendHitl.mockRejectedValueOnce(dupErr);

    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(mockSendHitl).toHaveBeenCalledTimes(2);
    expect(mockSendHitl.mock.calls[0][3]?.requestKey).toBeTruthy();
    expect(mockSendHitl.mock.calls[1][3]?.requestKey).toBe(mockSendHitl.mock.calls[0][3]?.requestKey);
    expect(mockSendHitl.mock.calls[0][3]?.llmModel).toBe('gemini-4-neon-high-oauth');
    expect(mockSendHitl.mock.calls[1][3]?.llmModel).toBe('gemini-4-neon-low-oauth');

    // Must confirm High (seq 9) from submission 1, NOT Low (seq 10) from submission 2.
    await waitFor(() => expect(result.current.activeTurnModel).toEqual({
      model: 'gemini-4-neon-high-oauth',
      pickSeq: 9,
      admitted: true,
    }));

    await act(async () => {
      resolveReconnect({ disconnected: false, aborted: false, contentLocation: null });
    });
  });

  // DALTON-HOOK(antigravity-series): R5b — when 409 duplicate_request names the old run's actual llm_model, verify against it instead of confirming a mismatched pick
  it('cross-checks errorInfo.llm_model on 409 duplicate_request and avoids confirming a mismatched pickSeq', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-low-oauth', modelPickSeq: 15, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    let resolveReconnect!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: true, run_id: 'run-dup-other', status: 'in_progress', active_tasks: [] });
    mockReconnectStream.mockImplementationOnce(() => new Promise((res) => { resolveReconnect = res; }));
    mockSendHitl.mockRejectedValueOnce(Object.assign(new Error('This request was already accepted'), {
      status: 409,
      errorInfo: {
        code: 'duplicate_request',
        run_id: 'run-dup-other',
        thread_id: 'thread-1',
        run_status: 'in_progress',
        llm_model: 'gemini-4-neon-high-oauth',
      },
    }));

    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(result.current.activeTurnModel).toEqual({
      model: 'gemini-4-neon-high-oauth',
      pickSeq: null,
      admitted: true,
    }));

    await act(async () => {
      resolveReconnect({ disconnected: false, aborted: false, contentLocation: null });
    });
  });

  // DALTON-HOOK(antigravity-series): R5 — when submission 1 sent model: null (relying on saved High) and 409 omits llm_model, activeTurnModel marks modelUnknown: true so the newly picked Low is never consumed
  it('marks adopted duplicate as modelUnknown when submission 1 omitted llm_model and 409 carries no llm_model', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    // Submission 1 omits llm_model (model: null, relying on saved High on the thread row).
    let shown: ModelOptions = { model: null, modelPickSeq: null, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    mockSendHitl.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    // User picks and saves Low (seq 20) before retrying the same answer.
    shown = { model: 'gemini-4-neon-low-oauth', modelPickSeq: 20, reasoningEffort: null, fastMode: false };

    let resolveReconnect!: (v: { disconnected: boolean; aborted: boolean; contentLocation: string | null }) => void;
    mockGetWorkflowStatus.mockResolvedValue({ can_reconnect: true, run_id: 'run-dup-unknown', status: 'in_progress', active_tasks: [] });
    mockReconnectStream.mockImplementationOnce(() => new Promise((res) => { resolveReconnect = res; }));
    mockSendHitl.mockRejectedValueOnce(Object.assign(new Error('This request was already accepted'), {
      status: 409,
      errorInfo: {
        code: 'duplicate_request',
        run_id: 'run-dup-unknown',
        thread_id: 'thread-1',
        run_status: 'in_progress',
      },
    }));

    await act(async () => {
      result.current.handleAnswerQuestion('NVDA', 'int-ask', 'int-ask');
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(result.current.activeTurnModel).toEqual({
      model: null,
      pickSeq: null,
      admitted: true,
      modelUnknown: true,
    }));

    await act(async () => {
      resolveReconnect({ disconnected: false, aborted: false, contentLocation: null });
    });
  });
});
