import type { MessageRecord } from '../../hooks/utils/types';
// DALTON-HOOK(deliverable-text): delegate per-segment images_by_message pinning to Dalton helper
import { pinCapturedImagesBySegment } from '@/lib/dalton/capturedImagesBySegment';

/**
 * The bubbles of the turn `bubbleId` writes to, holding where each image
 * path they streamed was captured. A turn a reconnect split across bubbles
 * shares one run, so every assistant bubble under its count takes its map.
 */
export function pinCapturedImages(
  messages: MessageRecord[],
  bubbleId: string,
  images: Readonly<Record<string, string>>,
  imagesByMessage?: Readonly<Record<string, Readonly<Record<string, string>>>>,
): MessageRecord[] {
  // DALTON-HOOK(deliverable-text): use images_by_message per segment with global fallback
  return pinCapturedImagesBySegment(messages, bubbleId, images, imagesByMessage);
}
