// @vitest-environment node
/**
 * A target's wake turn, attached live. Its query is written for the model, and
 * no query row comes with the run's stream, so the stream's first frame names
 * the notice and the alert row is drawn from it, ahead of the bubble the attach
 * opened. The row is keyed on the turn as replay keys it, so a replay and an
 * attach draw one row between them.
 */
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/types/chat';
import type { SSEEvent } from '../../types';
import { createStreamEventProcessor } from '../processStreamEvent';

type Processor = Parameters<typeof createStreamEventProcessor>;

// Neutral placeholder ticker and made-up prices only.
const NOTICE = {
  target_id: 'tg-1',
  notice: 'hit',
  symbol: 'ACME',
  kind: 'above',
  value: 205,
  pct_from: null,
  fired_price: 205.12,
  fired_at: '2030-01-01T15:05:00Z',
  expires_at: null,
} as const;

function setup(extra: Array<Record<string, unknown>> = []) {
  let messages = [
    { id: 'u0', role: 'user', content: 'watch ACME', turnIndex: 0, isHistory: true },
    { id: 'h0', role: 'assistant', content: 'Watching.', turnIndex: 0, isHistory: true },
    ...extra,
    { id: 'a1', role: 'assistant', content: '', isStreaming: true, turnIndex: 1, contentSegments: [] },
  ] as unknown as ChatMessage[];
  const setMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
    messages = updater(messages);
  };
  const rt = {
    setMessages,
    queueMessages: setMessages,
    flushMessages: vi.fn(),
    currentMessageRef: { current: 'a1' as string | null },
    currentRunIdRef: { current: null as string | null },
    latestRunIdRef: { current: null },
    latestRunEpochRef: { current: 0 },
    lastEventIdRef: { current: null },
    threadIdRef: { current: 'thread-1' },
  };
  const deps = { clearModelStatus: () => {}, refreshMarketWatch: vi.fn(), refreshWorkspaceFolder: null };
  const refs = {
    contentOrderCounterRef: { current: 0 },
    currentReasoningIdRef: { current: null },
    currentToolCallIdRef: { current: null },
    subagentStateRefs: {},
    isReconnect: false,
  };
  const process = createStreamEventProcessor(
    rt as unknown as Processor[0],
    deps as unknown as Processor[1],
    'a1',
    refs as unknown as Processor[3],
    (event) => (event.agent as string) ?? null,
  );
  const metadata = (extra: Record<string, unknown> = {}) =>
    process({ event: 'metadata', thread_id: 'thread-1', run_id: 'run-wake', turn_index: 1, ...extra } as SSEEvent);
  return { process, metadata, deps, read: () => messages as unknown as Array<Record<string, unknown>> };
}

describe('live: a wake turn attached to its run', () => {
  it("draws the alert row from the run's first frame, ahead of the attach's bubble", () => {
    const { metadata, read } = setup();
    metadata({ market_target: NOTICE });

    expect(read().map((m) => m.id)).toEqual(['u0', 'h0', 'history-market-alert-wake-1', 'a1']);
    expect(read()[2]).toMatchObject({
      role: 'notification',
      content: '',
      marketAlert: { source: 'wake', notices: [NOTICE] },
    });
    expect(read()[2].isHistory).toBeUndefined();
    expect(read()[3]).toMatchObject({ turnIndex: 1 });
  });

  it('re-reads the targets, since the wake spent one', () => {
    const { metadata, deps } = setup();
    metadata({ market_target: NOTICE });
    expect(deps.refreshMarketWatch).toHaveBeenCalledTimes(1);
  });

  it('keeps the row a replay drew for the turn, and draws one for a re-attach', () => {
    const replayed = { id: 'history-market-alert-wake-1', role: 'notification', content: '', isHistory: true };
    const { metadata, read } = setup([replayed]);
    metadata({ market_target: NOTICE });
    metadata({ market_target: NOTICE });

    expect(read().map((m) => m.id)).toEqual(['u0', 'h0', 'history-market-alert-wake-1', 'a1']);
    expect(read()[2]).toBe(replayed);
  });

  it('draws nothing for a run that is no wake', () => {
    const { metadata, read, deps } = setup();
    metadata();
    expect(read().some((m) => m.role === 'notification')).toBe(false);
    expect(deps.refreshMarketWatch).not.toHaveBeenCalled();
  });
});
