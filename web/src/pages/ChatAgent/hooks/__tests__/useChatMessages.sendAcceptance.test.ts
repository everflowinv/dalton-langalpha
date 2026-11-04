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

  it('returns the draft when the user stops while the thread is still being created — the POST never went out', async () => {
    // threadId stays '__default__' and the pre-create round-trip reports the
    // user's stop. The stream request was never sent, so the server holds
    // nothing: the draft comes back whole (no acceptance) and the optimistic
    // bubble, local only, leaves with a reload.
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

    expect(accepted).not.toHaveBeenCalled();
    expect(outcome).toBe(false);
    // The draft is back in the input, not settled over the local bubble.
    expect(result.current.messages.find((m) => m.role === 'user')?.content).toBe('stopped mid-create');
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
    function useDraftChatHarness(threadId?: string) {
      const chat = useChatMessages('ws-test', threadId);
      const composer = useComposerDraft({ draftKey: DRAFT_KEY });
      return { chat, composer };
    }

    /** Parks a composer send while compacting. The real acceptance callback
     *  (the composer's acceptSend) stays on the send; `accepted` observes it.
     *  `getTid` lets a test re-render the harness onto another thread. */
    function parkWhileCompacting(accepted: Mock, getTid?: () => string | undefined) {
      const { result, rerender, unmount } = renderHookWithProviders(() => useDraftChatHarness(getTid?.()));
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
      return { result, rerender, unmount, sendPromise };
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

    it('settles the parked composer send as failed and restores its draft when a widget send replaces it in the queue', async () => {
      mockSendStream.mockResolvedValue({ disconnected: false });

      const accepted = vi.fn();
      const { result, sendPromise } = parkWhileCompacting(accepted);
      expect(result.current.chat.queuedSend).toBe('held while compacting');
      expect(result.current.composer.message).toBe('');

      // A widget prompt arrives mid-compaction with no onAccepted callback,
      // replacing the parked composer send in queuedSendRef.
      let outcome: boolean | undefined;
      let widgetOutcome: boolean | undefined;
      await act(async () => {
        widgetOutcome = await result.current.chat.handleSendMessage('widget prompt B');
        outcome = await sendPromise;
      });

      // Displaced composer send settles false → draft returns to the input;
      // widget send B takes the queue as usual.
      expect(accepted).not.toHaveBeenCalled();
      expect(outcome).toBe(false);
      expect(widgetOutcome).toBe(true);
      expect(result.current.composer.message).toBe('held while compacting');
      expect(load(DRAFT_KEY)).toBe('held while compacting');
      expect(result.current.chat.queuedSend).toBe('widget prompt B');
    });

    it('settles the parked send as failed when switching threads mid-compaction', async () => {
      mockSendStream.mockResolvedValue({ disconnected: false });

      let tid: string | undefined = 'thread-A';
      const accepted = vi.fn();
      const { result, rerender, sendPromise } = parkWhileCompacting(accepted, () => tid);
      expect(result.current.chat.queuedSend).toBe('held while compacting');

      let outcome: boolean | undefined;
      await act(async () => {
        tid = 'thread-B';
        rerender();
        outcome = await sendPromise;
      });

      expect(accepted).not.toHaveBeenCalled();
      expect(outcome).toBe(false);
      expect(result.current.chat.queuedSend).toBe(false);
      expect(load(DRAFT_KEY)).toBe('held while compacting');
    });

    it('settles the parked send as failed when the component unmounts mid-compaction', async () => {
      mockSendStream.mockResolvedValue({ disconnected: false });

      const accepted = vi.fn();
      const { unmount, sendPromise } = parkWhileCompacting(accepted);

      let outcome: boolean | undefined;
      await act(async () => {
        unmount();
        outcome = await sendPromise;
      });

      expect(accepted).not.toHaveBeenCalled();
      expect(outcome).toBe(false);
      expect(load(DRAFT_KEY)).toBe('held while compacting');
    });
  });

  // #675 sends the response headers while the stream is still setting up, so a
  // drop after them is no longer proof the server took the send. The draft
  // settles only on what proves the server holds the turn — a first metadata
  // frame, a recovery naming the run, an adopted duplicate — and while a
  // scheduled resend still owes that proof, the stored draft keeps its
  // inFlight mark so a reload restores the text with the notice.
  describe('a send that only saw the response headers', () => {
    const DRAFT_KEY = 'thread:headers-only';

    /** The chat hook bound to a real composer draft, the way ChatInput wires them. */
    function useDraftChatHarness() {
      const chat = useChatMessages('ws-test');
      const composer = useComposerDraft({ draftKey: DRAFT_KEY });
      return { chat, composer };
    }

    /** The stored record under the harness's key, or null. */
    const storedDraft = (): { text?: string; inFlight?: boolean } | null => {
      const raw = localStorage.getItem(`dalton.chatDraft.v2:anon:${DRAFT_KEY}`);
      return raw ? (JSON.parse(raw) as { text?: string; inFlight?: boolean }) : null;
    };

    /** Lets timer-scheduled microtasks run between fake-clock steps. */
    async function settle(ms: number) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });
      for (let i = 0; i < 5; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
      }
    }

    beforeEach(() => {
      localStorage.clear();
    });

    afterEach(() => {
      localStorage.clear();
    });

    it('keeps the draft inFlight while the auto-resend waits, and settles only when the resend is taken', async () => {
      // The server's own recoverable failure — the retry frame error_handling
      // emits, auto_retry on and no run_id (the run never started) — then the
      // drop. The resend that answers (call 2) starts its run and its first
      // frame is what proves the message arrived.
      const requestKeyOf = (args: unknown[]) => (args[3] as { requestKey?: string }).requestKey;
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          if (mockSendStream.mock.calls.length === 1) {
            opts.onEvent?.({
              event: 'retry',
              message: 'Temporary error occurred, you can retry or resume the turn',
              auto_retry: true,
              error_type: 'timeout_error',
              error_class: 'RuntimeError',
              retry_count: 1,
              max_retries: 3,
              recovery: 'resend',
            } as never);
            return { disconnected: true };
          }
          opts.onEvent?.({ event: 'metadata', thread_id: 'thread-headers', run_id: 'run-headers-2' } as never);
          return { disconnected: false };
        },
      );

      const accepted = vi.fn();
      const handedOff = vi.fn();
      const { result, unmount } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('dropped in setup');
      });

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      let sendPromise!: Promise<boolean>;
      try {
        act(() => {
          result.current.composer.submitWithDraft((onAccepted, onHandedOff) => {
            sendPromise = result.current.chat.handleSendMessage('dropped in setup', null, null, {
              onAccepted: () => {
                onAccepted();
                accepted();
              },
              onHandedOff: () => {
                onHandedOff();
                handedOff();
              },
            });
            return sendPromise;
          });
        });
        await settle(0);

        // The backoff (1s) still owes the resend: nothing says the server took
        // the message. The send parks the draft — the stored copy keeps its
        // inFlight mark, which a reload restores with the notice — and the
        // input stays clear for the resend that owns the message.
        expect(mockSendStream).toHaveBeenCalledTimes(1);
        expect(handedOff).toHaveBeenCalledTimes(1);
        expect(accepted).not.toHaveBeenCalled();
        expect(result.current.composer.message).toBe('');
        expect(storedDraft()).toMatchObject({ text: 'dropped in setup', inFlight: true });

        // The server said this attempt failed before any run, so upstream sends
        // the resend as a new request (useChatMessages.autoRetry.test.ts,
        // "resends the same message as a send when the server started no run");
        // its first frame is what settles the draft.
        await settle(1000);
        expect(mockSendStream).toHaveBeenCalledTimes(2);
        expect(requestKeyOf(mockSendStream.mock.calls[1])).not.toBe(requestKeyOf(mockSendStream.mock.calls[0]));

        // The parked draft settles through the resend's acceptance alone; the
        // promise's own true (it resolves once the turn is handed off) settled
        // nothing.
        expect(accepted).toHaveBeenCalledTimes(1);
        expect(storedDraft()).toBeNull();
        let resolved: boolean | undefined;
        await act(async () => {
          void sendPromise.then((v) => { resolved = v; });
          await Promise.resolve();
        });
        expect(resolved).toBe(true);
      } finally {
        vi.useRealTimers();
      }
      unmount();
    });

    it('does not settle accepted when no resend can take the send — the draft returns to the composer', async () => {
      // A manual-recovery failure arrived before the drop — the non-recoverable
      // `error` frame shape the server emits with a recovery named and no
      // auto_retry — so no scheduled resend applies and nothing the stream
      // reported says the server kept the send.
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          opts.onEvent?.({
            event: 'error',
            error: 'Workspace ws-test not found',
            type: 'workflow_error',
            error_type: 'workspace_not_found',
            error_class: 'ValueError',
            recovery: 'resend',
          } as never);
          return { disconnected: true };
        },
      );

      const accepted = vi.fn();
      const { result } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('headers only, then dropped');
      });
      let sendPromise!: Promise<boolean>;
      let outcome: boolean | undefined;
      await act(async () => {
        result.current.composer.submitWithDraft((onAccepted) => {
          sendPromise = result.current.chat.handleSendMessage('headers only, then dropped', null, null, {
            onAccepted: () => {
              onAccepted();
              accepted();
            },
          });
          return sendPromise;
        });
        outcome = await sendPromise;
      });

      expect(accepted).not.toHaveBeenCalled();
      expect(outcome).toBe(false);
      // The draft came back: in the input and still stored.
      expect(result.current.composer.message).toBe('headers only, then dropped');
      expect(load(DRAFT_KEY)).toBe('headers only, then dropped');
    });

    it('returns the draft whole when the user stops before the run\'s first frame — the server holds nothing of it', async () => {
      // The stream hangs in setup (headers at most, no frame); the stop aborts
      // it. #675 sends the headers before admission, so nothing reached the
      // server and the draft comes back instead of settling sent.
      let hang!: (value: { disconnected: boolean; aborted: boolean }) => void;
      mockSendStream.mockImplementation(
        () => new Promise((resolve) => { hang = resolve; }),
      );

      const accepted = vi.fn();
      const { result } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('stopped before the run began');
      });
      let sendPromise!: Promise<boolean>;
      act(() => {
        result.current.composer.submitWithDraft((onAccepted) => {
          sendPromise = result.current.chat.handleSendMessage('stopped before the run began', null, null, {
            onAccepted: () => {
              onAccepted();
              accepted();
            },
          });
          return sendPromise;
        });
      });
      // Let the stream open (headers at most) before the stop lands.
      await act(async () => {
        await Promise.resolve();
      });

      await act(async () => {
        await result.current.chat.stopWorkflow();
      });
      await act(async () => {
        hang({ disconnected: false, aborted: true });
      });
      let outcome: boolean | undefined;
      await act(async () => {
        outcome = await sendPromise;
      });

      expect(accepted).not.toHaveBeenCalled();
      expect(outcome).toBe(false);
      expect(result.current.composer.message).toBe('stopped before the run began');
      expect(load(DRAFT_KEY)).toBe('stopped before the run began');
    });

    it('settles sent when the user stops after the run\'s first frame — the message is the server\'s', async () => {
      let hang!: (value: { disconnected: boolean; aborted: boolean }) => void;
      mockSendStream.mockImplementation(
        (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) =>
          new Promise((resolve) => {
            opts.onEvent?.({ event: 'metadata', thread_id: 'thread-stop-after', run_id: 'run-stop-after' } as never);
            hang = resolve;
          }),
      );

      const accepted = vi.fn();
      const { result } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('stopped after the run began');
      });
      let sendPromise!: Promise<boolean>;
      act(() => {
        result.current.composer.submitWithDraft((onAccepted) => {
          sendPromise = result.current.chat.handleSendMessage('stopped after the run began', null, null, {
            onAccepted: () => {
              onAccepted();
              accepted();
            },
          });
          return sendPromise;
        });
      });
      // The run's first frame arrives before the stop lands.
      await act(async () => {
        await Promise.resolve();
      });

      await act(async () => {
        await result.current.chat.stopWorkflow();
      });
      await act(async () => {
        hang({ disconnected: false, aborted: true });
      });
      let outcome: boolean | undefined;
      await act(async () => {
        outcome = await sendPromise;
      });

      expect(accepted).toHaveBeenCalledTimes(1);
      expect(outcome).toBe(true);
      expect(load(DRAFT_KEY)).toBe('');
    });

    it('settles accepted when a duplicate request is adopted — the server already took a copy', async () => {
      mockSendStream.mockRejectedValue(
        Object.assign(new Error('This request was already accepted'), {
          status: 409,
          errorInfo: {
            code: 'duplicate_request',
            message: 'This request was already accepted; reconnect to the existing run instead of resending.',
            thread_id: 'th-dup',
            run_id: 'run-dup',
            run_status: 'in_progress',
          },
        }),
      );

      const accepted = vi.fn();
      const { result } = renderHookWithProviders(() => useChatMessages('ws-test'));

      let outcome: boolean | undefined;
      await act(async () => {
        outcome = await result.current.handleSendMessage('the copy that got through', null, null, {
          onAccepted: accepted,
        });
      });

      expect(accepted).toHaveBeenCalledTimes(1);
      expect(outcome).toBe(true);
      expect(result.current.messageError).toBe(null);
    });

    it('puts the parked draft back when the user stops during the resend backoff', async () => {
      // The server's recoverable retry frame, then the drop: the send parks on
      // its automatic resend. The user stops before the backoff fires — the
      // stop cancels the resend, so the message is definitely not taken and
      // the draft goes back whole.
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          opts.onEvent?.({
            event: 'retry',
            message: 'Temporary error occurred, you can retry or resume the turn',
            auto_retry: true,
            error_type: 'timeout_error',
            error_class: 'RuntimeError',
            retry_count: 1,
            max_retries: 3,
            recovery: 'resend',
          } as never);
          return { disconnected: true };
        },
      );

      const accepted = vi.fn();
      const { result, unmount } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('stopped mid-backoff');
      });

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      try {
        act(() => {
          result.current.composer.submitWithDraft((onAccepted, onHandedOff, onRestore) => {
            return result.current.chat.handleSendMessage('stopped mid-backoff', null, null, {
              onAccepted: () => {
                onAccepted();
                accepted();
              },
              onHandedOff,
              onNotTaken: onRestore,
            });
          });
        });
        await settle(0);
        expect(mockSendStream).toHaveBeenCalledTimes(1);
        expect(accepted).not.toHaveBeenCalled();
        expect(storedDraft()).toMatchObject({ text: 'stopped mid-backoff', inFlight: true });

        await act(async () => {
          await result.current.chat.stopWorkflow();
        });
        // The cancelled backoff settles the parked draft back: in the input,
        // as an ordinary stored draft.
        await settle(1000);
        expect(mockSendStream).toHaveBeenCalledTimes(1);
        expect(accepted).not.toHaveBeenCalled();
        expect(result.current.composer.message).toBe('stopped mid-backoff');
        expect(load(DRAFT_KEY)).toBe('stopped mid-backoff');
        expect(storedDraft()?.inFlight).not.toBe(true);
      } finally {
        vi.useRealTimers();
      }
      unmount();
    });

    it('restores the draft when the resend chain gives up, and a manual Retry that succeeds settles it sent', async () => {
      // max_retries 1: the first resend is the last automatic attempt. It
      // fails the same way, the chain gives up, and the failed reply's Retry
      // carries the original acceptance — the Retry's run settles the draft.
      const retryFrame = {
        event: 'retry',
        message: 'Temporary error occurred, you can retry or resume the turn',
        auto_retry: true,
        error_type: 'timeout_error',
        error_class: 'RuntimeError',
        retry_count: 1,
        max_retries: 1,
        recovery: 'resend',
      } as never;
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          if (mockSendStream.mock.calls.length <= 2) {
            opts.onEvent?.(retryFrame);
            return { disconnected: true };
          }
          opts.onEvent?.({ event: 'metadata', thread_id: 'thread-exhaust', run_id: 'run-exhaust-3' } as never);
          return { disconnected: false };
        },
      );

      const accepted = vi.fn();
      const { result, unmount } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('exhausted then retried');
      });

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      try {
        act(() => {
          result.current.composer.submitWithDraft((onAccepted, onHandedOff, onRestore) => {
            return result.current.chat.handleSendMessage('exhausted then retried', null, null, {
              onAccepted: () => {
                onAccepted();
                accepted();
              },
              onHandedOff,
              onNotTaken: onRestore,
            });
          });
        });
        await settle(0);
        // The chain's first leg parks the draft.
        expect(storedDraft()).toMatchObject({ text: 'exhausted then retried', inFlight: true });

        // The last automatic attempt fails the same way: the chain gives up,
        // and the draft comes back whole.
        await settle(1000);
        expect(mockSendStream).toHaveBeenCalledTimes(2);
        expect(accepted).not.toHaveBeenCalled();
        expect(result.current.composer.message).toBe('exhausted then retried');
        expect(load(DRAFT_KEY)).toBe('exhausted then retried');

        // The failed reply's Retry, carrying the held acceptance, goes out and
        // the server takes it: the restored draft settles sent — and an
        // untouched box goes with the stored copy.
        await act(async () => {
          await result.current.chat.handleRetry();
        });
        expect(mockSendStream).toHaveBeenCalledTimes(3);
        expect(accepted).toHaveBeenCalledTimes(1);
        expect(load(DRAFT_KEY)).toBe('');
        expect(result.current.composer.message).toBe('');
      } finally {
        vi.useRealTimers();
      }
      unmount();
    });

    it('keeps the draft the user edited after the resend chain gave up, when a manual Retry succeeds', async () => {
      // Same chain as above, but the user edits the restored text before
      // pressing the failed reply's Retry: the edit is the user's and stays —
      // in the input and in storage — even though the Retry's send was taken.
      const retryFrame = {
        event: 'retry',
        message: 'Temporary error occurred, you can retry or resume the turn',
        auto_retry: true,
        error_type: 'timeout_error',
        error_class: 'RuntimeError',
        retry_count: 1,
        max_retries: 1,
        recovery: 'resend',
      } as never;
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          if (mockSendStream.mock.calls.length <= 2) {
            opts.onEvent?.(retryFrame);
            return { disconnected: true };
          }
          opts.onEvent?.({ event: 'metadata', thread_id: 'thread-edited', run_id: 'run-edited-3' } as never);
          return { disconnected: false };
        },
      );

      const accepted = vi.fn();
      const { result, unmount } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('restored words');
      });

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      try {
        act(() => {
          result.current.composer.submitWithDraft((onAccepted, onHandedOff, onRestore) => {
            return result.current.chat.handleSendMessage('restored words', null, null, {
              onAccepted: () => {
                onAccepted();
                accepted();
              },
              onHandedOff,
              onNotTaken: onRestore,
            });
          });
        });
        await settle(1000);
        await settle(1000);
        expect(result.current.composer.message).toBe('restored words');

        // The user edits what came back, and the edit lands in storage.
        act(() => {
          result.current.composer.setMessage('restored words, edited');
        });
        await settle(300);
        expect(load(DRAFT_KEY)).toBe('restored words, edited');

        await act(async () => {
          await result.current.chat.handleRetry();
        });
        expect(accepted).toHaveBeenCalledTimes(1);
        expect(load(DRAFT_KEY)).toBe('restored words, edited');
        expect(result.current.composer.message).toBe('restored words, edited');
      } finally {
        vi.useRealTimers();
      }
      unmount();
    });

    it('settles the draft sent when the resend meets a duplicate the running turn took as steering', async () => {
      // The first copy's answer was lost; the automatic resend gets the
      // server's 409 for a copy the running turn already took as a steer —
      // the message is the server's, so the parked draft settles sent.
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          if (mockSendStream.mock.calls.length === 1) {
            opts.onEvent?.({
              event: 'retry',
              message: 'Temporary error occurred, you can retry or resume the turn',
              auto_retry: true,
              error_type: 'timeout_error',
              error_class: 'RuntimeError',
              retry_count: 1,
              max_retries: 3,
              recovery: 'resend',
            } as never);
            return { disconnected: true };
          }
          throw Object.assign(new Error('This request was already accepted'), {
            status: 409,
            errorInfo: {
              code: 'duplicate_request',
              message: 'This request was already accepted; the running turn took it as steering.',
              thread_id: 'thread-steer',
              run_id: 'run-steer',
              steered: true,
            },
          });
        },
      );

      const accepted = vi.fn();
      const { result, unmount } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('steered away');
      });

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      try {
        act(() => {
          result.current.composer.submitWithDraft((onAccepted, onHandedOff, onRestore) => {
            return result.current.chat.handleSendMessage('steered away', null, null, {
              onAccepted: () => {
                onAccepted();
                accepted();
              },
              onHandedOff,
              onNotTaken: onRestore,
            });
          });
        });
        await settle(0);
        expect(storedDraft()).toMatchObject({ text: 'steered away', inFlight: true });

        await settle(1000);
        expect(mockSendStream).toHaveBeenCalledTimes(2);
        expect(accepted).toHaveBeenCalledTimes(1);
        expect(load(DRAFT_KEY)).toBe('');
      } finally {
        vi.useRealTimers();
      }
      unmount();
    });

    it('keeps a queued send\'s draft parked when the flushed send hands off to its own resend, and clears it when that resend is taken', async () => {
      // Parked during compaction, flushed into a setup drop that schedules an
      // automatic resend: the queue's deferred true settles nothing, and the
      // resend's own first frame clears the draft.
      mockSendStream.mockImplementation(
        async (_msg: string, _ws: string, _tid: string | null, opts: StreamOptions) => {
          if (mockSendStream.mock.calls.length === 1) {
            opts.onEvent?.({
              event: 'retry',
              message: 'Temporary error occurred, you can retry or resume the turn',
              auto_retry: true,
              error_type: 'timeout_error',
              error_class: 'RuntimeError',
              retry_count: 1,
              max_retries: 3,
              recovery: 'resend',
            } as never);
            return { disconnected: true };
          }
          opts.onEvent?.({ event: 'metadata', thread_id: 'thread-queued', run_id: 'run-queued-2' } as never);
          return { disconnected: false };
        },
      );

      const accepted = vi.fn();
      const { result, unmount } = renderHookWithProviders(() => useDraftChatHarness());
      act(() => {
        result.current.composer.setMessage('queued then resent');
      });
      act(() => {
        result.current.chat.setIsCompacting('summarize');
      });
      act(() => {
        result.current.composer.submitWithDraft((onAccepted, onHandedOff, onRestore) => {
          return result.current.chat.handleSendMessage('queued then resent', null, null, {
            onAccepted: () => {
              onAccepted();
              accepted();
            },
            onHandedOff,
            onNotTaken: onRestore,
          });
        });
      });
      // Parked, not taken: no stream, no acceptance, the draft inFlight.
      expect(mockSendStream).not.toHaveBeenCalled();
      expect(accepted).not.toHaveBeenCalled();

      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      try {
        act(() => {
          result.current.chat.setIsCompacting(false);
        });
        await settle(0);
        // The flushed send dropped in setup and handed off to its resend: the
        // queue's deferred outcome resolved true, and the draft stayed parked.
        expect(mockSendStream).toHaveBeenCalledTimes(1);
        expect(accepted).not.toHaveBeenCalled();
        expect(result.current.composer.message).toBe('');
        expect(storedDraft()).toMatchObject({ text: 'queued then resent', inFlight: true });

        // The resend is taken: the parked draft settles sent.
        await settle(1000);
        expect(mockSendStream).toHaveBeenCalledTimes(2);
        expect(accepted).toHaveBeenCalledTimes(1);
        expect(storedDraft()).toBeNull();
      } finally {
        vi.useRealTimers();
      }
      unmount();
    });
  });
});
