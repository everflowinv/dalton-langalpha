// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage, ProvenanceRecord } from '@/types/chat';
import { loadConversationHistory } from '../replayHistory';
import { buildRuntime, makeDeps } from './replayHarness';

const replayThreadHistory = vi.fn();

vi.mock('../../../utils/api', () => ({
  replayThreadHistory: (...args: unknown[]) => replayThreadHistory(...args),
}));

vi.mock('@/lib/network', () => ({
  isOnline: () => true,
}));

function makeReplayProvenanceEvent(
  turnIndex: number,
  i: number,
  toolCallId = `tc-${turnIndex}-${i}`,
  identifier = `doc-${turnIndex}-${i}`,
) {
  return {
    event: 'provenance',
    turn_index: turnIndex,
    record_id: `rec-${turnIndex}-${i}`,
    agent: 'main',
    source_type: 'research_doc',
    identifier,
    title: `Research doc ${i}`,
    tool_call_id: toolCallId,
    timestamp: '2026-11-06T00:00:00Z',
    result_snippet: `Snippet content ${i}`,
  };
}

function getProvenance(msg: ChatMessage | undefined) {
  return msg?.role === 'assistant' ? msg.provenanceRecords : undefined;
}

describe('replayHistory: batched provenance writes', () => {
  beforeEach(() => {
    replayThreadHistory.mockReset();
  });

  it('accumulates 5,000 provenance events in a turn and calls setMessages for provenance once per assistant message', async () => {
    const { rt, read } = buildRuntime();
    const deps = makeDeps();
    const realSetMessages = rt.setMessages;

    let provenanceSetMessagesCalls = 0;
    let setMessagesCallsDuringProvenanceLoop = 0;
    let inProvenanceLoop = false;

    const spySetMessages = vi.fn((updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      if (inProvenanceLoop) {
        setMessagesCallsDuringProvenanceLoop++;
      }
      const before = read();
      realSetMessages(updater);
      const after = read();
      const changedProvenance = after.some(
        (msg: ChatMessage, idx: number) => getProvenance(msg) !== getProvenance(before[idx]),
      );
      if (changedProvenance) {
        provenanceSetMessagesCalls++;
      }
    });
    rt.setMessages = spySetMessages as unknown as typeof rt.setMessages;

    replayThreadHistory.mockImplementationOnce(async (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
      onEvent({ event: 'user_message', role: 'user', content: 'Analyze high-growth sectors', turn_index: 0, run_id: 'run-0' });
      onEvent({ event: 'message_chunk', role: 'assistant', content_type: 'text', content: 'Report summary.', finish_reason: 'stop', turn_index: 0 });
      inProvenanceLoop = true;
      for (let i = 0; i < 5000; i++) {
        onEvent(makeReplayProvenanceEvent(0, i));
      }
      inProvenanceLoop = false;
      onEvent({ event: 'replay_done', latest_run_id: 'run-0' });
      return { disconnected: false, aborted: false };
    });

    const ok = await loadConversationHistory(rt, deps);

    expect(ok).toBe(true);
    expect(setMessagesCallsDuringProvenanceLoop).toBe(0);
    // Exactly 1 setMessages call for provenance (1 involved assistant message), independent of 5,000 events.
    expect(provenanceSetMessagesCalls).toBe(1);

    const assistants = read().filter((m): m is Extract<ChatMessage, { role: 'assistant' }> => m.role === 'assistant');
    expect(assistants).toHaveLength(1);
    const records = assistants[0].provenanceRecords as Record<string, ProvenanceRecord>;
    expect(records).toBeDefined();
    expect(Object.keys(records)).toHaveLength(5000);
    expect(records['tc-0-0:research_doc:doc-0-0'].record_id).toBe('rec-0-0');
    expect(records['tc-0-4999:research_doc:doc-0-4999'].record_id).toBe('rec-0-4999');
  });

  it('calls setMessages for provenance once per involved assistant message across multiple turns and overwrites duplicate keys', async () => {
    const { rt, read } = buildRuntime();
    const deps = makeDeps();
    const realSetMessages = rt.setMessages;

    let provenanceSetMessagesCalls = 0;
    const spySetMessages = vi.fn((updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      const before = read();
      realSetMessages(updater);
      const after = read();
      if (after.some((msg: ChatMessage, idx: number) => getProvenance(msg) !== getProvenance(before[idx]))) {
        provenanceSetMessagesCalls++;
      }
    });
    rt.setMessages = spySetMessages as unknown as typeof rt.setMessages;

    replayThreadHistory.mockImplementationOnce(async (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
      // Turn 0: 100 events + 1 overwrite on tc-0-0
      onEvent({ event: 'user_message', role: 'user', content: 'Turn 0', turn_index: 0, run_id: 'run-0' });
      onEvent({ event: 'message_chunk', role: 'assistant', content_type: 'text', content: 'Answer 0', finish_reason: 'stop', turn_index: 0 });
      for (let i = 0; i < 100; i++) {
        onEvent(makeReplayProvenanceEvent(0, i));
      }
      onEvent({
        ...makeReplayProvenanceEvent(0, 0, 'tc-0-0', 'doc-0-0'),
        record_id: 'rec-0-0-latest',
        title: 'Overwritten doc 0',
      });

      // Turn 1: 200 events
      onEvent({ event: 'user_message', role: 'user', content: 'Turn 1', turn_index: 1, run_id: 'run-1' });
      onEvent({ event: 'message_chunk', role: 'assistant', content_type: 'text', content: 'Answer 1', finish_reason: 'stop', turn_index: 1 });
      for (let i = 0; i < 200; i++) {
        onEvent(makeReplayProvenanceEvent(1, i));
      }

      onEvent({ event: 'replay_done', latest_run_id: 'run-1' });
      return { disconnected: false, aborted: false };
    });

    const ok = await loadConversationHistory(rt, deps);

    expect(ok).toBe(true);
    // 2 assistant messages with provenance -> 2 setMessages calls for provenance.
    expect(provenanceSetMessagesCalls).toBe(2);

    const assistants = read().filter((m): m is Extract<ChatMessage, { role: 'assistant' }> => m.role === 'assistant');
    expect(assistants).toHaveLength(2);
    const turn0Records = assistants[0].provenanceRecords as Record<string, ProvenanceRecord>;
    const turn1Records = assistants[1].provenanceRecords as Record<string, ProvenanceRecord>;
    expect(Object.keys(turn0Records)).toHaveLength(100);
    expect(turn0Records['tc-0-0:research_doc:doc-0-0'].record_id).toBe('rec-0-0-latest');
    expect(turn0Records['tc-0-0:research_doc:doc-0-0'].title).toBe('Overwritten doc 0');
    expect(Object.keys(turn1Records)).toHaveLength(200);
  });

  function replayTurnWithSources(count: number) {
    return async (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
      onEvent({ event: 'user_message', role: 'user', content: 'q', turn_index: 0, run_id: 'run-0' });
      onEvent({ event: 'message_chunk', role: 'assistant', content_type: 'text', content: 'a', finish_reason: 'stop', turn_index: 0 });
      for (let i = 0; i < count; i++) onEvent(makeReplayProvenanceEvent(0, i));
      onEvent({ event: 'replay_done', latest_run_id: 'run-0' });
      return { disconnected: false, aborted: false };
    };
  }

  it('an overtaken replay (thread switch or newer replay) writes none of its sources', async () => {
    const { rt, read } = buildRuntime();
    replayThreadHistory.mockImplementationOnce(replayTurnWithSources(10));

    const ok = await loadConversationHistory(rt, { ...makeDeps(), isCurrent: () => false });

    expect(ok).toBe(false);
    const assistant = read().find((m) => m.role === 'assistant');
    expect(getProvenance(assistant)).toBeUndefined();
  });

  it('a replay that fails after writing its bubbles still writes their sources while it is current', async () => {
    const { rt, read } = buildRuntime();
    replayThreadHistory.mockImplementationOnce(replayTurnWithSources(3));
    const deps = {
      ...makeDeps(),
      isCurrent: () => true,
      projectSubagentHistory: vi.fn(() => { throw new Error('projection failed'); }),
    };

    const ok = await loadConversationHistory(rt, deps);

    expect(ok).toBe(false);
    const assistant = read().find((m) => m.role === 'assistant');
    expect(Object.keys(getProvenance(assistant) ?? {})).toHaveLength(3);
  });

  it('an overtaken replay leaves the newer transcript alone when its events arrive late', async () => {
    const { rt, read } = buildRuntime();
    let current = true;
    replayThreadHistory.mockImplementationOnce(async (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
      onEvent({ event: 'user_message', role: 'user', content: 'thread A question', turn_index: 0, run_id: 'run-a' });
      // The view moves to another thread while this replay is still reading.
      current = false;
      rt.setMessages(() => [{ id: 'history-assistant-0', role: 'assistant', content: 'thread B answer', isHistory: true } as never]);
      onEvent({ event: 'message_chunk', role: 'assistant', content_type: 'text', content: 'thread A answer', finish_reason: 'stop', turn_index: 0 });
      onEvent(makeReplayProvenanceEvent(0, 0));
      onEvent({ event: 'replay_done', latest_run_id: 'run-a' });
      return { disconnected: false, aborted: false };
    });

    await loadConversationHistory(rt, { ...makeDeps(), isCurrent: () => current });

    const messages = read();
    expect(messages).toHaveLength(1);
    expect((messages[0] as { content?: string }).content).toBe('thread B answer');
    expect(getProvenance(messages[0])).toBeUndefined();
  });

  it('a replay overtaken before its first event ends without stripping or stamping the newer transcript', async () => {
    const { rt, read } = buildRuntime();
    let current = true;
    const beforeReveal = vi.fn();
    replayThreadHistory.mockImplementationOnce(async (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
      // The newer replay has already shown its history before this one's first event.
      current = false;
      rt.setMessages(() => [{ id: 'history-assistant-0', role: 'assistant', content: 'thread B answer', isHistory: true } as never]);
      onEvent({ event: 'user_message', role: 'user', content: 'thread A question', turn_index: 0, run_id: 'run-a', run_completed_at: '2026-11-06T00:00:00Z' });
      onEvent({ event: 'message_chunk', role: 'assistant', content_type: 'text', content: 'thread A answer', finish_reason: 'stop', turn_index: 0 });
      onEvent({ event: 'replay_done', latest_run_id: 'run-a' });
      return { disconnected: false, aborted: false };
    });

    const ok = await loadConversationHistory(rt, { ...makeDeps(), beforeReveal, isCurrent: () => current });

    expect(ok).toBe(false);
    const messages = read();
    expect(messages).toHaveLength(1);
    expect((messages[0] as { content?: string; completedAt?: number }).content).toBe('thread B answer');
    expect((messages[0] as { completedAt?: number }).completedAt).toBeUndefined();
    expect(beforeReveal).not.toHaveBeenCalled();
  });

  it.each([
    ['a late 404', new Error('HTTP 404 Not Found')],
    ['a late failure', new Error('HTTP 500 Internal Server Error')],
  ])('an overtaken replay ending in %s leaves the newer transcript and loading state alone', async (_label, failure) => {
    const { rt, read } = buildRuntime();
    let current = true;
    const beforeReveal = vi.fn();
    replayThreadHistory.mockImplementationOnce(async () => {
      current = false;
      rt.setMessages(() => [{ id: 'history-assistant-0', role: 'assistant', content: 'thread B answer', isHistory: true } as never]);
      rt.historyLoadingRef.current = true; // the newer replay is still loading
      throw failure;
    });

    const ok = await loadConversationHistory(rt, { ...makeDeps(), beforeReveal, isCurrent: () => current });

    expect(ok).toBe(false);
    expect(read()).toHaveLength(1);
    expect((read()[0] as { content?: string }).content).toBe('thread B answer');
    expect(rt.historyLoadingRef.current).toBe(true);
    expect(beforeReveal).not.toHaveBeenCalled();
  });
});
