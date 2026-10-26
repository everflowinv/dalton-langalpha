/**
 * A reader who scrolled up and then sends is taken to the end and followed
 * again: their message and the reply land there, below the fold otherwise. The
 * turn-end landing ('reply_start') then applies to that turn too, since it
 * moves only a reader the follow was carrying.
 *
 * The one end-to-end pass over the shipped wiring, on the REAL ChatView;
 * useTranscriptFollow.test.tsx covers each turn starter and the landing rules.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import { act, screen } from '@testing-library/react';

vi.mock('@/lib/framer', async () => (await import('./chatViewHarness')).framerMock());
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'en-US', resolvedLanguage: 'en-US' } }),
}));
vi.mock('@/lib/supabase', () => ({ supabase: null }));
vi.mock('@/hooks/useUser', () => ({ useUser: () => ({ user: null }) }));
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}));
vi.mock('../Markdown', async () => (await import('./chatViewHarness')).markdownMock());
vi.mock('../../../../components/ui/chat-input', async () => (await import('./chatViewHarness')).chatInputMock());
vi.mock('../../hooks/useChatMessages', async (importOriginal) =>
  (await import('./chatViewHarness')).chatMessagesMock(await importOriginal()));
vi.mock('../../hooks/useWorkspaceFiles', async () => (await import('./chatViewHarness')).workspaceFilesMock());
vi.mock('../../hooks/useNavigationData', async (importOriginal) =>
  (await import('./chatViewHarness')).navigationDataMock(await importOriginal()));
vi.mock('../../utils/api', async (importOriginal) => (await import('./chatViewHarness')).apiMock(await importOriginal()));

const prefs = vi.hoisted(() => ({ value: null as Record<string, unknown> | null }));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: prefs.value, isLoading: false }),
}));

import { assistant, chatInput, chatState, mountChatView, resetChatState, userMsg } from './chatViewHarness';
import ChatView from '../ChatView';
import { scrollMemory } from '@/lib/scrollMemory';

const reply = (id: string, content: string, isStreaming = false) =>
  assistant(id, { content, contentSegments: [{ type: 'text', content, order: 0 }], isStreaming });

// ---------------------------------------------------------------------------
// Layout: jsdom has none, so the transcript viewport and its bubbles are
// measured from these.
// ---------------------------------------------------------------------------

const VIEW_H = 500;
let contentH = 2000;
/** Each bubble's top within the transcript. */
let layout: Record<string, number> = {};
let observers: { cb: ResizeObserverCallback; targets: Element[] }[] = [];

/** The main transcript's scrolling element: the ScrollArea's inner div. */
const content = () => document.querySelector<HTMLElement>('.max-w-3xl.overflow-x-hidden')!;
const viewport = () => content().closest<HTMLElement>('.overflow-auto')!;

/** Clamped the way a browser clamps, so a request past the end lands on it. */
function setScrollTop(top: number) {
  const v = viewport();
  const clamped = Math.max(0, Math.min(top, contentH - VIEW_H));
  Object.defineProperty(v, 'scrollTop', { value: clamped, writable: true, configurable: true });
  v.dispatchEvent(new Event('scroll'));
}

/** The transcript grew to `height`, as the content's ResizeObserver reports it.
 *  It watches the padded wrapper, whose bottom padding tracks the composer. */
function grow(height: number) {
  contentH = height;
  const el = content().parentElement!;
  const observer = observers.find((o) => o.targets.includes(el))!;
  act(() => observer.cb([{ contentRect: { height } } as ResizeObserverEntry], {} as ResizeObserver));
}

/** A reader's own scroll: the wheel takes control, then the view moves. */
function userScrollTo(top: number) {
  act(() => {
    viewport().dispatchEvent(new Event('wheel'));
    setScrollTop(top);
  });
}

describe('ChatView send while scrolled up', () => {
  const originalScrollTo = HTMLElement.prototype.scrollTo;
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const OriginalRO = window.ResizeObserver;

  beforeEach(() => {
    resetChatState();
    prefs.value = null;
    contentH = 2000;
    layout = {};
    observers = [];
    scrollMemory.clear();
    window.ResizeObserver = class {
      targets: Element[] = [];
      constructor(cb: ResizeObserverCallback) {
        observers.push({ cb, targets: this.targets });
      }
      observe(el: Element) {
        this.targets.push(el);
      }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    HTMLElement.prototype.scrollTo = function (this: HTMLElement, opts?: unknown) {
      const top = (opts as { top?: number } | undefined)?.top;
      if (typeof top === 'number' && this === viewport()) setScrollTop(top);
    } as HTMLElement['scrollTo'];
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
      const id = this.closest<HTMLElement>('[data-message-id]')?.dataset.messageId;
      const top = id && id in layout ? layout[id] - viewport().scrollTop : 0;
      return { top, bottom: top, height: 0, left: 0, right: 0, width: 0, x: 0, y: top, toJSON: () => {} } as DOMRect;
    };
  });

  afterEach(() => {
    HTMLElement.prototype.scrollTo = originalScrollTo;
    HTMLElement.prototype.getBoundingClientRect = originalRect;
    window.ResizeObserver = OriginalRO;
  });

  it("takes the reader to the end on a send, follows the reply, and lands on its start under 'reply_start'", () => {
    prefs.value = { other_preference: { turn_end_scroll: 'reply_start' } };
    // A settled two-turn thread, opened at its end, then scrolled up to reread.
    chatState.messages = [userMsg('u1', 'how did the quarter go?'), reply('a1', 'Revenue rose.')];
    layout = { u1: 0, a1: 100 };
    const { update } = mountChatView(ChatView);
    Object.defineProperty(viewport(), 'clientHeight', { value: VIEW_H, configurable: true });
    Object.defineProperty(viewport(), 'scrollHeight', { get: () => contentH, configurable: true });
    grow(2000);
    expect(viewport().scrollTop).toBe(1500);
    userScrollTo(800);
    expect(screen.getByLabelText('chat.jumpToLatest.aria')).toBeInTheDocument();

    const onSend = chatInput.props!.onSend as (m: string, att: unknown[], cmds: unknown[], opts: unknown) => void;
    act(() => onSend('and the next quarter?', [], [], {}));

    expect(chatState.handleSendMessage).toHaveBeenCalledTimes(1);
    expect(viewport().scrollTop).toBe(1500);
    expect(screen.queryByLabelText('chat.jumpToLatest.aria')).toBeNull();

    // The send lands its bubble, then a reply streams in under it, longer than the view.
    layout = { ...layout, u2: 2000, a2: 2100 };
    update({
      isLoading: true,
      messages: [...chatState.messages, userMsg('u2', 'and the next quarter?'), reply('a2', 'Guidance', true)],
    });
    expect([2400, 3000].map((height) => {
      grow(height);
      return viewport().scrollTop;
    })).toEqual([1900, 2500]);

    update({
      isLoading: false,
      messages: [...chatState.messages.slice(0, 3), reply('a2', 'Guidance was raised.')],
    });

    // The reply's first line under the viewport top, the anchor's 16px gap above it.
    expect(viewport().scrollTop).toBe(2100 - 16);
  });

  it('lets a reader drag the scrollbar away from a reply their send is following', () => {
    chatState.messages = [userMsg('u1', 'how did the quarter go?'), reply('a1', 'Revenue rose.')];
    layout = { u1: 0, a1: 100 };
    const { update } = mountChatView(ChatView);
    Object.defineProperty(viewport(), 'clientHeight', { value: VIEW_H, configurable: true });
    Object.defineProperty(viewport(), 'scrollHeight', { get: () => contentH, configurable: true });
    grow(2000);

    const onSend = chatInput.props!.onSend as (m: string, att: unknown[], cmds: unknown[], opts: unknown) => void;
    act(() => onSend('and the next quarter?', [], [], {}));
    layout = { ...layout, u2: 2000, a2: 2100 };
    update({
      isLoading: true,
      messages: [...chatState.messages, userMsg('u2', 'and the next quarter?'), reply('a2', 'Guidance', true)],
    });
    grow(2400);
    expect(viewport().scrollTop).toBe(1900);

    // A scrollbar drag: a press on the scroller, then the view moves, no wheel.
    act(() => {
      viewport().dispatchEvent(new Event('pointerdown'));
      setScrollTop(800);
    });
    grow(2600);

    expect(viewport().scrollTop).toBe(800);
  });
});
