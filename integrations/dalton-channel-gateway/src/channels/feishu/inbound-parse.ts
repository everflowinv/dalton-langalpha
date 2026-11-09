/**
 * Feishu inbound event parser (`im.message.receive_v1`), mention normalization,
 * attachment descriptor extraction, and deduplication key resolution.
 *
 * Adapted from:
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/monitor.account-UbN1RhlP.mjs`
 *   (lines 276–433, 822–857, 2373–2404, 5215–5309, 5343–5565)
 * - `.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs`
 *   (lines 352–436, 461–630)
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 4 (`src/channels/feishu/inbound-parse.ts`).
 */

import crypto from "node:crypto";
import type {
  InboundAttachmentDescriptor,
  InboundChannelEvent,
} from "../../types.js";
import {
  parseInteractiveCardContent,
  parseMergeForwardContent,
  parsePostContent,
} from "./format.js";
import {
  buildDaltonFeishuChatAddress,
  buildDaltonFeishuThreadScopeKey,
} from "./targets.js";

export interface FeishuMentionId {
  open_id?: string;
  user_id?: string;
  union_id?: string;
}

export interface FeishuMentionItem {
  key?: string;
  id?: FeishuMentionId | string;
  name?: string;
  tenant_key?: string;
}

export interface FeishuRawSender {
  sender_id?: {
    open_id?: string;
    user_id?: string;
    union_id?: string;
  };
  sender_type?: string;
  tenant_key?: string;
  name?: string;
}

export interface FeishuRawMessage {
  message_id?: string;
  root_id?: string;
  parent_id?: string;
  create_time?: string;
  update_time?: string;
  chat_id?: string;
  thread_id?: string;
  chat_type?: string;
  message_type?: string;
  msg_type?: string;
  content?: string;
  mentions?: FeishuMentionItem[];
}

export interface FeishuMessageReceiveEventPayload {
  sender?: FeishuRawSender;
  message?: FeishuRawMessage;
  event?: {
    sender?: FeishuRawSender;
    message?: FeishuRawMessage;
  };
  header?: {
    event_id?: string;
    event_type?: string;
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function extractMentionOpenId(mention: FeishuMentionItem): string | undefined {
  if (!mention) {
    return undefined;
  }
  if (typeof mention.id === "string") {
    return mention.id.trim() || undefined;
  }
  if (isRecord(mention.id)) {
    const openId =
      typeof mention.id.open_id === "string"
        ? mention.id.open_id.trim()
        : typeof mention.id.user_id === "string"
          ? mention.id.user_id.trim()
          : undefined;
    return openId || undefined;
  }
  return undefined;
}

export function isFeishuBroadcastMention(mention: FeishuMentionItem): boolean {
  const key = mention.key?.trim().toLowerCase() ?? "";
  const openId = extractMentionOpenId(mention)?.toLowerCase() ?? "";
  return key === "@_all" || openId === "all" || openId === "@_all";
}

export function checkBotMentioned(params: {
  mentions?: FeishuMentionItem[];
  postMentionedOpenIds?: string[];
  rawContent?: string;
  botOpenId?: string;
}): boolean {
  const botId = params.botOpenId?.trim();
  const mentions = Array.isArray(params.mentions) ? params.mentions : [];
  const postMentioned = Array.isArray(params.postMentionedOpenIds)
    ? params.postMentionedOpenIds
    : [];

  if (botId) {
    for (const mention of mentions) {
      if (isFeishuBroadcastMention(mention)) {
        continue;
      }
      const openId = extractMentionOpenId(mention);
      if (openId === botId) {
        return true;
      }
    }
    if (postMentioned.includes(botId)) {
      return true;
    }
    return false;
  }

  // Without a resolved bot identity, no structured mention can be matched
  // to the bot.
  return false;
}

function extractPostMentionedOpenIds(content: string): string[] {
  const mentioned: string[] = [];
  const seen = new Set<string>();
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const entry of value) {
        visit(entry);
      }
      return;
    }
    if (!isRecord(value)) {
      return;
    }
    if (value.tag === "at") {
      const id =
        (typeof value.user_id === "string" && value.user_id.trim()) ||
        (typeof value.open_id === "string" && value.open_id.trim()) ||
        "";
      if (id && !seen.has(id)) {
        seen.add(id);
        mentioned.push(id);
      }
    }
    for (const entry of Object.values(value)) {
      visit(entry);
    }
  };
  try {
    visit(JSON.parse(content) as unknown);
  } catch {
    return [];
  }
  return mentioned;
}

export function hasUnresolvedGroupMention(
  rawPayload: unknown,
  botOpenId?: string,
): boolean {
  if (botOpenId?.trim()) {
    return false;
  }
  if (!isRecord(rawPayload)) {
    return false;
  }
  const eventObj = isRecord(rawPayload.event)
    ? (rawPayload.event as FeishuMessageReceiveEventPayload)
    : (rawPayload as FeishuMessageReceiveEventPayload);
  const message = eventObj.message;
  if (!message) {
    return false;
  }
  const rawChatType = (message.chat_type ?? "p2p").trim().toLowerCase();
  if (
    rawChatType === "p2p" ||
    rawChatType === "private" ||
    rawChatType === "dm"
  ) {
    return false;
  }
  const mentions = Array.isArray(message.mentions) ? message.mentions : [];
  for (const mention of mentions) {
    if (isFeishuBroadcastMention(mention)) {
      continue;
    }
    if (extractMentionOpenId(mention) || mention.key?.trim()) {
      return true;
    }
  }
  const messageType = (message.message_type ?? message.msg_type ?? "")
    .trim()
    .toLowerCase();
  if (
    (messageType === "post" || messageType === "interactive") &&
    typeof message.content === "string"
  ) {
    const mentionedOpenIds = new Set([
      ...parsePostContent(message.content).mentionedOpenIds,
      ...extractPostMentionedOpenIds(message.content),
    ]);
    return [...mentionedOpenIds].some((id) => {
      const normalized = id.trim().toLowerCase();
      return normalized.length > 0 && normalized !== "all" && normalized !== "@_all";
    });
  }
  return false;
}

/**
 * Normalizes Feishu `@_user_N` mention placeholders in message text:
 * - Strips the bot's own `@_user_N` placeholder so command prefixes
 *   (e.g. `@Bot /bind 123456`) are clean;
 * - Replaces other user `@_user_N` placeholders with `@DisplayName`.
 */
export function normalizeMentions(
  text: string,
  mentions?: FeishuMentionItem[],
  botStripId?: string,
): string {
  if (!text) {
    return "";
  }
  if (!Array.isArray(mentions) || mentions.length === 0) {
    return text.trim();
  }

  const replacements = new Map<string, string>();
  const normalizedBotId = botStripId?.trim();

  for (const mention of mentions) {
    const key = mention.key?.trim();
    const name = mention.name?.trim();
    const openId = extractMentionOpenId(mention);
    const isBot = Boolean(normalizedBotId && openId === normalizedBotId);

    if (!key) continue;
    if (isBot) {
      replacements.set(key, "");
    } else if (key && name) {
      replacements.set(key, `@${name}`);
    }
  }

  return text.replace(
    /(\s*)(@_user_\d+|@_all)(?![A-Za-z0-9_])/g,
    (match, spacing: string, key: string) => {
      const replacement = replacements.get(key);
      if (replacement === undefined) return match;
      return replacement ? `${spacing}${replacement}` : "";
    },
  ).trim();
}

export function parseMessageContent(
  content: string,
  messageType: string,
  botOpenId?: string,
): string {
  if (!content) {
    return "";
  }
  const normalizedType = messageType.trim().toLowerCase();

  if (normalizedType === "post") {
    return parsePostContent(content, botOpenId).textContent;
  }
  if (normalizedType === "interactive") {
    return parseInteractiveCardContent(content, botOpenId);
  }
  if (normalizedType === "merge_forward") {
    return parseMergeForwardContent({
      content,
      formatSubMessage: (subType, subBody) =>
        parseMessageContent(subBody, subType, botOpenId),
    });
  }
  if (normalizedType === "share_chat") {
    try {
      const parsed = JSON.parse(content);
      if (isRecord(parsed)) {
        const label =
          (typeof parsed.chat_name === "string" && parsed.chat_name.trim()) ||
          (typeof parsed.chat_id === "string" && parsed.chat_id.trim()) ||
          "";
        return label ? `[Shared chat: ${label}]` : "[Shared chat]";
      }
    } catch {
      // fall through
    }
    return "[Shared chat]";
  }
  if (
    normalizedType === "image" ||
    normalizedType === "file" ||
    normalizedType === "audio" ||
    normalizedType === "media" ||
    normalizedType === "video" ||
    normalizedType === "sticker"
  ) {
    return "";
  }

  try {
    const parsed = JSON.parse(content);
    if (isRecord(parsed) && typeof parsed.text === "string") {
      return parsed.text;
    }
    return content;
  } catch {
    return content;
  }
}

export function parseMediaKeys(
  content: string,
  messageType: string,
): {
  imageKey?: string;
  fileKey?: string;
  fileName?: string;
  fileSize?: number;
  mimeType?: string;
} {
  try {
    const parsed = JSON.parse(content);
    if (!isRecord(parsed)) {
      return {};
    }
    const imageKey =
      typeof parsed.image_key === "string" && parsed.image_key.trim()
        ? parsed.image_key.trim()
        : undefined;
    const fileKey =
      typeof parsed.file_key === "string" && parsed.file_key.trim()
        ? parsed.file_key.trim()
        : undefined;
    const fileName =
      typeof parsed.file_name === "string" && parsed.file_name.trim()
        ? parsed.file_name.trim()
        : undefined;
    const fileSize =
      typeof parsed.file_size === "number" && Number.isFinite(parsed.file_size)
        ? parsed.file_size
        : undefined;
    const mimeType =
      typeof parsed.mime_type === "string" && parsed.mime_type.trim()
        ? parsed.mime_type.trim()
        : undefined;

    switch (messageType.trim().toLowerCase()) {
      case "image":
        return { imageKey, fileName, fileSize, mimeType };
      case "file":
      case "audio":
      case "sticker":
        return { fileKey, fileName, fileSize, mimeType };
      case "media":
      case "video":
        return { imageKey, fileKey, fileName, fileSize, mimeType };
      default:
        return { imageKey, fileKey, fileName, fileSize, mimeType };
    }
  } catch {
    return {};
  }
}

export function extractInboundAttachments(
  content: string,
  messageType: string,
): InboundAttachmentDescriptor[] {
  if (!content) {
    return [];
  }
  const normalizedType = messageType.trim().toLowerCase();

  if (normalizedType === "post") {
    const post = parsePostContent(content);
    const descriptors: InboundAttachmentDescriptor[] = [];
    for (let idx = 0; idx < post.imageKeys.length; idx += 1) {
      const imageKey = post.imageKeys[idx]!;
      descriptors.push({
        id: imageKey,
        filename: `image_${idx + 1}.png`,
        mimeType: "image/png",
      });
    }
    for (let idx = 0; idx < post.mediaKeys.length; idx += 1) {
      const media = post.mediaKeys[idx]!;
      descriptors.push({
        id: media.fileKey,
        filename: media.fileName || `media_${idx + 1}.mp4`,
        mimeType: "video/mp4",
      });
    }
    for (let idx = 0; idx < post.files.length; idx += 1) {
      const file = post.files[idx]!;
      const filename = file.fileName || `${file.fileKey}.bin`;
      descriptors.push({
        id: file.fileKey,
        filename,
        mimeType: filename.toLowerCase().endsWith(".pdf")
          ? "application/pdf"
          : "application/octet-stream",
      });
    }
    return descriptors;
  }

  const keys = parseMediaKeys(content, normalizedType);
  if (normalizedType === "image" && keys.imageKey) {
    return [
      {
        id: keys.imageKey,
        filename: keys.fileName || `${keys.imageKey}.png`,
        mimeType: keys.mimeType || "image/png",
        sizeBytes: keys.fileSize,
      },
    ];
  }
  if (normalizedType === "sticker" && (keys.fileKey || keys.imageKey)) {
    const id = (keys.fileKey ?? keys.imageKey)!;
    return [
      {
        id,
        filename: keys.fileName || `${id}.png`,
        mimeType: keys.mimeType || "image/png",
        sizeBytes: keys.fileSize,
      },
    ];
  }
  if (normalizedType === "audio" && keys.fileKey) {
    return [
      {
        id: keys.fileKey,
        filename: keys.fileName || `${keys.fileKey}.opus`,
        mimeType: keys.mimeType || "audio/ogg",
        sizeBytes: keys.fileSize,
      },
    ];
  }
  if ((normalizedType === "media" || normalizedType === "video") && keys.fileKey) {
    return [
      {
        id: keys.fileKey,
        filename: keys.fileName || `${keys.fileKey}.mp4`,
        mimeType: keys.mimeType || "video/mp4",
        sizeBytes: keys.fileSize,
      },
    ];
  }
  if (normalizedType === "file" && keys.fileKey) {
    return [
      {
        id: keys.fileKey,
        filename: keys.fileName || `${keys.fileKey}.bin`,
        mimeType: keys.mimeType || "application/octet-stream",
        sizeBytes: keys.fileSize,
      },
    ];
  }
  return [];
}

const FEISHU_EXTERNAL_KEY_PATTERN =
  /^(?!\s)(?![\s\S]*\s$)(?![\s\S]*\.\.)[^\p{Cc}\p{Cs}/\\]{1,512}$/u;

function normalizeFeishuExternalKey(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim();
  return FEISHU_EXTERNAL_KEY_PATTERN.test(normalized) ? normalized : undefined;
}

function parseStrictNonNegativeInteger(value: unknown): number | undefined {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value >= 0 ? value : undefined;
  }
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) {
    return undefined;
  }
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function resolvePostMediaParts(content?: string): string[] {
  if (!content) {
    return [];
  }
  const { imageKeys, mediaKeys } = parsePostContent(content);
  return [
    ...imageKeys
      .map((key) => normalizeFeishuExternalKey(key))
      .filter((key): key is string => Boolean(key))
      .map((key) => `image_key:${key}`),
    ...mediaKeys
      .map(({ fileKey }) => normalizeFeishuExternalKey(fileKey))
      .filter((key): key is string => Boolean(key))
      .map((key) => `file_key:${key}`),
  ];
}

function resolveMessageMediaParts(
  messageType: string,
  content: string,
): string[] {
  if (messageType === "post") {
    return resolvePostMediaParts(content);
  }
  try {
    const parsed = JSON.parse(content);
    if (!isRecord(parsed)) {
      return [];
    }
    const imageKey = normalizeFeishuExternalKey(parsed.image_key);
    const fileKey = normalizeFeishuExternalKey(parsed.file_key);
    switch (messageType) {
      case "image":
        return imageKey ? [`image_key:${imageKey}`] : [];
      case "file":
      case "audio":
      case "sticker":
        return fileKey ? [`file_key:${fileKey}`] : [];
      default:
        return fileKey
          ? [`file_key:${fileKey}`]
          : imageKey
            ? [`image_key:${imageKey}`]
            : [];
    }
  } catch {
    return [];
  }
}

function resolveSenderIdentity(
  eventObj: FeishuMessageReceiveEventPayload,
): string | undefined {
  const senderId = eventObj.sender?.sender_id;
  return (
    senderId?.open_id?.trim() ||
    senderId?.union_id?.trim() ||
    senderId?.user_id?.trim() ||
    undefined
  );
}

function resolveTextRetryDedupeKey(
  eventObj: FeishuMessageReceiveEventPayload,
): string | undefined {
  const message = eventObj.message;
  if (!message || typeof message.content !== "string") {
    return undefined;
  }
  const createTime = message.create_time?.trim();
  const chatId = message.chat_id?.trim();
  const senderId = resolveSenderIdentity(eventObj);
  if (
    !createTime ||
    parseStrictNonNegativeInteger(createTime) === undefined ||
    !chatId ||
    !senderId
  ) {
    return undefined;
  }
  const contentHash = crypto
    .createHash("sha256")
    .update(message.content, "utf8")
    .digest("hex")
    .slice(0, 32);
  return JSON.stringify([
    "text-retry",
    senderId,
    chatId,
    createTime,
    contentHash,
  ]);
}

/**
 * Resolves the canonical deduplication key from a raw Feishu
 * `im.message.receive_v1` event payload, matching
 * `monitor.account-UbN1RhlP.mjs:822-872` (`codex-4`, `grok 高-3`).
 */
export function resolveFeishuMessageDedupeKey(
  rawPayload: unknown,
  _accountId?: string,
): string | undefined {
  if (!isRecord(rawPayload)) {
    return undefined;
  }
  const eventObj = (
    isRecord(rawPayload.event) ? rawPayload.event : rawPayload
  ) as FeishuMessageReceiveEventPayload;
  const message = isRecord(eventObj.message)
    ? (eventObj.message as FeishuRawMessage)
    : undefined;
  const messageId =
    typeof message?.message_id === "string"
      ? message.message_id.trim()
      : undefined;
  if (!messageId) {
    return undefined;
  }

  const messageType = (
    typeof message?.message_type === "string"
      ? message.message_type
      : typeof message?.msg_type === "string"
        ? message.msg_type
        : ""
  ).trim();
  const content = typeof message?.content === "string" ? message.content : "";
  const mediaParts = resolveMessageMediaParts(messageType, content);
  if (mediaParts.length > 0) {
    return JSON.stringify([messageId, ...mediaParts]);
  }
  if (messageType === "text") {
    return resolveTextRetryDedupeKey(eventObj) ?? messageId;
  }
  if (messageType === "post") {
    const retryKey = resolveTextRetryDedupeKey(eventObj);
    if (!retryKey) {
      return messageId;
    }
    const topicId =
      (typeof message?.root_id === "string" && message.root_id.trim()) ||
      (typeof message?.thread_id === "string" && message.thread_id.trim()) ||
      undefined;
    return topicId ? JSON.stringify([retryKey, topicId]) : retryKey;
  }
  return messageId;
}

/**
 * Parses a raw Feishu `im.message.receive_v1` payload into a Dalton
 * `InboundChannelEvent` (`src/types.ts`). Returns `null` if the payload is
 * malformed or authored by the bot itself.
 */
export function parseFeishuInboundEvent(
  rawPayload: unknown,
  options: { botOpenId?: string } = {},
): InboundChannelEvent | null {
  if (!isRecord(rawPayload)) {
    return null;
  }
  const eventObj = isRecord(rawPayload.event)
    ? (rawPayload.event as FeishuMessageReceiveEventPayload)
    : (rawPayload as FeishuMessageReceiveEventPayload);

  const sender = eventObj.sender;
  const message = eventObj.message;
  if (!sender || !message) {
    return null;
  }

  if (sender.sender_type === "app") {
    return null;
  }

  const messageId = message.message_id?.trim() ?? "";
  const chatId = message.chat_id?.trim() ?? "";
  const senderOpenId =
    sender.sender_id?.open_id?.trim() ||
    sender.sender_id?.user_id?.trim() ||
    "";

  if (!messageId || !chatId || !senderOpenId) {
    return null;
  }

  if (options.botOpenId && senderOpenId === options.botOpenId.trim()) {
    return null;
  }

  const rawChatType = (message.chat_type ?? "p2p").trim().toLowerCase();
  const chatType: "dm" | "group" =
    rawChatType === "p2p" || rawChatType === "private" || rawChatType === "dm"
      ? "dm"
      : "group";

  const messageType = (
    message.message_type ??
    message.msg_type ??
    "text"
  ).trim();
  const normalizedMessageType = messageType.toLowerCase();
  const rawContent = typeof message.content === "string" ? message.content : "";

  const postParsed =
    normalizedMessageType === "post" || normalizedMessageType === "interactive"
      ? parsePostContent(rawContent, options.botOpenId)
      : undefined;

  const mentionsBot = checkBotMentioned({
    mentions: message.mentions,
    postMentionedOpenIds:
      postParsed !== undefined
        ? [
            ...new Set([
              ...postParsed.mentionedOpenIds,
              ...extractPostMentionedOpenIds(rawContent),
            ]),
          ]
        : undefined,
    rawContent,
    botOpenId: options.botOpenId,
  });

  const parsedContentText =
    normalizedMessageType === "post" && postParsed !== undefined
      ? postParsed.textContent
      : parseMessageContent(rawContent, messageType, options.botOpenId);
  const text = normalizeMentions(
    parsedContentText,
    message.mentions,
    options.botOpenId,
  );
  const rawCommandCandidate =
    normalizedMessageType === "post" && postParsed !== undefined
      ? (postParsed.commandText ?? postParsed.textContent)
      : normalizedMessageType === "interactive" &&
          postParsed !== undefined &&
          postParsed.textContent.trim() !== "[Rich text message]" &&
          parsedContentText === postParsed.textContent.trim()
        ? (postParsed.commandText ?? parsedContentText)
        : parsedContentText;
  const commandText = normalizeMentions(
    rawCommandCandidate,
    message.mentions,
    options.botOpenId,
  );

  const attachments = extractInboundAttachments(rawContent, messageType);

  return {
    platform: "feishu",
    platformUserId: senderOpenId,
    platformUsername: sender.name?.trim() || undefined,
    platformTenantKey: sender.tenant_key?.trim() || undefined,
    chatType,
    chatAddress: buildDaltonFeishuChatAddress({ chatType, chatId }),
    chatId,
    threadScopeKey: buildDaltonFeishuThreadScopeKey({
      chatType,
      chatId,
      senderOpenId,
      threadId: message.thread_id,
    }),
    messageId,
    text,
    commandText,
    isGroupTrigger: chatType === "group" ? mentionsBot : undefined,
    attachments: attachments.length > 0 ? attachments : undefined,
    rawPayload: rawPayload as Record<string, unknown>,
  };
}
