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
  const lookaheadEnd = Math.min(text.length, hardEnd + 64);
  for (const seg of graphemeSegmenter().segment(text.slice(start, lookaheadEnd))) {
    const segEnd = start + seg.index + seg.segment.length;
    if (segEnd <= hardEnd) end = segEnd;
    else break;
  }
  if (end > start) return end;
  const safe = truncateUtf16Safe(text.slice(start, hardEnd), hardEnd - start);
  return start + (safe.length || 1);
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

function splitOverlongGraphemeLine(line: string, step: number): string[] {
  const out: string[] = [];
  let start = 0;
  while (start < line.length) {
    if (line.length - start <= step) {
      out.push(line.slice(start));
      break;
    }
    const graphemeEnd = findGraphemeChunkEnd(line, start, start + step);
    const window = line.slice(start, graphemeEnd);
    const wsIdx = Math.max(window.lastIndexOf(" "), window.lastIndexOf("\t"));
    if (wsIdx > Math.floor(step * 0.5)) {
      out.push(window.slice(0, wsIdx));
      start += wsIdx + 1;
      while (start < line.length && (line[start] === " " || line[start] === "\t")) {
        start += 1;
      }
    } else {
      const end = graphemeEnd > start ? graphemeEnd : start + 1;
      out.push(line.slice(start, end));
      start = end;
    }
  }
  return out;
}

function isFenceLine(line: string): boolean {
  const trimmed = line.trimStart();
  return trimmed.startsWith("```") || trimmed.startsWith("~~~");
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
  const rendered = renderDiscordMarkdown(text).replace(/\r\n/g, "\n");
  if (rendered.length <= limit) return [rendered];

  const lines = rendered.split("\n");
  const chunks: string[] = [];
  let buf = "";
  let openFence: string | null = null;
  let fenceMarker = "```";

  const closeFence = (marker: string): string => `\n${marker}`;

  const flush = (isFinal = false): void => {
    if (buf.length === 0) return;
    if (openFence !== null) {
      if (!isFinal && buf === openFence) {
        buf = "";
        return;
      }
      if (!isFinal && buf.endsWith(`\n${openFence}`)) {
        const prose = buf.slice(0, buf.length - openFence.length - 1);
        if (prose.length > 0) chunks.push(prose);
        buf = "";
        return;
      }
      chunks.push(`${buf}${closeFence(fenceMarker)}`);
    } else {
      chunks.push(buf);
    }
    buf = "";
  };

  for (const line of lines) {
    const togglesFence = isFenceLine(line);
    const nextFence: string | null = togglesFence
      ? openFence === null
        ? line.trimEnd()
        : null
      : openFence;
    if (togglesFence && openFence === null) {
      fenceMarker = line.trimStart().startsWith("~~~") ? "~~~" : "```";
    }
    const reserve = nextFence !== null ? closeFence(fenceMarker).length : 0;
    const candidate = buf.length === 0 ? line : `${buf}\n${line}`;

    if (candidate.length + reserve > limit) {
      if (buf.length > 0) flush(false);
      const prefix = openFence !== null ? `${openFence}\n` : "";
      const lineReserve = openFence !== null ? closeFence(fenceMarker).length : 0;
      if (prefix.length + lineReserve >= limit) {
        openFence = null;
        for (const piece of splitOverlong(line, limit)) {
          chunks.push(piece);
        }
        continue;
      }
      if (prefix.length + line.length + lineReserve > limit) {
        const step = Math.max(1, limit - prefix.length - lineReserve);
        const pieces = splitOverlongGraphemeLine(line, step);
        if (openFence !== null) {
          for (let i = 0; i < pieces.length - 1; i++) {
            chunks.push(`${prefix}${pieces[i]!}${closeFence(fenceMarker)}`);
          }
          if (pieces.length > 0) {
            buf = `${prefix}${pieces[pieces.length - 1]!}`;
          }
        } else {
          for (let i = 0; i < pieces.length - 1; i++) {
            chunks.push(pieces[i]!);
          }
          if (pieces.length > 0) {
            buf = pieces[pieces.length - 1]!;
          }
        }
        openFence = nextFence;
        continue;
      }
      buf = `${prefix}${line}`;
      openFence = nextFence;
      continue;
    }

    buf = candidate;
    openFence = nextFence;
  }

  flush(true);

  const safeChunks: string[] = [];
  for (const chunk of chunks) {
    if (!chunk) continue;
    if (chunk.length <= limit) safeChunks.push(chunk);
    else safeChunks.push(...splitOverlong(chunk, limit));
  }
  return safeChunks;
}

export function chunkDiscordTextWithMode(
  text: string,
  opts: { maxChars?: number; chunkMode?: "length" | "newline" } = {},
): string[] {
  // Both modes end at the same fence/grapheme-safe splitter; paragraph
  // preference is already applied inside `chunkTextForOutbound`.
  return chunkDiscordText(text, opts);
}
