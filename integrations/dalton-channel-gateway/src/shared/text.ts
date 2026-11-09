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

export interface CodeRegion {
  start: number;
  end: number;
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

    const findSafeBreak = (pattern: string): number => {
      let idx = window.lastIndexOf(pattern);
      while (idx > 0) {
        if (!isInsideCode(idx, codeRegions)) {
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
      breakIdx = window.lastIndexOf(" ");
      skipLen = 1;
    }

    if (breakIdx <= 0) {
      const safeSlice = truncateUtf16Safe(window, limit);
      breakIdx = safeSlice.length || limit;
      skipLen = 0;
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
  const lines = text.split("\n");
  let current = "";
  let activeFence: {
    openLine: string;
    closeLine: string;
    markerChar: string;
    markerLen: number;
  } | null = null;

  const flushCurrent = (): void => {
    if (!current) {
      return;
    }
    if (activeFence) {
      results.push(`${current}\n${activeFence.closeLine}`.trimEnd());
    } else {
      results.push(current);
    }
    current = activeFence ? activeFence.openLine : "";
  };

  for (const line of lines) {
    const fenceMatch = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    const candidate = current ? `${current}\n${line}` : line;
    const fenceClosingReserve = activeFence ? activeFence.closeLine.length + 2 : 0;

    if (candidate.length + fenceClosingReserve > limit && current.length > 0) {
      flushCurrent();
    }

    const fenceOverhead = activeFence
      ? activeFence.openLine.length + activeFence.closeLine.length + 4
      : 0;
    if (line.length + fenceOverhead > limit) {
      if (current && current !== (activeFence ? activeFence.openLine : "")) {
        flushCurrent();
      }
      const step = Math.max(1, limit - fenceOverhead);
      let rest = line;
      while (rest.length > step) {
        const piece = truncateUtf16Safe(rest, step);
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
    } else {
      current = current ? `${current}\n${line}` : line;
    }

    if (fenceMatch) {
      const marker = fenceMatch[1]!;
      const trailing = (fenceMatch[2] ?? "").trim();
      if (!activeFence) {
        const markerChar = marker[0]!;
        const markerLen = marker.length;
        activeFence = {
          openLine: `${marker}${fenceMatch[2] ?? ""}`.trimEnd(),
          closeLine: markerChar.repeat(markerLen),
          markerChar,
          markerLen,
        };
      } else if (
        trailing === "" &&
        marker[0] === activeFence.markerChar &&
        marker.length >= activeFence.markerLen
      ) {
        activeFence = null;
      }
    }
  }

  if (current) {
    if (activeFence && current !== activeFence.openLine) {
      results.push(`${current}\n${activeFence.closeLine}`);
    } else if (!activeFence) {
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
