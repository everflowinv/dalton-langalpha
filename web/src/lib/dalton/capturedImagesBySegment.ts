/**
 * Per-segment captured image pinning for live `run_end` frames.
 *
 * Keeps `capturedImages.ts` and `processStreamEvent.ts` to thin `DALTON-HOOK`
 * delegates while resolving `event.images_by_message` per text segment (falling
 * back to global `event.images` only for unmatched segments or bubbles without
 * matched segments), so same-path chart rewrites across continuation messages
 * match reload/channel behavior and each bubble only receives its own map.
 */
import type { MessageRecord } from '@/pages/ChatAgent/hooks/utils/types';
import type { ContentSegmentRecord } from '@/pages/ChatAgent/components/messageList/types';

function hasEntries(map: Readonly<Record<string, string>> | undefined): map is Readonly<Record<string, string>> {
  return !!map && Object.keys(map).length > 0;
}

export function pinCapturedImagesBySegment(
  messages: MessageRecord[],
  bubbleId: string,
  images: Readonly<Record<string, string>>,
  imagesByMessage?: Readonly<Record<string, Readonly<Record<string, string>>>>,
): MessageRecord[] {
  const hasGlobal = hasEntries(images);
  const hasPerMsg = !!imagesByMessage && Object.values(imagesByMessage).some((m) => hasEntries(m));
  if (!hasGlobal && !hasPerMsg) return messages;

  const bubble = messages.find((m) => m.id === bubbleId);
  if (!bubble) return messages;
  const turn = bubble.turnIndex;

  let changed = false;
  const next = messages.map((m) => {
    if (m.role !== 'assistant') return m;
    if (m.id !== bubbleId && (turn === undefined || m.turnIndex !== turn)) return m;

    const existingBubbleImages = (m.capturedImages as Record<string, string> | undefined) ?? undefined;
    if (!hasPerMsg) {
      changed = true;
      return { ...m, capturedImages: { ...existingBubbleImages, ...images } };
    }

    const rawSegments = m.contentSegments as ContentSegmentRecord[] | undefined;
    if (!rawSegments || rawSegments.length === 0) {
      if (!hasGlobal) return m;
      changed = true;
      return { ...m, capturedImages: { ...existingBubbleImages, ...images } };
    }

    let sawTextSegment = false;
    let allTextSegmentsMatched = true;
    let segmentsChanged = false;
    const ownMerged: Record<string, string> = {};

    const updatedSegments = rawSegments.map((seg) => {
      if (seg.type !== 'text') return seg;
      sawTextSegment = true;
      const own = seg.messageId ? imagesByMessage?.[seg.messageId] : undefined;
      if (hasEntries(own)) {
        Object.assign(ownMerged, own);
        segmentsChanged = true;
        return {
          ...seg,
          capturedImages: { ...(seg.capturedImages ?? {}), ...own },
        };
      }
      allTextSegmentsMatched = false;
      if (hasGlobal) {
        segmentsChanged = true;
        return {
          ...seg,
          capturedImages: { ...(seg.capturedImages ?? {}), ...images },
        };
      }
      return seg;
    });

    let nextBubbleImages: Record<string, string> | undefined;
    if (sawTextSegment && allTextSegmentsMatched) {
      // Every text segment in this bubble matched its own per-message map:
      // keep ONLY this bubble's own segment maps (never pull in another bubble's images).
      nextBubbleImages = { ...existingBubbleImages, ...ownMerged };
    } else if (hasGlobal || Object.keys(ownMerged).length > 0) {
      // Unmatched segment(s) or no text segments: fall back to global images,
      // with any matched per-segment entries preserved on those segments individually.
      nextBubbleImages = {
        ...existingBubbleImages,
        ...(hasGlobal ? images : {}),
        ...(hasGlobal ? {} : ownMerged),
      };
    }

    if (!segmentsChanged && !nextBubbleImages) return m;
    changed = true;
    return {
      ...m,
      ...(segmentsChanged ? { contentSegments: updatedSegments } : {}),
      ...(nextBubbleImages ? { capturedImages: nextBubbleImages } : {}),
    };
  });

  return changed ? next : messages;
}
