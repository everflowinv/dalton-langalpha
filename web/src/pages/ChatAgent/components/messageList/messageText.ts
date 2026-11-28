import type { ArtifactCommentsShown } from '@/types/api';
// DALTON-HOOK(deliverable-text): reuse collapsed deliverable text selector for copy/preview
import { selectDeliverableAssistantText } from '@/lib/dalton/deliverableProjection';

/** The reply's prose in transcript order. Segments can land out of order, so they are sorted by `order` the way the bubble renders them; a message with no text segments falls back to its flat `content`. Shared by copy, the minimap preview and the deliverables deck so all three read the same text. `max` stops reading once that much text is in hand, for a caller that shows only the head of a reply still streaming. */
export function assistantText(message: { contentSegments?: unknown; content?: unknown; toolCallProcesses?: unknown }, max = Infinity): string {
  // DALTON-HOOK(deliverable-text): exclude commentary and pre-tool preamble identically to collapsed view
  return selectDeliverableAssistantText(message, max);
}

/** A user message's text as its bubble shows it: its words, then a line for each file comment it sent. For Copy and the minimap preview. */
export function userMessageText(message: { content?: unknown; artifactComments?: unknown }): string {
  const words = typeof message.content === 'string' ? message.content : '';
  const shown = message.artifactComments as ArtifactCommentsShown | undefined;
  if (!shown) return words;
  const lines = shown.comments.map((c) => {
    const on = [c.label, c.quote ? `"${c.quote}"` : null].filter(Boolean).join(' ');
    return `${c.number}. ${on ? `${on}: ` : ''}${c.text}`;
  });
  return [words.trim(), ...lines].filter(Boolean).join('\n');
}
