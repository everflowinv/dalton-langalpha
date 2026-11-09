/**
 * Discord outbound text formatting: `normalizeDiscordBold`, GFM table rendering,
 * and 2000-character grapheme/code-fence aware chunking.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/send.receipt-BAlzXd8e.mjs`
 * (lines 44-178: `findGraphemeChunkEnd`, `chunkDiscordText`, `chunkDiscordTextWithMode`)
 * and `send.outbound-_jx4Xpd3.mjs` (lines 173-347: `normalizeDiscordBold`,
 * `renderDiscordMarkdown`). License: MIT, Copyright (c) 2025 Peter Steinberger.
 * Code-region and fence balancing primitives come from `src/shared/text.ts`.
 */

import {
  chunkTextForOutbound,
  convertMarkdownTables,
  findCodeRegions,
  truncateUtf16Safe,
  type MarkdownTableMode,
} from "../../shared/text.js";

export const DISCORD_MAX_CHARS = 2000;

let segmenter: Intl.Segmenter | undefined;
function graphemeSegmenter(): Intl.Segmenter {
  segmenter ??= new Intl.Segmenter(undefined, { granularity: "grapheme" });
  return segmenter;
}

/** Last grapheme boundary at or before `maxEnd`, never splitting a surrogate pair. */
export function findGraphemeChunkEnd(text: string, start: number, maxEnd: number): number {
  const hardEnd = Math.min(maxEnd, text.length);
  if (hardEnd <= start) return start;
  if (hardEnd === text.length) return hardEnd;
  const before = text.charCodeAt(hardEnd - 1);
  const after = text.charCodeAt(hardEnd);
  if (before <= 127 && after <= 127 && !(before === 13 && after === 10)) return hardEnd;
  let end = start;
  for (const seg of graphemeSegmenter().segment(text.slice(start, hardEnd + 2))) {
    const segEnd = start + seg.index + seg.segment.length;
    if (segEnd <= hardEnd) end = segEnd;
    else break;
  }
  if (end > start) return end;
  const safe = truncateUtf16Safe(text.slice(start, hardEnd), hardEnd - start);
  return start + (safe.length || Math.min(2, hardEnd - start));
}

/**
 * Converts `__bold__` to `**bold**` outside code regions. Discord renders
 * `__` as underline, so this preserves the author's bold intent.
 */
export function normalizeDiscordBold(markdown: string): string {
  if (!markdown.includes("__")) return markdown;
  const regions = findCodeRegions(markdown);
  const isInCode = (pos: number): boolean => regions.some((r) => pos >= r.start && pos < r.end);
  let out = "";
  let i = 0;
  while (i < markdown.length) {
    if (!isInCode(i) && markdown.startsWith("__", i)) {
      const close = markdown.indexOf("__", i + 2);
      if (close > i + 2 && !isInCode(close)) {
        const inner = markdown.slice(i + 2, close);
        if (inner.trim().length > 0 && !inner.includes("\n\n")) {
          out += `**${inner}**`;
          i = close + 2;
          continue;
        }
      }
    }
    out += markdown[i];
    i += 1;
  }
  return out;
}

export function renderDiscordMarkdown(markdown: string, tableMode: MarkdownTableMode = "code"): string {
  return normalizeDiscordBold(convertMarkdownTables(markdown, tableMode));
}

function splitOverlong(text: string, limit: number): string[] {
  const pieces: string[] = [];
  let start = 0;
  while (start < text.length) {
    const end = findGraphemeChunkEnd(text, start, start + limit);
    const piece = text.slice(start, end > start ? end : start + 1);
    if (piece) pieces.push(piece);
    start += piece.length || 1;
  }
  return pieces;
}

/**
 * Splits rendered Discord markdown into chunks of at most 2000 characters,
 * balancing fenced code blocks and never splitting a grapheme/surrogate pair.
 * Each returned chunk is one Discord message; the adapter sends exactly one
 * chunk per `sendTextChunk` call.
 */
export function chunkDiscordText(text: string, opts: { maxChars?: number } = {}): string[] {
  const limit = Math.max(1, Math.floor(opts.maxChars ?? DISCORD_MAX_CHARS));
  if (!text) return [];
  const rendered = renderDiscordMarkdown(text);
  if (rendered.length <= limit) return [rendered];
  const chunks: string[] = [];
  for (const chunk of chunkTextForOutbound(rendered, limit)) {
    if (chunk.length <= limit) chunks.push(chunk);
    else chunks.push(...splitOverlong(chunk, limit));
  }
  return chunks.filter((c) => c.length > 0);
}

export function chunkDiscordTextWithMode(
  text: string,
  opts: { maxChars?: number; chunkMode?: "length" | "newline" } = {},
): string[] {
  // Both modes end at the same fence/grapheme-safe splitter; paragraph
  // preference is already applied inside `chunkTextForOutbound`.
  return chunkDiscordText(text, opts);
}
