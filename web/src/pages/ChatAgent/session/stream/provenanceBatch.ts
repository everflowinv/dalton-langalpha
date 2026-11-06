import type { ProvenanceRecord } from '@/types/chat';
import type { ProvenanceEvent } from '@/types/sse';
import type { MessageRecord, SetMessages } from '../../hooks/utils/types';
import { provenanceEventToRecord, provenanceRecordKey } from './provenance';

/**
 * Merges a batch of provenance records into an assistant message's existing
 * `provenanceRecords` map with a single shallow copy.
 */
export function mergeProvenanceRecords(
  existing: Record<string, ProvenanceRecord> | undefined,
  batch: Record<string, ProvenanceRecord>,
): Record<string, ProvenanceRecord> {
  return { ...(existing || {}), ...batch };
}

/**
 * Applies a batch of provenance records to one assistant message in a single
 * `setMessages` call.
 */
export function applyProvenanceBatch(
  setMessages: SetMessages,
  assistantMessageId: string,
  batch: Record<string, ProvenanceRecord>,
): void {
  if (Object.keys(batch).length === 0) return;
  setMessages((prev: MessageRecord[]) =>
    prev.map((msg: MessageRecord) => {
      if (msg.id !== assistantMessageId) return msg;
      const provenanceRecords = mergeProvenanceRecords(
        msg.provenanceRecords as Record<string, ProvenanceRecord> | undefined,
        batch,
      );
      return { ...msg, provenanceRecords };
    }),
  );
}

/**
 * Accumulates a replayed provenance event into the per-message replay map
 * without touching React state. Later events with the same key overwrite
 * earlier ones.
 */
export function accumulateReplayProvenance(
  byMessage: Map<string, Record<string, ProvenanceRecord>>,
  assistantMessageId: string,
  event: ProvenanceEvent,
): boolean {
  if (!event || !event.record_id) {
    return false;
  }
  const record = provenanceEventToRecord(event);
  const key = provenanceRecordKey(record);
  let bucket = byMessage.get(assistantMessageId);
  if (!bucket) {
    bucket = {};
    byMessage.set(assistantMessageId, bucket);
  }
  bucket[key] = record;
  return true;
}

/**
 * Flushes all accumulated replay provenance records at replay end, calling
 * `setMessages` once per assistant message in `byMessage`.
 */
export function flushReplayProvenance(
  byMessage: Map<string, Record<string, ProvenanceRecord>>,
  setMessages: SetMessages,
): void {
  for (const [assistantMessageId, batch] of byMessage) {
    applyProvenanceBatch(setMessages, assistantMessageId, batch);
  }
  byMessage.clear();
}

export interface ProvenanceFrameBuffer {
  /** Buffer a live provenance event for `assistantMessageId` to flush on the next frame. */
  queue: (assistantMessageId: string, event: ProvenanceEvent, threadId?: string | null) => boolean;
  /** Immediately flush buffered provenance records for the current thread. */
  flush: () => void;
  /** Discard buffered provenance records and cancel any pending frame or timer. */
  clear: () => void;
}

function getRequestAnimationFrame(): ((cb: FrameRequestCallback) => number) | null {
  const desc = Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame');
  if (desc && 'get' in desc) return null;
  return typeof globalThis.requestAnimationFrame === 'function'
    ? globalThis.requestAnimationFrame.bind(globalThis)
    : null;
}

function getCancelAnimationFrame(): ((id: number) => void) | null {
  const desc = Object.getOwnPropertyDescriptor(globalThis, 'cancelAnimationFrame');
  if (desc && 'get' in desc) return null;
  return typeof globalThis.cancelAnimationFrame === 'function'
    ? globalThis.cancelAnimationFrame.bind(globalThis)
    : null;
}

/**
 * Frame-batched buffer for live `provenance` events. Groups incoming records by
 * assistant message ID and flushes once on the next `requestAnimationFrame`
 * (or `setTimeout(0)` when `requestAnimationFrame` is unavailable), calling
 * `setMessages` once per message. Records belonging to an older thread are
 * discarded when switching threads.
 */
export function createProvenanceFrameBuffer({
  setMessages,
  getThreadId,
}: {
  setMessages: SetMessages;
  getThreadId?: () => string | null;
}): ProvenanceFrameBuffer {
  const pending = new Map<string, { batch: Record<string, ProvenanceRecord>; threadId: string | null }>();
  let rafId: number | null = null;
  let timerId: ReturnType<typeof setTimeout> | null = null;

  function cancelScheduled(): void {
    if (rafId !== null) {
      const caf = getCancelAnimationFrame();
      caf?.(rafId);
      rafId = null;
    }
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
  }

  function flush(): void {
    cancelScheduled();
    if (pending.size === 0) return;
    const entries = [...pending.entries()];
    pending.clear();
    const activeThreadId = getThreadId ? getThreadId() : null;
    for (const [assistantMessageId, entry] of entries) {
      if (
        activeThreadId !== null &&
        entry.threadId !== null &&
        entry.threadId !== '__default__' &&
        entry.threadId !== activeThreadId
      ) {
        continue;
      }
      applyProvenanceBatch(setMessages, assistantMessageId, entry.batch);
    }
  }

  function scheduleFlush(): void {
    if (rafId !== null || timerId !== null) return;
    const raf = getRequestAnimationFrame();
    if (raf) {
      rafId = raf(() => {
        rafId = null;
        flush();
      });
    } else {
      timerId = setTimeout(() => {
        timerId = null;
        flush();
      }, 0);
    }
  }

  function queue(assistantMessageId: string, event: ProvenanceEvent, threadId?: string | null): boolean {
    if (!event || !event.record_id) {
      return false;
    }
    const effectiveThreadId = threadId !== undefined ? threadId : (getThreadId ? getThreadId() : null);
    if (effectiveThreadId && effectiveThreadId !== '__default__') {
      for (const [msgId, entry] of pending) {
        if (entry.threadId === '__default__') {
          entry.threadId = effectiveThreadId;
        } else if (entry.threadId !== null && entry.threadId !== effectiveThreadId) {
          pending.delete(msgId);
        }
      }
    }

    const record = provenanceEventToRecord(event);
    const key = provenanceRecordKey(record);
    let entry = pending.get(assistantMessageId);
    if (!entry) {
      entry = { batch: {}, threadId: effectiveThreadId };
      pending.set(assistantMessageId, entry);
    } else if (effectiveThreadId !== null) {
      entry.threadId = effectiveThreadId;
    }
    entry.batch[key] = record;
    scheduleFlush();
    return true;
  }

  function clear(): void {
    cancelScheduled();
    pending.clear();
  }

  return { queue, flush, clear };
}
