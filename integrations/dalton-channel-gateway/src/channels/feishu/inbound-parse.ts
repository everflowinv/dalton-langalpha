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
import { escapeRegExp } from "../../shared/text.js";
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
    if (
      typeof params.rawContent === "string" &&
      params.rawContent.includes(`@${botId}`)
    ) {
      return true;
    }
    return false;
  }

  // If botOpenId was not resolved from probe, only treat explicit non-broadcast
  // mentions with `@_user_` placeholder as mentioning when there is a single bot mention
  return false;
}

/**
 * Normalizes Feishu `@_user_N` mention placeholders in message text:
 * - Strips the bot's own `@_user_N` / `@BotName` mention so command prefixes
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

  let result = text;
  const normalizedBotId = botStripId?.trim();

  for (const mention of mentions) {
    const key = mention.key?.trim();
    const name = mention.name?.trim();
    const openId = extractMentionOpenId(mention);
    const isBot = Boolean(normalizedBotId && openId === normalizedBotId);

    if (isBot) {
      if (key) {
        result = result.replace(
          new RegExp(`\\s*${escapeRegExp(key)}\\s*`, "g"),
          " ",
        );
      }
      if (name) {
        result = result.replace(
          new RegExp(`@${escapeRegExp(name)}\\s*`, "g"),
          "",
        );
      }
    } else if (key && name) {
      result = result.replace(new RegExp(escapeRegExp(key), "g"), `@${name}`);
    }
  }

  return result.trim();
}

export function parseMessageContent(
  content: string,
  messageType: string,
): string {
  if (!content) {
    return "";
  }
  const normalizedType = messageType.trim().toLowerCase();

  if (normalizedType === "post") {
    return parsePostContent(content).textContent;
  }
  if (normalizedType === "interactive") {
    return parseInteractiveCardContent(content);
  }
  if (normalizedType === "merge_forward") {
    return parseMergeForwardContent({
      content,
      formatSubMessage: (subType, subBody) =>
        parseMessageContent(subBody, subType),
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

function resolvePostMediaParts(content?: string): string[] {
  if (!content) {
    return [];
  }
  const { imageKeys, mediaKeys } = parsePostContent(content);
  return [
    ...imageKeys.map((key) => `img:${key}`),
    ...mediaKeys.map(({ fileKey }) => `media:${fileKey}`),
  ];
}

function resolveMessageMediaParts(
  message: FeishuRawMessage | undefined,
): string[] {
  if (!message) {
    return [];
  }
  const messageType = (
    message.message_type ??
    message.msg_type ??
    ""
  ).trim().toLowerCase();
  const content = typeof message.content === "string" ? message.content : undefined;
  if (messageType === "post") {
    return resolvePostMediaParts(content);
  }
  if (
    !content ||
    !["image", "file", "audio", "video", "media", "sticker"].includes(
      messageType,
    )
  ) {
    return [];
  }
  const mediaKeys = parseMediaKeys(content, messageType);
  return [
    mediaKeys.imageKey ? `img:${mediaKeys.imageKey}` : undefined,
    mediaKeys.fileKey ? `file:${mediaKeys.fileKey}` : undefined,
  ].filter((part): part is string => Boolean(part));
}

function resolveTextRetryDedupeKey(
  eventObj: FeishuMessageReceiveEventPayload,
  prefix: string,
): string | undefined {
  const message = eventObj.message;
  if (!message) {
    return undefined;
  }
  const messageType = (
    message.message_type ??
    message.msg_type ??
    ""
  ).trim().toLowerCase();
  if (messageType !== "text" && messageType !== "post") {
    return undefined;
  }
  const senderId =
    eventObj.sender?.sender_id?.open_id?.trim() ||
    eventObj.sender?.sender_id?.user_id?.trim();
  const chatId = message.chat_id?.trim();
  const createTime = message.create_time?.trim();
  const content = typeof message.content === "string" ? message.content.trim() : "";
  if (!senderId || !chatId || !createTime || !content) {
    return undefined;
  }
  const contentDigest = crypto
    .createHash("sha256")
    .update(content)
    .digest("hex")
    .slice(0, 16);
  return `${prefix}:text:${senderId}:${chatId}:${createTime}:${contentDigest}`;
}

/**
 * Resolves the canonical deduplication key from a raw Feishu
 * `im.message.receive_v1` event payload, matching
 * `monitor.account-UbN1RhlP.mjs:803-872` (`grok-11`).
 */
export function resolveFeishuMessageDedupeKey(
  rawPayload: unknown,
  accountId?: string,
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
    (typeof message?.message_id === "string" && message.message_id.trim()) ||
    (typeof rawPayload.message_id === "string" && rawPayload.message_id.trim()) ||
    undefined;

  const prefix = accountId?.trim() ? `feishu:${accountId.trim()}` : "feishu";
  const messageType = (
    message?.message_type ??
    message?.msg_type ??
    ""
  ).trim().toLowerCase();
  const mediaParts = resolveMessageMediaParts(message);

  if (messageType === "post") {
    const postKeySuffix = [
      message?.root_id?.trim() || message?.thread_id?.trim() || undefined,
      mediaParts.length > 0 ? mediaParts.join("|") : undefined,
    ]
      .filter((part): part is string => Boolean(part))
      .join(":");
    if (postKeySuffix) {
      return messageId
        ? `${prefix}:${messageId}:post:${postKeySuffix}`
        : `${prefix}:post:${postKeySuffix}`;
    }
    if (!messageId) {
      return resolveTextRetryDedupeKey(eventObj, prefix);
    }
    return `${prefix}:${messageId}`;
  }

  if (mediaParts.length > 0) {
    return `${prefix}:media:${messageType}:${mediaParts.join("|")}`;
  }

  if (!messageId) {
    return resolveTextRetryDedupeKey(eventObj, prefix);
  }

  return `${prefix}:${messageId}`;
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
  const rawContent = typeof message.content === "string" ? message.content : "";

  const postParsed =
    messageType.toLowerCase() === "post"
      ? parsePostContent(rawContent)
      : undefined;

  const mentionsBot = checkBotMentioned({
    mentions: message.mentions,
    postMentionedOpenIds: postParsed?.mentionedOpenIds,
    rawContent,
    botOpenId: options.botOpenId,
  });

  const parsedContentText = parseMessageContent(rawContent, messageType);
  const text = normalizeMentions(
    parsedContentText,
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
    isGroupTrigger: chatType === "group" ? mentionsBot : undefined,
    attachments: attachments.length > 0 ? attachments : undefined,
    rawPayload: rawPayload as Record<string, unknown>,
  };
}
