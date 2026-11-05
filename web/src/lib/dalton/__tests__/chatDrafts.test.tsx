import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRef } from 'react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ChatInput, { type ChatInputHandle, type ChatInputProps } from '@/components/ui/chat-input';
import ChatInputCard from '@/pages/Dashboard/components/ChatInputCard';
import { queryKeys } from '@/lib/queryKeys';
import { quoteToken } from '@/components/ui/chat-input.quoteTokens';
import type { QuoteMention } from '@/components/ui/chat-input.types';
import {
  DRAFT_SAVE_DEBOUNCE_MS,
  DRAFT_STORAGE_PREFIX_V1,
  DRAFT_TTL_MS,
  MAX_DRAFT_BYTES,
  buildDraftStorageKey,
  clear,
  load,
  loadInFlightCopies,
  pruneExpired,
  save,
  settleSendResult,
} from '../chatDrafts';

const mockGetHomeWorkspace = vi.fn().mockResolvedValue({ workspace_id: 'ws-home' });

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
  getHomeWorkspace: (...args: unknown[]) => mockGetHomeWorkspace(...args),
}));

vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({ data: { workspaces: [] }, isLoading: false }),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: undefined, isLoaded: true, isLoading: false }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
  toast: vi.fn(),
}));

function createQueryClient(userId?: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  if (userId) {
    queryClient.setQueryData(queryKeys.user.me(), { user_id: userId });
  }
  return queryClient;
}

function renderComposer(
  props: Partial<ChatInputProps> = {},
  options: { userId?: string; queryClient?: QueryClient; ref?: React.Ref<ChatInputHandle> } = {},
) {
  const queryClient = options.queryClient ?? createQueryClient(options.userId);
  const onSend = props.onSend ?? vi.fn<ChatInputProps['onSend']>().mockReturnValue(true);
  const ref = options.ref ?? createRef<ChatInputHandle>();

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatInput ref={ref} onSend={onSend} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  const getTextarea = () => screen.getAllByRole('textbox')[0] as HTMLTextAreaElement;

  const rerenderComposer = (nextProps: Partial<ChatInputProps>) => {
    utils.rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInput ref={ref} onSend={nextProps.onSend ?? onSend} {...props} {...nextProps} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  };

  return {
    ...utils,
    onSend,
    queryClient,
    ref,
    getTextarea,
    rerenderComposer,
  };
}

describe('chatDrafts storage module & ChatInput composer drafts', () => {
  beforeEach(() => {
    localStorage.clear();
    mockGetHomeWorkspace.mockReset();
    mockGetHomeWorkspace.mockResolvedValue({ workspace_id: 'ws-home' });
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('restores draft content after typing, unmounting, and remounting (with debounce and sync unmount flush)', () => {
    vi.useFakeTimers();

    // 1. Type and unmount immediately (before 300ms debounce fires) -> sync save on unmount
    const first = renderComposer({ draftKey: 'thread:t-1' }, { userId: 'u-1' });
    fireEvent.change(first.getTextarea(), {
      target: { value: 'half-typed analysis for @report.md ', selectionStart: 35 },
    });
    first.unmount();

    const storageKey = buildDraftStorageKey('thread:t-1', 'u-1');
    // v2 format: quote chips and the attachments-lost hint ride the stored draft.
    expect(storageKey).toBe('dalton.chatDraft.v2:u-1:thread:t-1');
    expect(load('thread:t-1', 'u-1')).toBe('half-typed analysis for @report.md ');

    // 2. Remount with the same draftKey -> content is restored
    const second = renderComposer({ draftKey: 'thread:t-1' }, { userId: 'u-1' });
    expect(second.getTextarea().value).toBe('half-typed analysis for @report.md ');

    // 3. Edit and wait 300ms -> debounced save updates localStorage while still mounted
    fireEvent.change(second.getTextarea(), {
      target: { value: 'updated after debounce', selectionStart: 22 },
    });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(load('thread:t-1', 'u-1')).toBe('updated after debounce');
    second.unmount();
  });

  it('saves and restores each draftKey independently when switching draftKey on the same mounted instance', () => {
    const view = renderComposer({ draftKey: 'thread:alpha' }, { userId: 'u-1' });

    // Type in thread:alpha
    fireEvent.change(view.getTextarea(), {
      target: { value: 'draft in alpha', selectionStart: 14 },
    });
    expect(view.getTextarea().value).toBe('draft in alpha');

    // Switch same instance to thread:beta before debounce timer fires
    view.rerenderComposer({ draftKey: 'thread:beta' });
    expect(load('thread:alpha', 'u-1')).toBe('draft in alpha');
    expect(view.getTextarea().value).toBe('');

    // Type in thread:beta
    fireEvent.change(view.getTextarea(), {
      target: { value: 'draft in beta', selectionStart: 13 },
    });
    expect(view.getTextarea().value).toBe('draft in beta');

    // Switch back to thread:alpha -> beta is saved, alpha is restored
    view.rerenderComposer({ draftKey: 'thread:alpha' });
    expect(load('thread:beta', 'u-1')).toBe('draft in beta');
    expect(view.getTextarea().value).toBe('draft in alpha');

    // Switch back to thread:beta -> beta is restored
    view.rerenderComposer({ draftKey: 'thread:beta' });
    expect(view.getTextarea().value).toBe('draft in beta');
  });

  it('isolates market panel new-chat drafts by workspace for the same symbol (ws A vs ws B)', () => {
    const wsAKey = 'market:panel:ws-a:AAPL';
    const wsBKey = 'market:panel:ws-b:AAPL';

    const panelA = renderComposer({ draftKey: wsAKey }, { userId: 'u-1' });
    fireEvent.change(panelA.getTextarea(), {
      target: { value: 'AAPL thesis in workspace A', selectionStart: 26 },
    });
    panelA.unmount();

    expect(load(wsAKey, 'u-1')).toBe('AAPL thesis in workspace A');
    expect(load(wsBKey, 'u-1')).toBe('');

    const panelB = renderComposer({ draftKey: wsBKey }, { userId: 'u-1' });
    expect(panelB.getTextarea().value).toBe('');
    panelB.unmount();
  });

  it('clears the stored draft and cancels pending debounce save on explicit New Chat', () => {
    const draftKey = 'market:panel:ws-a:AAPL';
    const view = renderComposer({ draftKey }, { userId: 'u-1' });

    fireEvent.change(view.getTextarea(), {
      target: { value: 'unsent market draft before clicking New Chat', selectionStart: 44 },
    });

    // Explicit New Chat calls clear(draftKey) and then remounts the panel body
    act(() => {
      clear(draftKey);
    });
    view.unmount();

    expect(load(draftKey, 'u-1')).toBe('');
    const remounted = renderComposer({ draftKey }, { userId: 'u-1' });
    expect(remounted.getTextarea().value).toBe('');
    remounted.unmount();
  });

  it('clears the stored draft on successful send, when emptied by user, and does not migrate sent text from ws:<id>:new to thread:<id>', () => {
    // A send the server took reports acceptance (the real path fires it at the
    // run's first frame); only that may delete the stored draft.
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation((send) => {
      send.modelOptions.onAccepted?.();
      return true;
    });
    const view = renderComposer({ draftKey: 'ws:ws-1:new', onSend }, { userId: 'u-1' });

    fireEvent.change(view.getTextarea(), {
      target: { value: 'first question in workspace', selectionStart: 27 },
    });
    save('ws:ws-1:new', 'first question in workspace', 'u-1');
    expect(load('ws:ws-1:new', 'u-1')).toBe('first question in workspace');

    const sendBtn = screen.getByLabelText('Send message');
    fireEvent.click(sendBtn);

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(view.getTextarea().value).toBe('');
    expect(load('ws:ws-1:new', 'u-1')).toBe('');

    // New conversation transitions from ws:ws-1:new to thread:new-thread-id without follow-up typing
    view.rerenderComposer({ draftKey: 'thread:new-thread-id', onSend });
    expect(view.getTextarea().value).toBe('');
    expect(load('ws:ws-1:new', 'u-1')).toBe('');
    expect(load('thread:new-thread-id', 'u-1')).toBe('');

    // Typing and then deleting to empty also deletes the stored draft
    fireEvent.change(view.getTextarea(), {
      target: { value: 'temporary text', selectionStart: 14 },
    });
    save('thread:new-thread-id', 'temporary text', 'u-1');
    expect(load('thread:new-thread-id', 'u-1')).toBe('temporary text');

    fireEvent.change(view.getTextarea(), {
      target: { value: '', selectionStart: 0 },
    });
    expect(load('thread:new-thread-id', 'u-1')).toBe('');
  });

  it('migrates post-send continuation text from ws:<id>:new to thread:<id> and deletes the old key', () => {
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation((send) => {
      send.modelOptions.onAccepted?.();
      return true;
    });
    const view = renderComposer({ draftKey: 'ws:ws-1:new', onSend }, { userId: 'u-1' });

    // Send initial message to start the new conversation
    fireEvent.change(view.getTextarea(), {
      target: { value: 'initial prompt', selectionStart: 14 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(load('ws:ws-1:new', 'u-1')).toBe('');

    // Before currentThreadId settles, user starts typing a follow-up message
    fireEvent.change(view.getTextarea(), {
      target: { value: 'follow-up typed before thread id settled', selectionStart: 40 },
    });

    // Thread ID settles -> draftKey switches from ws:ws-1:new to thread:t-settled
    view.rerenderComposer({ draftKey: 'thread:t-settled', onSend });

    expect(view.getTextarea().value).toBe('follow-up typed before thread id settled');
    expect(load('thread:t-settled', 'u-1')).toBe('follow-up typed before thread id settled');
    expect(load('ws:ws-1:new', 'u-1')).toBe('');
  });

  it('does not migrate ws:<id>:new draft or overwrite existing thread draft when switching without sending', () => {
    save('thread:existing-thread', 'saved draft on existing thread', 'u-1');

    const view = renderComposer({ draftKey: 'ws:ws-1:new' }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), {
      target: { value: 'unsent new chat draft', selectionStart: 21 },
    });

    // Switch same instance directly from ws:ws-1:new to thread:existing-thread without sending
    view.rerenderComposer({ draftKey: 'thread:existing-thread' });

    expect(load('ws:ws-1:new', 'u-1')).toBe('unsent new chat draft');
    expect(load('thread:existing-thread', 'u-1')).toBe('saved draft on existing thread');
    expect(view.getTextarea().value).toBe('saved draft on existing thread');
    view.unmount();
  });

  it('restores original text into the input and preserves the draft when onSend returns false or a rejected Promise', async () => {
    // 1. onSend returns false (sync or resolved false)
    const onSendFalse = vi.fn<ChatInputProps['onSend']>().mockReturnValue(false);
    const view1 = renderComposer({ draftKey: 'market:main', onSend: onSendFalse }, { userId: 'u-1' });

    fireEvent.change(view1.getTextarea(), {
      target: { value: 'question when workspace is missing', selectionStart: 34 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));

    expect(onSendFalse).toHaveBeenCalledTimes(1);
    expect(view1.getTextarea().value).toBe('question when workspace is missing');
    expect(load('market:main', 'u-1')).toBe('question when workspace is missing');
    view1.unmount();

    // 2. onSend returns a Promise that resolves to false
    const onSendAsyncFalse = vi.fn<ChatInputProps['onSend']>().mockResolvedValue(false);
    const view2 = renderComposer({ draftKey: 'market:main', onSend: onSendAsyncFalse }, { userId: 'u-1' });
    expect(view2.getTextarea().value).toBe('question when workspace is missing');
    fireEvent.click(screen.getByLabelText('Send message'));

    await waitFor(() => {
      expect(view2.getTextarea().value).toBe('question when workspace is missing');
    });
    expect(load('market:main', 'u-1')).toBe('question when workspace is missing');
    view2.unmount();

    // 3. onSend returns a rejected Promise
    const onSendReject = vi.fn<ChatInputProps['onSend']>().mockRejectedValue(new Error('network error'));
    const view3 = renderComposer({ draftKey: 'market:main', onSend: onSendReject }, { userId: 'u-1' });
    fireEvent.change(view3.getTextarea(), {
      target: { value: 'question that rejects on send', selectionStart: 29 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));

    await waitFor(() => {
      expect(view3.getTextarea().value).toBe('question that rejects on send');
    });
    expect(load('market:main', 'u-1')).toBe('question that rejects on send');
    view3.unmount();

    // 4. onSend returns undefined (missing return in wrapper) -> treated as failure, draft preserved
    const onSendUndefined = vi.fn<ChatInputProps['onSend']>().mockReturnValue(undefined);
    const view4 = renderComposer({ draftKey: 'thread:t-undef', onSend: onSendUndefined }, { userId: 'u-1' });
    fireEvent.change(view4.getTextarea(), {
      target: { value: 'draft when wrapper omits return', selectionStart: 31 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));

    expect(onSendUndefined).toHaveBeenCalledTimes(1);
    expect(view4.getTextarea().value).toBe('draft when wrapper omits return');
    expect(load('thread:t-undef', 'u-1')).toBe('draft when wrapper omits return');
    view4.unmount();
  });

  it('preserves thread composer draft on budget rejection (402/429) or network failure, and clears it only on successful send', async () => {
    // Simulate ChatView's handleSendWithAttachments -> stableSendMessage -> handleSendMessage chain
    let nextOutcome: 'budget_402' | 'rate_429' | 'network_err' | 'ok' = 'budget_402';
    const simulatedThreadSend = vi.fn(async (send?: Parameters<ChatInputProps['onSend']>[0]): Promise<boolean> => {
      try {
        if (nextOutcome === 'budget_402') {
          throw Object.assign(new Error('Monthly token budget exceeded'), {
            status: 402,
            errorInfo: { code: 'budget_exceeded', message: 'Monthly token budget exceeded' },
          });
        }
        if (nextOutcome === 'rate_429') {
          throw Object.assign(new Error('Rate limit exceeded'), {
            status: 429,
            rateLimitInfo: { code: 'rate_limited' },
          });
        }
        if (nextOutcome === 'network_err') {
          throw new TypeError('Failed to fetch');
        }
        // The server took the turn: acceptance rides before the boolean.
        send?.modelOptions.onAccepted?.();
        return true;
      } catch {
        // Mirrors useChatMessages.ts catch branch returning false on 402/429/network failure
        return false;
      }
    });

    const handleSendWithAttachments: ChatInputProps['onSend'] = (send) => simulatedThreadSend(send);
    const view = renderComposer(
      { draftKey: 'thread:t-budget', onSend: handleSendWithAttachments },
      { userId: 'u-1' },
    );

    fireEvent.change(view.getTextarea(), {
      target: { value: 'deep dive on NVDA Q4 gross margin drivers', selectionStart: 41 },
    });

    // 1. Budget admission rejection (HTTP 402) -> draft preserved in input and localStorage
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => {
      expect(view.getTextarea().value).toBe('deep dive on NVDA Q4 gross margin drivers');
    });
    expect(load('thread:t-budget', 'u-1')).toBe('deep dive on NVDA Q4 gross margin drivers');

    // 2. Rate-limit / quota rejection (HTTP 429) -> draft still preserved
    nextOutcome = 'rate_429';
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => {
      expect(view.getTextarea().value).toBe('deep dive on NVDA Q4 gross margin drivers');
    });
    expect(load('thread:t-budget', 'u-1')).toBe('deep dive on NVDA Q4 gross margin drivers');

    // 3. Network failure -> draft still preserved
    nextOutcome = 'network_err';
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => {
      expect(view.getTextarea().value).toBe('deep dive on NVDA Q4 gross margin drivers');
    });
    expect(load('thread:t-budget', 'u-1')).toBe('deep dive on NVDA Q4 gross margin drivers');

    // 4. Subsequent send succeeds -> input stays cleared and stored draft is removed
    nextOutcome = 'ok';
    fireEvent.click(screen.getByLabelText('Send message'));
    await waitFor(() => {
      expect(load('thread:t-budget', 'u-1')).toBe('');
    });
    expect(view.getTextarea().value).toBe('');
    view.unmount();
  });

  it('settleSendResult only treats explicit true (sync or resolved) as success', async () => {
    const outcomes: boolean[] = [];
    const makePending = () => ({
      acceptSend: () => {},
      finalizeSend: (ok: boolean) => outcomes.push(ok),
      // B15: the handle's handoff and forced restore; unused by these outcomes.
      handOffToResend: () => {},
      restoreSend: () => {},
      markHostReturned: () => {},
    });

    settleSendResult(makePending(), () => true);
    settleSendResult(makePending(), () => false);
    settleSendResult(makePending(), () => undefined);
    settleSendResult(makePending(), () => Promise.resolve(true));
    settleSendResult(makePending(), () => Promise.resolve(false));
    settleSendResult(makePending(), () => Promise.resolve(undefined));
    settleSendResult(makePending(), () => Promise.reject(new Error('boom')));

    await waitFor(() => {
      expect(outcomes).toHaveLength(7);
    });
    expect(outcomes).toEqual([true, false, false, true, false, false, false]);
  });

  it('a superseded send settles without touching the newer send\'s stored draft or input', async () => {
    // A streams forever (its promise never settles by itself); B is a steer
    // sent mid-stream on the same draft key. A completing afterwards — in
    // success or failure — must not delete or restore over B. A send the
    // server takes reports acceptance before its boolean resolves.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v) send.modelOptions.onAccepted?.();
          resolve(v);
        });
      }),
    );
    const view = renderComposer({ draftKey: 'thread:supersede', onSend }, { userId: 'u-1' });

    // Send A
    fireEvent.change(view.getTextarea(), { target: { value: 'first message', selectionStart: 13 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');
    expect(loadInFlightCopies('thread:supersede', 'u-1')[0]?.text).toBe('first message');

    // Send B while A is still streaming: B appends its OWN copy — A's still
    // stands beside it (its verdict is unknown), so B's is found by text.
    fireEvent.change(view.getTextarea(), { target: { value: 'second steer', selectionStart: 11 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(loadInFlightCopies('thread:supersede', 'u-1').map((c) => c.text)).toContain('second steer');

    // A's turn completes — before the fix this deleted B's stored copy. A's
    // acceptance now clears A's own copy alone, superseded or not.
    await act(async () => {
      resolvers[0](true);
    });
    expect(loadInFlightCopies('thread:supersede', 'u-1').map((c) => c.text)).toEqual(['second steer']);

    // B's turn completes — its own settle clears its own copy.
    await act(async () => {
      resolvers[1](true);
    });
    expect(load('thread:supersede', 'u-1')).toBe('');
    expect(loadInFlightCopies('thread:supersede', 'u-1')).toHaveLength(0);
    expect(view.getTextarea().value).toBe('');
    view.unmount();

    // The failure twin: A' superseded and failed must not restore A's text
    // into an input that B' owns, and must not overwrite B' in storage.
    const resolvers2: Array<(v: boolean) => void> = [];
    const onSend2 = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers2.push(resolve); }),
    );
    const view2 = renderComposer({ draftKey: 'thread:supersede2', onSend: onSend2 }, { userId: 'u-1' });
    fireEvent.change(view2.getTextarea(), { target: { value: 'first again', selectionStart: 11 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    fireEvent.change(view2.getTextarea(), { target: { value: 'steer again', selectionStart: 10 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    await act(async () => {
      resolvers2[0](false);
    });
    // Round 9 rule: A' was superseded and definitely not taken, so it comes
    // back into the box (it is the user's words, not a "may have been sent"
    // offer) and its copy goes; B' is still out, so B''s copy stands.
    expect(view2.getTextarea().value).toBe('first again');
    expect(loadInFlightCopies('thread:supersede2', 'u-1').map((c) => c.text)).toEqual(['steer again']);
    view2.unmount();
  });

  it('an older send stays superseded after the newer send on its key has already settled', async () => {
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const view = renderComposer({ draftKey: 'thread:gen', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'older A', selectionStart: 7 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    fireEvent.change(view.getTextarea(), { target: { value: 'newer B', selectionStart: 7 } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // B fails first: its copy comes back into the box and the live draft, and
    // B's copy entry goes. A is still out — its copy stands until its verdict.
    await act(async () => {
      resolvers[1](false);
    });
    expect(load('thread:gen', 'u-1')).toBe('newer B');
    expect(loadInFlightCopies('thread:gen', 'u-1').map((c) => c.text)).toEqual(['older A']);

    // A then succeeds: superseded, so it touches neither B's box nor B's
    // draft — but its acceptance clears its OWN copy, and nothing lingers.
    await act(async () => {
      resolvers[0](true);
    });
    expect(load('thread:gen', 'u-1')).toBe('newer B');
    expect(loadInFlightCopies('thread:gen', 'u-1')).toHaveLength(0);
    expect(view.getTextarea().value).toBe('newer B');
    view.unmount();
  });

  it('a send superseded only by a send on another key still clears its own key', async () => {
    // The market panel's key moves from its workspace form to the new thread's
    // once the thread exists; a send on the new key must not strand the first.
    // The first send is accepted (the real path reports it), which clears its
    // own key alone.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v) send.modelOptions.onAccepted?.();
          resolve(v);
        });
      }),
    );
    const view = renderComposer({ draftKey: 'market:panel:ws-1:NVDA', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'first on old key', selectionStart: 16 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    // The unresolved send's copy stands under its own key; the live draft is gone with the box.
    expect(load('market:panel:ws-1:NVDA', 'u-1')).toBe('');
    expect(loadInFlightCopies('market:panel:ws-1:NVDA', 'u-1')[0]?.text).toBe('first on old key');

    view.rerenderComposer({ draftKey: 'market:panel:th-9', onSend });
    fireEvent.change(view.getTextarea(), { target: { value: 'second on new key', selectionStart: 17 } });
    fireEvent.click(screen.getByLabelText('Send message'));

    await act(async () => {
      resolvers[0](true);
    });
    expect(load('market:panel:ws-1:NVDA', 'u-1')).toBe('');
    expect(loadInFlightCopies('market:panel:ws-1:NVDA', 'u-1')).toHaveLength(0);
    expect(loadInFlightCopies('market:panel:th-9', 'u-1')[0]?.text).toBe('second on new key');
    view.unmount();
  });

  it('acceptSend settles the draft as sent before the stream ends, and a later failure does not restore it', async () => {
    // The wiring useChatMessages provides: onAccepted fires when the server
    // takes the send (run_id metadata), long before the stream promise
    // resolves. The draft must be gone by then, so a reload mid-stream never
    // restores a sent message; the eventual return value is only a fallback.
    let resolveStream!: (v: boolean) => void;
    const seenAccepted: Array<boolean | undefined> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => {
        seenAccepted.push(typeof send.modelOptions.onAccepted === 'function');
        send.modelOptions.onAccepted?.();
        return new Promise<boolean>((resolve) => { resolveStream = resolve; });
      },
    );
    const view = renderComposer({ draftKey: 'thread:accept', onSend }, { userId: 'u-1' });

    fireEvent.change(view.getTextarea(), { target: { value: 'long running analysis', selectionStart: 21 } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // The stream is still running (promise pending), but acceptance already
    // settled the send: storage cleared, input cleared.
    expect(seenAccepted).toEqual([true]);
    expect(load('thread:accept', 'u-1')).toBe('');
    expect(view.getTextarea().value).toBe('');

    // The stream later reports failure (e.g. a post-acceptance transport
    // error surfaced as false): acceptance already settled — nothing comes back.
    await act(async () => {
      resolveStream(false);
    });
    expect(load('thread:accept', 'u-1')).toBe('');
    expect(view.getTextarea().value).toBe('');
    view.unmount();
  });

  it('preserves follow-up text typed while awaiting a send that later fails across unmount and remount', async () => {
    let resolveSend!: (val: boolean) => void;
    const onSendPending = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );

    const view = renderComposer({ draftKey: 'thread:pending-fail', onSend: onSendPending }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), {
      target: { value: 'original sent message', selectionStart: 21 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');

    // User types follow-up while send is still in flight
    fireEvent.change(view.getTextarea(), {
      target: { value: 'continuation typed while waiting', selectionStart: 32 },
    });

    // Send fails
    await act(async () => {
      resolveSend(false);
    });

    const expectedMerged = 'original sent message\n\ncontinuation typed while waiting';
    expect(view.getTextarea().value).toBe(expectedMerged);
    expect(load('thread:pending-fail', 'u-1')).toBe(expectedMerged);

    // Unmount and remount -> merged text with continuation is still preserved
    view.unmount();
    const remounted = renderComposer({ draftKey: 'thread:pending-fail' }, { userId: 'u-1' });
    expect(remounted.getTextarea().value).toBe(expectedMerged);
    remounted.unmount();
  });

  it('restores text and keeps draft when Dashboard ChatInputCard fails to resolve workspace', async () => {
    mockGetHomeWorkspace.mockResolvedValue({ workspace_id: '' });
    const queryClient = createQueryClient('u-1');

    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInputCard />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const textarea = screen.getAllByRole('textbox')[0] as HTMLTextAreaElement;
    fireEvent.change(textarea, {
      target: { value: 'dashboard prompt with failing workspace', selectionStart: 39 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));

    await waitFor(() => {
      expect(textarea.value).toBe('dashboard prompt with failing workspace');
    });
    expect(load('dashboard:new', 'u-1')).toBe('dashboard prompt with failing workspace');
    unmount();
  });

  it('gives priority to external prefill (prefillMessage) over a stored draft', () => {
    save('market:main', 'older stored draft', 'u-1');
    const onClearPrefill = vi.fn();

    const view = renderComposer(
      {
        draftKey: 'market:main',
        prefillMessage: 'Analyze NVDA earnings',
        onClearPrefill,
      },
      { userId: 'u-1' },
    );

    expect(view.getTextarea().value).toBe('Analyze NVDA earnings');
    expect(onClearPrefill).toHaveBeenCalledTimes(1);

    // Parent clears prefillMessage prop after onClearPrefill -> prefill text remains, not overwritten by old draft
    view.rerenderComposer({
      draftKey: 'market:main',
      prefillMessage: '',
      onClearPrefill,
    });
    expect(view.getTextarea().value).toBe('Analyze NVDA earnings');
  });

  it('does not restore drafts older than 7 days, validates with Zod, and removes old draft when exceeding 100KB', () => {
    const now = Date.now();
    const eightDaysAgo = now - DRAFT_TTL_MS - 24 * 60 * 60 * 1000;
    const expiredKey = buildDraftStorageKey('thread:expired', 'anon');
    localStorage.setItem(
      expiredKey,
      JSON.stringify({ text: 'stale draft from 8 days ago', savedAt: eightDaysAgo }),
    );

    const view = renderComposer({ draftKey: 'thread:expired' });
    expect(view.getTextarea().value).toBe('');
    expect(localStorage.getItem(expiredKey)).toBeNull();

    // Invalid schema payloads are rejected by Zod and removed from localStorage
    const corruptKey = buildDraftStorageKey('thread:corrupt', 'anon');
    localStorage.setItem(corruptKey, JSON.stringify({ text: 12345, savedAt: 'invalid' }));
    expect(load('thread:corrupt', 'anon', now)).toBe('');
    expect(localStorage.getItem(corruptKey)).toBeNull();

    // pruneExpired also cleans other expired or invalid keys in localStorage
    const otherExpiredKey = buildDraftStorageKey('thread:other-expired', 'anon');
    const freshKey = buildDraftStorageKey('thread:fresh', 'anon');
    localStorage.setItem(
      otherExpiredKey,
      JSON.stringify({ text: 'expired', savedAt: eightDaysAgo }),
    );
    localStorage.setItem(
      freshKey,
      JSON.stringify({ text: 'still valid', savedAt: now }),
    );
    expect(pruneExpired(now)).toBe(1);
    expect(localStorage.getItem(otherExpiredKey)).toBeNull();
    expect(load('thread:fresh', 'anon', now)).toBe('still valid');

    // Drafts > 100KB are not saved AND delete any previous draft under that key so it cannot resurrect
    save('thread:oversized', 'earlier short draft', 'anon', now);
    expect(load('thread:oversized', 'anon', now)).toBe('earlier short draft');

    const oversized = 'x'.repeat(MAX_DRAFT_BYTES + 10);
    expect(save('thread:oversized', oversized, 'anon', now)).toBe(false);
    expect(load('thread:oversized', 'anon', now)).toBe('');
  });

  it('degrades silently without throwing when localStorage throws errors', () => {
    const getItemSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Blocked', 'SecurityError');
    });
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError');
    });
    const removeItemSpy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('Blocked', 'SecurityError');
    });

    expect(() => load('thread:err', 'u-1')).not.toThrow();
    expect(load('thread:err', 'u-1')).toBe('');
    expect(() => save('thread:err', 'some text', 'u-1')).not.toThrow();
    expect(save('thread:err', 'some text', 'u-1')).toBe(false);
    expect(() => clear('thread:err', 'u-1')).not.toThrow();
    expect(() => pruneExpired()).not.toThrow();

    expect(() => {
      const view = renderComposer({ draftKey: 'thread:err' }, { userId: 'u-1' });
      fireEvent.change(view.getTextarea(), {
        target: { value: 'typing with broken storage', selectionStart: 26 },
      });
      view.rerenderComposer({ draftKey: 'thread:err-2' });
      view.unmount();
    }).not.toThrow();

    getItemSpy.mockRestore();
    setItemSpy.mockRestore();
    removeItemSpy.mockRestore();
  });

  it('offers an unresolved send\'s copy above the box after a reload: put back, drop, or send beside it', async () => {
    const noticeText =
      'Your last send may already have gone through. Check the conversation before sending it again.';

    // 1. Ordinary draft (typed, never sent) -> no offer on mount or after remount
    const ordinary = renderComposer({ draftKey: 'thread:ordinary' }, { userId: 'u-1' });
    fireEvent.change(ordinary.getTextarea(), {
      target: { value: 'ordinary unsent draft', selectionStart: 21 },
    });
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    ordinary.unmount();

    const ordinaryRemounted = renderComposer({ draftKey: 'thread:ordinary' }, { userId: 'u-1' });
    expect(ordinaryRemounted.getTextarea().value).toBe('ordinary unsent draft');
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    ordinaryRemounted.unmount();

    // 2. A send whose verdict never arrived: the reload keeps the box's live
    // draft (here empty) and offers the copy above it — never merged in.
    const pendingSend = new Promise<boolean>(() => {});
    const onSendPending = vi.fn<ChatInputProps['onSend']>().mockReturnValue(pendingSend);
    const sentView = renderComposer(
      { draftKey: 'thread:in-flight-edit', onSend: onSendPending },
      { userId: 'u-1' },
    );
    fireEvent.change(sentView.getTextarea(), {
      target: { value: 'message sent before reload', selectionStart: 26 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(sentView.getTextarea().value).toBe('');
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    sentView.unmount();

    const offered = renderComposer(
      { draftKey: 'thread:in-flight-edit' },
      { userId: 'u-1' },
    );
    // The box is empty (nothing was typed since); the offer stands above it.
    expect(offered.getTextarea().value).toBe('');
    expect(screen.getByTestId('in-flight-draft-notice').textContent).toContain(noticeText);
    expect(screen.getByTestId('in-flight-draft-text').textContent).toBe('message sent before reload');
    offered.unmount();

    // 3. "Put back" seats the copy into the box — ahead of whatever is typed
    // since — and removes the offer; the live draft that lands is what a
    // reload keeps.
    const offeredWithTyping = renderComposer(
      { draftKey: 'thread:in-flight-edit' },
      { userId: 'u-1' },
    );
    fireEvent.change(offeredWithTyping.getTextarea(), {
      target: { value: 'typed meanwhile', selectionStart: 14 },
    });
    expect(screen.getByTestId('in-flight-draft-notice').textContent).toContain(noticeText);
    fireEvent.click(screen.getByTestId('in-flight-draft-restore'));
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    expect(offeredWithTyping.getTextarea().value).toBe('message sent before reload\n\ntyped meanwhile');
    offeredWithTyping.unmount();

    const afterRestore = renderComposer({ draftKey: 'thread:in-flight-edit' }, { userId: 'u-1' });
    expect(afterRestore.getTextarea().value).toBe('message sent before reload\n\ntyped meanwhile');
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    afterRestore.unmount();

    // 4. "Discard" drops the copy: the offer and the stored copy both go,
    // the live draft stays as typed.
    const pending2 = new Promise<boolean>(() => {});
    const sentAgain = renderComposer(
      { draftKey: 'thread:in-flight-discard', onSend: vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending2) },
      { userId: 'u-1' },
    );
    fireEvent.change(sentAgain.getTextarea(), {
      target: { value: 'gone without an answer', selectionStart: 22 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    sentAgain.unmount();

    const toDiscard = renderComposer({ draftKey: 'thread:in-flight-discard' }, { userId: 'u-1' });
    expect(screen.getByTestId('in-flight-draft-notice').textContent).toContain(noticeText);
    fireEvent.click(screen.getByTestId('in-flight-draft-discard'));
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    expect(toDiscard.getTextarea().value).toBe('');
    toDiscard.unmount();

    const afterDiscard = renderComposer({ draftKey: 'thread:in-flight-discard' }, { userId: 'u-1' });
    expect(afterDiscard.getTextarea().value).toBe('');
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    afterDiscard.unmount();

    // 5. Sending beside the offer (the box's own draft) accepts the new send
    // alone; the offer keeps standing until the user answers it.
    const pending3 = new Promise<boolean>(() => {});
    const besideView = renderComposer(
      { draftKey: 'thread:in-flight-beside', onSend: vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending3) },
      { userId: 'u-1' },
    );
    fireEvent.change(besideView.getTextarea(), {
      target: { value: 'first, unresolved', selectionStart: 18 },
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    besideView.unmount();

    const beside = renderComposer(
      { draftKey: 'thread:in-flight-beside', onSend: vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending3) },
      { userId: 'u-1' },
    );
    fireEvent.change(beside.getTextarea(), {
      target: { value: 'second thought', selectionStart: 13 },
    });
    expect(screen.getByTestId('in-flight-draft-notice').textContent).toContain(noticeText);
    beside.unmount();
  });
});

// sync3 B15: the stored draft is v2 — beside its text it keeps the quote chips
// the text still holds (token included) and whether attachments stood beside
// them. A v1 entry is the draft's earlier, text-only self: it restores as
// plain text until a v2 save replaces it. Attachments are never persisted; a
// restore that had them says to add them again instead of leaving placeholder
// words in the draft.
describe('chatDrafts v2 — quotes and the attachments-lost notice', () => {
  const ANALYST = { kind: 'message', speaker: 'Analyst' } as const;

  /** A QuoteMention exactly as the composer mints it, on the token it would. */
  const heldQuote = (snippet: string): QuoteMention => ({
    kind: 'quote',
    token: quoteToken(snippet, []),
    snippet,
    from: ANALYST,
  });

  /** Stages an attachment through the composer's hidden file input. */
  const attachFile = (view: ReturnType<typeof renderComposer>, name = 'page.txt') => {
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
    fireEvent.change(input, {
      target: { files: [new File(['x'], name, { type: 'text/plain' })] },
    });
  };

  beforeEach(() => {
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('restores a v1 entry as plain text after a reload, and the next save writes v2', () => {
    vi.useFakeTimers();
    localStorage.setItem(
      buildDraftStorageKey('thread:t-v1', 'u-1', DRAFT_STORAGE_PREFIX_V1),
      JSON.stringify({ text: '从 v1 恢复的文字', savedAt: Date.now() }),
    );

    const view = renderComposer({ draftKey: 'thread:t-v1' }, { userId: 'u-1' });
    expect(view.getTextarea().value).toBe('从 v1 恢复的文字');
    // Plain text only: no quote chips came back, and no notices stand on it.
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(0);
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    expect(screen.queryByTestId('attachments-lost-notice')).toBeNull();

    // The restored draft is ordinary: editing it re-saves under the v2 key.
    fireEvent.change(view.getTextarea(), {
      target: { value: '从 v1 恢复的文字，改过', selectionStart: 12 },
    });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(
      localStorage.getItem(buildDraftStorageKey('thread:t-v1', 'u-1')),
    ).toContain('从 v1 恢复的文字，改过');
    view.unmount();
    vi.useRealTimers();
  });

  it('prefers the v2 entry over a stale v1 entry under the same draftKey', () => {
    localStorage.setItem(
      buildDraftStorageKey('thread:t-both', 'u-1', DRAFT_STORAGE_PREFIX_V1),
      JSON.stringify({ text: 'v1 的旧文字', savedAt: Date.now() }),
    );
    const quote = heldQuote('Revenue grew');
    save('thread:t-both', `${quote.token} why?`, 'u-1', Date.now(), { parts: { quotes: [quote] } });

    const view = renderComposer({ draftKey: 'thread:t-both' }, { userId: 'u-1' });
    expect(view.getTextarea().value).toBe(`${quote.token} why?`);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(1);
    view.unmount();
  });

  it('restores a v2 draft with its quote chips after a reload, and a send spells the quote back out', () => {
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(true);
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-q2', onSend }, { userId: 'u-1', ref });

    // Stage a quote the way a selection does, then words around its token.
    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Revenue grew by a lot', from: ANALYST });
    });
    const withToken = view.getTextarea().value;
    expect(withToken).toContain('\u2060');
    fireEvent.change(view.getTextarea(), {
      target: { value: `${withToken}说了什么`, selectionStart: (withToken + '说了什么').length },
    });
    view.unmount();

    // The stored entry is v2 and carries the quote with its token.
    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-q2', 'u-1')) ?? '{}',
    ) as { text?: string; quotes?: QuoteMention[] };
    expect(stored.text).toBe(`${withToken}说了什么`);
    expect(stored.quotes).toHaveLength(1);
    expect(stored.quotes![0]).toMatchObject({ kind: 'quote', snippet: 'Revenue grew by a lot' });

    // Reload (remount): the text is back with the chip on its token, and a
    // send of it carries the quote block the chip stands for.
    const reopened = renderComposer({ draftKey: 'thread:t-q2', onSend }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe(`${withToken}说了什么`);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(1);
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0].message).toContain(
      '<quote source="Analyst">Revenue grew by a lot</quote>',
    );
    expect(onSend.mock.calls[0][0].message).toContain('说了什么');
    reopened.unmount();
  });

  it('shows the attachments-again notice on a restored draft that had attachments, and hides it once files are beside it again', async () => {
    const view = renderComposer({ draftKey: 'thread:t-att' }, { userId: 'u-1' });
    attachFile(view, 'page.txt');
    fireEvent.change(view.getTextarea(), {
      target: { value: 'draft that went with a file', selectionStart: 26 },
    });
    view.unmount();

    const reopened = renderComposer({ draftKey: 'thread:t-att' }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe('draft that went with a file');
    const notice = screen.getByTestId('attachments-lost-notice');
    expect(notice.textContent).toBe(
      'Attachments are not kept in drafts — add them again before sending.',
    );
    // No placeholder words stand in for the file: the draft is its text alone.
    expect(reopened.getTextarea().value).not.toContain('page.txt');
    expect(screen.queryByText('page.txt')).toBeNull();

    // The notice leaves with the file that replaces it.
    attachFile(reopened, 'page-again.txt');
    await waitFor(() => {
      expect(screen.queryByTestId('attachments-lost-notice')).toBeNull();
    });
    expect(screen.getByText('page-again.txt')).toBeTruthy();
    reopened.unmount();
  });

  it('a failed send of a draft with a quote and attachments restores text, chip and notice in place, and a resend carries the quote whole', () => {
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(false);
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-fail', onSend }, { userId: 'u-1', ref });

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Margins slipped', from: ANALYST });
    });
    const withToken = view.getTextarea().value;
    fireEvent.change(view.getTextarea(), {
      target: { value: `${withToken}why?`, selectionStart: (withToken + 'why?').length },
    });
    attachFile(view, 'exhibit.txt');
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(onSend).toHaveBeenCalledTimes(1);

    // The refused send puts its whole self straight back: the text on its
    // token, the chip on the token, and the notice that the attachment did
    // not survive — no reload needed for any of it.
    expect(view.getTextarea().value).toBe(`${withToken}why?`);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(1);
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();

    // Sending it again, right here, carries the quote's body and source —
    // not the token's clipped preview words.
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(onSend).toHaveBeenCalledTimes(2);
    expect(onSend.mock.calls[1][0].message).toContain(
      '<quote source="Analyst">Margins slipped</quote>',
    );
    expect(onSend.mock.calls[1][0].message).toContain('why?');

    // What a reload restores is the draft as it was sent, chip and notice too.
    view.unmount();
    const reopened = renderComposer({ draftKey: 'thread:t-fail', onSend }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe(`${withToken}why?`);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(1);
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();
    reopened.unmount();
  });

  it('a failed send whose quote meets a same-started quote added since seats each once, paired with its own words', async () => {
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-collide', onSend }, { userId: 'u-1', ref });

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Revenue grew by a lot', from: ANALYST });
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');

    // While the send is out, another quote starting with the same words —
    // the composer, empty again, gives it the same token.
    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Revenue grew by a little', from: ANALYST });
    });
    const typedSince = view.getTextarea().value;
    expect(typedSince).toContain('\u2060');

    await act(async () => {
      resolvers[0](false);
    });

    // The returning quote is renumbered against the one that took its words:
    // two chips, and a send spells each body exactly once.
    expect(view.getTextarea().value).not.toBe(typedSince);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(2);
    fireEvent.click(screen.getByLabelText('Send message'));
    const resent = onSend.mock.calls[1][0].message;
    expect(resent.match(/<quote source="Analyst">Revenue grew by a lot<\/quote>/g)).toHaveLength(1);
    expect(resent.match(/<quote source="Analyst">Revenue grew by a little<\/quote>/g)).toHaveLength(1);
    view.unmount();
  });

  it('a host that takes refused sends back owns the restore: the settle keeps only the stored draft, and its giveBack seats it once', async () => {
    // The host's real shape: it reports taking the send back (onReturnedUnsent,
    // what the chat fires with its returnedDraft) before the false resolves.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v === false) send.modelOptions.onReturnedUnsent?.();
          resolve(v);
        });
      }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer(
      { draftKey: 'thread:t-docked', onSend },
      { userId: 'u-1', ref },
    );

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Margins slipped', from: ANALYST });
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    const sentDraft = onSend.mock.calls[0][0];
    expect(view.getTextarea().value).toBe('');

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Margins held', from: ANALYST });
    });
    const typedSince = view.getTextarea().value;

    await act(async () => {
      resolvers[0](false);
    });

    // The settle wrote nothing into the input: still only what was typed
    // since, one chip of its own — the host's giveBack owns the send's return.
    expect(view.getTextarea().value).toBe(typedSince);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(1);
    // The copy is gone, and (round-7 item 2) the stored draft already holds
    // copy + typed-since merged — synchronously, with no gap before the
    // giveBack; the giveBack's withGivenBack dedup keeps the box clean.
    expect(load('thread:t-docked', 'u-1')).toBe(`${sentDraft.text}\n\n${typedSince}`);
    expect(loadInFlightCopies('thread:t-docked', 'u-1')).toHaveLength(0);

    // The host seats the returned draft the way the chat does; no text or
    // chip lands twice.
    act(() => {
      ref.current!.giveBack(sentDraft.text, [], { mentions: sentDraft.mentions, commands: sentDraft.commands });
    });
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(2);
    fireEvent.click(screen.getByLabelText('Send message'));
    const resent = onSend.mock.calls[1][0].message;
    expect(resent.match(/<quote source="Analyst">Margins slipped<\/quote>/g)).toHaveLength(1);
    expect(resent.match(/<quote source="Analyst">Margins held<\/quote>/g)).toHaveLength(1);
    view.unmount();
  });

  it('saves an attachment added after the text was stored, so a reopen says to add it again', () => {
    vi.useFakeTimers();
    const view = renderComposer({ draftKey: 'thread:t-att-late' }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), {
      target: { value: 'text saved first', selectionStart: 16 },
    });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    // Stored by the text alone: no attachments stood beside it yet.
    const first = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-att-late', 'u-1')) ?? '{}',
    ) as { hadAttachments?: boolean };
    expect(first.hadAttachments).toBeUndefined();

    // The attachment lands with no keystroke after it; leaving now must still
    // carry it into the stored draft.
    attachFile(view, 'page.txt');
    view.unmount();

    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-att-late', 'u-1')) ?? '{}',
    ) as { hadAttachments?: boolean };
    expect(stored.hadAttachments).toBe(true);

    const reopened = renderComposer({ draftKey: 'thread:t-att-late' }, { userId: 'u-1' });
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();
    reopened.unmount();
    vi.useRealTimers();
  });

  it('a host that never takes the send back — a stop — leaves the restore to the composer, even docked', async () => {
    // The docked host returns refused sends through its giveBack, but a stop
    // produces no returnedDraft: the host reports nothing, so the composer
    // restores its own send whole — text, chip and notice in place.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-dockstop', onSend }, { userId: 'u-1', ref });

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Margins slipped', from: ANALYST });
    });
    const withToken = view.getTextarea().value;
    attachFile(view, 'exhibit.txt');
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');

    await act(async () => {
      resolvers[0](false);
    });

    // Restored in place, once: the token text, the chip on it, the notice.
    expect(view.getTextarea().value).toBe(withToken);
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(1);
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();
    view.unmount();
  });

  it('a real same-token collision in the docked flow seats the returned quote once, paired with its own words', async () => {
    // Two quotes whose first 38 width-characters match clip to the SAME token
    // (quoteToken's cap), which is the real collision. The host takes the
    // refused send back; its giveBack renumbers the returning quote against
    // the one typed since, and each spells out exactly once, with its own body.
    const sameStart = 'Margins slipped three percent in the fourth quarter of the trading year, and';
    const snippetA = `${sameStart} the outlook dimmed`;
    const snippetB = `${sameStart} the outlook brightened`;
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v === false) send.modelOptions.onReturnedUnsent?.();
          resolve(v);
        });
      }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-38', onSend }, { userId: 'u-1', ref });

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: snippetA, from: ANALYST });
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    const sentDraft = onSend.mock.calls[0][0];
    expect(view.getTextarea().value).toBe('');

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: snippetB, from: ANALYST });
    });
    const typedSince = view.getTextarea().value;
    // Same opening words, so the empty composer minted the same token.
    expect(typedSince.trim()).toBe(sentDraft.text.trim());

    await act(async () => {
      resolvers[0](false);
    });

    // The settle wrote nothing into the input; the host owns the return, so
    // the copy is removed and the live draft holds what was typed since.
    expect(view.getTextarea().value).toBe(typedSince);
    // Known limitation (accepted by lead): the two quotes mint the SAME
    // token, so what was typed since reads exactly like the sent text — the
    // stored draft keeps only that one copy, and a reload restores one of
    // the two quotes. The giveBack below re-seats both in the input.
    expect(load('thread:t-38', 'u-1')).toContain(sentDraft.text.trim());
    expect(loadInFlightCopies('thread:t-38', 'u-1')).toHaveLength(0);

    // The host's giveBack seats the returned quote renumbered: two chips, and
    // a send spells each body exactly once with its own ending.
    act(() => {
      ref.current!.giveBack(sentDraft.text, [], { mentions: sentDraft.mentions, commands: sentDraft.commands });
    });
    expect(document.querySelectorAll('.draft-token-quote')).toHaveLength(2);
    fireEvent.click(screen.getByLabelText('Send message'));
    const resent = onSend.mock.calls[1][0].message;
    expect(resent.match(/<quote source="Analyst">[^<]*the outlook dimmed<\/quote>/g)).toHaveLength(1);
    expect(resent.match(/<quote source="Analyst">[^<]*the outlook brightened<\/quote>/g)).toHaveLength(1);
    view.unmount();
  });

  it('keeps the attachments-again notice through the edits that follow a restore', () => {
    vi.useFakeTimers();
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(false);
    const view = renderComposer({ draftKey: 'thread:t-att-keep', onSend }, { userId: 'u-1' });

    fireEvent.change(view.getTextarea(), {
      target: { value: 'draft with a file', selectionStart: 16 },
    });
    attachFile(view, 'page.txt');
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();

    // Typing more after the restore saves the merged draft — the notice must
    // survive that save, in storage and across a reopen.
    fireEvent.change(view.getTextarea(), {
      target: { value: 'draft with a file, edited', selectionStart: 24 },
    });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-att-keep', 'u-1')) ?? '{}',
    ) as { text?: string; hadAttachments?: boolean };
    expect(stored.text).toBe('draft with a file, edited');
    expect(stored.hadAttachments).toBe(true);

    view.unmount();
    const reopened = renderComposer({ draftKey: 'thread:t-att-keep' }, { userId: 'u-1' });
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();
    reopened.unmount();
    vi.useRealTimers();
  });

  it('a not-taken send seats its copy back ahead of what was typed since, and the copy is gone', async () => {
    // The send's own storage entry keeps what went out; the live draft keeps
    // what was typed since. A definite not-taken seats the copy back into the
    // box FIRST, merged once, and removes the separate entry.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const view = renderComposer({ draftKey: 'thread:t-putback', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'sent words', selectionStart: 10 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');
    expect(loadInFlightCopies('thread:t-putback', 'u-1')[0]?.text).toBe('sent words');
    expect(load('thread:t-putback', 'u-1')).toBe('');

    fireEvent.change(view.getTextarea(), { target: { value: 'typed meanwhile', selectionStart: 14 } });
    expect(loadInFlightCopies('thread:t-putback', 'u-1')[0]?.text).toBe('sent words');

    await act(async () => {
      resolvers[0](false);
    });

    // Both, in order: the sent words first, the typed-since after; the copy
    // entry is gone and the live draft holds the merged text.
    expect(view.getTextarea().value).toBe('sent words\n\ntyped meanwhile');
    expect(loadInFlightCopies('thread:t-putback', 'u-1')).toHaveLength(0);
    view.unmount();
    const reopened = renderComposer({ draftKey: 'thread:t-putback' }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe('sent words\n\ntyped meanwhile');
    expect(screen.queryByTestId('in-flight-draft-notice')).toBeNull();
    reopened.unmount();
  });

  it('clearing what was typed since never deletes the unresolved send\'s copy — a reload offers it', () => {
    vi.useFakeTimers();
    const pending = new Promise<boolean>(() => {});
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending);
    const view = renderComposer({ draftKey: 'thread:t-cleared', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'sent and unresolved', selectionStart: 19 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');

    // The user types a next thought and deletes it again: the live draft is
    // gone, but the send's copy — under its own key — is untouched.
    fireEvent.change(view.getTextarea(), { target: { value: 'fleeting thought', selectionStart: 16 } });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    fireEvent.change(view.getTextarea(), { target: { value: '', selectionStart: 0 } });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(load('thread:t-cleared', 'u-1')).toBe('');
    expect(loadInFlightCopies('thread:t-cleared', 'u-1')[0]?.text).toBe('sent and unresolved');
    view.unmount();

    // The reload offers the copy above an empty box.
    const reopened = renderComposer({ draftKey: 'thread:t-cleared' }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe('');
    expect(screen.getByTestId('in-flight-draft-notice').textContent).toContain('may already have gone through');
    expect(screen.getByTestId('in-flight-draft-text').textContent).toBe('sent and unresolved');
    reopened.unmount();
    vi.useRealTimers();
  });

  it('moves the unresolved send\'s copy and the typed continuation together when ws:new resolves to a thread', () => {
    vi.useFakeTimers();
    const pending = new Promise<boolean>(() => {});
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending);
    const view = renderComposer({ draftKey: 'ws:ws-7:new', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'the opening question', selectionStart: 20 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    expect(view.getTextarea().value).toBe('');

    // A follow-up typed while the thread id settles.
    fireEvent.change(view.getTextarea(), { target: { value: 'typed before the id settled', selectionStart: 29 } });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });

    // The key resolves: both the copy and the live continuation move.
    view.rerenderComposer({ draftKey: 'thread:t-resolved', onSend });
    expect(loadInFlightCopies('ws:ws-7:new', 'u-1')).toHaveLength(0);
    expect(loadInFlightCopies('thread:t-resolved', 'u-1')[0]?.text).toBe('the opening question');
    expect(view.getTextarea().value).toBe('typed before the id settled');
    expect(load('thread:t-resolved', 'u-1')).toBe('typed before the id settled');
    view.unmount();

    // A reload offers the send's copy beside the typed continuation.
    const reopened = renderComposer({ draftKey: 'thread:t-resolved' }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe('typed before the id settled');
    expect(screen.getByTestId('in-flight-draft-text').textContent).toBe('the opening question');
    reopened.unmount();
    vi.useRealTimers();
  });

  it('a new send appends its own copy: the offer standing in the banner survives it and settles independently', async () => {
    // A reload offers copy A; the user types B and sends it without answering
    // the offer. B gets its OWN copy — A keeps standing. B's acceptance
    // removes B's copy alone.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v) send.modelOptions.onAccepted?.();
          resolve(v);
        });
      }),
    );
    // Send A on an earlier page; its verdict never arrives.
    const first = renderComposer({ draftKey: 'thread:t-two', onSend }, { userId: 'u-1' });
    fireEvent.change(first.getTextarea(), { target: { value: 'send A', selectionStart: 7 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    first.unmount();
    expect(loadInFlightCopies('thread:t-two', 'u-1')).toHaveLength(1);

    // Reload: A is offered above the box. Type B and send it.
    const view = renderComposer({ draftKey: 'thread:t-two', onSend }, { userId: 'u-1' });
    expect(screen.getByTestId('in-flight-draft-text').textContent).toBe('send A');
    fireEvent.change(view.getTextarea(), { target: { value: 'send B', selectionStart: 7 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    // B appended its own copy; the offer for A still stands.
    expect(loadInFlightCopies('thread:t-two', 'u-1').map((c) => c.text)).toEqual(['send A', 'send B']);
    expect(screen.getByTestId('in-flight-draft-text').textContent).toBe('send A');

    // B is taken: B's copy goes, A keeps standing — in storage and offered.
    await act(async () => {
      resolvers[1](true);
    });
    expect(loadInFlightCopies('thread:t-two', 'u-1').map((c) => c.text)).toEqual(['send A']);
    expect(screen.getByTestId('in-flight-draft-text').textContent).toBe('send A');
    view.unmount();
  });

  it('a docked not-taken seats the words into the SEND key at once — no gap, no waiting for the giveBack', async () => {
    // The host reports taking the refused send back. Even before its giveBack
    // lands, the stored draft for the send's key holds copy + typed-since,
    // synchronously — a refresh at this instant finds the words.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v === false) send.modelOptions.onReturnedUnsent?.();
          resolve(v);
        });
      }),
    );
    const view = renderComposer({ draftKey: 'thread:t-gap', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'the refused words', selectionStart: 16 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    fireEvent.change(view.getTextarea(), { target: { value: 'typed meanwhile', selectionStart: 14 } });

    await act(async () => {
      resolvers[0](false);
    });
    // Immediately — no debounce, no giveBack yet: the send's key holds both,
    // and the box keeps only what was typed since for the host's giveBack.
    expect(load('thread:t-gap', 'u-1')).toBe('the refused words\n\ntyped meanwhile');
    expect(loadInFlightCopies('thread:t-gap', 'u-1')).toHaveLength(0);
    expect(view.getTextarea().value).toBe('typed meanwhile');
    view.unmount();
  });

  it('a docked not-taken after the box moved to another session writes the old key alone', async () => {
    vi.useFakeTimers();
    // The send went out on key one; the composer has since switched to key
    // two and holds another draft. The not-taken writes the SEND's key with
    // its own words and touches neither the new box nor the new key's draft.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v === false) send.modelOptions.onReturnedUnsent?.();
          resolve(v);
        });
      }),
    );
    const view = renderComposer({ draftKey: 'thread:t-old', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'sent on the old session', selectionStart: 24 } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // Before the switch, a follow-up was typed AND SAVED on the old key
    // (round-9 item 5: the merge base is the old key's saved draft).
    fireEvent.change(view.getTextarea(), { target: { value: 'follow-up on the old session', selectionStart: 30 } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(load('thread:t-old', 'u-1')).toBe('follow-up on the old session');

    // The box moves to another session's key and something is typed there.
    view.rerenderComposer({ draftKey: 'thread:t-new', onSend });
    fireEvent.change(view.getTextarea(), { target: { value: 'typed on the new session', selectionStart: 25 } });

    await act(async () => {
      resolvers[0](false);
    });
    // The send's words joined the old key's SAVED follow-up (not the new
    // box's content); the copy is gone; the new session's box and draft keep
    // to themselves (its draft rides its own debounce, completed below).
    expect(load('thread:t-old', 'u-1')).toBe('sent on the old session\n\nfollow-up on the old session');
    expect(loadInFlightCopies('thread:t-old', 'u-1')).toHaveLength(0);
    expect(view.getTextarea().value).toBe('typed on the new session');
    view.unmount();
    expect(load('thread:t-new', 'u-1')).toBe('typed on the new session');
    vi.useRealTimers();
  });

  it('a not-taken keep saves the quotes added while the send was out, beside the sent ones', async () => {
    // Plain-text send A; quote B added while it is out; A is not taken. The
    // synchronous save must carry BOTH — A's own quote (from its parts) and
    // B (from the box as it stands) — or a reload would lose B's body.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-mixed', onSend }, { userId: 'u-1', ref });

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Revenue grew by a lot', from: ANALYST });
    });
    fireEvent.click(screen.getByLabelText('Send message'));

    // While the send is out, another quote joins the (empty) box.
    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Margins slipped', from: ANALYST });
    });

    await act(async () => {
      resolvers[0](false);
    });

    // Immediately: the stored draft's quotes carry both bodies.
    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-mixed', 'u-1')) ?? '{}',
    ) as { text?: string; quotes?: Array<{ snippet?: string }> };
    expect(stored.quotes?.map((q) => q.snippet)).toEqual(
      expect.arrayContaining(['Revenue grew by a lot', 'Margins slipped']),
    );
    view.unmount();
  });

  it('a migration leaves nothing behind on the resolved ws:new key', () => {
    vi.useFakeTimers();
    const pending = new Promise<boolean>(() => {});
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending);
    const view = renderComposer({ draftKey: 'ws:ws-9:new', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'the opening question', selectionStart: 20 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    // The follow-up lands in storage on the old key before the id settles.
    fireEvent.change(view.getTextarea(), { target: { value: 'follow-up flushed early', selectionStart: 24 } });
    act(() => {
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(load('ws:ws-9:new', 'u-1')).toBe('follow-up flushed early');

    view.rerenderComposer({ draftKey: 'thread:t-clean', onSend });
    // The follow-up moved to the thread's key; the resolved key keeps
    // neither a live draft nor a copy.
    expect(load('thread:t-clean', 'u-1')).toBe('follow-up flushed early');
    expect(load('ws:ws-9:new', 'u-1')).toBe('');
    expect(loadInFlightCopies('ws:ws-9:new', 'u-1')).toHaveLength(0);
    expect(loadInFlightCopies('thread:t-clean', 'u-1').map((c) => c.text)).toEqual(['the opening question']);
    view.unmount();
    vi.useRealTimers();
  });

  it('keeps every unresolved send\'s copy — six sends, six copies, none dropped', async () => {
    // No cap: a sixth unresolved send must not push the oldest copy out of
    // storage (round-9 item 4). The banner folds past three, but everything
    // stays stored and reachable.
    const pending = new Promise<boolean>(() => {});
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(pending);
    const view = renderComposer({ draftKey: 'thread:t-six', onSend }, { userId: 'u-1' });
    for (let n = 1; n <= 6; n += 1) {
      fireEvent.change(view.getTextarea(), { target: { value: `send number ${n}`, selectionStart: 13 } });
      fireEvent.click(screen.getByLabelText('Send message'));
    }
    const copies = loadInFlightCopies('thread:t-six', 'u-1');
    expect(copies.map((c) => c.text)).toEqual([
      'send number 1', 'send number 2', 'send number 3',
      'send number 4', 'send number 5', 'send number 6',
    ]);
    // A reload offers every one of them; the banner folds past three — three
    // entries shown, an expander counting the rest — and the copy behind
    // every offer is in storage.
    view.unmount();
    const reopened = renderComposer({ draftKey: 'thread:t-six' }, { userId: 'u-1' });
    expect(screen.getAllByTestId('in-flight-draft-entry')).toHaveLength(3);
    expect(screen.getByTestId('in-flight-draft-expand').textContent).toContain('3');
    fireEvent.click(screen.getByTestId('in-flight-draft-expand'));
    expect(screen.getAllByTestId('in-flight-draft-entry')).toHaveLength(6);
    reopened.unmount();
  });

  it('a not-taken on the old key merges its SAVED draft — quote and follow-up — never the new box\'s', async () => {
    // Round-9 item 5: the send went out with a quote, a follow-up was typed
    // and saved on that key, then the box switched to another session. The
    // not-taken writes the old key with quote + follow-up — the quote from
    // the send's own parts, the follow-up from the old key's saved draft —
    // and the new session's box and draft are untouched.
    vi.useFakeTimers();
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-merge', onSend }, { userId: 'u-1', ref });

    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Revenue grew by a lot', from: ANALYST });
    });
    fireEvent.click(screen.getByLabelText('Send message'));
    fireEvent.change(view.getTextarea(), { target: { value: 'follow-up kept on the old key', selectionStart: 29 } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(load('thread:t-merge', 'u-1')).toBe('follow-up kept on the old key');

    view.rerenderComposer({ draftKey: 'thread:t-other', onSend });
    fireEvent.change(view.getTextarea(), { target: { value: 'other session words', selectionStart: 19 } });

    await act(async () => {
      resolvers[0](false);
    });

    // Old key: the token text (the quote's) AHEAD of the saved follow-up,
    // with the quote's parts — the other session's words and quotes nowhere
    // in it.
    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-merge', 'u-1')) ?? '{}',
    ) as { text?: string; quotes?: Array<{ snippet?: string }> };
    // The quote token keeps its words together with non-breaking spaces.
    const storedText = (stored.text ?? '').replace(/\s/g, ' ');
    expect(storedText).toContain('Revenue grew by a lot');
    expect(storedText).toContain('follow-up kept on the old key');
    expect(storedText.indexOf('Revenue grew by a lot')).toBeLessThan(storedText.indexOf('follow-up kept on the old key'));
    expect(stored.quotes?.map((q) => q.snippet)).toContain('Revenue grew by a lot');
    expect(stored.text).not.toContain('other session words');
    expect(loadInFlightCopies('thread:t-merge', 'u-1')).toHaveLength(0);
    expect(view.getTextarea().value).toBe('other session words');
    view.unmount();
    expect(load('thread:t-other', 'u-1')).toBe('other session words');
    vi.useRealTimers();
  });

  it('an old request failing after the switch never cancels the new session\'s pending save', async () => {
    // Round-10 item 1: the send went out on key A, the box moved to key B and
    // typed content whose 300ms save is still pending when A fails. The
    // settle must not kill B's timer: after the debounce, a reload finds B's
    // content.
    vi.useFakeTimers();
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const view = renderComposer({ draftKey: 'thread:old-a', onSend }, { userId: 'u-1' });
    fireEvent.change(view.getTextarea(), { target: { value: 'sent on A', selectionStart: 10 } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // Switch to session B and type — the save is pending, not fired.
    view.rerenderComposer({ draftKey: 'thread:new-b', onSend });
    fireEvent.change(view.getTextarea(), { target: { value: 'typed on B', selectionStart: 10 } });
    expect(load('thread:new-b', 'u-1')).toBe('');

    // The old request fails while B's debounce is pending.
    await act(async () => {
      resolvers[0](false);
    });
    expect(load('thread:old-a', 'u-1')).toBe('sent on A');

    // The debounce runs to term: B's content is saved; a reload finds it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DRAFT_SAVE_DEBOUNCE_MS);
    });
    expect(load('thread:new-b', 'u-1')).toBe('typed on B');
    view.unmount();
    const reopened = renderComposer({ draftKey: 'thread:new-b' }, { userId: 'u-1' });
    expect(reopened.getTextarea().value).toBe('typed on B');
    reopened.unmount();
    vi.useRealTimers();
  });

  it('an older send\'s restore keeps the newer send\'s attachments-again notice standing', async () => {
    // Round-10 item 4: send A without attachments, send B with one; B is not
    // taken first (notice on), then A is not taken beside it. A's restore
    // must not clear B's notice — on the box or in what a save keeps.
    vi.useFakeTimers();
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      () => new Promise<boolean>((resolve) => { resolvers.push(resolve); }),
    );
    const view = renderComposer({ draftKey: 'thread:t-notice', onSend }, { userId: 'u-1' });

    fireEvent.change(view.getTextarea(), { target: { value: 'send A plain', selectionStart: 13 } });
    fireEvent.click(screen.getByLabelText('Send message'));
    fireEvent.change(view.getTextarea(), { target: { value: 'send B with file', selectionStart: 17 } });
    attachFile(view, 'exhibit.txt');
    fireEvent.click(screen.getByLabelText('Send message'));

    // B (newer) not taken: its words and its attachment notice come back.
    await act(async () => {
      resolvers[1](false);
    });
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();

    // A (older, superseded) not taken: seated ahead, and the notice stays.
    await act(async () => {
      resolvers[0](false);
    });
    expect(view.getTextarea().value).toContain('send A plain');
    expect(view.getTextarea().value).toContain('send B with file');
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();

    // What a save keeps carries the notice too.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DRAFT_SAVE_DEBOUNCE_MS);
    });
    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-notice', 'u-1')) ?? '{}',
    ) as { hadAttachments?: boolean };
    expect(stored.hadAttachments).toBe(true);
    view.unmount();
    const reopened = renderComposer({ draftKey: 'thread:t-notice' }, { userId: 'u-1' });
    expect(screen.getByTestId('attachments-lost-notice')).toBeTruthy();
    reopened.unmount();
    vi.useRealTimers();
  });

  it('a host-owned return saves the quotes added while the send was out, before its giveBack lands', async () => {
    // Round-10 item 5: docked send, a DIFFERENT-opening quote joins the box
    // while it is out, the host reports taking the return. The synchronous
    // save must carry the added quote's body beside the sent words — read at
    // once, before the giveBack's debounce.
    const resolvers: Array<(v: boolean) => void> = [];
    const onSend = vi.fn<ChatInputProps['onSend']>().mockImplementation(
      (send) => new Promise<boolean>((resolve) => {
        resolvers.push((v: boolean) => {
          if (v === false) send.modelOptions.onReturnedUnsent?.();
          resolve(v);
        });
      }),
    );
    const ref = createRef<ChatInputHandle>();
    const view = renderComposer({ draftKey: 'thread:t-hostq', onSend }, { userId: 'u-1', ref });
    fireEvent.change(view.getTextarea(), { target: { value: 'the refused words', selectionStart: 16 } });
    fireEvent.click(screen.getByLabelText('Send message'));

    // While the send is out, a quote with a DIFFERENT opening joins the box.
    act(() => {
      ref.current!.addContext({ kind: 'quote', snippet: 'Margins slipped three percent', from: ANALYST });
    });

    await act(async () => {
      resolvers[0](false);
    });
    // Immediately — no debounce, no giveBack yet: the stored draft's quotes
    // carry the added quote's body (and the send's words are in the text).
    const stored = JSON.parse(
      localStorage.getItem(buildDraftStorageKey('thread:t-hostq', 'u-1')) ?? '{}',
    ) as { text?: string; quotes?: Array<{ snippet?: string }> };
    expect(stored.text).toContain('the refused words');
    expect(stored.quotes?.map((q) => q.snippet)).toContain('Margins slipped three percent');
    expect(loadInFlightCopies('thread:t-hostq', 'u-1')).toHaveLength(0);
    // The host's giveBack owns the box: untouched by the settle.
    expect(view.getTextarea().value).not.toContain('the refused words');
    view.unmount();
  });
});
