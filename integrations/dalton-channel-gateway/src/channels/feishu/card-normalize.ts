/**
 * Pure Schema 2.0 Feishu card normalizer and minimal markdown-only fallback builder.
 *
 * Normalizes common LLM-authored card shape mistakes before outbound delivery
 * without mutating the caller's object or guessing unknown element schemas.
 */

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
        content: extractTextElementContent(rawEl),
      };
    }

    const cloned: Record<string, unknown> = structuredClone(rawEl);
    if (tagLower === "column_set" && Array.isArray(cloned.columns)) {
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

function collectFallbackBlocksFromElements(
  elements: unknown[],
  blocks: string[],
): void {
  for (const rawEl of elements) {
    if (!isRecord(rawEl)) {
      continue;
    }
    const tagLower = readTagLower(rawEl);

    if (tagLower === "markdown" || TEXT_ELEMENT_TAGS.has(tagLower)) {
      const text = extractTextElementContent(rawEl).trim();
      if (text) {
        blocks.push(text);
      }
      continue;
    }

    if (tagLower === "div") {
      if (isRecord(rawEl.text) && typeof rawEl.text.content === "string") {
        const text = rawEl.text.content.trim();
        if (text) {
          blocks.push(text);
        }
      } else if (typeof rawEl.text === "string" && rawEl.text.trim()) {
        blocks.push(rawEl.text.trim());
      }
      if (Array.isArray(rawEl.fields)) {
        for (const field of rawEl.fields) {
          if (!isRecord(field)) {
            continue;
          }
          if (isRecord(field.text) && typeof field.text.content === "string") {
            const text = field.text.content.trim();
            if (text) {
              blocks.push(text);
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
            noteParts.push(text);
          }
        }
      }
      if (noteParts.length > 0) {
        blocks.push(noteParts.join(" "));
      }
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

/**
 * Builds a minimal Schema 2.0 card containing only a single `markdown` element
 * combining the original card's title, subtitle, and markdown/text blocks.
 * Used as a one-shot fallback when Feishu still rejects a card with 200621.
 */
export function buildMinimalMarkdownFallbackCard(
  card: Record<string, unknown>,
  options?: { maxBytes?: number },
): FeishuSchema2Card {
  const parts: string[] = [];

  if (isRecord(card.header)) {
    const title = readHeaderText(card.header.title);
    if (title) {
      parts.push(`**${title}**`);
    }
    const subtitle = readHeaderText(card.header.subtitle);
    if (subtitle) {
      parts.push(subtitle);
    }
  }

  if (isRecord(card.body) && Array.isArray(card.body.elements)) {
    collectFallbackBlocksFromElements(card.body.elements, parts);
  }
  if (Array.isArray(card.elements)) {
    collectFallbackBlocksFromElements(card.elements, parts);
  }

  const rawText = parts.length > 0 ? parts.join("\n\n") : FALLBACK_EMPTY_TEXT;
  const escaped = escapeFeishuCardMarkdownText(rawText);
  return fitMinimalCardWithinMaxBytes(
    escaped,
    options?.maxBytes ?? FEISHU_CARD_MAX_BYTES,
  );
}
