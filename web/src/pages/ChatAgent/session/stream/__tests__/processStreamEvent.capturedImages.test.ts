// @vitest-environment node
/**
 * A reply streams its images as sandbox paths, and the agent rewrites charts
 * in place: a later turn that rebuilt one showed the earlier turn's picture,
 * or the earlier turn showed the later one's. The run's `run_end` says where
 * each path was captured, and the turn's bubbles keep that copy, as a reload
 * of the turn shows it.
 */
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/types/chat';
import type { SSEEvent } from '../../types';
import { createStreamEventProcessor } from '../processStreamEvent';

type Processor = Parameters<typeof createStreamEventProcessor>;

const bubble = (id: string, turnIndex: number) => ({
  id, role: 'assistant', content: '', isStreaming: id === 'live', turnIndex, contentSegments: [],
  reasoningProcesses: {}, toolCallProcesses: {},
}) as unknown as ChatMessage;

function setup() {
  let messages: ChatMessage[] = [
    bubble('earlier', 2),
    { id: 'ask', role: 'user', content: 'fix the title', turnIndex: 3 } as unknown as ChatMessage,
    bubble('dropped', 3),
    bubble('live', 3),
  ];
  const setMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
    messages = updater(messages);
  };
  const rt = {
    setMessages,
    queueMessages: setMessages,
    flushMessages: vi.fn(),
    updateSubagentCard: vi.fn(),
    lastEventIdRef: { current: null },
    threadIdRef: { current: 'thread-1' },
    threadId: 'thread-1',
    setThreadId: vi.fn(),
    pendingPTCBackfillRef: { current: new Map() },
    subagentHistory: { toolCalls: new Map() },
    setTokenUsage: vi.fn(),
    setNextSummary: vi.fn(),
    nextSummaryOwedRef: { current: false },
    t: (k: string) => k,
  };
  const deps = { clearModelStatus: () => {}, armReportBack: vi.fn(), refreshWorkspaces: vi.fn(), refreshClaudeCodePlan: vi.fn() };
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
    'live',
    refs as unknown as Processor[3],
    (event) => (event.agent as string) ?? null,
  );
  const captured = (id: string) =>
    (messages.find((m) => m.id === id) as unknown as Record<string, unknown>).capturedImages;
  return { process, captured };
}

const IMAGES = { 'charts/drop_cn.png': 'https://cdn.example/response-images/t/0e2d/drop_cn.png' };

describe('live: where a run\'s images were captured', () => {
  it('pins them on every bubble of the run\'s turn, and only that turn', () => {
    const { process, captured } = setup();

    process({ event: 'run_end', thread_id: 'thread-1', run_id: 'run-1', outcome: 'completed', images: IMAGES } as SSEEvent);

    expect(captured('live')).toEqual(IMAGES);
    // A reconnect left the start of the reply in a bubble of its own.
    expect(captured('dropped')).toEqual(IMAGES);
    // An earlier turn showed its own copy of the same path.
    expect(captured('earlier')).toBeUndefined();
  });

  it('pins nothing for a run that captured nothing', () => {
    const { process, captured } = setup();

    process({ event: 'run_end', thread_id: 'thread-1', run_id: 'run-1', outcome: 'completed' } as SSEEvent);

    expect(captured('live')).toBeUndefined();
  });

  // DALTON-HOOK(deliverable-text): verify per-message images_by_message scoping across bubbles and fallback
  it('pins images_by_message per segment and scopes each bubble to its own segment map with global fallback for unmatched segments', () => {
    const v1 = 'https://cdn.example/response-images/t/v1/price.png';
    const v2 = 'https://cdn.example/response-images/t/v2/price.png';
    const fallbackUrl = 'https://cdn.example/response-images/t/v2/extra.png';

    let messages: ChatMessage[] = [
      {
        ...bubble('dropped', 3),
        contentSegments: [
          { type: 'text', content: '第一版：![图](charts/price.png)', order: 1, messageId: 'm-1' },
        ],
      } as unknown as ChatMessage,
      {
        ...bubble('live', 3),
        contentSegments: [
          { type: 'text', content: '第二版：![图](charts/price.png)', order: 2, messageId: 'm-2', continuedFromMessageId: 'm-1' },
        ],
      } as unknown as ChatMessage,
      {
        ...bubble('unmatched', 3),
        contentSegments: [
          { type: 'text', content: '未匹配段：![图](charts/extra.png)', order: 3, messageId: 'm-unmatched' },
        ],
      } as unknown as ChatMessage,
    ];
    const setMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      messages = updater(messages);
    };
    const rt = {
      setMessages,
      queueMessages: setMessages,
      flushMessages: vi.fn(),
      updateSubagentCard: vi.fn(),
      lastEventIdRef: { current: null },
      threadIdRef: { current: 'thread-1' },
      threadId: 'thread-1',
      setThreadId: vi.fn(),
      pendingPTCBackfillRef: { current: new Map() },
      subagentHistory: { toolCalls: new Map() },
      setTokenUsage: vi.fn(),
      setNextSummary: vi.fn(),
      nextSummaryOwedRef: { current: false },
      t: (k: string) => k,
    };
    const deps = { clearModelStatus: () => {}, armReportBack: vi.fn(), refreshWorkspaces: vi.fn(), refreshClaudeCodePlan: vi.fn() };
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
      'live',
      refs as unknown as Processor[3],
      (event) => (event.agent as string) ?? null,
    );

    process({
      event: 'run_end',
      thread_id: 'thread-1',
      run_id: 'run-1',
      outcome: 'completed',
      images: { 'charts/price.png': v2, 'charts/extra.png': fallbackUrl },
      images_by_message: {
        'm-1': { 'charts/price.png': v1 },
        'm-2': { 'charts/price.png': v2 },
      },
    } as SSEEvent);

    const byId = (id: string) => messages.find((m) => m.id === id) as unknown as {
      capturedImages?: Record<string, string>;
      contentSegments: Array<{ messageId?: string; capturedImages?: Record<string, string> }>;
    };

    // Each bubble gets ONLY its own segment's map (dropped gets v1, NOT v2 or extra.png)
    expect(byId('dropped').capturedImages).toEqual({ 'charts/price.png': v1 });
    expect(byId('dropped').contentSegments[0].capturedImages).toEqual({ 'charts/price.png': v1 });
    expect(byId('live').capturedImages).toEqual({ 'charts/price.png': v2 });
    expect(byId('live').contentSegments[0].capturedImages).toEqual({ 'charts/price.png': v2 });
    // Unmatched segment falls back to global images
    expect(byId('unmatched').capturedImages).toEqual({ 'charts/price.png': v2, 'charts/extra.png': fallbackUrl });
    expect(byId('unmatched').contentSegments[0].capturedImages).toEqual({ 'charts/price.png': v2, 'charts/extra.png': fallbackUrl });
  });

  // DALTON-HOOK(deliverable-text): verify per-segment capturedImages stay distinct inside one bubble on same-path rewrites
  it('keeps per-segment capturedImages distinct inside a single multi-segment bubble for same-path rewrites', () => {
    const v1 = 'https://cdn.example/response-images/t/v1/price.png';
    const v2 = 'https://cdn.example/response-images/t/v2/price.png';

    let messages: ChatMessage[] = [
      {
        ...bubble('live', 3),
        contentSegments: [
          { type: 'text', content: '第一版：![图](charts/price.png)', order: 1, messageId: 'm-1' },
          { type: 'text', content: '第二版：![图](charts/price.png)', order: 2, messageId: 'm-2', continuedFromMessageId: 'm-1' },
        ],
      } as unknown as ChatMessage,
    ];
    const setMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      messages = updater(messages);
    };
    const rt = {
      setMessages,
      queueMessages: setMessages,
      flushMessages: vi.fn(),
      updateSubagentCard: vi.fn(),
      lastEventIdRef: { current: null },
      threadIdRef: { current: 'thread-1' },
      threadId: 'thread-1',
      setThreadId: vi.fn(),
      pendingPTCBackfillRef: { current: new Map() },
      subagentHistory: { toolCalls: new Map() },
      setTokenUsage: vi.fn(),
      setNextSummary: vi.fn(),
      nextSummaryOwedRef: { current: false },
      t: (k: string) => k,
    };
    const deps = { clearModelStatus: () => {}, armReportBack: vi.fn(), refreshWorkspaces: vi.fn(), refreshClaudeCodePlan: vi.fn() };
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
      'live',
      refs as unknown as Processor[3],
      (event) => (event.agent as string) ?? null,
    );

    process({
      event: 'run_end',
      thread_id: 'thread-1',
      run_id: 'run-1',
      outcome: 'completed',
      images: { 'charts/price.png': v2 },
      images_by_message: {
        'm-1': { 'charts/price.png': v1 },
        'm-2': { 'charts/price.png': v2 },
      },
    } as SSEEvent);

    const live = messages[0] as unknown as {
      contentSegments: Array<{ messageId?: string; capturedImages?: Record<string, string> }>;
    };
    expect(live.contentSegments[0].capturedImages).toEqual({ 'charts/price.png': v1 });
    expect(live.contentSegments[1].capturedImages).toEqual({ 'charts/price.png': v2 });
  });
});
