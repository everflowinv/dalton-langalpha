// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/types/chat';
import type { SSEEvent } from '../../types';
import { createStreamEventProcessor } from '../processStreamEvent';

type Processor = Parameters<typeof createStreamEventProcessor>;

function setup() {
  let messages: ChatMessage[] = [
    {
      id: 'live',
      role: 'assistant',
      content: '',
      isStreaming: true,
      turnIndex: 1,
      contentSegments: [],
      reasoningProcesses: {},
      toolCallProcesses: {},
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
  const deps = {
    clearModelStatus: () => {},
    armReportBack: vi.fn(),
    refreshWorkspaces: vi.fn(),
    refreshClaudeCodePlan: vi.fn(),
  };
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
  const current = () => messages[0]!;
  return { process, current };
}

describe('message_discarded with mismatched stream chunk id vs final AIMessage id', () => {
  it('retracts fragment when event carries both final id and stream_id', () => {
    const { process, current } = setup();

    process({
      event: 'message_chunk',
      agent: 'main',
      id: 'lc_run--chunk-99',
      content_type: 'text',
      content: '═',
    } as SSEEvent);
    expect(current().content).toBe('═');

    process({
      event: 'message_discarded',
      agent: 'main',
      id: 'msg-final-99',
      stream_id: 'lc_run--chunk-99',
      ids: ['msg-final-99', 'lc_run--chunk-99'],
    } as SSEEvent);
    expect(current().content).toBe('');

    process({
      event: 'message_chunk',
      agent: 'main',
      id: 'msg-final-100',
      content_type: 'text',
      content: '完整正式回答',
    } as SSEEvent);
    expect(current().content).toBe('完整正式回答');
  });

  it('does NOT mistakenly retract an earlier lc_run-- prefix when only a new attempt final id is discarded', () => {
    const { process, current } = setup();

    process({
      event: 'message_chunk',
      agent: 'main',
      id: 'lc_run--prefix-0',
      content_type: 'text',
      content: '前半段正文',
    } as SSEEvent);

    // Attempt 1 returned an empty aggregate with no streamed chunk of its own:
    // server emits only its committed final id (no stream_id).
    process({
      event: 'message_discarded',
      agent: 'main',
      id: 'msg-empty-attempt-1',
    } as SSEEvent);
    expect(current().content).toBe('前半段正文');

    process({
      event: 'message_chunk',
      agent: 'main',
      id: 'dalton-notice-1',
      continued_from_message_id: 'lc_run--prefix-0',
      content_type: 'text',
      content: '截断提示',
    } as SSEEvent);
    expect(current().content).toBe('前半段正文截断提示');
  });
});
