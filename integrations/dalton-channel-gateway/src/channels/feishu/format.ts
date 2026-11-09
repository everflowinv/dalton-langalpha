/**
 * Feishu outbound formatting (Post rich text & Schema 2.0 Interactive Cards),
 * Markdown table/size chunking, and inbound Post/Card/MergeForward rendering.
 *
 * Adapted from:
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` (lines 36–630)
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` (lines 295–345)
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 5 (`src/channels/feishu/format.ts`).
 */

import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmTableFromMarkdown } from "mdast-util-gfm-table";
import { gfmTable } from "micromark-extension-gfm-table";
import { chunkMarkdownTextWithMode, type ChunkMode } from "../../shared/text.js";

export const FEISHU_POST_MAX_BYTES = 30_720;
export const FEISHU_CARD_MAX_BYTES = 30_720;
export const FEISHU_CARD_MAX_ELEMENTS = 200;
export const FEISHU_CARD_TABLE_LIMIT = 5;

export interface FeishuMarkdownAstNode {
  type: string;
  children?: FeishuMarkdownAstNode[];
  position?: {
    start?: { line: number; column: number; offset?: number };
    end?: { line: number; column: number; offset?: number };
  };
  [key: string]: unknown;
}

export interface FeishuSchema2CardElement {
  tag: "markdown" | "hr" | "div" | "button" | "action";
  content?: string;
  [key: string]: unknown;
}

export interface FeishuSchema2Card {
  schema: "2.0";
  config: {
    width_mode?: "fill" | "default";
    wide_screen_mode?: boolean;
    [key: string]: unknown;
  };
  header?: {
    title: {
      tag: "plain_text";
      content: string;
    };
    template?: string;
  };
  body: {
    elements: FeishuSchema2CardElement[];
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const feishuMarkdownExtensions = [gfmTable()];
const feishuMdastExtensions = [gfmTableFromMarkdown()];

export function parseFeishuMarkdown(text: string): FeishuMarkdownAstNode {
  return fromMarkdown(text, {
    extensions: feishuMarkdownExtensions,
    mdastExtensions: feishuMdastExtensions,
  }) as unknown as FeishuMarkdownAstNode;
}

export function visitFeishuMarkdown(
  tree: FeishuMarkdownAstNode,
  visitor: (node: FeishuMarkdownAstNode) => void,
): void {
  const stack: FeishuMarkdownAstNode[] = [tree];
  while (stack.length > 0) {
    const current = stack.pop();
    if (!current) {
      continue;
    }
    visitor(current);
    if (!Array.isArray(current.children)) {
      continue;
    }
    for (let index = current.children.length - 1; index >= 0; index -= 1) {
      const child = current.children[index];
      if (child && typeof child === "object" && "type" in child) {
        stack.push(child);
      }
    }
  }
}

export function countMarkdownTables(text: string): number {
  if (!text.includes("|")) {
    return 0;
  }
  let tables = 0;
  visitFeishuMarkdown(parseFeishuMarkdown(text), (node) => {
    if (node.type === "table") {
      tables += 1;
    }
  });
  return tables;
}

export function withinCardTableLimit(
  text: string,
  maxTables: number = FEISHU_CARD_TABLE_LIMIT,
): boolean {
  return countMarkdownTables(text) <= maxTables;
}

/**
 * Determines whether outbound Markdown text should be rendered as a Schema 2.0
 * interactive card (contains fenced code blocks or GFM tables) rather than a
 * rich-text `post` message.
 */
export function shouldUseFeishuCard(text: string): boolean {
  if (/```[\s\S]*?```/.test(text) || /~~~[\s\S]*?~~~/.test(text)) {
    return true;
  }
  if (!text.includes("|")) {
    return false;
  }
  return countMarkdownTables(text) > 0;
}

function collectSoftBreakOffsets(
  tree: FeishuMarkdownAstNode,
  text: string,
): number[] {
  const offsets: number[] = [];
  visitFeishuMarkdown(tree, (node) => {
    if (node.type !== "text") {
      return;
    }
    const start = node.position?.start?.offset;
    const end = node.position?.end?.offset;
    if (typeof start !== "number" || typeof end !== "number") {
      return;
    }
    for (let offset = start; offset < end; offset += 1) {
      const char = text[offset];
      if (char === "\n") {
        if (text[offset - 1] !== "\r") {
          offsets.push(offset);
        }
        continue;
      }
      if (char === "\r") {
        offsets.push(offset);
        if (text[offset + 1] === "\n") {
          offset += 1;
        }
      }
    }
  });
  return offsets.sort((left, right) => left - right);
}

/**
 * Materializes Markdown soft line breaks inside AST text nodes by appending
 * two trailing spaces (`"  "`) before `\n` (or `\r\n` / `\r`), preserving list/blockquote
 * structure instead of inserting blank lines (`reply-delivery-result-iCCrdxE1.mjs:495-536`).
 */
export function materializeFeishuPostMarkdownSoftBreaks(text: string): string {
  if (!text.includes("\n") && !text.includes("\r")) {
    return text;
  }
  const softBreakOffsets = collectSoftBreakOffsets(
    parseFeishuMarkdown(text),
    text,
  );
  if (softBreakOffsets.length === 0) {
    return text;
  }
  let cursor = 0;
  let output = "";
  for (const offset of softBreakOffsets) {
    const lineEnding =
      text[offset] === "\r"
        ? text[offset + 1] === "\n"
          ? "\r\n"
          : "\r"
        : "\n";
    let trimmedBreakStart = offset;
    while (
      trimmedBreakStart > cursor &&
      (text[trimmedBreakStart - 1] === " " ||
        text[trimmedBreakStart - 1] === "\t")
    ) {
      trimmedBreakStart -= 1;
    }
    output += text.slice(cursor, trimmedBreakStart);
    output += `  ${lineEnding}`;
    cursor = offset + lineEnding.length;
  }
  output += text.slice(cursor);
  return output;
}

export function buildFeishuPostMessagePayload(params: {
  messageText: string;
}): {
  zh_cn: {
    content: Array<Array<{ tag: "md"; text: string }>>;
  };
} {
  return {
    zh_cn: {
      content: [
        [
          {
            tag: "md",
            text: materializeFeishuPostMarkdownSoftBreaks(params.messageText),
          },
        ],
      ],
    },
  };
}

export function buildFeishuPostMessageContent(params: {
  messageText: string;
}): string {
  return JSON.stringify(buildFeishuPostMessagePayload(params));
}

export function escapeFeishuCardPlainText(text: string): string {
  return text.replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const FEISHU_CARD_ALLOWED_OPEN_TAG_RE =
  /^<(?:font\b[^>\n]*|at\b[^>\n]*|text_tag\b[^>\n]*|[biu]|del|s|br\s*\/?)>/i;
const FEISHU_CARD_ALLOWED_CLOSE_TAG_RE =
  /^<\/(?:font|at|text_tag|[biu]|del|s)>/i;

function isAllowedFeishuCardTag(text: string, ltIndex: number): boolean {
  const rest = text.slice(ltIndex);
  return (
    FEISHU_CARD_ALLOWED_OPEN_TAG_RE.test(rest) ||
    FEISHU_CARD_ALLOWED_CLOSE_TAG_RE.test(rest)
  );
}

function escapeNonFeishuTagAngles(segment: string): string {
  let out = "";
  for (let index = 0; index < segment.length; index += 1) {
    const ch = segment[index]!;
    if (ch !== "<") {
      out += ch;
      continue;
    }
    if (isAllowedFeishuCardTag(segment, index)) {
      const closeIndex = segment.indexOf(">", index + 1);
      if (closeIndex !== -1) {
        out += segment.slice(index, closeIndex + 1);
        index = closeIndex;
        continue;
      }
    }
    out += "&lt;";
  }
  return out;
}

export function escapeFeishuCardMarkdownText(text: string): string {
  if (!text.includes("<")) {
    return text;
  }
  const pattern = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g;
  let out = "";
  let lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    const matchIndex = match.index ?? 0;
    out += escapeNonFeishuTagAngles(text.slice(lastIndex, matchIndex));
    out += match[0];
    lastIndex = matchIndex + match[0].length;
  }
  out += escapeNonFeishuTagAngles(text.slice(lastIndex));
  return out;
}

function splitMarkdownSectionsByHr(text: string): string[] {
  const lines = text.split("\n");
  const sections: string[] = [];
  let current: string[] = [];
  let inFence = false;

  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      current.push(line);
      continue;
    }
    if (!inFence && /^\s*(?:-\s*){3,}$|^\s*(?:\*\s*){3,}$|^\s*(?:_\s*){3,}$/.test(line)) {
      sections.push(current.join("\n"));
      current = [];
      continue;
    }
    current.push(line);
  }
  sections.push(current.join("\n"));
  return sections;
}

/**
 * Builds a Schema 2.0 Feishu interactive card (`schema: "2.0"`).
 */
export function buildStructuredCard(
  text: string,
  options?: { title?: string; template?: string },
): FeishuSchema2Card {
  const sections = splitMarkdownSectionsByHr(text);
  const elements: FeishuSchema2CardElement[] = [];

  for (let i = 0; i < sections.length; i += 1) {
    const content = sections[i]!.trim();
    if (content) {
      elements.push({
        tag: "markdown",
        content: escapeFeishuCardMarkdownText(content),
      });
    }
    if (i < sections.length - 1) {
      elements.push({ tag: "hr" });
    }
  }

  if (elements.length === 0) {
    elements.push({
      tag: "markdown",
      content: escapeFeishuCardMarkdownText(text),
    });
  }

  const title = options?.title?.trim();
  return {
    schema: "2.0",
    config: {
      width_mode: "fill",
    },
    ...(title
      ? {
          header: {
            title: {
              tag: "plain_text",
              content: title,
            },
            template: options?.template ?? "blue",
          },
        }
      : {}),
    body: {
      elements,
    },
  };
}

function countCardBodyElements(card: Record<string, unknown>): number {
  const body = isRecord(card.body) ? card.body : undefined;
  const elements = Array.isArray(body?.elements)
    ? body.elements
    : Array.isArray(card.elements)
      ? card.elements
      : [];
  let total = elements.length;
  for (const element of elements) {
    if (!isRecord(element)) {
      continue;
    }
    if (element.tag === "action" && Array.isArray(element.actions)) {
      total += element.actions.length;
    }
  }
  return total;
}

export function isFeishuCardWithinEnvelope(
  card: Record<string, unknown> | FeishuSchema2Card,
  options?: { maxBytes?: number; maxElements?: number },
): boolean {
  const maxBytes = options?.maxBytes ?? FEISHU_CARD_MAX_BYTES;
  const maxElements = options?.maxElements ?? FEISHU_CARD_MAX_ELEMENTS;
  return (
    Buffer.byteLength(JSON.stringify(card), "utf8") <= maxBytes &&
    countCardBodyElements(card as Record<string, unknown>) <= maxElements
  );
}

export function feishuCardWithinTableLimit(
  card: Record<string, unknown> | FeishuSchema2Card,
  maxTables: number = FEISHU_CARD_TABLE_LIMIT,
): boolean {
  const rawCard = card as Record<string, unknown>;
  const body = isRecord(rawCard.body) ? rawCard.body : undefined;
  const elements = Array.isArray(body?.elements)
    ? body.elements
    : Array.isArray(rawCard.elements)
      ? rawCard.elements
      : [];
  let tables = 0;
  for (const element of elements) {
    if (!isRecord(element) || typeof element.content !== "string") {
      continue;
    }
    tables += countMarkdownTables(element.content);
    if (tables > maxTables) {
      return false;
    }
  }
  return true;
}

export function assertFeishuCardWithinEnvelope(
  card: Record<string, unknown> | FeishuSchema2Card,
  context = "Feishu card",
  options?: { maxBytes?: number; maxElements?: number },
): void {
  const maxBytes = options?.maxBytes ?? FEISHU_CARD_MAX_BYTES;
  const maxElements = options?.maxElements ?? FEISHU_CARD_MAX_ELEMENTS;
  const bytes = Buffer.byteLength(JSON.stringify(card), "utf8");
  if (bytes > maxBytes) {
    throw new Error(
      `${context} exceeds ${maxBytes} byte card limit (${bytes} bytes)`,
    );
  }
  const elements = countCardBodyElements(card as Record<string, unknown>);
  if (elements > maxElements) {
    throw new Error(
      `${context} exceeds ${maxElements} element card limit (${elements} elements)`,
    );
  }
}

export function chunkFeishuMarkdownByEnvelope(
  text: string,
  options: {
    fits: (chunk: string) => boolean;
    chunkMode?: ChunkMode;
    initialLimit?: number;
    payloadBytes?: (chunk: string) => number;
    maxBytes?: number;
  },
): string[] {
  if (!text) {
    return [];
  }
  const chunkMode = options.chunkMode ?? "length";
  const initialLimit = Math.max(1, options.initialLimit ?? text.length);

  const splitOversizedSegment = (segment: string): string[] => {
    if (!segment || options.fits(segment)) {
      return segment ? [segment] : [];
    }
    let currentLimit = Math.max(1, Math.min(initialLimit, segment.length - 1));
    while (currentLimit >= 1) {
      const candidates = chunkMarkdownTextWithMode(
        segment,
        currentLimit,
        chunkMode,
      );
      let largestContentBytes = 0;
      let oversizedChunk: string | undefined;
      for (const chunk of candidates) {
        const bytes = options.payloadBytes
          ? options.payloadBytes(chunk)
          : Buffer.byteLength(chunk, "utf8") * 2;
        largestContentBytes = Math.max(largestContentBytes, bytes);
        if (!options.fits(chunk) && oversizedChunk === undefined) {
          oversizedChunk = chunk;
        }
      }
      if (oversizedChunk === undefined && candidates.length > 0) {
        return candidates;
      }
      if (currentLimit === 1) {
        break;
      }
      const targetMaxBytes = options.maxBytes ?? initialLimit;
      const effectiveBase = Math.min(
        currentLimit,
        (oversizedChunk ?? segment).length,
      );
      const ratioLimit =
        largestContentBytes > targetMaxBytes && largestContentBytes > 0
          ? Math.floor((effectiveBase * targetMaxBytes) / largestContentBytes) -
            1
          : Math.floor(effectiveBase * 0.75);
      currentLimit = Math.max(1, Math.min(currentLimit - 1, ratioLimit));
    }

    let low = 1;
    let high = segment.length;
    let best = 1;
    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      const prefix = segment.slice(0, mid);
      if (options.fits(prefix)) {
        best = mid;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    let splitAt = best;
    const prevCode = segment.charCodeAt(splitAt - 1);
    if (splitAt > 1 && prevCode >= 0xd800 && prevCode <= 0xdbff) {
      splitAt -= 1;
    }
    const head = segment.slice(0, splitAt);
    const tail = segment.slice(splitAt);
    return [head, ...(tail ? splitOversizedSegment(tail) : [])];
  };

  return splitOversizedSegment(text);
}

export function chunkFeishuPostMarkdown(
  text: string,
  maxBytes: number = FEISHU_POST_MAX_BYTES,
): string[] {
  const measurePostBytes = (chunk: string): number =>
    Buffer.byteLength(
      buildFeishuPostMessageContent({ messageText: chunk }),
      "utf8",
    );
  return chunkFeishuMarkdownByEnvelope(text, {
    fits: (chunk) => measurePostBytes(chunk) <= maxBytes,
    payloadBytes: measurePostBytes,
    maxBytes,
    initialLimit: Math.max(1, Math.min(text.length, maxBytes)),
  });
}

/**
 * Splits Markdown containing more than `maxTables` (default 5) GFM tables into
 * segments where each segment contains at most `maxTables` tables.
 */
export function splitMarkdownByTableLimit(
  text: string,
  maxTables: number = FEISHU_CARD_TABLE_LIMIT,
): string[] {
  if (!text) {
    return [];
  }
  const limit = Math.max(1, maxTables);
  if ( withinCardTableLimit(text, limit) ) {
    return [text];
  }

  const tree = parseFeishuMarkdown(text);
  const tableEndOffsets: number[] = [];
  visitFeishuMarkdown(tree, (node) => {
    if (node.type === "table") {
      const endOffset = node.position?.end?.offset;
      if (typeof endOffset === "number" && endOffset > 0) {
        tableEndOffsets.push(endOffset);
      }
    }
  });
  tableEndOffsets.sort((a, b) => a - b);

  if (tableEndOffsets.length <= limit) {
    return [text];
  }

  const chunks: string[] = [];
  let startOffset = 0;
  for (let i = limit - 1; i < tableEndOffsets.length; i += limit) {
    const cutOffset = tableEndOffsets[i]!;
    // Advance past trailing newline if present
    let sliceEnd = cutOffset;
    while (sliceEnd < text.length && (text[sliceEnd] === "\r" || text[sliceEnd] === "\n")) {
      sliceEnd += 1;
    }
    const piece = text.slice(startOffset, sliceEnd).trim();
    if (piece.length > 0) {
      chunks.push(piece);
    }
    startOffset = sliceEnd;
  }

  const remainder = text.slice(startOffset).trim();
  if (remainder.length > 0) {
    chunks.push(remainder);
  }

  return chunks.length > 0 ? chunks : [text];
}

/**
 * Splits Markdown for Schema 2.0 Interactive Cards so that every returned chunk:
 * 1. Contains at most `FEISHU_CARD_TABLE_LIMIT` (5) Markdown tables;
 * 2. Serializes to at most `FEISHU_CARD_MAX_BYTES` (30,720 bytes) and
 *    `FEISHU_CARD_MAX_ELEMENTS` (200 elements).
 */
export function chunkFeishuCardMarkdown(
  text: string,
  options?: {
    maxBytes?: number;
    maxElements?: number;
    maxTables?: number;
    chunkMode?: ChunkMode;
  },
): string[] {
  if (!text) {
    return [];
  }
  const maxBytes = options?.maxBytes ?? FEISHU_CARD_MAX_BYTES;
  const maxElements = options?.maxElements ?? FEISHU_CARD_MAX_ELEMENTS;
  const maxTables = options?.maxTables ?? FEISHU_CARD_TABLE_LIMIT;

  const tableSegments = splitMarkdownByTableLimit(text, maxTables);
  const measureCardBytes = (chunk: string): number =>
    Buffer.byteLength(JSON.stringify(buildStructuredCard(chunk)), "utf8");
  return tableSegments.flatMap((segment) =>
    chunkFeishuMarkdownByEnvelope(segment, {
      chunkMode: options?.chunkMode,
      initialLimit: Math.max(1, Math.min(segment.length, maxBytes)),
      payloadBytes: measureCardBytes,
      maxBytes,
      fits: (chunk) => {
        const card = buildStructuredCard(chunk);
        return (
          isFeishuCardWithinEnvelope(card, { maxBytes, maxElements }) &&
          feishuCardWithinTableLimit(card, maxTables)
        );
      },
    }),
  );
}

/**
 * High-level outbound text chunker for `FeishuChannelAdapter.splitTextChunks`.
 * Uses Schema 2.0 card chunking when `shouldUseFeishuCard(text)` is true,
 * or rich-text `post` chunking otherwise.
 */
export function chunkFeishuOutboundText(
  text: string,
  options?: {
    maxBytes?: number;
    maxTables?: number;
  },
): string[] {
  if (!text) {
    return [];
  }
  if (shouldUseFeishuCard(text)) {
    return chunkFeishuCardMarkdown(text, {
      maxBytes: options?.maxBytes ?? FEISHU_CARD_MAX_BYTES,
      maxTables: options?.maxTables ?? FEISHU_CARD_TABLE_LIMIT,
    });
  }
  return chunkFeishuPostMarkdown(
    text,
    options?.maxBytes ?? FEISHU_POST_MAX_BYTES,
  );
}

// --- Inbound Post, Interactive Card, and Merge-Forward Parsers ---

function toBoolean(value: unknown): boolean {
  return value === true;
}

function wrapStyle(text: string, style: unknown): string {
  if (!text || !isRecord(style)) {
    return text;
  }
  let out = text;
  if (toBoolean(style.code)) {
    return `\`${out}\``;
  }
  if (toBoolean(style.bold)) {
    out = `**${out}**`;
  }
  if (toBoolean(style.italic)) {
    out = `*${out}*`;
  }
  if (toBoolean(style.strikethrough) || toBoolean(style.lineThrough)) {
    out = `~~${out}~~`;
  }
  if (toBoolean(style.underline)) {
    out = `<u>${out}</u>`;
  }
  return out;
}

function escapeMarkdownText(text: string): string {
  return text.replace(/([\\`*_{}[\]()#+\-.!|>~])/g, "\\$1");
}

function sanitizeFenceLanguage(lang: string): string {
  return lang.trim().replace(/[^a-zA-Z0-9_+#.-]/g, "");
}

function resolvePostPayload(content: string): {
  title: string;
  paragraphs: unknown[];
} | null {
  try {
    const parsed = JSON.parse(content);
    if (!isRecord(parsed)) {
      return null;
    }
    const candidate =
      isRecord(parsed.zh_cn)
        ? parsed.zh_cn
        : isRecord(parsed.en_us)
          ? parsed.en_us
          : isRecord(parsed.ja_jp)
            ? parsed.ja_jp
            : parsed;
    if (!isRecord(candidate)) {
      return null;
    }
    return {
      title: typeof candidate.title === "string" ? candidate.title : "",
      paragraphs: Array.isArray(candidate.content) ? candidate.content : [],
    };
  } catch {
    return null;
  }
}

export interface ParsedPostContent {
  textContent: string;
  imageKeys: string[];
  mediaKeys: Array<{ fileKey: string; fileName?: string }>;
  mentionedOpenIds: string[];
}

export function renderPostContent(content: string): ParsedPostContent | null {
  const resolved = resolvePostPayload(content);
  if (!resolved) {
    return null;
  }
  const imageKeys: string[] = [];
  const mediaKeys: Array<{ fileKey: string; fileName?: string }> = [];
  const mentionedOpenIds: string[] = [];
  const paragraphLines: string[] = [];

  for (const paragraph of resolved.paragraphs) {
    if (!Array.isArray(paragraph)) {
      continue;
    }
    let line = "";
    for (const rawEl of paragraph) {
      if (!isRecord(rawEl)) {
        continue;
      }
      const tag = typeof rawEl.tag === "string" ? rawEl.tag : "";
      if (tag === "text") {
        const text = typeof rawEl.text === "string" ? rawEl.text : "";
        line += toBoolean(rawEl.un_escape)
          ? wrapStyle(text, rawEl.style)
          : wrapStyle(escapeMarkdownText(text), rawEl.style);
        continue;
      }
      if (tag === "a") {
        const text =
          typeof rawEl.text === "string" && rawEl.text
            ? rawEl.text
            : typeof rawEl.href === "string"
              ? rawEl.href
              : "";
        const href = typeof rawEl.href === "string" ? rawEl.href.trim() : "";
        const styled = wrapStyle(escapeMarkdownText(text), rawEl.style);
        line += href ? `[${styled}](${href})` : styled;
        continue;
      }
      if (tag === "at") {
        const userId = typeof rawEl.user_id === "string" ? rawEl.user_id.trim() : "";
        if (userId) {
          mentionedOpenIds.push(userId);
        }
        const userName =
          typeof rawEl.user_name === "string" && rawEl.user_name
            ? rawEl.user_name
            : userId;
        line += wrapStyle(`@${escapeMarkdownText(userName)}`, rawEl.style);
        continue;
      }
      if (tag === "img") {
        if (typeof rawEl.image_key === "string" && rawEl.image_key) {
          imageKeys.push(rawEl.image_key);
          line += "![image]";
        }
        continue;
      }
      if (tag === "media") {
        if (typeof rawEl.file_key === "string" && rawEl.file_key) {
          const fileName =
            typeof rawEl.file_name === "string" && rawEl.file_name
              ? rawEl.file_name
              : undefined;
          mediaKeys.push({ fileKey: rawEl.file_key, fileName });
          line += fileName ? `[media: ${fileName}]` : "[media]";
        }
        continue;
      }
      if (tag === "code_block") {
        const lang = sanitizeFenceLanguage(
          typeof rawEl.language === "string" ? rawEl.language : "",
        );
        const code = (typeof rawEl.text === "string" ? rawEl.text : "").replace(
          /\r\n/g,
          "\n",
        );
        const trailingNewline = code.endsWith("\n") ? "" : "\n";
        line += `\n\`\`\`${lang}\n${code}${trailingNewline}\`\`\`\n`;
        continue;
      }
      if (tag === "hr") {
        line += "\n---\n";
        continue;
      }
      if (tag === "md") {
        line += typeof rawEl.text === "string" ? rawEl.text : "";
        continue;
      }
      if (tag === "emotion") {
        const emojiType =
          typeof rawEl.emoji_type === "string" && rawEl.emoji_type
            ? rawEl.emoji_type
            : "emoji";
        line += `:${emojiType}:`;
      }
    }
    paragraphLines.push(line.trim());
  }

  const title = resolved.title.trim();
  const body = paragraphLines.filter((row) => row.length > 0).join("\n");
  return {
    textContent: [title, body].filter(Boolean).join("\n\n").trim(),
    imageKeys,
    mediaKeys,
    mentionedOpenIds,
  };
}

export function parsePostContent(content: string): ParsedPostContent {
  const rendered = renderPostContent(content);
  if (rendered) {
    return rendered;
  }
  return {
    textContent: "[Rich text message]",
    imageKeys: [],
    mediaKeys: [],
    mentionedOpenIds: [],
  };
}

export function parseInteractiveCardContent(content: string): string {
  try {
    const parsed = JSON.parse(content);
    if (!isRecord(parsed)) {
      return "[Interactive card]";
    }
    const texts: string[] = [];

    const header = isRecord(parsed.header) ? parsed.header : undefined;
    const headerTitle = isRecord(header?.title) ? header.title : undefined;
    if (typeof headerTitle?.content === "string" && headerTitle.content.trim()) {
      texts.push(headerTitle.content.trim());
    }

    const extractElements = (elements: unknown[]): void => {
      for (const el of elements) {
        if (!isRecord(el)) {
          continue;
        }
        if (el.tag === "div" && isRecord(el.text) && typeof el.text.content === "string") {
          texts.push(el.text.content);
        } else if (el.tag === "markdown" && typeof el.content === "string") {
          texts.push(el.content);
        } else if (el.tag === "hr") {
          texts.push("---");
        } else if (Array.isArray(el.elements)) {
          extractElements(el.elements);
        }
      }
    };

    const body = isRecord(parsed.body) ? parsed.body : undefined;
    if (Array.isArray(body?.elements)) {
      extractElements(body.elements);
    } else if (Array.isArray(parsed.elements)) {
      extractElements(parsed.elements);
    }

    return texts.filter(Boolean).join("\n").trim() || "[Interactive card]";
  } catch {
    return "[Interactive card]";
  }
}

function formatDefaultSubMessageContent(
  rawBody: string,
  msgType: string,
): string {
  try {
    const parsed = JSON.parse(rawBody);
    switch (msgType) {
      case "text":
        return isRecord(parsed) && typeof parsed.text === "string"
          ? parsed.text
          : rawBody;
      case "post":
        return parsePostContent(rawBody).textContent;
      case "interactive":
        return parseInteractiveCardContent(rawBody);
      case "image":
        return "[Image]";
      case "file":
        return `[File: ${isRecord(parsed) && typeof parsed.file_name === "string" ? parsed.file_name : "unknown"}]`;
      case "audio":
        return "[Audio]";
      case "video":
      case "media":
        return "[Video]";
      case "merge_forward":
        return "[Nested Merged Forward]";
      default:
        return `[${msgType}]`;
    }
  } catch {
    return rawBody || `[${msgType}]`;
  }
}

function parseCreateTimeInt(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    return Number.parseInt(value.trim(), 10);
  }
  return 0;
}

/**
 * Formats sub-messages from a Feishu `merge_forward` message (`im.message.get`
 * `items` array or JSON string), matching `reply-delivery-result-iCCrdxE1.mjs:2182-2195`.
 */
export function parseMergeForwardContent(
  input:
    | unknown[]
    | {
        content?: string;
        items?: unknown[];
        depth?: number;
        maxDepth?: number;
        formatSubMessage?: (msgType: string, bodyContent: string) => string;
      },
): string {
  const isArrayInput = Array.isArray(input);
  const params = isArrayInput ? { items: input } : input;
  const maxDepth = params.maxDepth ?? 3;
  const depth = params.depth ?? 0;
  if (depth >= maxDepth) {
    return "[Merged and Forwarded Message]";
  }

  let rawMessages: unknown[];
  if (Array.isArray(params.items)) {
    rawMessages = params.items;
  } else if (typeof params.content === "string" && params.content.trim()) {
    try {
      const parsed = JSON.parse(params.content);
      if (Array.isArray(parsed)) {
        rawMessages = parsed;
      } else if (isRecord(parsed) && Array.isArray(parsed.messages)) {
        rawMessages = parsed.messages;
      } else if (isRecord(parsed) && Array.isArray(parsed.items)) {
        rawMessages = parsed.items;
      } else {
        return "[Merged and Forwarded Message]";
      }
    } catch {
      return "[Merged and Forwarded Message]";
    }
  } else {
    return "[Merged and Forwarded Message]";
  }

  if (rawMessages.length === 0) {
    return "[Merged and Forwarded Message - no sub-messages]";
  }

  const recordItems = rawMessages.filter(isRecord);
  const hasApiHierarchy = recordItems.some(
    (item) =>
      typeof item.upper_message_id === "string" ||
      (item.msg_type === "merge_forward" && !item.upper_message_id && recordItems.length > 1),
  );

  let subMessages: Record<string, unknown>[];
  if (hasApiHierarchy) {
    const container = recordItems.find(
      (item) => item.msg_type === "merge_forward" && !item.upper_message_id,
    );
    subMessages = container
      ? recordItems.filter((item) => item !== container)
      : recordItems.filter((item) => Boolean(item.upper_message_id));
    if (subMessages.length === 0) {
      return "[Merged and Forwarded Message - no sub-messages found]";
    }
    subMessages = [...subMessages].sort(
      (a, b) => parseCreateTimeInt(a.create_time) - parseCreateTimeInt(b.create_time),
    );
  } else {
    subMessages = recordItems;
  }

  const maxMessages = 50;
  const lines: string[] = ["[Merged and Forwarded Messages]"];
  const indent = depth > 0 ? "  ".repeat(depth + 1) : "";

  for (const item of subMessages.slice(0, maxMessages)) {
    const msgType =
      (typeof item.msg_type === "string" ? item.msg_type : undefined) ??
      (typeof item.message_type === "string" ? item.message_type : undefined) ??
      "text";
    const bodyObj = isRecord(item.body) ? item.body : undefined;
    const rawBody =
      (typeof bodyObj?.content === "string" ? bodyObj.content : undefined) ??
      (typeof item.content === "string" ? item.content : "");
    let formatted: string;
    if (msgType === "merge_forward") {
      formatted = hasApiHierarchy
        ? "[Nested Merged Forward]"
        : parseMergeForwardContent({
            content: rawBody,
            depth: depth + 1,
            maxDepth,
            formatSubMessage: params.formatSubMessage,
          });
    } else if (params.formatSubMessage) {
      formatted = params.formatSubMessage(msgType, rawBody);
    } else {
      formatted = formatDefaultSubMessageContent(rawBody, msgType);
    }
    lines.push(`${indent}- ${formatted}`);
  }

  if (subMessages.length > maxMessages) {
    lines.push(`... and ${subMessages.length - maxMessages} more messages`);
  }

  const rendered = lines.join("\n");
  if (rendered.length <= 20_000) {
    return rendered;
  }
  return `${rendered.slice(0, 19_961).trimEnd()}\n... [Merged-forward content truncated]`;
}
