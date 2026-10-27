/**
 * DALTON-HOOK(composer-drafts): the send path reports acceptance — the first
 * sign the server took the send — through SendOptions.onAccepted, so the
 * composer's stored draft settles sent before the stream ends (a reload
 * mid-stream must not restore an already-sent message) and a user stop on a
 * brand-new thread (whose message is already in the transcript) does not
 * restore its text over the record.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('@/components/ui/use-toast', () => ({
  toast: vi.fn(),
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', async () => (await import('./chatHookHarness')).apiMockModule());

vi.mock('../../session/threadCreation', () => ({
  ensureThreadId: vi.fn().mockResolvedValue({ threadId: 'thread-created', aborted: false }),
}));

vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: null, isLoading: false, isError: false }),
}));

import { sendChatMessageStream } from '../../utils/api';
import { ensureThreadId } from '../../session/threadCreation';
import { useChatMessages } from '../useChatMessages';
import { useComposerDraft, load } from '@/lib/dalton/chatDrafts';

const mockSendStream = sendChatMessageStream as Mock;
const mockEnsureThreadId = ensureThreadId as Mock;

type OnEvent = (e: Record<string, unknown>) => void;
interface StreamOptions {
  onEvent: OnEvent;
  onRunIdResolved?: (runId: string, threadId: string | null) => void;
}

describe('useChatMessages – send acceptance (DALTON composer drafts)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockEnsureThreadId.mockResolvedValue({ threadId: 'thread-created', aborted: false });
  });

  it('fires onAccepted once the first metadata carries a run_id, before the stream ends', async () => {
    // The stream hangs after resolving its run id: a long-running turn whose
    // promise has not settled. Acceptance must fire in there.
    mockSendStream.mockImplementation(
      async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
        opts.onEvent?.({ event: 'thread_id', thread_id: 'thread-accept-1' });
        opts.onRunIdResolved?.('run-accept-1', 'thread-accept-1');
        // The header's run id is minted before admission: not acceptance yet.
        await Promise.resolve();
        opts.onEvent?.({ event: 'metadata', thread_id: 'thread-accept-1', run_id: 'run-accept-1' } as never);
        await new Promise(() => {});
        return { disconnected: false };
      },
    );

    const accepted = vi.fn();
    const { result } = renderHookWithProviders(() => useChatMessages('ws-test'));

    let sendPromise!: Promise<boolean>;
    act(() => {
      sendPromise = result.current.handleSendMessage('long running analysis', null, null, {
        onAccepted: accepted,
      });
    });

    await waitFor(() => {
      expect(accepted).toHaveBeenCalledTimes(1);
    });
    // The send itself is still pending — acceptance arrived first.
    let settled = false;
    void sendPromise.then(() => { settled = true; });
    expect(settled).toBe(false);
  });

  it('does not fire onAccepted more than once when several acceptance signals land', async () => {
    // run_id resolved AND the stream then ends successfully: one callback.
    mockSendStream.mockImplementation(
      async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
        opts.onEvent?.({ event: 'thread_id', thread_id: 'thread-accept-2' });
        opts.onRunIdResolved?.('run-accept-2', 'thread-accept-2');
        opts.onEvent?.({ event: 'metadata', thread_id: 'thread-accept-2', run_id: 'run-accept-2' } as never);
        opts.onEvent?.({ event: 'metadata', thread_id: 'thread-accept-2', run_id: 'run-accept-2' } as never);
        return { disconnected: false };
      },
    );

    const accepted = vi.fn();
    const { result } = renderHookWithProviders(() => useChatMessages('ws-test'));

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.handleSendMessage('short one', null, null, {
        onAccepted: accepted,
      });
    });

    expect(accepted).toHaveBeenCalledTimes(1);
    expect(outcome).toBe(true);
  });

  it('does not settle on the header run id when admission then refuses the send', async () => {
    // Content-Location carries a run id minted before admission; the server can
    // still refuse (compaction, stop, capacity) before START. The draft must stay.
    mockSendStream.mockImplementation(
      async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
        opts.onEvent?.({ event: 'thread_id', thread_id: 'thread-accept-3' });
        opts.onRunIdResolved?.('run-accept-3', 'thread-accept-3');
        opts.onEvent?.({ event: 'error', error: 'compacting', recovery: 'resend' } as never);
        return { disconnected: false };
      },
    );

    const accepted = vi.fn();
    const { result } = renderHookWithProviders(() => useChatMessages('ws-test'));
    await act(async () => {
      await result.current.handleSendMessage('refused after headers', null, null, {
        onAccepted: accepted,
      });
    });

    expect(accepted).not.toHaveBeenCalled();
  });

  it('treats a send stopped while the thread is still being created as accepted — the message is already in the transcript', async () => {
    // threadId stays '__default__' and the pre-create round-trip reports the
    // user's stop: the user bubble is on screen, so the draft settles sent
    // (no restore over the record) while the boolean stays false (no turn ran).
    mockEnsureThreadId.mockResolvedValue({ threadId: '__default__', aborted: true });
    mockSendStream.mockResolvedValue({ disconnected: false });

    const accepted = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useChatMessages('ws-test', '__default__'),
    );

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.handleSendMessage('stopped mid-create', null, null, {
        onAccepted: accepted,
      });
    });

    expect(accepted).toHaveBeenCalledTimes(1);
    expect(outcome).toBe(false);
    // The user's message stayed in the transcript — the reason the draft
    // settles sent instead of coming back.
    const bubble = result.current.messages.find((m) => m.role === 'user');
    expect(bubble?.content).toBe('stopped mid-create');
  });

  it('fires onAccepted when a steer sent mid-stream is accepted', async () => {
    // First send hangs with a thread_id so isLoading stays true; the second
    // send goes the steering route and is accepted by the running turn.
    mockSendStream.mockImplementation(
      async (
        _msg: string,
        _ws: string,
        _tid: string | null,
        opts: StreamOptions,
      ) => {
        if (mockSendStream.mock.calls.length === 1) {
          opts.onEvent?.({ event: 'thread_id', thread_id: 'thread-steer' });
          await new Promise(() => {});
          return { disconnected: false };
        }
        opts.onEvent?.({ event: 'steering_accepted', position: 1 });
        return { disconnected: false };
      },
    );

    const steerAccepted = vi.fn();
    const { result } = renderHookWithProviders(() => useChatMessages('ws-test'));

    act(() => {
      void result.current.handleSendMessage('first turn');
    });
    await waitFor(() => {
      expect(result.current.isLoading).toBe(true);
    });

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.handleSendMessage('a steer mid-stream', null, null, {
        onAccepted: steerAccepted,
      });
    });

    expect(steerAccepted).toHaveBeenCalledTimes(1);
    expect(outcome).toBe(true);
  });

  // DALTON-HOOK(composer-drafts): a send parked during compaction is not
  // acceptance — the message may still be dropped by a stop, so the composer
  // draft stays live until the flushed send goes out (accepted → draft
  // cleared) or the queue is dropped (settled as failed → draft restored).
  describe('a send queued during compaction (DALTON composer drafts)', () => {
    const DRAFT_KEY = 'thread:compaction-draft';

    /** The chat hook bound to a real composer draft, the way ChatInput wires them. */
    function useDraftChatHarness() {
      const chat = useChatMessages('ws-test');
      const composer = useComposerDraft({ draftKey: DRAFT_KEY });
      return { chat, composer };
    }

    /** Parks a composer send while compacting. The real acceptance callback
     *  (the composer's acceptSend) stays on the send; `accepted` observes it. */
    function parkWhileCompacting(accepted: Mock) {
      const { result } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('held while compacting');
      });
      act(() => {
        result.current.chat.setIsCompacting('summarize');
      });
      let sendPromise!: Promise<boolean>;
      act(() => {
        result.current.composer.submitWithDraft((onAccepted) => {
          sendPromise = result.current.chat.handleSendMessage(
            'held while compacting',
            null,
            null,
            { onAccepted: () => { onAccepted(); accepted(); } },
          );
          return sendPromise;
        });
      });
      return { result, sendPromise };
    }

    beforeEach(() => {
      localStorage.clear();
    });

    it('does not accept the parked send — its promise stays open and the draft stays live', () => {
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          opts.onEvent?.({ event: 'metadata', thread_id: 'thread-queued', run_id: 'run-queued' } as never);
          return { disconnected: false };
        },
      );

      const accepted = vi.fn();
      const { result, sendPromise } = parkWhileCompacting(accepted);

      // Parked, not taken: no stream, no acceptance, and the send's outcome is
      // still undecided — a stop may yet drop the message.
      expect(mockSendStream).not.toHaveBeenCalled();
      expect(accepted).not.toHaveBeenCalled();
      let settled = false;
      void sendPromise.then(() => { settled = true; });
      expect(settled).toBe(false);
      expect(result.current.chat.queuedSend).toBe('held while compacting');
      // The composer input is clear for the next thought, but the draft text
      // survives in storage — the queue cleared nothing.
      expect(result.current.composer.message).toBe('');
      expect(load(DRAFT_KEY)).toBe('held while compacting');
    });

    it('settles the parked send as failed when the user stops, so the draft returns to the composer', async () => {
      mockSendStream.mockResolvedValue({ disconnected: false });

      const accepted = vi.fn();
      const { result, sendPromise } = parkWhileCompacting(accepted);
      expect(result.current.chat.queuedSend).toBe('held while compacting');

      await act(async () => {
        await result.current.chat.stopWorkflow();
        await sendPromise;
      });

      // The parked message never reached the server: the send resolves false,
      // which the draft lifecycle maps to a restore — the text is back in the
      // input and still in storage.
      expect(mockSendStream).not.toHaveBeenCalled();
      expect(accepted).not.toHaveBeenCalled();
      expect(result.current.chat.queuedSend).toBe(false);
      expect(result.current.composer.message).toBe('held while compacting');
      expect(load(DRAFT_KEY)).toBe('held while compacting');
    });

    it('accepts the parked send once compaction finishes and it actually goes out, clearing the draft', async () => {
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          opts.onEvent?.({ event: 'thread_id', thread_id: 'thread-queued' });
          opts.onRunIdResolved?.('run-queued', 'thread-queued');
          // The header's run id is minted before admission: not acceptance yet.
          await Promise.resolve();
          opts.onEvent?.({ event: 'metadata', thread_id: 'thread-queued', run_id: 'run-queued' } as never);
          return { disconnected: false };
        },
      );

      const accepted = vi.fn();
      const { result, sendPromise } = parkWhileCompacting(accepted);
      expect(accepted).not.toHaveBeenCalled();
      expect(result.current.chat.queuedSend).toBe('held while compacting');

      let outcome: boolean | undefined;
      await act(async () => {
        result.current.chat.setIsCompacting(false);
        await Promise.resolve();
      });
      // The flushed send goes out and its metadata frame reports acceptance.
      await waitFor(() => {
        expect(accepted).toHaveBeenCalledTimes(1);
      });
      await act(async () => {
        outcome = await sendPromise;
        await Promise.resolve();
      });

      // The flushed send went out for real: acceptance fired at its metadata
      // frame, the deferred outcome settled true, and the draft is cleared.
      expect(mockSendStream).toHaveBeenCalledTimes(1);
      expect(mockSendStream.mock.calls[0][0]).toBe('held while compacting');
      expect(outcome).toBe(true);
      expect(result.current.chat.queuedSend).toBe(false);
      expect(result.current.composer.message).toBe('');
      expect(load(DRAFT_KEY)).toBe('');
    });
  });
});
