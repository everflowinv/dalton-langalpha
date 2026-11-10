// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import { createHash } from "node:crypto";
import path from "node:path";
import type {
  InboundAttachmentDescriptor,
  InboundChannelEvent,
} from "../../types.js";
import {
  MessageItemType,
  type CDNMedia,
  type MessageItem,
  type RefMessage,
  type WeixinMessage,
  type WeixinPersistedAttachmentMeta,
} from "./types.js";

const EXT_TO_MIME: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".csv": "text/csv",
  ".json": "application/json",
  ".mp4": "video/mp4",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".doc": "application/msword",
  ".docx":
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xls": "application/vnd.ms-excel",
  ".xlsx":
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".ppt": "application/vnd.ms-powerpoint",
  ".pptx":
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".zip": "application/zip",
};

function guessMimeType(fileName: string | undefined, fallback: string): string {
  if (!fileName) return fallback;
  const ext = path.extname(fileName).toLowerCase();
  return EXT_TO_MIME[ext] ?? fallback;
}

export function getMediaLabel(type: number | undefined): string {
  switch (type) {
    case MessageItemType.IMAGE:
      return "[图片]";
    case MessageItemType.VIDEO:
      return "[视频]";
    case MessageItemType.FILE:
      return "[文件]";
    case MessageItemType.VOICE:
      return "[语音]";
    default:
      return "";
  }
}

export function getWeixinRawMessageId(msg: WeixinMessage): string | undefined {
  for (const candidate of [msg.message_id, msg.msg_id, msg.svr_id]) {
    const str =
      typeof candidate === "string"
        ? candidate.trim()
        : candidate !== undefined && candidate !== null
          ? String(candidate).trim()
          : "";
    if (str) return str;
  }

  for (const item of msg.item_list ?? []) {
    const itemId =
      typeof item.msg_id === "string"
        ? item.msg_id.trim()
        : item.msg_id !== undefined && item.msg_id !== null
          ? String(item.msg_id).trim()
          : "";
    if (itemId) return itemId;
  }

  if (msg.seq !== undefined && msg.seq !== null && String(msg.seq).trim() !== "") {
    return `seq:${String(msg.seq).trim()}`;
  }
  return undefined;
}

function canonicalizeMessageForInvalidHash(msg: WeixinMessage): string {
  const normalizedItems = (msg.item_list ?? []).map((item) => ({
    type: item.type ?? null,
    msg_id: item.msg_id ?? null,
    create_time_ms: item.create_time_ms ?? null,
    text: item.text_item?.text ?? null,
    voice_text: item.voice_item?.text ?? null,
    file_name: item.file_item?.file_name ?? null,
    file_len: item.file_item?.len ?? null,
    image_aeskey: item.image_item?.aeskey ?? null,
    media_param:
      item.image_item?.media?.encrypt_query_param ??
      item.file_item?.media?.encrypt_query_param ??
      item.video_item?.media?.encrypt_query_param ??
      item.voice_item?.media?.encrypt_query_param ??
      null,
  }));
  return JSON.stringify({
    from_user_id: msg.from_user_id ?? "",
    to_user_id: msg.to_user_id ?? "",
    group_id: msg.group_id ?? "",
    client_id: msg.client_id ?? "",
    session_id: msg.session_id ?? "",
    seq: msg.seq ?? null,
    create_time_ms: msg.create_time_ms ?? null,
    items: normalizedItems,
  });
}

/**
 * Build a deterministic, bot-scoped internal message ID.
 * - Includes the stable `ilink_bot_id` so re-login of the same Bot preserves dedup IDs
 *   while switching to a different Bot does not collide.
 * - When a message lacks a valid platform message ID or sender, generates a deterministic
 *   rejection key from the canonical payload (never random).
 */
export function buildWeixinScopedMessageId(
  botId: string,
  msg: WeixinMessage,
): string {
  const normalizedBotId =
    (botId || "weixin:unconfigured").trim() || "weixin:unconfigured";
  const rawId = getWeixinRawMessageId(msg);
  const fromUserId =
    typeof msg.from_user_id === "string" ? msg.from_user_id.trim() : "";
  if (rawId && fromUserId) {
    return `${normalizedBotId}:${rawId}`;
  }
  const digest = createHash("sha256")
    .update(canonicalizeMessageForInvalidHash(msg))
    .digest("hex")
    .slice(0, 32);
  return `${normalizedBotId}:invalid:${digest}`;
}

export function isWeixinGroupMessage(msg: WeixinMessage): boolean {
  if (typeof msg.group_id === "string" && msg.group_id.trim().length > 0) {
    return true;
  }
  if (
    typeof msg.chat_room_id === "string" &&
    msg.chat_room_id.trim().length > 0
  ) {
    return true;
  }
  if (typeof msg.room_id === "string" && msg.room_id.trim().length > 0) {
    return true;
  }
  const fromId =
    typeof msg.from_user_id === "string" ? msg.from_user_id.trim() : "";
  const toId = typeof msg.to_user_id === "string" ? msg.to_user_id.trim() : "";
  if (fromId.endsWith("@chatroom") || toId.endsWith("@chatroom")) {
    return true;
  }
  return false;
}

function extractSingleItemPreview(item: MessageItem | undefined): string {
  if (!item) return "";
  if (item.type === MessageItemType.TEXT && item.text_item?.text != null) {
    return String(item.text_item.text).trim();
  }
  if (item.type === MessageItemType.VOICE && item.voice_item?.text) {
    return String(item.voice_item.text).trim();
  }
  return getMediaLabel(item.type);
}

/**
 * Extract inline quote content from `ref_msg`.
 * ID-only quotes without inline content (`title` or `message_item`) are ignored
 * without querying any external quote store.
 */
export function extractInlineQuote(ref: RefMessage | undefined): {
  replyToId?: string;
  quotedText?: string;
} {
  if (!ref) return {};
  const rawReplyId =
    ref.svr_id ?? ref.message_id ?? ref.msg_id ?? ref.message_item?.msg_id;
  const replyToId =
    rawReplyId !== undefined && rawReplyId !== null
      ? String(rawReplyId).trim() || undefined
      : undefined;

  const parts: string[] = [];
  if (typeof ref.title === "string" && ref.title.trim().length > 0) {
    parts.push(ref.title.trim());
  }
  if (ref.message_item) {
    const itemPreview = extractSingleItemPreview(ref.message_item);
    if (itemPreview) {
      parts.push(itemPreview);
    }
  }
  const quotedText = parts.length > 0 ? parts.join(" | ") : undefined;
  return {
    ...(replyToId ? { replyToId } : {}),
    ...(quotedText ? { quotedText } : {}),
  };
}

function parseNumericSize(val: unknown): number | undefined {
  if (typeof val === "number" && Number.isFinite(val) && val >= 0) {
    return Math.trunc(val);
  }
  if (typeof val === "string" && /^\d+$/.test(val.trim())) {
    const parsed = Number.parseInt(val.trim(), 10);
    if (Number.isSafeInteger(parsed) && parsed >= 0) {
      return parsed;
    }
  }
  return undefined;
}

function hasCdnReference(media: CDNMedia | undefined): media is CDNMedia {
  if (!media) return false;
  const hasParam =
    typeof media.encrypt_query_param === "string" &&
    media.encrypt_query_param.trim().length > 0;
  const hasFullUrl =
    typeof media.full_url === "string" && media.full_url.trim().length > 0;
  return hasParam || hasFullUrl;
}

export interface ParseWeixinInboundOptions {
  ilinkBotId: string;
  botGeneration: number;
  /** Context order is `msg.seq` only. This field is not a sequence. */
  batchSeq?: number;
}

export function parseWeixinInboundMessage(
  msg: WeixinMessage,
  opts: ParseWeixinInboundOptions,
): InboundChannelEvent {
  const ilinkBotId =
    (opts.ilinkBotId || "weixin:unconfigured").trim() || "weixin:unconfigured";
  const botGeneration = opts.botGeneration >= 1 ? opts.botGeneration : 1;
  const messageId = buildWeixinScopedMessageId(ilinkBotId, msg);
  const fromUserId =
    typeof msg.from_user_id === "string" ? msg.from_user_id.trim() : "";
  const rawMessageId = getWeixinRawMessageId(msg);

  const protocolSeq = parseNumericSize(msg.seq);
  const contextSeq =
    protocolSeq !== undefined && protocolSeq > 0 ? protocolSeq : undefined;

  if (isWeixinGroupMessage(msg)) {
    const groupId =
      (typeof msg.group_id === "string" && msg.group_id.trim()) ||
      (typeof msg.chat_room_id === "string" && msg.chat_room_id.trim()) ||
      (typeof msg.room_id === "string" && msg.room_id.trim()) ||
      fromUserId ||
      "unknown_group";
    return {
      platform: "weixin",
      platformUserId: fromUserId || "unknown_user",
      chatType: "group",
      chatAddress: `weixin:${groupId}`,
      chatId: groupId,
      threadScopeKey: `chat:${groupId}`,
      messageId,
      text: "",
      commandText: "",
      isGroupTrigger: false,
      attachments: [],
      weixinBotId: ilinkBotId,
      weixinBotGeneration: botGeneration,
      ...(contextSeq !== undefined ? { weixinContextSeq: contextSeq } : {}),
      rawPayload: {
        platform_user_id: fromUserId || "unknown_user",
        ilink_bot_id: ilinkBotId,
        bot_generation: botGeneration,
        raw_message_id: rawMessageId ?? null,
        group_id: groupId,
        chat_type: "group",
        rejected_reason: "weixin_group_unsupported",
      },
    };
  }

  if (!rawMessageId || !fromUserId) {
    return {
      platform: "weixin",
      platformUserId: fromUserId,
      chatType: "dm",
      chatAddress: "weixin:@me",
      chatId: fromUserId || "invalid",
      threadScopeKey: fromUserId ? `dm:${fromUserId}` : "dm:invalid",
      messageId,
      text: "",
      commandText: "",
      attachments: [],
      weixinBotId: ilinkBotId,
      weixinBotGeneration: botGeneration,
      ...(contextSeq !== undefined ? { weixinContextSeq: contextSeq } : {}),
      rawPayload: {
        platform_user_id: fromUserId,
        ilink_bot_id: ilinkBotId,
        bot_generation: botGeneration,
        raw_message_id: rawMessageId ?? null,
        invalid_inbound_event: true,
        rejected_reason: "invalid_inbound_event",
      },
    };
  }

  const textSegments: string[] = [];
  const attachments: InboundAttachmentDescriptor[] = [];
  const persistedAttachments: WeixinPersistedAttachmentMeta[] = [];
  let voiceWithoutTranscription = false;
  let inlineQuote: { replyToId?: string; quotedText?: string } = {};

  for (let i = 0; i < (msg.item_list ?? []).length; i++) {
    const item = msg.item_list![i]!;
    if (item.ref_msg && !inlineQuote.quotedText && !inlineQuote.replyToId) {
      inlineQuote = extractInlineQuote(item.ref_msg);
    }

    if (item.type === MessageItemType.TEXT) {
      const rawText = item.text_item?.text;
      if (rawText !== undefined && rawText !== null && String(rawText).length > 0) {
        textSegments.push(String(rawText));
      }
      continue;
    }

    if (item.type === MessageItemType.VOICE) {
      const transcribed = item.voice_item?.text?.trim();
      if (transcribed) {
        textSegments.push(transcribed);
      } else {
        voiceWithoutTranscription = true;
      }
      continue;
    }

    if (item.type === MessageItemType.IMAGE) {
      const media = hasCdnReference(item.image_item?.media)
        ? item.image_item!.media!
        : hasCdnReference(item.image_item?.thumb_media)
          ? item.image_item!.thumb_media!
          : undefined;
      if (media) {
        const attId = `${messageId}:att:${persistedAttachments.length}`;
        const filename = `image_${persistedAttachments.length + 1}.jpg`;
        const sizeBytes =
          parseNumericSize(item.image_item?.mid_size) ??
          parseNumericSize(item.image_item?.hd_size);
        const meta: WeixinPersistedAttachmentMeta = {
          id: attId,
          kind: "image",
          filename,
          mimeType: "image/jpeg",
          ...(sizeBytes !== undefined ? { sizeBytes } : {}),
          ...(media.encrypt_query_param
            ? { encryptQueryParam: media.encrypt_query_param }
            : {}),
          ...(media.full_url ? { fullUrl: media.full_url } : {}),
          ...(media.aes_key ? { aesKeyBase64: media.aes_key } : {}),
          ...(item.image_item?.aeskey ? { aesKeyHex: item.image_item.aeskey } : {}),
        };
        persistedAttachments.push(meta);
        attachments.push({
          id: attId,
          filename,
          mimeType: "image/jpeg",
          ...(sizeBytes !== undefined ? { sizeBytes } : {}),
        });
      }
      continue;
    }

    if (item.type === MessageItemType.FILE) {
      const media = hasCdnReference(item.file_item?.media)
        ? item.file_item!.media!
        : undefined;
      if (media) {
        const attId = `${messageId}:att:${persistedAttachments.length}`;
        const filename =
          item.file_item?.file_name?.trim() ||
          `file_${persistedAttachments.length + 1}.bin`;
        const mimeType = guessMimeType(filename, "application/octet-stream");
        const sizeBytes = parseNumericSize(item.file_item?.len);
        const meta: WeixinPersistedAttachmentMeta = {
          id: attId,
          kind: "file",
          filename,
          mimeType,
          ...(sizeBytes !== undefined ? { sizeBytes } : {}),
          ...(media.encrypt_query_param
            ? { encryptQueryParam: media.encrypt_query_param }
            : {}),
          ...(media.full_url ? { fullUrl: media.full_url } : {}),
          ...(media.aes_key ? { aesKeyBase64: media.aes_key } : {}),
        };
        persistedAttachments.push(meta);
        attachments.push({
          id: attId,
          filename,
          mimeType,
          ...(sizeBytes !== undefined ? { sizeBytes } : {}),
        });
      }
      continue;
    }

    if (item.type === MessageItemType.VIDEO) {
      const media = hasCdnReference(item.video_item?.media)
        ? item.video_item!.media!
        : undefined;
      if (media) {
        const attId = `${messageId}:att:${persistedAttachments.length}`;
        const filename = `video_${persistedAttachments.length + 1}.mp4`;
        const sizeBytes = parseNumericSize(item.video_item?.video_size);
        const meta: WeixinPersistedAttachmentMeta = {
          id: attId,
          kind: "video",
          filename,
          mimeType: "video/mp4",
          ...(sizeBytes !== undefined ? { sizeBytes } : {}),
          ...(media.encrypt_query_param
            ? { encryptQueryParam: media.encrypt_query_param }
            : {}),
          ...(media.full_url ? { fullUrl: media.full_url } : {}),
          ...(media.aes_key ? { aesKeyBase64: media.aes_key } : {}),
        };
        persistedAttachments.push(meta);
        attachments.push({
          id: attId,
          filename,
          mimeType: "video/mp4",
          ...(sizeBytes !== undefined ? { sizeBytes } : {}),
        });
      }
      continue;
    }
  }

  const userText = textSegments.join("\n").trim();
  const formattedQuote = inlineQuote.quotedText
    ? `> [引用] ${inlineQuote.quotedText}`
    : "";
  const combinedModelText =
    formattedQuote && userText
      ? `${formattedQuote}\n\n${userText}`
      : formattedQuote || userText;

  const contextToken =
    typeof msg.context_token === "string" && msg.context_token.trim().length > 0
      ? msg.context_token.trim()
      : undefined;

  return {
    platform: "weixin",
    platformUserId: fromUserId,
    chatType: "dm",
    chatAddress: "weixin:@me",
    chatId: fromUserId,
    threadScopeKey: `dm:${fromUserId}`,
    messageId,
    text: combinedModelText,
    commandText: userText,
    attachments,
    ...(contextToken ? { weixinContextToken: contextToken } : {}),
    weixinBotId: ilinkBotId,
    weixinBotGeneration: botGeneration,
    ...(contextSeq !== undefined ? { weixinContextSeq: contextSeq } : {}),
    ...(voiceWithoutTranscription ? { voiceWithoutTranscription: true } : {}),
    rawPayload: {
      platform_user_id: fromUserId,
      ilink_bot_id: ilinkBotId,
      bot_generation: botGeneration,
      raw_message_id: rawMessageId,
      ...(contextSeq !== undefined
        ? { seq: contextSeq, weixin_context_seq: contextSeq }
        : {}),
      ...(msg.create_time_ms !== undefined
        ? { create_time_ms: msg.create_time_ms }
        : {}),
      ...(msg.session_id ? { session_id: msg.session_id } : {}),
      ...(inlineQuote.replyToId || inlineQuote.quotedText
        ? {
            quoted_message: {
              ...(inlineQuote.replyToId ? { id: inlineQuote.replyToId } : {}),
              ...(inlineQuote.quotedText ? { text: inlineQuote.quotedText } : {}),
            },
          }
        : {}),
      ...(voiceWithoutTranscription ? { voice_without_transcription: true } : {}),
      ...(persistedAttachments.length > 0
        ? {
            attachments: persistedAttachments,
            weixin_attachments: persistedAttachments,
          }
        : {}),
    },
  };
}

/**
 * Reconstruct a persisted attachment metadata record from `event.rawPayload`
 * by attachment ID (or fallback to single attachment).
 */
export function resolvePersistedAttachmentMeta(
  descriptor: InboundAttachmentDescriptor,
  event: InboundChannelEvent,
): WeixinPersistedAttachmentMeta | undefined {
  const raw = event.rawPayload as Record<string, unknown> | undefined;
  const candidates: unknown[] = [];
  if (Array.isArray(raw?.weixin_attachments)) {
    candidates.push(...raw!.weixin_attachments);
  }
  if (Array.isArray(raw?.attachments)) {
    candidates.push(...raw!.attachments);
  }
  for (const entry of candidates) {
    if (entry && typeof entry === "object") {
      const candidate = entry as Partial<WeixinPersistedAttachmentMeta> & {
        name?: string;
      };
      if (
        candidate.id === descriptor.id &&
        (candidate.encryptQueryParam || candidate.fullUrl)
      ) {
        return {
          id: candidate.id,
          kind: candidate.kind ?? "file",
          filename: candidate.filename ?? candidate.name ?? descriptor.filename,
          mimeType: candidate.mimeType ?? descriptor.mimeType,
          sizeBytes: candidate.sizeBytes ?? descriptor.sizeBytes,
          encryptQueryParam: candidate.encryptQueryParam,
          fullUrl: candidate.fullUrl,
          aesKeyHex: candidate.aesKeyHex,
          aesKeyBase64: candidate.aesKeyBase64,
        };
      }
    }
  }
  for (const entry of candidates) {
    if (entry && typeof entry === "object") {
      const candidate = entry as Partial<WeixinPersistedAttachmentMeta> & {
        name?: string;
      };
      if (candidate.encryptQueryParam || candidate.fullUrl) {
        return {
          id: candidate.id ?? descriptor.id,
          kind: candidate.kind ?? "file",
          filename: candidate.filename ?? candidate.name ?? descriptor.filename,
          mimeType: candidate.mimeType ?? descriptor.mimeType,
          sizeBytes: candidate.sizeBytes ?? descriptor.sizeBytes,
          encryptQueryParam: candidate.encryptQueryParam,
          fullUrl: candidate.fullUrl,
          aesKeyHex: candidate.aesKeyHex,
          aesKeyBase64: candidate.aesKeyBase64,
        };
      }
    }
  }
  return undefined;
}
