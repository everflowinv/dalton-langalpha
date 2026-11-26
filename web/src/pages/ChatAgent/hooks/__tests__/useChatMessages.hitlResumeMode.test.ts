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

import { sendChatMessageStream, sendHitlResponse } from '../../utils/api';
import { useChatMessages } from '../useChatMessages';
import type { ModelOptions } from '../../session/types';

const mockSendStream = sendChatMessageStream as Mock;
const mockSendHitl = sendHitlResponse as Mock;

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
function renderChat(initial: { onFileArtifact: (e: unknown) => void; readComposerOptions?: () => ModelOptions | undefined }) {
  let props = initial;
  const rendered = renderHookWithProviders(() =>
    useChatMessages('ws-test', 'thread-1', { onFileArtifact: props.onFileArtifact, readComposerOptions: props.readComposerOptions ?? null }),
  );
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
    mockSendHitl.mockResolvedValue({ disconnected: false });
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
    mockSendHitl.mockImplementation(async (...args: unknown[]) => {
      (args[3] as { onEvent: (e: Record<string, unknown>) => void }).onEvent(fileArtifact);
      return { disconnected: false };
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

    let resolveStream!: (v: { disconnected: boolean; aborted: boolean }) => void;
    mockSendHitl.mockImplementation(async (...args: unknown[]) => {
      const opts = args[3] as {
        onEvent: (e: Record<string, unknown>) => void;
        onRunIdResolved?: (id: string) => void;
      };
      opts.onRunIdResolved?.('run-stale');
      opts.onEvent({
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
      resolveStream({ disconnected: false, aborted: false });
    });
  });

  // DALTON-HOOK(antigravity-series): R5b — adopting a 409 duplicate_request on HITL resume must mark activeTurnModel admitted: true
  it('marks activeTurnModel admitted when HITL resume adopts a 409 duplicate_request run', async () => {
    brandMock.isDaltonBuild = true;
    sendRaises(questionInterrupt);
    const shown: ModelOptions = { model: 'gemini-4-neon-high-oauth', modelPickSeq: 9, reasoningEffort: null, fastMode: false };
    const { result } = renderChat({ onFileArtifact: vi.fn(), readComposerOptions: () => shown });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', null, null, shown);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

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

    await waitFor(() => expect(result.current.activeTurnModel).toEqual({
      model: 'gemini-4-neon-high-oauth',
      pickSeq: 9,
      admitted: true,
    }));
  });
});
