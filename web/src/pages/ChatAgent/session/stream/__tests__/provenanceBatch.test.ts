import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage, ProvenanceRecord } from '@/types/chat';
import type { ProvenanceEvent } from '@/types/sse';
import type { MessageRecord, SetMessages } from '../../../hooks/utils/types';
import type { SSEEvent } from '../../types';
import { mockFrames, pendingFrames, runFrame, settleFrames } from '@/test/frames';
import { handleProvenance } from '../mainEventHandlers';
import { createStreamEventProcessor } from '../processStreamEvent';
import { provenanceEventToRecord, provenanceRecordKey } from '../provenance';
import {
  createProvenanceFrameBuffer,
  mergeProvenanceRecords,
} from '../provenanceBatch';

function makeEvent(id: string, overrides: Partial<ProvenanceEvent> = {}): ProvenanceEvent {
  return {
    event: 'provenance',
    record_id: id,
    agent: 'main',
    source_type: 'research_doc',
    identifier: `doc-${id}`,
    title: `Document ${id}`,
    tool_call_id: `tc-${id}`,
    timestamp: '2026-11-06T00:00:00Z',
    result_snippet: `Snippet for ${id}`,
    ...overrides,
  };
}

describe('mergeProvenanceRecords', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('merges multiple records in one copy and overwrites matching keys', () => {
    const first = provenanceEventToRecord(
      makeEvent('1', { tool_call_id: 'tc-shared', identifier: 'doc-shared', title: 'v1' }),
    );
    const untouched = provenanceEventToRecord(
      makeEvent('2', { tool_call_id: 'tc-keep', identifier: 'doc-keep', title: 'keep' }),
    );
    const existing: Record<string, ProvenanceRecord> = {
      [provenanceRecordKey(first)]: first,
      [provenanceRecordKey(untouched)]: untouched,
    };

    const overwritten = provenanceEventToRecord(
      makeEvent('1-new', {
        tool_call_id: 'tc-shared',
        identifier: 'doc-shared',
        title: 'v2',
        result_snippet: 'updated',
      }),
    );
    const added = provenanceEventToRecord(
      makeEvent('3', { tool_call_id: 'tc-added', identifier: 'doc-added', title: 'added' }),
    );
    const batch: Record<string, ProvenanceRecord> = {
      [provenanceRecordKey(overwritten)]: overwritten,
      [provenanceRecordKey(added)]: added,
    };

    const merged = mergeProvenanceRecords(existing, batch);

    expect(merged).not.toBe(existing);
    expect(Object.keys(merged)).toHaveLength(3);
    expect(merged[provenanceRecordKey(first)].title).toBe('v2');
    expect(merged[provenanceRecordKey(first)].result_snippet).toBe('updated');
    expect(merged[provenanceRecordKey(untouched)].title).toBe('keep');
    expect(merged[provenanceRecordKey(added)].title).toBe('added');
    // Original map is not mutated.
    expect(existing[provenanceRecordKey(first)].title).toBe('v1');
  });

  it('keeps handleProvenance single-event signature working via mergeProvenanceRecords', () => {
    let messages: MessageRecord[] = [{ id: 'a-1', role: 'assistant', content: '' }];
    const setMessages: SetMessages = vi.fn((updater) => {
      messages = updater(messages);
    });

    expect(handleProvenance({ assistantMessageId: 'a-1', event: makeEvent('1'), setMessages })).toBe(true);
    expect(handleProvenance({ assistantMessageId: 'a-1', event: {} as ProvenanceEvent, setMessages })).toBe(false);

    const records = messages[0].provenanceRecords as Record<string, ProvenanceRecord>;
    expect(Object.keys(records)).toHaveLength(1);
    expect(Object.values(records)[0].record_id).toBe('1');
  });
});

describe('createProvenanceFrameBuffer', () => {
  afterEach(() => {
    settleFrames();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('batches records per message and flushes once on the next animation frame with same-key overwrite', () => {
    mockFrames();
    let messages: MessageRecord[] = [
      { id: 'msg-1', role: 'assistant', content: '' },
      { id: 'msg-2', role: 'assistant', content: '' },
    ];
    const setMessages = vi.fn<SetMessages>((updater) => {
      messages = updater(messages);
    });

    const buffer = createProvenanceFrameBuffer({
      setMessages,
      getThreadId: () => 'thread-1',
    });

    // Queue multiple records for msg-1 (including a same-key overwrite) and msg-2.
    buffer.queue('msg-1', makeEvent('1', { tool_call_id: 'tc-1', identifier: 'doc-1', title: 'old' }));
    buffer.queue('msg-1', makeEvent('2', { tool_call_id: 'tc-1', identifier: 'doc-1', title: 'new' }));
    buffer.queue('msg-1', makeEvent('3', { tool_call_id: 'tc-2', identifier: 'doc-2', title: 'second' }));
    buffer.queue('msg-2', makeEvent('4', { tool_call_id: 'tc-3', identifier: 'doc-3', title: 'other-msg' }));

    expect(setMessages).not.toHaveBeenCalled();
    expect(pendingFrames()).toBe(1);

    runFrame();

    // Exactly one setMessages call per assistant message in the batch.
    expect(setMessages).toHaveBeenCalledTimes(2);
    const msg1Records = messages[0].provenanceRecords as Record<string, ProvenanceRecord>;
    const msg2Records = messages[1].provenanceRecords as Record<string, ProvenanceRecord>;
    expect(Object.keys(msg1Records)).toHaveLength(2);
    expect(msg1Records['tc-1:research_doc:doc-1'].title).toBe('new');
    expect(msg1Records['tc-2:research_doc:doc-2'].title).toBe('second');
    expect(Object.keys(msg2Records)).toHaveLength(1);
    expect(msg2Records['tc-3:research_doc:doc-3'].title).toBe('other-msg');
  });

  it('discards buffered records from the old thread when switching threads', () => {
    mockFrames();
    let activeThread = 'thread-old';
    let messages: MessageRecord[] = [
      { id: 'msg-old', role: 'assistant', content: '' },
      { id: 'msg-new', role: 'assistant', content: '' },
    ];
    const setMessages = vi.fn<SetMessages>((updater) => {
      messages = updater(messages);
    });

    const buffer = createProvenanceFrameBuffer({
      setMessages,
      getThreadId: () => activeThread,
    });

    buffer.queue('msg-old', makeEvent('1', { tool_call_id: 'tc-old' }));
    expect(pendingFrames()).toBe(1);

    // Switch thread before the scheduled frame fires; old thread's records must not land.
    activeThread = 'thread-new';
    runFrame();

    expect(setMessages).not.toHaveBeenCalled();
    expect(messages[0].provenanceRecords).toBeUndefined();

    // Explicit clear() on thread switch / unmount also cancels the scheduled frame.
    buffer.queue('msg-new', makeEvent('2', { tool_call_id: 'tc-new' }));
    expect(pendingFrames()).toBe(1);
    buffer.clear();
    runFrame();

    expect(setMessages).not.toHaveBeenCalled();
    expect(messages[1].provenanceRecords).toBeUndefined();
  });

  it('falls back to setTimeout(0) when requestAnimationFrame is unavailable', () => {
    vi.useFakeTimers();
    vi.stubGlobal('requestAnimationFrame', undefined);

    let messages: MessageRecord[] = [{ id: 'msg-1', role: 'assistant', content: '' }];
    const setMessages = vi.fn<SetMessages>((updater) => {
      messages = updater(messages);
    });

    const buffer = createProvenanceFrameBuffer({
      setMessages,
      getThreadId: () => 'thread-1',
    });

    buffer.queue('msg-1', makeEvent('1', { tool_call_id: 'tc-1' }));
    buffer.queue('msg-1', makeEvent('2', { tool_call_id: 'tc-2' }));
    expect(setMessages).not.toHaveBeenCalled();

    vi.runAllTimers();

    expect(setMessages).toHaveBeenCalledTimes(1);
    const records = messages[0].provenanceRecords as Record<string, ProvenanceRecord>;
    expect(Object.keys(records)).toHaveLength(2);
  });

  it('flushes immediately on run_end and error in createStreamEventProcessor', () => {
    mockFrames();
    let messages: ChatMessage[] = [{
      id: 'a-live',
      role: 'assistant',
      content: '',
      isStreaming: true,
      contentSegments: [],
      reasoningProcesses: {},
      toolCallProcesses: {},
    } as unknown as ChatMessage];

    const setMessages = vi.fn((updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      messages = updater(messages);
    });
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
      setFallbackSuggestion: vi.fn(),
      setMessageError: vi.fn(),
      t: (k: string) => k,
    };
    const deps = {
      clearModelStatus: () => {},
      armReportBack: vi.fn(),
      refreshWorkspaces: vi.fn(),
    };
    const refs = {
      contentOrderCounterRef: { current: 0 },
      currentReasoningIdRef: { current: null },
      currentToolCallIdRef: { current: null },
      subagentStateRefs: {},
      isReconnect: false,
    };

    type ProcessorParams = Parameters<typeof createStreamEventProcessor>;
    const process = createStreamEventProcessor(
      rt as unknown as ProcessorParams[0],
      deps as unknown as ProcessorParams[1],
      'a-live',
      refs as unknown as ProcessorParams[3],
      (event) => (event.agent as string) ?? null,
    );

    process({ ...makeEvent('1', { tool_call_id: 'tc-1' }) } as unknown as SSEEvent);
    process({ ...makeEvent('2', { tool_call_id: 'tc-2' }) } as unknown as SSEEvent);

    expect(setMessages).not.toHaveBeenCalled();
    expect(pendingFrames()).toBe(1);

    // run_end flushes buffered provenance immediately without waiting for the animation frame.
    process({ event: 'run_end', thread_id: 'thread-1', run_id: 'run-1', outcome: 'completed' } as SSEEvent);

    expect(setMessages).toHaveBeenCalledTimes(1);
    const recordsAfterRunEnd = (messages[0] as unknown as MessageRecord).provenanceRecords as Record<string, ProvenanceRecord>;
    expect(Object.keys(recordsAfterRunEnd)).toHaveLength(2);

    // Queue another provenance event and verify error flushes immediately before settling the failed bubble.
    process({ ...makeEvent('3', { tool_call_id: 'tc-3' }) } as unknown as SSEEvent);
    process({ event: 'error', error: 'Upstream failed', error_kind: 'upstream' } as unknown as SSEEvent);

    const recordsAfterError = (messages[0] as unknown as MessageRecord).provenanceRecords as Record<string, ProvenanceRecord>;
    expect(Object.keys(recordsAfterError)).toHaveLength(3);
  });
});
