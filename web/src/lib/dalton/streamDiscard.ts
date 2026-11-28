/**
 * Dalton explicit-ID retraction helper for ``message_discarded`` live SSE events.
 *
 * Keeps ``mainEventHandlers.ts`` to a thin 1–4 line ``DALTON-HOOK(deliverable-text)``
 * delegate and only retracts text segments whose ``messageId`` explicitly matches
 * one of the server-emitted IDs (committed ``id``, attempt-correlated ``stream_id``,
 * or ``ids``) — never guessing or retracting an unrelated earlier ``lc_run--`` prefix.
 */
import type { ContentSegment, TextSegment } from '@/types/chat';
import type { MessageRecord } from '@/pages/ChatAgent/hooks/utils/types';

export interface MessageDiscardParams {
  assistantMessageId: string;
  discardedMessageId?: string;
  discardedStreamId?: string;
  discardedMessageIds?: string[];
}

export function collectDiscardedIds(params: Omit<MessageDiscardParams, 'assistantMessageId'>): Set<string> {
  const ids = new Set<string>();
  if (params.discardedMessageId) ids.add(params.discardedMessageId);
  if (params.discardedStreamId) ids.add(params.discardedStreamId);
  if (params.discardedMessageIds) {
    for (const id of params.discardedMessageIds) {
      if (id) ids.add(id);
    }
  }
  return ids;
}

export function retractDiscardedSegmentsOnMessage(
  msg: MessageRecord,
  ids: ReadonlySet<string>,
): MessageRecord {
  const segs = (msg.contentSegments as ContentSegment[]) || [];
  const kept = segs.filter(
    (s) => !(s.type === 'text' && s.messageId !== undefined && ids.has(s.messageId)),
  );
  if (kept.length === segs.length) return msg;
  const content = kept
    .filter((s): s is TextSegment => s.type === 'text')
    .map((s) => s.content || '')
    .join('');
  return { ...msg, contentSegments: kept, content };
}
