// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import { randomBytes } from "node:crypto";
import type { HostIpResolver } from "../../shared/safe-fetch.js";
import {
  ChannelDeliveryError,
  type SendFileResult,
  type SendTextChunkResult,
} from "../adapter.js";
import { sendWeixinMessage, WeixinApiError } from "./api.js";
import {
  detectImageMimeFromMagicBytes,
  uploadBufferToWeixinCdn,
} from "./cdn.js";
import { filterWeixinMarkdown } from "./markdown-filter.js";
import {
  MessageItemType,
  MessageState,
  MessageType,
  UploadMediaType,
  type MessageItem,
  type SendMessageReq,
} from "./types.js";

const IMAGE_MIME_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

export function generateWeixinClientId(): string {
  return `dlwx_${Date.now().toString(36)}_${randomBytes(6).toString("hex")}`;
}

export function normalizeRecipientUserId(
  rawTarget: string,
  recipientPlatformUserId?: string | null,
): string {
  if (recipientPlatformUserId && recipientPlatformUserId.trim().length > 0) {
    return recipientPlatformUserId.trim();
  }
  const trimmed = rawTarget.trim();
  if (trimmed.startsWith("weixin:")) {
    const rest = trimmed.slice("weixin:".length).trim();
    if (rest.startsWith("dm:")) {
      return rest.slice("dm:".length).trim();
    }
    return rest;
  }
  if (trimmed.startsWith("dm:")) {
    return trimmed.slice("dm:".length).trim();
  }
  return trimmed;
}

export interface WeixinSendContext {
  baseUrl: string;
  cdnBaseUrl: string;
  botToken: string;
  contextToken: string;
  toUserId: string;
  timeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  allowedApiDomains?: readonly string[];
  allowedCdnDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  onSessionExpired?: (err: WeixinApiError) => Promise<void> | void;
}

async function classifyAndThrowSendMessageError(
  err: unknown,
  onSessionExpired?: (err: WeixinApiError) => Promise<void> | void,
): Promise<never> {
  if (err instanceof ChannelDeliveryError) {
    throw err;
  }
  if (err instanceof WeixinApiError) {
    if (err.kind === "session_expired") {
      await onSessionExpired?.(err);
      throw new ChannelDeliveryError(
        "Weixin bot session has expired (-14); please re-scan QR code in settings.",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    if (err.kind === "rate_limited") {
      throw new ChannelDeliveryError(err.message, {
        outcome: "rate_limited",
        code: "rate_limited",
        retryAfterMs: err.retryAfterMs,
      });
    }
    if (
      err.requestSent &&
      (err.kind === "network_error" ||
        (err.kind === "http_error" && (err.status ?? 0) >= 500))
    ) {
      throw new ChannelDeliveryError(
        `Weixin sendmessage response lost after dispatch: ${err.message}`,
        {
          outcome: "unknown",
          code: "unavailable",
        },
      );
    }
    throw new ChannelDeliveryError(err.message, {
      outcome: "failed",
      code: "unavailable",
    });
  }
  throw new ChannelDeliveryError(
    err instanceof Error ? err.message : String(err),
    {
      outcome: "failed",
      code: "unavailable",
    },
  );
}

async function classifyAndThrowUploadStageError(
  err: unknown,
  onSessionExpired?: (err: WeixinApiError) => Promise<void> | void,
): Promise<never> {
  if (err instanceof ChannelDeliveryError) {
    throw err;
  }
  if (err instanceof WeixinApiError) {
    if (err.kind === "session_expired") {
      await onSessionExpired?.(err);
      throw new ChannelDeliveryError(
        "Weixin bot session has expired (-14); please re-scan QR code in settings.",
        {
          outcome: "failed",
          code: "unavailable",
        },
      );
    }
    if (err.kind === "rate_limited") {
      throw new ChannelDeliveryError(err.message, {
        outcome: "rate_limited",
        code: "rate_limited",
        retryAfterMs: err.retryAfterMs,
      });
    }
    // Upload failure occurred before sendmessage was called, so no message was delivered.
    throw new ChannelDeliveryError(err.message, {
      outcome: "failed",
      code: "unavailable",
    });
  }
  throw new ChannelDeliveryError(
    err instanceof Error ? err.message : String(err),
    {
      outcome: "failed",
      code: "unavailable",
    },
  );
}

export async function sendWeixinTextMessage(
  ctx: WeixinSendContext,
  rawText: string,
): Promise<SendTextChunkResult> {
  const toUserId = ctx.toUserId.trim();
  if (!toUserId || toUserId === "@me") {
    throw new ChannelDeliveryError("Invalid Weixin recipient user ID", {
      outcome: "failed",
      code: "target_not_found",
    });
  }
  const contextToken = ctx.contextToken?.trim();
  if (!contextToken) {
    throw new ChannelDeliveryError(
      "Missing active Weixin context_token for recipient",
      {
        outcome: "failed",
        code: "unavailable",
      },
    );
  }
  const botToken = ctx.botToken?.trim();
  if (!botToken) {
    throw new ChannelDeliveryError("Weixin bot is not logged in", {
      outcome: "failed",
      code: "unavailable",
    });
  }

  const filteredText = filterWeixinMarkdown(rawText);
  const clientId = generateWeixinClientId();
  const req: SendMessageReq = {
    msg: {
      from_user_id: "",
      to_user_id: toUserId,
      client_id: clientId,
      message_type: MessageType.BOT,
      message_state: MessageState.FINISH,
      item_list: [{ type: MessageItemType.TEXT, text_item: { text: filteredText } }],
      context_token: contextToken,
    },
  };

  try {
    const resp = await sendWeixinMessage({
      baseUrl: ctx.baseUrl,
      botToken,
      body: req,
      timeoutMs: ctx.timeoutMs,
      signal: ctx.signal,
      fetchImpl: ctx.fetchImpl,
      allowedDomains: ctx.allowedApiDomains,
      resolveHostIps: ctx.resolveHostIps,
      checkResolvedIps: ctx.checkResolvedIps,
    });
    const rawMsgId = resp.message_id ?? resp.msg_id;
    return {
      messageId:
        rawMsgId !== undefined && rawMsgId !== null && String(rawMsgId).trim()
          ? String(rawMsgId).trim()
          : clientId,
      resolvedAddress: "weixin:@me",
    };
  } catch (err) {
    return classifyAndThrowSendMessageError(err, ctx.onSessionExpired);
  }
}

export async function sendWeixinMediaAttachment(
  ctx: WeixinSendContext,
  file: {
    filename: string;
    content: Buffer;
    mimeType: string;
  },
): Promise<SendFileResult> {
  const toUserId = ctx.toUserId.trim();
  if (!toUserId || toUserId === "@me") {
    throw new ChannelDeliveryError("Invalid Weixin recipient user ID", {
      outcome: "failed",
      code: "target_not_found",
    });
  }
  const contextToken = ctx.contextToken?.trim();
  if (!contextToken) {
    throw new ChannelDeliveryError(
      "Missing active Weixin context_token for recipient",
      {
        outcome: "failed",
        code: "unavailable",
      },
    );
  }
  const botToken = ctx.botToken?.trim();
  if (!botToken) {
    throw new ChannelDeliveryError("Weixin bot is not logged in", {
      outcome: "failed",
      code: "unavailable",
    });
  }

  const normalizedMime = (file.mimeType || "application/octet-stream")
    .toLowerCase()
    .trim();
  const detectedMagicMime = detectImageMimeFromMagicBytes(file.content);
  const isVerifiedImage =
    IMAGE_MIME_TYPES.has(normalizedMime) && detectedMagicMime !== null;
  const uploadMediaType = isVerifiedImage
    ? UploadMediaType.IMAGE
    : UploadMediaType.FILE;

  let uploaded;
  try {
    uploaded = await uploadBufferToWeixinCdn({
      baseUrl: ctx.baseUrl,
      botToken,
      cdnBaseUrl: ctx.cdnBaseUrl,
      toUserId,
      buffer: file.content,
      mediaType: uploadMediaType,
      signal: ctx.signal,
      fetchImpl: ctx.fetchImpl,
      allowedDomains: ctx.allowedApiDomains,
      allowedCdnDomains: ctx.allowedCdnDomains,
      resolveHostIps: ctx.resolveHostIps,
      checkResolvedIps: ctx.checkResolvedIps,
    });
  } catch (err) {
    return classifyAndThrowUploadStageError(err, ctx.onSessionExpired);
  }

  const mediaItem: MessageItem = isVerifiedImage
    ? {
        type: MessageItemType.IMAGE,
        image_item: {
          media: {
            encrypt_query_param: uploaded.downloadEncryptedQueryParam,
            aes_key: uploaded.aeskeyBase64,
            encrypt_type: 1,
          },
          mid_size: uploaded.filesize,
        },
      }
    : {
        type: MessageItemType.FILE,
        file_item: {
          media: {
            encrypt_query_param: uploaded.downloadEncryptedQueryParam,
            aes_key: uploaded.aeskeyBase64,
            encrypt_type: 1,
          },
          file_name: file.filename || "attachment.bin",
          len: String(uploaded.rawsize),
        },
      };

  const clientId = generateWeixinClientId();
  const req: SendMessageReq = {
    msg: {
      from_user_id: "",
      to_user_id: toUserId,
      client_id: clientId,
      message_type: MessageType.BOT,
      message_state: MessageState.FINISH,
      item_list: [mediaItem],
      context_token: contextToken,
    },
  };

  try {
    const resp = await sendWeixinMessage({
      baseUrl: ctx.baseUrl,
      botToken,
      body: req,
      timeoutMs: ctx.timeoutMs,
      signal: ctx.signal,
      fetchImpl: ctx.fetchImpl,
      allowedDomains: ctx.allowedApiDomains,
      resolveHostIps: ctx.resolveHostIps,
      checkResolvedIps: ctx.checkResolvedIps,
    });
    const rawMsgId = resp.message_id ?? resp.msg_id;
    return {
      messageId:
        rawMsgId !== undefined && rawMsgId !== null && String(rawMsgId).trim()
          ? String(rawMsgId).trim()
          : clientId,
      status: "sent",
      reason: null,
    };
  } catch (err) {
    return classifyAndThrowSendMessageError(err, ctx.onSessionExpired);
  }
}
