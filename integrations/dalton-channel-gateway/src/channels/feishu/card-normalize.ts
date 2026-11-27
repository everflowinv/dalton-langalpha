/**
 * Pure Schema 2.0 Feishu card normalizer and minimal markdown-only fallback builder.
 *
 * Normalizes common LLM-authored card shape mistakes before outbound delivery
 * without mutating the caller's object or guessing unknown element schemas.
 */

import { rewriteSandboxLinks } from "../../shared/sandbox-links.js";
import { parseChannelMarkdown, type MdastNode } from "../../shared/text.js";
import {
  escapeFeishuCardMarkdownText,
  FEISHU_CARD_MAX_BYTES,
  type FeishuSchema2Card,
} from "./format.js";

const TEXT_ELEMENT_TAGS = new Set(["text", "plain_text", "lark_md"]);
const NESTED_ELEMENTS_CONTAINER_TAGS = new Set([
  "column",
  "collapsible_panel",
  "form",
  "interactive_container",
]);

const FALLBACK_EMPTY_TEXT = "（卡片内容无法以原格式展示）";
const FALLBACK_TRUNCATED_SUFFIX = "\n\n...(内容过长已截断)";

const QUANTITY_CURR_PREFIX =
  "(?:US\\$|HK\\$|CN\\$|NT\\$|A\\$|C\\$|S\\$|[$€£¥￥]|(?:USD|HKD|CNY|RMB|EUR|GBP|JPY|TWD|KRW) )";
const QUANTITY_PREFIX = `(?:[+\\-−]?${QUANTITY_CURR_PREFIX}|${QUANTITY_CURR_PREFIX}[+\\-−]|[+\\-−])?`;
const QUANTITY_NUM = "(?:\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.\\d+)?";
const QUANTITY_SUFFIX =
  "(?:%|％| ?(?:bps|bp|ppts|ppt|pp)|[xX]|(?:[KkMmBbTt]|[mM][nN]|[bB][nN]|[tT][nN])(?: ?(?:USD|HKD|CNY|RMB|EUR|GBP|JPY|TWD|KRW))?| ?(?:USD|HKD|CNY|RMB|EUR|GBP|JPY|TWD|KRW)| ?(?:千|万|百万|千万|亿|十亿|百亿|千亿|万亿)?(?:美元|港元|港币|人民币|欧元|日元|英镑|美金|元|倍|个百分点|百分点)| ?(?:千|万|百万|千万|亿|十亿|百亿|千亿|万亿))?";
const QUANTITY_ATOM = `${QUANTITY_PREFIX}${QUANTITY_NUM}${QUANTITY_SUFFIX}`;
const RANGE_SEP = "(?: ?(?:[-–—~～]|至|到) ?| to )";

const INLINE_CODE_QUANTITY_RE = new RegExp(
  `^${QUANTITY_ATOM}(?:${RANGE_SEP}${QUANTITY_ATOM})?$`,
);
const INLINE_CODE_TICKER_RE =
  /^(?:[A-Z]{1,6}|[A-Z]{1,5}\.[A-Z]|\d{3,6}\.(?:HK|SH|SS|SZ|BJ|TW|TWO|KS|KQ|T|JP|SI|KL|BK|AX|NS|BO|US)|[A-Z][A-Z0-9]{0,5}\.(?:HK|US|N|O|OQ|L|PA|DE|AS|SW|MI|MC|ST|CO|HE|OL|VI|BR|LS|TO|AX|NZ|SI|NS|BO|KL|BK|JK|SA|MX|KS|KQ|TW|TWO|T|JP))$/;
const PERIOD_ATOM =
  "(?:(?:19|20)\\d{2} ?(?:Q[1-4]|H[12]|M(?:0?[1-9]|1[0-2])|FY|CY|YTD)[EAPF]?|[1-4]Q(?:\\d{2}|(?:19|20)\\d{2})[EAPF]?|[12]H(?:\\d{2}|(?:19|20)\\d{2})[EAPF]?|(?:FY|CY)(?: ?(?:'?\\d{2}|(?:19|20)\\d{2})(?:Q[1-4]|H[12])?)[EAPF]?|Q[1-4](?: ?(?:'?\\d{2}|(?:19|20)\\d{2}))?[EAPF]?|H[12](?: ?(?:'?\\d{2}|(?:19|20)\\d{2}))?[EAPF]?|(?:19|20)\\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\\d|3[01]))";
const INLINE_CODE_PERIOD_RE = new RegExp(
  `^${PERIOD_ATOM}(?:${RANGE_SEP}${PERIOD_ATOM})?$`,
);

const FENCE_OPEN_RE = /^[ ]{0,3}(`{3,}|~{3,})(.*)$/;
const FENCE_CLOSE_BACKTICK_RE = /^[ ]{0,3}(`{3,})[ \t]*$/;
const FENCE_CLOSE_TILDE_RE = /^[ ]{0,3}(~{3,})[ \t]*$/;
const RANGE_TILDE_RE = / ?[~～] ?/g;

function shouldStripInlineCodeBackticks(inner: string): boolean {
  if (!inner || inner !== inner.trim() || inner.includes("$")) {
    return false;
  }
  return (
    INLINE_CODE_QUANTITY_RE.test(inner) ||
    INLINE_CODE_TICKER_RE.test(inner) ||
    INLINE_CODE_PERIOD_RE.test(inner)
  );
}

function stripInlineCodeOnLine(line: string): string {
  if (!line.includes("`")) {
    return line;
  }
  let out = "";
  let i = 0;
  while (i < line.length) {
    const tickPos = line.indexOf("`", i);
    if (tickPos === -1) {
      out += line.slice(i);
      break;
    }
    out += line.slice(i, tickPos);

    let runEnd = tickPos;
    while (runEnd < line.length && line[runEnd] === "`") {
      runEnd += 1;
    }
    const runLen = runEnd - tickPos;

    let backslashes = 0;
    let bsPos = tickPos - 1;
    while (bsPos >= 0 && line[bsPos] === "\\") {
      backslashes += 1;
      bsPos -= 1;
    }
    if (backslashes % 2 === 1) {
      out += line.slice(tickPos, runEnd);
      i = runEnd;
      continue;
    }

    let searchPos = runEnd;
    let closeStart = -1;
    while (searchPos < line.length) {
      const nextTick = line.indexOf("`", searchPos);
      if (nextTick === -1) {
        break;
      }
      let nextEnd = nextTick;
      while (nextEnd < line.length && line[nextEnd] === "`") {
        nextEnd += 1;
      }
      if (nextEnd - nextTick === runLen) {
        closeStart = nextTick;
        break;
      }
      searchPos = nextEnd;
    }

    if (closeStart === -1) {
      out += line.slice(tickPos, runEnd);
      i = runEnd;
      continue;
    }

    const closeEnd = closeStart + runLen;
    const inner = line.slice(runEnd, closeStart);
    if (runLen === 1 && shouldStripInlineCodeBackticks(inner)) {
      out += inner.replace(RANGE_TILDE_RE, "至");
    } else {
      out += line.slice(tickPos, closeEnd);
    }
    i = closeEnd;
  }
  return out;
}

/**
 * Removes decorative single-backtick inline code wrapping around financial
 * numbers/amounts/percentages/multiples/ranges, stock tickers, and reporting
 * periods inside Feishu card markdown content so Feishu does not render them
 * as gray code badges. Leaves code blocks (fenced) and technical inline code
 * (paths, commands, snake_case identifiers, etc.) untouched.
 */
export function normalizeFeishuMarkdownInlineCode(content: string): string {
  if (!content.includes("`")) {
    return content;
  }
  const parts = content.split(/(\r?\n)/);
  let inFence: { char: "`" | "~"; len: number } | null = null;
  for (let idx = 0; idx < parts.length; idx += 2) {
    const line = parts[idx];
    if (inFence !== null) {
      const closeRe =
        inFence.char === "`" ? FENCE_CLOSE_BACKTICK_RE : FENCE_CLOSE_TILDE_RE;
      const closeMatch = closeRe.exec(line);
      if (closeMatch && closeMatch[1].length >= inFence.len) {
        inFence = null;
      }
      continue;
    }
    const openMatch = FENCE_OPEN_RE.exec(line);
    if (openMatch) {
      const marker = openMatch[1];
      const info = openMatch[2];
      const fenceChar = marker[0] as "`" | "~";
      if (fenceChar === "~" || !info.includes("`")) {
        inFence = { char: fenceChar, len: marker.length };
        continue;
      }
    }
    parts[idx] = stripInlineCodeOnLine(line);
  }
  return parts.join("");
}

export interface FeishuCardNormalizeResult {
  card: Record<string, unknown>;
  normalizedPaths: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readTagLower(node: Record<string, unknown>): string {
  return typeof node.tag === "string" ? node.tag.trim().toLowerCase() : "";
}

function extractTextElementContent(element: Record<string, unknown>): string {
  if (typeof element.content === "string") {
    return element.content;
  }
  if (isRecord(element.text) && typeof element.text.content === "string") {
    return element.text.content;
  }
  if (typeof element.text === "string") {
    return element.text;
  }
  return "";
}

function normalizeHeader(
  header: Record<string, unknown>,
  normalizedPaths: string[],
): Record<string, unknown> {
  const out: Record<string, unknown> = structuredClone(header);

  if (typeof out.title === "string") {
    out.title = { tag: "plain_text", content: out.title };
    normalizedPaths.push("header.title");
  }

  if (typeof out.subtitle === "string") {
    out.subtitle = { tag: "plain_text", content: out.subtitle };
    normalizedPaths.push("header.subtitle");
  }

  if (Array.isArray(out.text_tag_list)) {
    out.text_tag_list = out.text_tag_list.map((entry, idx) => {
      if (typeof entry === "string") {
        normalizedPaths.push(`header.text_tag_list[${idx}]`);
        return {
          tag: "text_tag",
          text: { tag: "plain_text", content: entry },
        };
      }
      if (!isRecord(entry)) {
        return entry;
      }
      const nextEntry: Record<string, unknown> = { ...entry };
      if (typeof nextEntry.tag !== "string" || nextEntry.tag.trim() === "") {
        nextEntry.tag = "text_tag";
        normalizedPaths.push(`header.text_tag_list[${idx}].tag`);
      }
      if (typeof nextEntry.text === "string") {
        nextEntry.text = { tag: "plain_text", content: nextEntry.text };
        normalizedPaths.push(`header.text_tag_list[${idx}].text`);
      }
      return nextEntry;
    });
  }

  return out;
}

function normalizeElementList(
  elements: unknown[],
  pathPrefix: string,
  normalizedPaths: string[],
): unknown[] {
  return elements.map((rawEl, idx) => {
    const elPath = `${pathPrefix}[${idx}]`;
    if (!isRecord(rawEl)) {
      return rawEl;
    }

    const tagLower = readTagLower(rawEl);
    if (TEXT_ELEMENT_TAGS.has(tagLower)) {
      normalizedPaths.push(elPath);
      return {
        tag: "markdown",
        content: normalizeFeishuMarkdownInlineCode(
          extractTextElementContent(rawEl),
        ),
      };
    }

    const cloned: Record<string, unknown> = structuredClone(rawEl);
    if (tagLower === "markdown" && typeof cloned.content === "string") {
      const nextContent = normalizeFeishuMarkdownInlineCode(cloned.content);
      if (nextContent !== cloned.content) {
        cloned.content = nextContent;
        normalizedPaths.push(`${elPath}.content`);
      }
    } else if (tagLower === "column_set" && Array.isArray(cloned.columns)) {
      cloned.columns = cloned.columns.map((col, colIdx) => {
        if (!isRecord(col) || !Array.isArray(col.elements)) {
          return col;
        }
        return {
          ...col,
          elements: normalizeElementList(
            col.elements,
            `${elPath}.columns[${colIdx}].elements`,
            normalizedPaths,
          ),
        };
      });
    } else if (
      NESTED_ELEMENTS_CONTAINER_TAGS.has(tagLower) &&
      Array.isArray(cloned.elements)
    ) {
      cloned.elements = normalizeElementList(
        cloned.elements,
        `${elPath}.elements`,
        normalizedPaths,
      );
    }

    return cloned;
  });
}

/**
 * Pure Schema 2.0 card normalizer.
 *
 * - Fills missing `tag: "text_tag"` in `header.text_tag_list[*]` and wraps
 *   string `text` (or string list entries) as `{ tag: "plain_text", content }`.
 * - Wraps string `header.title` / `header.subtitle` as `{ tag: "plain_text", content }`.
 * - Converts body elements with `tag` in `text` / `plain_text` / `lark_md`
 *   into `{ tag: "markdown", content }`.
 * - Leaves unknown elements untouched and records only normalized field paths.
 */
export function normalizeFeishuCard(
  card: Record<string, unknown>,
): FeishuCardNormalizeResult {
  const normalizedPaths: string[] = [];
  const out: Record<string, unknown> = structuredClone(card);

  if (isRecord(out.header)) {
    out.header = normalizeHeader(out.header, normalizedPaths);
  }

  if (isRecord(out.body) && Array.isArray(out.body.elements)) {
    out.body = {
      ...out.body,
      elements: normalizeElementList(
        out.body.elements,
        "body.elements",
        normalizedPaths,
      ),
    };
  }

  if (Array.isArray(out.elements)) {
    out.elements = normalizeElementList(
      out.elements,
      "elements",
      normalizedPaths,
    );
  }

  return { card: out, normalizedPaths };
}

function readHeaderText(value: unknown): string {
  if (typeof value === "string") {
    return value.trim();
  }
  if (isRecord(value) && typeof value.content === "string") {
    return value.content.trim();
  }
  return "";
}

function isLocalHostname(rawHostname: string): boolean {
  const host = rawHostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.+$/, "");
  if (!host) return true;
  return (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "127.0.0.1" ||
    host === "::1" ||
    host === "0.0.0.0" ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)
  );
}

function toVerifiedPublicHttpUrl(rawUrl: unknown): string | null {
  if (typeof rawUrl !== "string") {
    return null;
  }
  const trimmed = rawUrl.trim();
  if (!trimmed || !/^https?:\/\//i.test(trimmed)) {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return null;
  }
  if (isLocalHostname(parsed.hostname)) {
    return null;
  }
  return parsed.href.replace(/\(/g, "%28").replace(/\)/g, "%29");
}

function readFirstUrlField(node: Record<string, unknown>): string | null {
  for (const key of ["default_url", "url", "pc_url", "ios_url", "android_url"]) {
    const verified = toVerifiedPublicHttpUrl(node[key]);
    if (verified) {
      return verified;
    }
  }
  return null;
}

function extractButtonVerifiedOpenUrl(button: Record<string, unknown>): string | null {
  if (Array.isArray(button.behaviors)) {
    for (const behavior of button.behaviors) {
      if (!isRecord(behavior)) {
        continue;
      }
      const behaviorType =
        typeof behavior.type === "string" ? behavior.type.trim().toLowerCase() : "";
      if (behaviorType === "open_url") {
        const verified = readFirstUrlField(behavior);
        if (verified) {
          return verified;
        }
      }
    }
  }
  if (isRecord(button.multi_url)) {
    const verified = readFirstUrlField(button.multi_url);
    if (verified) {
      return verified;
    }
  }
  return toVerifiedPublicHttpUrl(button.url);
}

/**
 * Escapes Markdown syntax in text sourced from Feishu `plain_text` fields so
 * it cannot inject links, emphasis, code spans, or raw Feishu tags when
 * concatenated into a minimal markdown-only fallback card, while keeping
 * URLs/paths recognizable to `rewriteSandboxLinks`.
 */
export function escapePlainTextForFeishuMarkdown(text: string): string {
  if (!text) {
    return text;
  }
  let out = text
    .replace(/\\/g, "\\\\")
    .replace(/([`*~[\]()|!])/g, "\\$1")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/(?<![A-Za-z0-9/])_|_(?![A-Za-z0-9/])/g, "\\_");
  out = out
    .replace(/^([ \t]*)(#{1,6})(?=[ \t]|$)/gm, "$1\\$2")
    .replace(/^([ \t]*)([-+])(?=[ \t])/gm, "$1\\$2")
    .replace(/^([ \t]*\d+)\.(?=[ \t])/gm, "$1\\.");
  return out;
}

function isMarkdownTextTag(node: Record<string, unknown>): boolean {
  const tagLower = readTagLower(node);
  return tagLower === "markdown" || tagLower === "lark_md";
}

function collectFallbackBlocksFromElements(
  elements: unknown[],
  blocks: string[],
): void {
  for (const rawEl of elements) {
    if (!isRecord(rawEl)) {
      continue;
    }
    const tagLower = readTagLower(rawEl);

    if (tagLower === "markdown" || tagLower === "lark_md") {
      const text = normalizeFeishuMarkdownInlineCode(
        extractTextElementContent(rawEl).trim(),
      );
      if (text) {
        blocks.push(text);
      }
      continue;
    }

    if (tagLower === "plain_text" || tagLower === "text") {
      const text = extractTextElementContent(rawEl).trim();
      if (text) {
        const isMd = isRecord(rawEl.text) && isMarkdownTextTag(rawEl.text);
        blocks.push(isMd ? text : escapePlainTextForFeishuMarkdown(text));
      }
      continue;
    }

    if (tagLower === "div") {
      if (isRecord(rawEl.text) && typeof rawEl.text.content === "string") {
        const text = rawEl.text.content.trim();
        if (text) {
          blocks.push(
            isMarkdownTextTag(rawEl.text)
              ? text
              : escapePlainTextForFeishuMarkdown(text),
          );
        }
      } else if (typeof rawEl.text === "string" && rawEl.text.trim()) {
        blocks.push(escapePlainTextForFeishuMarkdown(rawEl.text.trim()));
      }
      if (Array.isArray(rawEl.fields)) {
        for (const field of rawEl.fields) {
          if (!isRecord(field)) {
            continue;
          }
          if (isRecord(field.text) && typeof field.text.content === "string") {
            const text = field.text.content.trim();
            if (text) {
              blocks.push(
                isMarkdownTextTag(field.text)
                  ? text
                  : escapePlainTextForFeishuMarkdown(text),
              );
            }
          }
        }
      }
      continue;
    }

    if (tagLower === "note" && Array.isArray(rawEl.elements)) {
      const noteParts: string[] = [];
      for (const noteEl of rawEl.elements) {
        if (isRecord(noteEl) && typeof noteEl.content === "string") {
          const text = noteEl.content.trim();
          if (text) {
            noteParts.push(
              isMarkdownTextTag(noteEl)
                ? text
                : escapePlainTextForFeishuMarkdown(text),
            );
          }
        }
      }
      if (noteParts.length > 0) {
        blocks.push(noteParts.join(" "));
      }
      continue;
    }

    if (tagLower === "button") {
      const verifiedUrl = extractButtonVerifiedOpenUrl(rawEl);
      if (verifiedUrl) {
        const label = readHeaderText(rawEl.text) || "打开链接";
        blocks.push(`[${escapePlainTextForFeishuMarkdown(label)}](${verifiedUrl})`);
      }
      continue;
    }

    if (tagLower === "action" && Array.isArray(rawEl.actions)) {
      collectFallbackBlocksFromElements(rawEl.actions, blocks);
      continue;
    }

    if (tagLower === "column_set" && Array.isArray(rawEl.columns)) {
      for (const col of rawEl.columns) {
        if (isRecord(col) && Array.isArray(col.elements)) {
          collectFallbackBlocksFromElements(col.elements, blocks);
        }
      }
      continue;
    }

    if (Array.isArray(rawEl.elements)) {
      collectFallbackBlocksFromElements(rawEl.elements, blocks);
    }
  }
}

function makeMinimalCard(content: string): FeishuSchema2Card {
  return {
    schema: "2.0",
    config: {
      width_mode: "fill",
    },
    body: {
      elements: [
        {
          tag: "markdown",
          content,
        },
      ],
    },
  };
}

function fitMinimalCardWithinMaxBytes(
  content: string,
  maxBytes: number = FEISHU_CARD_MAX_BYTES,
): FeishuSchema2Card {
  const full = makeMinimalCard(content);
  if (Buffer.byteLength(JSON.stringify(full), "utf8") <= maxBytes) {
    return full;
  }

  let lo = 0;
  let hi = content.length;
  while (lo < hi) {
    let mid = Math.ceil((lo + hi) / 2);
    if (mid > 0 && mid < content.length) {
      const code = content.charCodeAt(mid - 1);
      if (code >= 0xd800 && code <= 0xdbff) {
        mid -= 1;
      }
    }
    if (mid <= lo) {
      break;
    }
    const candidate = makeMinimalCard(
      `${content.slice(0, mid).trimEnd()}${FALLBACK_TRUNCATED_SUFFIX}`,
    );
    if (Buffer.byteLength(JSON.stringify(candidate), "utf8") <= maxBytes) {
      lo = mid;
    } else {
      hi = mid - 1;
    }
  }

  return makeMinimalCard(
    `${content.slice(0, lo).trimEnd()}${FALLBACK_TRUNCATED_SUFFIX}`,
  );
}

export interface FeishuMarkdownFallbackBuildResult {
  card: FeishuSchema2Card;
  retainedLinkCount: number;
}

function countVerifiedMarkdownLinks(markdown: string): number {
  if (!markdown) {
    return 0;
  }
  const tree = parseChannelMarkdown(markdown);
  let count = 0;
  const walk = (node: MdastNode): void => {
    if (node.type === "link" && toVerifiedPublicHttpUrl(node.url)) {
      count += 1;
      return;
    }
    if (Array.isArray(node.children)) {
      for (const child of node.children) {
        walk(child);
      }
    }
  };
  walk(tree);
  return count;
}

/**
 * Builds a minimal Schema 2.0 card containing only a single `markdown` element
 * combining the original card's title, subtitle, markdown/text blocks, and
 * verified public `open_url` button links, along with the count of verified
 * public links actually retained in the final fallback markdown body.
 */
export function buildMinimalMarkdownFallbackCardWithMeta(
  card: Record<string, unknown>,
  options?: {
    maxBytes?: number;
    webBaseUrl?: string | null;
    threadId?: string | null;
    workspaceId?: string | null;
  },
): FeishuMarkdownFallbackBuildResult {
  const webBaseUrl = options?.webBaseUrl ?? null;
  const threadId = options?.threadId ?? null;
  const workspaceId = options?.workspaceId ?? null;
  const parts: string[] = [];

  if (isRecord(card.header)) {
    const title = readHeaderText(card.header.title);
    if (title) {
      const safeTitle = rewriteSandboxLinks(
        escapePlainTextForFeishuMarkdown(title),
        webBaseUrl,
        threadId,
        workspaceId,
      );
      parts.push(`**${safeTitle}**`);
    }
    const subtitle = readHeaderText(card.header.subtitle);
    if (subtitle) {
      parts.push(escapePlainTextForFeishuMarkdown(subtitle));
    }
  }

  if (isRecord(card.body) && Array.isArray(card.body.elements)) {
    collectFallbackBlocksFromElements(card.body.elements, parts);
  }
  if (Array.isArray(card.elements)) {
    collectFallbackBlocksFromElements(card.elements, parts);
  }

  const rawText = parts.length > 0 ? parts.join("\n\n") : FALLBACK_EMPTY_TEXT;
  const rewritten = rewriteSandboxLinks(
    rawText,
    webBaseUrl,
    threadId,
    workspaceId,
  );
  const escaped = escapeFeishuCardMarkdownText(rewritten);
  const fitted = fitMinimalCardWithinMaxBytes(
    escaped,
    options?.maxBytes ?? FEISHU_CARD_MAX_BYTES,
  );
  const finalContent = fitted.body.elements[0]?.content ?? "";
  return {
    card: fitted,
    retainedLinkCount: countVerifiedMarkdownLinks(finalContent),
  };
}

/**
 * Builds a minimal Schema 2.0 card containing only a single `markdown` element
 * combining the original card's title, subtitle, markdown/text blocks, and
 * verified public `open_url` button links. Escapes Markdown syntax from
 * `plain_text` fields and runs sandbox/local link rewriting across the final
 * fallback body. Used as a one-shot fallback when Feishu still rejects a card
 * with 200621.
 */
export function buildMinimalMarkdownFallbackCard(
  card: Record<string, unknown>,
  options?: {
    maxBytes?: number;
    webBaseUrl?: string | null;
    threadId?: string | null;
    workspaceId?: string | null;
  },
): FeishuSchema2Card {
  return buildMinimalMarkdownFallbackCardWithMeta(card, options).card;
}
