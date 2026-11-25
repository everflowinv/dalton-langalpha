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
  degradeOversizedLink,
  findAtomicLinkSpans,
  findCodeRegions,
  findSpanStraddling,
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
 * Converts `__bold__` to `**bold**` outside code regions and link targets.
 * Discord renders `__` as underline, so this preserves the author's bold intent
 * without mangling double underscores in URLs or file paths (e.g. `a__b__c.md`).
 */
export function normalizeDiscordBold(markdown: string): string {
  if (!markdown.includes("__")) return markdown;
  const regions = findCodeRegions(markdown);
  const linkSpans = findAtomicLinkSpans(markdown);
  const isProtected = (pos: number): boolean =>
    regions.some((r) => pos >= r.start && pos < r.end) ||
    linkSpans.some((s) => pos >= s.destStart && pos < s.destEnd);
  let out = "";
  let i = 0;
  while (i < markdown.length) {
    if (!isProtected(i) && markdown.startsWith("__", i)) {
      const close = markdown.indexOf("__", i + 2);
      if (close > i + 2 && !isProtected(close)) {
        const inner = markdown.slice(i + 2, close);
        const crossesProtected =
          regions.some((r) => r.start < close && r.end > i + 2) ||
          linkSpans.some((s) => s.destStart < close && s.destEnd > i + 2);
        if (!crossesProtected && inner.trim().length > 0 && !inner.includes("\n\n")) {
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
  let rest = text;
  while (rest.length > 0) {
    if (rest.length <= limit) {
      pieces.push(rest);
      break;
    }
    const linkSpans = findAtomicLinkSpans(rest);
    let end = findGraphemeChunkEnd(rest, 0, limit);
    if (end <= 0) end = 1;
    const straddling = findSpanStraddling(linkSpans, end);
    if (straddling) {
      if (straddling.start > 0) {
        end = straddling.start;
      } else {
        const degraded = degradeOversizedLink(straddling);
        if (degraded !== null && degraded.length < straddling.end) {
          rest = degraded + rest.slice(straddling.end);
          continue;
        }
      }
    }
    const piece = rest.slice(0, end);
    if (piece) pieces.push(piece);
    rest = rest.slice(piece.length || 1);
  }
  return pieces;
}

function splitOverlongGraphemeLine(line: string, step: number): string[] {
  const out: string[] = [];
  let rest = line;
  while (rest.length > 0) {
    if (rest.length <= step) {
      out.push(rest);
      break;
    }
    const linkSpans = findAtomicLinkSpans(rest);
    const graphemeEnd = findGraphemeChunkEnd(rest, 0, step);
    const window = rest.slice(0, graphemeEnd);
    let wsIdx = Math.max(window.lastIndexOf(" "), window.lastIndexOf("\t"));
    while (wsIdx > Math.floor(step * 0.5) && findSpanStraddling(linkSpans, wsIdx)) {
      wsIdx = Math.max(
        window.lastIndexOf(" ", wsIdx - 1),
        window.lastIndexOf("\t", wsIdx - 1),
      );
    }
    if (wsIdx > Math.floor(step * 0.5)) {
      out.push(window.slice(0, wsIdx));
      let nextStart = wsIdx + 1;
      while (nextStart < rest.length && (rest[nextStart] === " " || rest[nextStart] === "\t")) {
        nextStart += 1;
      }
      rest = rest.slice(nextStart);
      continue;
    }
    let end = graphemeEnd > 0 ? graphemeEnd : 1;
    const straddling = findSpanStraddling(linkSpans, end);
    if (straddling) {
      if (straddling.start > 0) {
        end = straddling.start;
      } else {
        const degraded = degradeOversizedLink(straddling);
        if (degraded !== null && degraded.length < straddling.end) {
          rest = degraded + rest.slice(straddling.end);
          continue;
        }
      }
    }
    out.push(rest.slice(0, end));
    rest = rest.slice(end);
  }
  return out;
}

interface OpenDiscordFence {
  openLine: string;
  closeLine: string;
  markerChar: string;
  markerLen: number;
}

function parseFenceMarker(line: string): { marker: string; info: string } | null {
  const match = /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
  if (!match) return null;
  return { marker: match[1]!, info: match[2] ?? "" };
}

function openFenceFromLine(line: string, limit: number): OpenDiscordFence | null {
  const parsed = parseFenceMarker(line);
  if (!parsed) return null;
  const markerChar = parsed.marker[0]!;
  const markerLen = parsed.marker.length;
  const closeLine = markerChar.repeat(markerLen);
  const trimmed = line.trimEnd();
  const openLine = trimmed.length + 1 + closeLine.length <= limit ? trimmed : closeLine;
  return { openLine, closeLine, markerChar, markerLen };
}

/** A close must use the same character, be at least as long, and carry no info string. */
function isSuitableFenceClose(line: string, open: OpenDiscordFence): boolean {
  const parsed = parseFenceMarker(line);
  if (!parsed || parsed.info.trim() !== "") return false;
  return parsed.marker[0] === open.markerChar && parsed.marker.length >= open.markerLen;
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
  let openFence: OpenDiscordFence | null = null;

  const closeReserve = (fence: OpenDiscordFence | null): number =>
    fence ? fence.closeLine.length + 1 : 0;

  const flush = (isFinal = false): void => {
    if (buf.length === 0) return;
    if (openFence !== null) {
      if (!isFinal && buf === openFence.openLine) {
        buf = "";
        return;
      }
      if (!isFinal && buf.endsWith(`\n${openFence.openLine}`)) {
        const prose = buf.slice(0, buf.length - openFence.openLine.length - 1);
        if (prose.length > 0) chunks.push(prose);
        buf = "";
        return;
      }
      chunks.push(`${buf}\n${openFence.closeLine}`);
    } else {
      chunks.push(buf);
    }
    buf = "";
  };

  for (const line of lines) {
    let nextFence: OpenDiscordFence | null = openFence;
    if (openFence && isSuitableFenceClose(line, openFence)) {
      nextFence = null;
    } else if (!openFence) {
      const opened = openFenceFromLine(line, limit);
      if (opened) nextFence = opened;
    }
    const reserve = closeReserve(nextFence);
    const candidate = buf.length === 0 ? line : `${buf}\n${line}`;

    if (candidate.length + reserve > limit) {
      if (buf.length > 0) flush(false);
      const prefix = openFence !== null ? `${openFence.openLine}\n` : "";
      const lineReserve = closeReserve(openFence);
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
          const closer = `\n${openFence.closeLine}`;
          for (let i = 0; i < pieces.length - 1; i++) {
            chunks.push(`${prefix}${pieces[i]!}${closer}`);
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
