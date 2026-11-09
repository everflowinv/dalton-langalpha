/**
 * Feishu outbound message and Schema 2.0 card delivery, reply-fallback handling,
 * error classification (`ChannelDeliveryError`), and message lookup.
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs`
 * (lines 750–838, 1110–1113, 2439–3015).
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 6 (`src/channels/feishu/send.ts`):
 * - Retains `sendMessageFeishu`, `sendCardFeishu`, `sendStructuredCardFeishu`,
 *   `sendReplyOrFallbackDirect`, `isWithdrawnReplyError`, `shouldFallbackFromReplyTarget`,
 *   `getMessageFeishu`.
 * - Excludes `withFeishuSendContext` and `withFeishuMessageDispatch` authority fencing.
 */

import type * as Lark from "@larksuiteoapi/node-sdk";
import { ChannelDeliveryError } from "../adapter.js";
import {
  assertFeishuCardWithinEnvelope,
  buildFeishuPostMessageContent,
  buildStructuredCard,
  type FeishuSchema2Card,
  parseMergeForwardContent,
} from "./format.js";
import { parseMessageContent } from "./inbound-parse.js";
import {
  normalizeFeishuTarget,
  parseFeishuConversationId,
  resolveReceiveIdType,
} from "./targets.js";

export const WITHDRAWN_REPLY_ERROR_CODES = new Set<number>([230011, 231003]);
export const FEISHU_SEND_RATE_LIMIT_CODES = new Set<number>([230020, 11232]);

const TARGET_NOT_FOUND_FEISHU_CODES = new Set<number>([
  230002,
  230006,
  230013,
  230035,
  231003,
]);

const PERMISSION_DENIED_FEISHU_CODES = new Set<number>([
  230001,
  99991663,
  99991668,
  99991672,
]);

const NETWORK_UNCERTAIN_ERROR_CODES = new Set<string>([
  "ETIMEDOUT",
  "ESOCKETTIMEDOUT",
  "ECONNABORTED",
  "ECONNRESET",
  "EPIPE",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_BODY_TIMEOUT",
  "UND_ERR_SOCKET",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readStringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0
    ? value.trim()
    : undefined;
}

export interface FeishuApiErrorMeta {
  message: string;
  code?: string;
  httpStatus?: number;
  feishuCode?: number | string;
  feishuMsg?: string;
  feishuLogId?: string;
  nestedErrorLogId?: string;
  troubleshooter?: string;
}

export function extractFeishuApiErrorMeta(error: unknown): FeishuApiErrorMeta {
  if (!isRecord(error)) {
    return {
      message: typeof error === "string" ? error : JSON.stringify(error),
    };
  }
  const response = isRecord(error.response) ? error.response : undefined;
  const responseData = isRecord(response?.data) ? response.data : undefined;
  const nestedError = isRecord(responseData?.error)
    ? responseData.error
    : undefined;
  const directFeishuCode =
    typeof error.code === "number"
      ? error.code
      : typeof responseData?.code === "number"
        ? responseData.code
        : readStringValue(responseData?.code);

  return {
    message:
      typeof error.message === "string"
        ? error.message
        : typeof responseData?.msg === "string"
          ? responseData.msg
          : JSON.stringify(error),
    code: readStringValue(error.code),
    httpStatus:
      typeof response?.status === "number" ? response.status : undefined,
    feishuCode: directFeishuCode,
    feishuMsg:
      readStringValue(responseData?.msg) ?? readStringValue(error.msg),
    feishuLogId:
      readStringValue(responseData?.log_id) ?? readStringValue(error.log_id),
    nestedErrorLogId: readStringValue(nestedError?.log_id),
    troubleshooter:
      readStringValue(responseData?.troubleshooter) ||
      readStringValue(nestedError?.troubleshooter),
  };
}

export function formatFeishuApiError(error: unknown): string {
  if (!isRecord(error)) {
    return typeof error === "string" ? error : JSON.stringify(error);
  }
  const meta = extractFeishuApiErrorMeta(error);
  return JSON.stringify({
    message: meta.message,
    code: meta.code,
    http_status: meta.httpStatus,
    feishu_code: meta.feishuCode,
    feishu_msg: meta.feishuMsg,
    feishu_log_id: meta.feishuLogId || meta.nestedErrorLogId,
    feishu_troubleshooter: meta.troubleshooter,
  });
}

export function getFeishuSendRateLimitCode(error: unknown): number | undefined {
  if (!isRecord(error)) {
    return undefined;
  }
  const response = isRecord(error.response) ? error.response : undefined;
  if (response?.status === 429) {
    return 429;
  }
  const data = isRecord(response?.data) ? response.data : error;
  const code = typeof data.code === "number" ? data.code : undefined;
  return code !== undefined && FEISHU_SEND_RATE_LIMIT_CODES.has(code)
    ? code
    : undefined;
}

export function isWithdrawnReplyError(err: unknown): boolean {
  if (!isRecord(err)) {
    return false;
  }
  if (
    typeof err.code === "number" &&
    WITHDRAWN_REPLY_ERROR_CODES.has(err.code)
  ) {
    return true;
  }
  const response = isRecord(err.response) ? err.response : undefined;
  const data = isRecord(response?.data) ? response.data : undefined;
  if (
    typeof data?.code === "number" &&
    WITHDRAWN_REPLY_ERROR_CODES.has(data.code)
  ) {
    return true;
  }
  return false;
}

export function shouldFallbackFromReplyTarget(err: unknown): boolean {
  if (isWithdrawnReplyError(err)) {
    return true;
  }
  if (err instanceof Error) {
    return /withdrawn|not found/i.test(err.message);
  }
  return false;
}

/**
 * Classifies a Feishu SDK or HTTP error into a `ChannelDeliveryError` with the
 * appropriate `outcome` (`"failed"`, `"rate_limited"`, or `"unknown"`) and `code`.
 *
 * When `options.stage === "upload"`, the message has not yet been dispatched, so
 * network/5xx/timeout errors are confirmed undelivered (`outcome: "failed"`).
 */
export function classifyFeishuDeliveryError(
  err: unknown,
  defaultPrefix = "Feishu message send failed",
  options?: { stage?: "upload" | "send" },
): ChannelDeliveryError {
  const stage = options?.stage ?? "send";
  if (err instanceof ChannelDeliveryError) {
    if (stage === "upload" && err.outcome === "unknown") {
      return new ChannelDeliveryError(err.message, {
        outcome: "failed",
        code: err.code,
      });
    }
    return err;
  }

  const meta = extractFeishuApiErrorMeta(err);
  const numericFeishuCode =
    typeof meta.feishuCode === "number"
      ? meta.feishuCode
      : typeof meta.feishuCode === "string" && /^\d+$/.test(meta.feishuCode)
        ? Number.parseInt(meta.feishuCode, 10)
        : undefined;

  const detailMsg = meta.feishuMsg || meta.message || defaultPrefix;
  const formattedMsg = `${defaultPrefix}: ${detailMsg}`;

  if (
    meta.httpStatus === 429 ||
    (numericFeishuCode !== undefined &&
      FEISHU_SEND_RATE_LIMIT_CODES.has(numericFeishuCode))
  ) {
    return new ChannelDeliveryError(formattedMsg, {
      outcome: "rate_limited",
      code: "rate_limited",
    });
  }

  if (
    meta.httpStatus === 404 ||
    (numericFeishuCode !== undefined &&
      TARGET_NOT_FOUND_FEISHU_CODES.has(numericFeishuCode))
  ) {
    return new ChannelDeliveryError(formattedMsg, {
      outcome: "failed",
      code: "target_not_found",
    });
  }

  if (
    meta.httpStatus === 401 ||
    meta.httpStatus === 403 ||
    (numericFeishuCode !== undefined &&
      PERMISSION_DENIED_FEISHU_CODES.has(numericFeishuCode))
  ) {
    return new ChannelDeliveryError(formattedMsg, {
      outcome: "failed",
      code: "not_allowed",
    });
  }

  if (
    (meta.code && NETWORK_UNCERTAIN_ERROR_CODES.has(meta.code.toUpperCase())) ||
    (err instanceof Error &&
      (err.name === "AbortError" ||
        err.name === "TimeoutError" ||
        /timeout|timed out|socket hang up|econnreset|network/i.test(
          err.message,
        ))) ||
    (meta.httpStatus !== undefined && meta.httpStatus >= 500)
  ) {
    return new ChannelDeliveryError(formattedMsg, {
      outcome: stage === "upload" ? "failed" : "unknown",
      code: "unavailable",
    });
  }

  if (
    (meta.httpStatus !== undefined &&
      meta.httpStatus >= 400 &&
      meta.httpStatus < 500) ||
    (numericFeishuCode !== undefined && numericFeishuCode !== 0)
  ) {
    return new ChannelDeliveryError(formattedMsg, {
      outcome: "failed",
      code: "unavailable",
    });
  }

  return new ChannelDeliveryError(formattedMsg, {
    outcome: stage === "upload" ? "failed" : "unknown",
    code: "unavailable",
  });
}

export interface FeishuMessageSendResult {
  messageId: string;
  chatId: string;
}

export function toFeishuMessageSendResult(
  response: { data?: { message_id?: string } } | undefined,
  chatId: string,
): FeishuMessageSendResult {
  const messageId = response?.data?.message_id?.trim();
  if (!messageId) {
    throw new ChannelDeliveryError(
      "Feishu send succeeded without returning a message_id",
      {
        outcome: "unknown",
        code: "unavailable",
      },
    );
  }
  return {
    messageId,
    chatId,
  };
}

export function resolveFeishuSendTarget(params: {
  to: string;
  replyToMessageId?: string | null;
  replyInThread?: boolean;
}): {
  target: string;
  receiveIdType: "chat_id" | "open_id" | "user_id";
  replyToMessageId?: string;
  replyInThread: boolean;
} {
  const normalizedTarget = normalizeFeishuTarget(params.to);
  const parsedTopicTarget = normalizedTarget
    ? parseFeishuConversationId({ conversationId: normalizedTarget })
    : null;
  const effectiveTarget = parsedTopicTarget?.chatId ?? normalizedTarget;
  if (!effectiveTarget || effectiveTarget === "@me") {
    throw new ChannelDeliveryError(`Invalid Feishu target: ${params.to}`, {
      outcome: "failed",
      code: "target_not_found",
    });
  }
  const effectiveReplyToMessageId =
    params.replyToMessageId?.trim() || parsedTopicTarget?.topicId || undefined;
  const effectiveReplyInThread =
    Boolean(params.replyInThread) || Boolean(parsedTopicTarget?.topicId);

  return {
    target: effectiveTarget,
    receiveIdType: resolveReceiveIdType(effectiveTarget),
    replyToMessageId: effectiveReplyToMessageId,
    replyInThread: effectiveReplyInThread,
  };
}

export async function sendReplyOrFallbackDirect(
  client: Lark.Client,
  params: {
    replyToMessageId?: string;
    replyInThread?: boolean;
    content: string;
    msgType: string;
    target: string;
    receiveIdType: "chat_id" | "open_id" | "user_id";
  },
): Promise<{ data?: { message_id?: string }; code?: number; msg?: string }> {
  const sendDirect = async (): Promise<{
    data?: { message_id?: string };
    code?: number;
    msg?: string;
  }> => {
    try {
      const response = (await client.im.message.create({
        params: { receive_id_type: params.receiveIdType },
        data: {
          receive_id: params.target,
          content: params.content,
          msg_type: params.msgType,
        },
      })) as { data?: { message_id?: string }; code?: number; msg?: string };
      if (response.code !== undefined && response.code !== 0) {
        throw classifyFeishuDeliveryError(
          { response: { status: 200, data: response } },
          "Feishu send failed",
        );
      }
      return response;
    } catch (err) {
      throw classifyFeishuDeliveryError(err, "Feishu send failed");
    }
  };

  if (!params.replyToMessageId) {
    return sendDirect();
  }

  let replyResponse: {
    data?: { message_id?: string };
    code?: number;
    msg?: string;
  };
  try {
    replyResponse = (await client.im.message.reply({
      path: { message_id: params.replyToMessageId },
      data: {
        content: params.content,
        msg_type: params.msgType,
        ...(params.replyInThread ? { reply_in_thread: true } : {}),
      },
    })) as { data?: { message_id?: string }; code?: number; msg?: string };
  } catch (err) {
    if (!params.replyInThread && shouldFallbackFromReplyTarget(err)) {
      return sendDirect();
    }
    throw classifyFeishuDeliveryError(err, "Feishu reply failed");
  }

  if (replyResponse.code !== undefined && replyResponse.code !== 0) {
    if (
      !params.replyInThread &&
      WITHDRAWN_REPLY_ERROR_CODES.has(replyResponse.code)
    ) {
      return sendDirect();
    }
    throw classifyFeishuDeliveryError(
      { response: { status: 200, data: replyResponse } },
      "Feishu reply failed",
    );
  }

  return replyResponse;
}

export async function sendMessageFeishu(
  client: Lark.Client,
  params: {
    to: string;
    text: string;
    replyToMessageId?: string | null;
    replyInThread?: boolean;
  },
): Promise<FeishuMessageSendResult> {
  const { target, receiveIdType, replyToMessageId, replyInThread } =
    resolveFeishuSendTarget({
      to: params.to,
      replyToMessageId: params.replyToMessageId,
      replyInThread: params.replyInThread,
    });

  const contentPayload = buildFeishuPostMessageContent({
    messageText: params.text,
  });

  const response = await sendReplyOrFallbackDirect(client, {
    replyToMessageId,
    replyInThread,
    content: contentPayload,
    msgType: "post",
    target,
    receiveIdType,
  });

  return toFeishuMessageSendResult(response, target);
}

export async function sendCardFeishu(
  client: Lark.Client,
  params: {
    to: string;
    card: FeishuSchema2Card | Record<string, unknown>;
    replyToMessageId?: string | null;
    replyInThread?: boolean;
  },
): Promise<FeishuMessageSendResult> {
  const { target, receiveIdType, replyToMessageId, replyInThread } =
    resolveFeishuSendTarget({
      to: params.to,
      replyToMessageId: params.replyToMessageId,
      replyInThread: params.replyInThread,
    });

  assertFeishuCardWithinEnvelope(params.card, "Feishu outbound card");

  const response = await sendReplyOrFallbackDirect(client, {
    replyToMessageId,
    replyInThread,
    content: JSON.stringify(params.card),
    msgType: "interactive",
    target,
    receiveIdType,
  });

  return toFeishuMessageSendResult(response, target);
}

export async function sendStructuredCardFeishu(
  client: Lark.Client,
  params: {
    to: string;
    text: string;
    replyToMessageId?: string | null;
    replyInThread?: boolean;
    title?: string;
  },
): Promise<FeishuMessageSendResult> {
  const card = buildStructuredCard(params.text, { title: params.title });
  return sendCardFeishu(client, {
    to: params.to,
    card,
    replyToMessageId: params.replyToMessageId,
    replyInThread: params.replyInThread,
  });
}

export interface FeishuFetchedMessage {
  messageId: string;
  chatId: string;
  chatType?: "p2p" | "group" | "private";
  senderId?: string;
  senderOpenId?: string;
  senderType?: string;
  content: string;
  contentType: string;
  createTime?: number;
  threadId?: string;
}

export async function getMessageFeishu(
  client: Lark.Client,
  params: { messageId: string },
): Promise<FeishuFetchedMessage | null> {
  try {
    const response = (await client.im.message.get({
      params: { card_msg_content_type: "user_card_content" },
      path: { message_id: params.messageId },
    })) as {
      code?: number;
      msg?: string;
      data?: {
        items?: Array<{
          message_id?: string;
          upper_message_id?: string;
          root_id?: string;
          chat_id?: string;
          chat_type?: "p2p" | "group" | "private";
          msg_type?: string;
          body?: { content?: string };
          sender?: { id?: string; id_type?: string; sender_type?: string };
          create_time?: string;
          thread_id?: string;
        }>;
      };
    };

    if (response.code !== undefined && response.code !== 0) {
      return null;
    }

    const items = response.data?.items ?? [];
    const item =
      items.find(
        (candidate) =>
          candidate.msg_type === "merge_forward" && !candidate.upper_message_id,
      ) ?? items[0];
    if (!item) {
      return null;
    }

    const msgType = item.msg_type ?? "text";
    const rawContent = item.body?.content ?? "";
    const content =
      msgType === "merge_forward" && items.length > 0
        ? parseMergeForwardContent({
            items,
            content: rawContent,
            formatSubMessage: (subType, subBody) =>
              parseMessageContent(subBody, subType),
          })
        : parseMessageContent(rawContent, msgType);

    return {
      messageId: item.message_id ?? params.messageId,
      chatId: item.chat_id ?? "",
      chatType: item.chat_type,
      senderId: item.sender?.id,
      senderOpenId:
        item.sender?.id_type === "open_id" ? item.sender?.id : undefined,
      senderType: item.sender?.sender_type,
      content,
      contentType: msgType,
      createTime: item.create_time
        ? Number.parseInt(item.create_time, 10)
        : undefined,
      threadId: item.thread_id || undefined,
    };
  } catch {
    return null;
  }
}
