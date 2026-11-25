/**
 * Shared text and Markdown manipulation utilities across channel adapters.
 *
 * Adapted from OpenClaw text/markdown chunking helpers:
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` (lines 15–261)
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` (lines 11–31)
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.1 (`src/shared/text.ts`).
 */

import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmTableFromMarkdown } from "mdast-util-gfm-table";
import { gfmTable } from "micromark-extension-gfm-table";

export interface MdastNode {
  type: string;
  url?: string;
  title?: string | null;
  alt?: string | null;
  identifier?: string;
  label?: string | null;
  children?: MdastNode[];
  position?: {
    start?: { offset?: number };
    end?: { offset?: number };
  };
}

const MARKDOWN_EXTENSIONS = [gfmTable()];
const MDAST_EXTENSIONS = [gfmTableFromMarkdown()];

/**
 * Shared Markdown AST parser (CommonMark + GFM tables) used by both the
 * sandbox-link rewriter and outbound channel chunkers so link/code spans match.
 */
export function parseChannelMarkdown(text: string): MdastNode {
  return fromMarkdown(text, {
    extensions: MARKDOWN_EXTENSIONS,
    mdastExtensions: MDAST_EXTENSIONS,
  }) as unknown as MdastNode;
}

export interface CodeRegion {
  start: number;
  end: number;
}

export interface AtomicLinkSpan {
  start: number;
  end: number;
  isMarkdownLink: boolean;
  label: string;
  destStart: number;
  destEnd: number;
  dest: string;
}

const BARE_HTTP_URL_RE =
  /https?:\/\/[^\s<>"'`()[\]{}，。；：、！？（）【】《》「」『』“”‘’]+/gi;

const STOCK_SANDBOX_PREFIX_RE = /^\/?(?:home\/workspace|home\/daytona)\//;

function findBracketCloseUnbounded(
  text: string,
  bracketOpen: number,
  limit: number,
): number {
  let i = bracketOpen + 1;
  let bracketDepth = 0;
  while (i < limit) {
    const ch = text[i]!;
    if (ch === "\\" && i + 1 < limit) {
      i += 2;
      continue;
    }
    if (ch === "`") {
      let run = 0;
      while (i + run < limit && text[i + run] === "`") run += 1;
      let k = i + run;
      let foundClose = -1;
      while (k < limit) {
        if (text[k] !== "`") {
          k += 1;
          continue;
        }
        let closeRun = 0;
        while (k + closeRun < limit && text[k + closeRun] === "`") {
          closeRun += 1;
        }
        if (closeRun === run) {
          foundClose = k;
          break;
        }
        k += closeRun;
      }
      i = foundClose !== -1 ? foundClose + run : i + run;
      continue;
    }
    if (ch === "[") {
      bracketDepth += 1;
      i += 1;
      continue;
    }
    if (ch === "]") {
      if (bracketDepth === 0) {
        return i;
      }
      bracketDepth -= 1;
      i += 1;
      continue;
    }
    i += 1;
  }
  return -1;
}

/**
 * Finds atomic Markdown links (`[label](url)`, `![alt](url)`, including long
 * and multiline labels) and bare `http(s)://...` URLs outside Markdown AST code
 * nodes so chunkers never split mid-link and never degrade code blocks, and
 * formatters (such as Discord `__` bold conversion) never mangle URLs.
 */
export function findAtomicLinkSpans(text: string): AtomicLinkSpan[] {
  if (!text) return [];
  if (!text.includes("[") && !text.includes("<") && !/https?:\/\//i.test(text)) {
    return [];
  }

  const tree = parseChannelMarkdown(text);
  const codeRanges: CodeRegion[] = [];
  const spans: AtomicLinkSpan[] = [];

  const visit = (node: MdastNode): void => {
    const start = node.position?.start?.offset;
    const end = node.position?.end?.offset;
    const hasOffsets = typeof start === "number" && typeof end === "number" && end > start;

    if (node.type === "code" || node.type === "inlineCode") {
      if (hasOffsets) {
        codeRanges.push({ start, end });
      }
      return;
    }

    if ((node.type === "link" || node.type === "image") && hasOffsets) {
      const isAutolink =
        node.type === "link" && text[start] === "<" && text[end - 1] === ">";
      if (isAutolink) {
        const url = typeof node.url === "string" ? node.url : text.slice(start + 1, end - 1);
        spans.push({
          start,
          end,
          isMarkdownLink: false,
          label: url,
          destStart: start,
          destEnd: end,
          dest: url,
        });
        return;
      }

      const bracketOpen = node.type === "image" && text[start] === "!" ? start + 1 : start;
      const bracketClose =
        bracketOpen < end && text[bracketOpen] === "["
          ? findBracketCloseUnbounded(text, bracketOpen, end)
          : -1;
      const label =
        bracketClose !== -1
          ? text.slice(bracketOpen + 1, bracketClose)
          : (node.alt ?? "");
      const hasInlineParens =
        bracketClose !== -1 &&
        bracketClose + 1 < end &&
        text[bracketClose + 1] === "(" &&
        text[end - 1] === ")";
      const destStart = hasInlineParens ? bracketClose + 2 : start;
      const destEnd = hasInlineParens ? end - 1 : end;
      const dest =
        typeof node.url === "string"
          ? node.url
          : text.slice(destStart, destEnd).trim();
      spans.push({
        start,
        end,
        isMarkdownLink: true,
        label,
        destStart,
        destEnd,
        dest,
      });
      return;
    }

    if (Array.isArray(node.children)) {
      for (const child of node.children) {
        visit(child);
      }
    }
  };

  visit(tree);

  // Also protect bare http(s):// URLs outside AST code nodes and Markdown links.
  for (const match of text.matchAll(BARE_HTTP_URL_RE)) {
    const start = match.index ?? 0;
    let raw = match[0]!;
    while (raw.length > 0 && /[.,;:!?]$/.test(raw)) {
      raw = raw.slice(0, -1);
    }
    const end = start + raw.length;
    if (end <= start) continue;
    const inCode = codeRanges.some((r) => start >= r.start && start < r.end);
    const inLink = spans.some((s) => start >= s.start && start < s.end);
    if (!inCode && !inLink) {
      spans.push({
        start,
        end,
        isMarkdownLink: false,
        label: raw,
        destStart: start,
        destEnd: end,
        dest: raw,
      });
    }
  }

  spans.sort((a, b) => a.start - b.start);
  return spans;
}

/**
 * Splits `text` on `\n`, keeping newlines that fall strictly inside an atomic
 * Markdown link span (e.g. multiline link labels `[line1\nline2](url)`) inside
 * the same logical unit so line-oriented chunkers never split a multiline link.
 */
export function splitLogicalLinesPreservingLinks(text: string): string[] {
  if (!text.includes("\n")) return [text];
  const spans = findAtomicLinkSpans(text);
  if (spans.length === 0) return text.split("\n");
  const lines: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "\n" && !findSpanStraddling(spans, i)) {
      lines.push(text.slice(start, i));
      start = i + 1;
    }
  }
  lines.push(text.slice(start));
  return lines;
}

/**
 * Degrades an oversized Markdown file deep link (when a single link exceeds the
 * channel's chunk limit) into plain text + workspace-relative path hint.
 */
export function degradeOversizedLink(span: AtomicLinkSpan): string | null {
  if (!span.isMarkdownLink) {
    return null;
  }
  const urlStr = stripTitleFromUrl(span.dest).replace(/^<|>$/g, "");
  try {
    const parsed = new URL(urlStr);
    const fileParam = parsed.searchParams.get("file");
    if (fileParam) {
      const displayPath = fileParam.replace(STOCK_SANDBOX_PREFIX_RE, "");
      const hint = `（在网页工作区查看：${displayPath}）`;
      return span.label ? `${span.label}${hint}` : hint;
    }
  } catch {
    // Non-URL link destination: fall back to label below
  }
  return span.label || null;
}

/**
 * Finds the atomic link span strictly straddling `cutIndex` (`span.start < cutIndex < span.end`),
 * or `null` if `cutIndex` is at a safe boundary.
 */
export function findSpanStraddling(
  spans: readonly AtomicLinkSpan[],
  cutIndex: number,
): AtomicLinkSpan | null {
  for (const span of spans) {
    if (span.start >= cutIndex) break;
    if (span.start < cutIndex && cutIndex < span.end) {
      return span;
    }
  }
  return null;
}

export type ChunkMode = "length" | "newline";
export type MarkdownTableMode = "off" | "code" | "bullets";

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Truncates `value` to at most `maxChars` UTF-16 code units without splitting
 * a surrogate pair at the boundary.
 */
export function truncateUtf16Safe(value: string, maxChars: number): string {
  const limit = Math.max(0, Math.floor(maxChars));
  if (value.length <= limit) {
    return value;
  }
  if (limit === 0) {
    return "";
  }
  const code = value.charCodeAt(limit - 1);
  if (code >= 0xd800 && code <= 0xdbff) {
    return value.slice(0, limit - 1);
  }
  return value.slice(0, limit);
}

/**
 * Finds fenced (` ``` ` / `~~~`) and inline (`` ` ``) code regions in Markdown text.
 */
export function findCodeRegions(text: string): CodeRegion[] {
  const regions: CodeRegion[] = [];
  const fencedRe = /(^|\n)(```|~~~)[^\n]*\n[\s\S]*?(?:\n\2(?:\n|$)|$)/g;
  for (const match of text.matchAll(fencedRe)) {
    const start = (match.index ?? 0) + match[1].length;
    regions.push({
      start,
      end: start + match[0].length - match[1].length,
    });
  }

  const inlineRe = /`+[^`]+`+/g;
  for (const match of text.matchAll(inlineRe)) {
    const start = match.index ?? 0;
    const end = start + match[0].length;
    const insideFenced = regions.some((r) => start >= r.start && end <= r.end);
    if (!insideFenced) {
      regions.push({ start, end });
    }
  }

  regions.sort((a, b) => a.start - b.start);
  return regions;
}

export function isInsideCode(
  pos: number,
  regions: readonly CodeRegion[],
): boolean {
  return regions.some((r) => pos >= r.start && pos < r.end);
}

export function stripTitleFromUrl(raw: string): string {
  const trimmed = raw.trim();
  const match = trimmed.match(/^(\S+?)(?:\s+["'(][\s\S]*["')])?$/);
  return match ? match[1] : trimmed;
}

const MARKDOWN_LINK_RE = /!?\[([^\]]*)\]\(([^)]+)\)/g;

export function protectMarkdownLinks(text: string): {
  text: string;
  restore: (input: string) => string;
  links: Array<{ placeholder: string; label: string; url: string; raw: string }>;
} {
  const codeRegions = findCodeRegions(text);
  const links: Array<{
    placeholder: string;
    label: string;
    url: string;
    raw: string;
  }> = [];
  let counter = 0;

  const replaced = text.replace(
    MARKDOWN_LINK_RE,
    (raw, label: string, urlPart: string, offset: number) => {
      if (isInsideCode(offset, codeRegions)) {
        return raw;
      }
      const placeholder = `\u0000MDLNK_${counter++}\u0000`;
      links.push({
        placeholder,
        label,
        url: stripTitleFromUrl(urlPart),
        raw,
      });
      return placeholder;
    },
  );

  const restore = (input: string): string => {
    let result = input;
    for (const item of links) {
      result = result.replaceAll(item.placeholder, item.raw);
    }
    return result;
  };

  return { text: replaced, restore, links };
}

/**
 * Splits text into chunks up to `maxChars`, preferring paragraph (`\n\n`),
 * line (`\n`), and whitespace boundaries outside fenced code blocks.
 */
export function chunkByParagraph(
  text: string,
  maxChars: number,
  options: { preserveInternalNewlines?: boolean } = {},
): string[] {
  const limit = Math.max(1, Math.floor(maxChars));
  if (!text) {
    return [];
  }
  if (text.length <= limit) {
    return [text];
  }

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > limit) {
    const window = remaining.slice(0, limit);
    const codeRegions = findCodeRegions(remaining);
    const linkSpans = findAtomicLinkSpans(remaining);

    const findSafeBreak = (pattern: string): number => {
      let idx = window.lastIndexOf(pattern);
      while (idx > 0) {
        if (
          !isInsideCode(idx, codeRegions) &&
          !findSpanStraddling(linkSpans, idx)
        ) {
          return idx;
        }
        idx = window.lastIndexOf(pattern, idx - 1);
      }
      return -1;
    };

    let breakIdx = findSafeBreak("\n\n");
    let skipLen = 2;

    if (breakIdx <= 0) {
      breakIdx = findSafeBreak("\n");
      skipLen = 1;
    }

    if (breakIdx <= 0) {
      breakIdx = findSafeBreak(" ");
      skipLen = 1;
    }

    if (breakIdx <= 0) {
      const safeSlice = truncateUtf16Safe(window, limit);
      breakIdx = safeSlice.length || limit;
      skipLen = 0;
    }

    const straddling = findSpanStraddling(linkSpans, breakIdx);
    if (straddling) {
      if (straddling.start > 0) {
        breakIdx = straddling.start;
        skipLen = 0;
      } else {
        const degraded = degradeOversizedLink(straddling);
        if (degraded !== null && degraded.length < straddling.end) {
          remaining = degraded + remaining.slice(straddling.end);
          continue;
        }
      }
    }

    const head = remaining.slice(0, breakIdx);
    const trimmedHead = options.preserveInternalNewlines ? head : head.trimEnd();
    if (trimmedHead.length > 0) {
      chunks.push(trimmedHead);
    }
    remaining = remaining.slice(breakIdx + skipLen);
    if (!options.preserveInternalNewlines) {
      remaining = remaining.replace(/^\s+/, "");
    }
  }

  if (remaining.length > 0) {
    chunks.push(remaining);
  }
  return chunks;
}

const MAX_FENCE_INFO_CHARS = 64;

/**
 * Splits Markdown text while keeping fenced code blocks balanced across chunks
 * when a single code block exceeds `maxChars`.
 */
export function chunkTextForOutbound(text: string, maxChars: number): string[] {
  const limit = Math.max(1, Math.floor(maxChars));
  if (!text) {
    return [];
  }
  if (text.length <= limit) {
    return [text];
  }

  const results: string[] = [];
  const lines = splitLogicalLinesPreservingLinks(text);
  let current = "";
  let activeFence: {
    openLine: string;
    closeLine: string;
    markerChar: string;
    markerLen: number;
  } | null = null;
  let reopenedEmptyFence = false;

  const flushCurrent = (): void => {
    if (!current) {
      return;
    }
    if (activeFence) {
      if (reopenedEmptyFence && current === activeFence.openLine) {
        return;
      }
      results.push(`${current}\n${activeFence.closeLine}`.trimEnd());
      current = activeFence.openLine;
      reopenedEmptyFence = true;
    } else {
      results.push(current);
      current = "";
      reopenedEmptyFence = false;
    }
  };

  const splitOverlongLine = (lineToSplit: string): void => {
    const fenceOverhead = activeFence
      ? activeFence.openLine.length + activeFence.closeLine.length + 4
      : 0;
    const step = Math.max(1, limit - fenceOverhead);
    let rest = lineToSplit;
    while (rest.length > step) {
      const linkSpans = activeFence ? [] : findAtomicLinkSpans(rest);
      let piece = truncateUtf16Safe(rest, step);
      if (!piece) {
        piece = rest.slice(0, Math.min(rest.length, 2));
      }
      if (!activeFence) {
        const straddling = findSpanStraddling(linkSpans, piece.length);
        if (straddling) {
          if (straddling.start > 0) {
            piece = rest.slice(0, straddling.start);
          } else {
            const degraded = degradeOversizedLink(straddling);
            if (degraded !== null && degraded.length < straddling.end) {
              rest = degraded + rest.slice(straddling.end);
              continue;
            }
          }
        }
      }
      if (activeFence) {
        results.push(
          `${activeFence.openLine}\n${piece}\n${activeFence.closeLine}`,
        );
      } else {
        results.push(piece);
      }
      rest = rest.slice(piece.length);
    }
    current = activeFence ? `${activeFence.openLine}\n${rest}` : rest;
    reopenedEmptyFence = false;
  };

  for (const line of lines) {
    const fenceMatch = line.match(/^\s*(`{3,}|~{3,})(.*)$/);

    if (activeFence && fenceMatch) {
      const marker = fenceMatch[1]!;
      const trailing = (fenceMatch[2] ?? "").trim();
      if (
        trailing === "" &&
        marker[0] === activeFence.markerChar &&
        marker.length >= activeFence.markerLen
      ) {
        if (reopenedEmptyFence && current === activeFence.openLine) {
          current = "";
          activeFence = null;
          reopenedEmptyFence = false;
          continue;
        }
        const candidate = current ? `${current}\n${line}` : line;
        if (candidate.length <= limit) {
          current = candidate;
        } else {
          flushCurrent();
          current = "";
        }
        activeFence = null;
        reopenedEmptyFence = false;
        continue;
      }
    }

    if (!activeFence && fenceMatch) {
      const marker = fenceMatch[1]!;
      const markerChar = marker[0]!;
      const markerLen = marker.length;
      const closeLine = markerChar.repeat(markerLen);
      const canBalance = closeLine.length * 2 + 4 <= limit;
      const rawInfo = (fenceMatch[2] ?? "").trimEnd();
      const candidateOpenLine = `${marker}${rawInfo}`;
      const openLine =
        rawInfo.length <= MAX_FENCE_INFO_CHARS &&
        candidateOpenLine.length + closeLine.length + 4 <= limit
          ? candidateOpenLine
          : closeLine;

      if (line.length > limit) {
        if (current) {
          flushCurrent();
        }
        if (canBalance) {
          activeFence = {
            openLine: closeLine,
            closeLine,
            markerChar,
            markerLen,
          };
          const markerEnd = line.indexOf(marker) + marker.length;
          splitOverlongLine(line.slice(markerEnd));
        } else {
          splitOverlongLine(line);
        }
        continue;
      }

      const candidate = current ? `${current}\n${line}` : line;
      const reserve = canBalance ? closeLine.length + 2 : 0;
      if (candidate.length + reserve > limit && current.length > 0) {
        flushCurrent();
      }
      current = current ? `${current}\n${line}` : line;
      if (canBalance) {
        activeFence = {
          openLine,
          closeLine,
          markerChar,
          markerLen,
        };
        reopenedEmptyFence = false;
      }
      continue;
    }

    const candidate = current ? `${current}\n${line}` : line;
    const fenceClosingReserve = activeFence
      ? activeFence.closeLine.length + 2
      : 0;

    if (candidate.length + fenceClosingReserve > limit && current.length > 0) {
      flushCurrent();
    }

    const fenceOverhead = activeFence
      ? activeFence.openLine.length + activeFence.closeLine.length + 4
      : 0;
    if (line.length + fenceOverhead > limit) {
      if (current && !(reopenedEmptyFence && activeFence && current === activeFence.openLine)) {
        flushCurrent();
      }
      splitOverlongLine(line);
    } else {
      current = current ? `${current}\n${line}` : line;
      reopenedEmptyFence = false;
    }
  }

  if (current) {
    if (activeFence) {
      if (!(reopenedEmptyFence && current === activeFence.openLine)) {
        results.push(`${current}\n${activeFence.closeLine}`);
      }
    } else {
      results.push(current);
    }
  }

  return results.filter((chunk) => chunk.length > 0);
}

export function chunkMarkdownTextWithMode(
  text: string,
  limit: number,
  mode: ChunkMode = "length",
): string[] {
  if (!text) {
    return [];
  }
  if (limit <= 0 || text.length <= limit) {
    return [text];
  }
  if (mode === "newline") {
    const paragraphChunks = chunkByParagraph(text, limit);
    return paragraphChunks.flatMap((chunk) =>
      chunk.length <= limit ? [chunk] : chunkTextForOutbound(chunk, limit),
    );
  }
  return chunkTextForOutbound(text, limit);
}

/**
 * Converts GFM markdown tables into fixed-width code blocks (`mode = "code"`)
 * or bullet lists (`mode = "bullets"`) for channels that do not render native
 * Markdown tables (e.g. Discord, Weixin, or Feishu post fallback).
 */
export function convertMarkdownTables(
  text: string,
  mode: MarkdownTableMode = "code",
): string {
  if (!text || mode === "off" || !text.includes("|")) {
    return text;
  }

  const lines = text.split("\n");
  const out: string[] = [];
  let inFence = false;
  let i = 0;

  const isTableRow = (line: string): boolean => {
    const trimmed = line.trim();
    return trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 2;
  };

  const isSeparatorRow = (line: string): boolean => {
    const trimmed = line.trim();
    return /^\|(?:\s*:?-{2,}:?\s*\|)+$/.test(trimmed);
  };

  const parseCells = (line: string): string[] =>
    line
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((cell) => cell.trim());

  while (i < lines.length) {
    const line = lines[i]!;
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      out.push(line);
      i += 1;
      continue;
    }

    if (
      !inFence &&
      i + 1 < lines.length &&
      isTableRow(line) &&
      isSeparatorRow(lines[i + 1]!)
    ) {
      const headers = parseCells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && isTableRow(lines[i]!)) {
        rows.push(parseCells(lines[i]!));
        i += 1;
      }

      if (mode === "bullets") {
        for (const row of rows) {
          const pairs = headers
            .map((h, idx) => `${h}: ${row[idx] ?? ""}`)
            .join(" | ");
          out.push(`- ${pairs}`);
        }
      } else {
        const colCount = Math.max(
          headers.length,
          ...rows.map((r) => r.length),
        );
        const widths = Array.from({ length: colCount }, (_, colIdx) =>
          Math.max(
            (headers[colIdx] ?? "").length,
            ...rows.map((r) => (r[colIdx] ?? "").length),
            1,
          ),
        );
        const formatRow = (cells: string[]): string =>
          `| ${widths
            .map((w, idx) => (cells[idx] ?? "").padEnd(w, " "))
            .join(" | ")} |`;
        const divider = `+-${widths.map((w) => "-".repeat(w)).join("-+-")}-+`;
        out.push("```");
        out.push(divider);
        out.push(formatRow(headers));
        out.push(divider);
        for (const row of rows) {
          out.push(formatRow(row));
        }
        out.push(divider);
        out.push("```");
      }
      continue;
    }

    out.push(line);
    i += 1;
  }

  return out.join("\n");
}
