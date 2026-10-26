import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ChatInput, { type ChatInputProps } from '@/components/ui/chat-input';
import ChatInputCard from '@/pages/Dashboard/components/ChatInputCard';
import { queryKeys } from '@/lib/queryKeys';
import {
  DRAFT_SAVE_DEBOUNCE_MS,
  DRAFT_TTL_MS,
  MAX_DRAFT_BYTES,
  buildDraftStorageKey,
  clear,
  load,
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
  options: { userId?: string; queryClient?: QueryClient } = {},
) {
  const queryClient = options.queryClient ?? createQueryClient(options.userId);
  const onSend = props.onSend ?? vi.fn<ChatInputProps['onSend']>().mockReturnValue(true);

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatInput onSend={onSend} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  const getTextarea = () => screen.getAllByRole('textbox')[0] as HTMLTextAreaElement;

  const rerenderComposer = (nextProps: Partial<ChatInputProps>) => {
    utils.rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatInput onSend={nextProps.onSend ?? onSend} {...props} {...nextProps} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  };

  return {
    ...utils,
    onSend,
    queryClient,
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
    expect(storageKey).toBe('dalton.chatDraft.v1:u-1:thread:t-1');
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
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(true);
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
    const onSend = vi.fn<ChatInputProps['onSend']>().mockReturnValue(true);
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
    const simulatedThreadSend = vi.fn(async (): Promise<boolean> => {
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
        return true;
      } catch {
        // Mirrors useChatMessages.ts catch branch returning false on 402/429/network failure
        return false;
      }
    });

    const handleSendWithAttachments: ChatInputProps['onSend'] = () => simulatedThreadSend();
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
      finalizeSend: (ok: boolean) => outcomes.push(ok),
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
});
