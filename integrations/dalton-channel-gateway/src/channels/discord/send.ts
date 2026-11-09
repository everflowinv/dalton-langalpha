/**
 * Discord outbound message/file sending over the REST client.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/send.outbound-_jx4Xpd3.mjs`
 * (lines 382-420) and `send.shared-CjQ4jbMA.mjs` (lines 184-226, 457-620). License: MIT,
 * Copyright (c) 2025 Peter Steinberger. OpenClaw host/config/directory resolution is
 * removed; callers pass the shared `RequestClient` and a Dalton target address.
 * Each call sends exactly one already-chunked piece of text; chunking and
 * delivery progress accounting stay with the caller / delivery service.
 */

import { randomUUID } from "node:crypto";
import { ChannelDeliveryError } from "../adapter.js";
import { classifyDiscordDeliveryError, type DiscordFilePart, type RequestClient } from "./rest.js";
import { createThreadDiscord, ensureThreadUnarchived, getDiscordChannel, sanitizeDiscordThreadName } from "./threading.js";

export interface ParsedDiscordTarget {
  kind: "dm" | "channel" | "thread";
  guildId?: string;
  channelId: string;
  parentChannelId?: string;
  threadId?: string;
}

/** Parses `discord:@me`, `discord:<channel>`, `discord:<guild>/<channel>`, and `...:thread:<id>` forms. */
export function parseDiscordTarget(address: string): ParsedDiscordTarget | null {
  let raw = address.trim();
  if (!raw) return null;
  if (raw.toLowerCase().startsWith("discord:")) raw = raw.slice("discord:".length);
  if (raw === "@me") return { kind: "dm", channelId: "@me" };
  let threadId: string | undefined;
  const threadMatch = /:thread:([^:]+)$/.exec(raw);
  if (threadMatch) {
    threadId = threadMatch[1];
    raw = raw.slice(0, threadMatch.index);
  }
  for (const prefix of ["channel:", "user:"]) {
    if (raw.toLowerCase().startsWith(prefix)) raw = raw.slice(prefix.length);
  }
  const parts = raw.split("/").filter(Boolean);
  if (parts.length === 0) return null;
  if (threadId) {
    return parts.length >= 2
      ? { kind: "thread", guildId: parts[0], channelId: threadId, parentChannelId: parts[1], threadId }
      : { kind: "thread", channelId: threadId, threadId };
  }
  if (parts.length >= 2) return { kind: "channel", guildId: parts[0], channelId: parts[1]! };
  return { kind: "channel", channelId: parts[0]! };
}

export function formatDiscordThreadAddress(params: { guildId?: string; parentChannelId: string; threadId: string }): string {
  return params.guildId
    ? `discord:${params.guildId}/${params.parentChannelId}:thread:${params.threadId}`
    : `discord:${params.parentChannelId}:thread:${params.threadId}`;
}

/** Extracts a thread id from a Dalton thread scope key (`chat:<guild>/<parent>:thread:<id>`). */
export function threadIdFromScopeKey(scopeKey?: string | null): string | null {
  if (!scopeKey) return null;
  const match = /:thread:([^:]+)$/.exec(scopeKey.trim());
  return match ? match[1]! : null;
}

export function createDiscordMessageNonce(): string {
  // Discord accepts a client nonce per message create; reusing the same
  // request body on a 429 retry keeps this value identical. It does not by
  // itself guarantee exactly-once delivery.
  return randomUUID().replace(/-/g, "");
}

export interface SendMessageDiscordParams {
  targetAddress: string;
  recipientPlatformUserId?: string | null;
  text: string;
  replyToMessageId?: string | null;
  newThread?: boolean;
  threadScopeKey?: string | null;
  /** Resolves (and caches) a DM channel id for a recipient user id. */
  resolveDmChannelId?: (recipientUserId: string) => Promise<string>;
}

export interface SendMessageDiscordResult {
  messageId: string;
  channelId: string;
  resolvedAddress: string;
  nonce: string;
}

export async function sendMessageDiscord(
  rest: RequestClient,
  params: SendMessageDiscordParams,
): Promise<SendMessageDiscordResult> {
  const parsed = parseDiscordTarget(params.targetAddress);
  if (!parsed) {
    throw new ChannelDeliveryError(`Invalid Discord target: ${params.targetAddress}`, {
      outcome: "failed", code: "target_not_found",
    });
  }
  const scopeThreadId = threadIdFromScopeKey(params.threadScopeKey);
  let channelId: string;
  let resolvedAddress = params.targetAddress;

  if (parsed.kind === "dm") {
    const recipient = params.recipientPlatformUserId?.trim();
    if (!recipient) {
      throw new ChannelDeliveryError("Discord DM target requires a recipient user id", {
        outcome: "failed", code: "target_not_found",
      });
    }
    channelId = params.resolveDmChannelId
      ? await params.resolveDmChannelId(recipient)
      : await createDmChannelId(rest, recipient);
  } else if (scopeThreadId) {
    channelId = scopeThreadId;
    const channel = await getDiscordChannel(rest, channelId);
    await ensureThreadUnarchived(rest, channel);
  } else if (parsed.kind === "thread" && parsed.threadId) {
    channelId = parsed.threadId;
    const channel = await getDiscordChannel(rest, channelId);
    await ensureThreadUnarchived(rest, channel);
  } else {
    channelId = parsed.channelId;
  }

  if (params.newThread && parsed.kind !== "dm" && !scopeThreadId && parsed.kind !== "thread") {
    const name = sanitizeDiscordThreadName(params.text, params.replyToMessageId ?? undefined);
    const thread = await createThreadDiscord(rest, {
      channelId,
      messageId: params.replyToMessageId ?? null,
      name,
    });
    channelId = thread.id;
    resolvedAddress = formatDiscordThreadAddress({
      guildId: parsed.guildId ?? thread.guild_id,
      parentChannelId: parsed.channelId,
      threadId: thread.id,
    });
  }

  const nonce = createDiscordMessageNonce();
  const body: Record<string, unknown> = {
    content: params.text,
    nonce,
    allowed_mentions: { parse: [] },
  };
  if (params.replyToMessageId && !params.newThread) {
    body.message_reference = { message_id: params.replyToMessageId };
  }
  let response: unknown;
  try {
    response = await rest.post(`/channels/${encodeURIComponent(channelId)}/messages`, body);
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "send", message: "Discord message send failed" });
  }
  const messageId =
    response && typeof response === "object" && "id" in response && typeof (response as { id?: unknown }).id === "string"
      ? (response as { id: string }).id
      : "";
  if (!messageId) {
    throw new ChannelDeliveryError("Discord send succeeded without returning a message id", {
      outcome: "unknown", code: "unavailable",
    });
  }
  return { messageId, channelId, resolvedAddress, nonce };
}

export async function createDmChannelId(rest: RequestClient, recipientUserId: string): Promise<string> {
  let response: unknown;
  try {
    response = await rest.post("/users/@me/channels", { recipient_id: recipientUserId });
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "read", message: "Discord DM channel lookup failed" });
  }
  const id =
    response && typeof response === "object" && "id" in response && typeof (response as { id?: unknown }).id === "string"
      ? (response as { id: string }).id
      : "";
  if (!id) {
    throw new ChannelDeliveryError("Discord DM channel lookup returned no channel id", {
      outcome: "failed", code: "target_not_found",
    });
  }
  return id;
}

export async function sendFileDiscord(
  rest: RequestClient,
  params: {
    targetAddress: string;
    recipientPlatformUserId?: string | null;
    filename: string;
    content: Buffer;
    mimeType: string;
    threadScopeKey?: string | null;
    resolveDmChannelId?: (recipientUserId: string) => Promise<string>;
  },
): Promise<{ messageId: string; channelId: string }> {
  const parsed = parseDiscordTarget(params.targetAddress);
  if (!parsed) {
    throw new ChannelDeliveryError(`Invalid Discord target: ${params.targetAddress}`, {
      outcome: "failed", code: "target_not_found",
    });
  }
  const scopeThreadId = threadIdFromScopeKey(params.threadScopeKey);
  let channelId: string;
  if (parsed.kind === "dm") {
    const recipient = params.recipientPlatformUserId?.trim();
    if (!recipient) {
      throw new ChannelDeliveryError("Discord DM target requires a recipient user id", {
        outcome: "failed", code: "target_not_found",
      });
    }
    channelId = params.resolveDmChannelId
      ? await params.resolveDmChannelId(recipient)
      : await createDmChannelId(rest, recipient);
  } else if (scopeThreadId) {
    channelId = scopeThreadId;
    await ensureThreadUnarchived(rest, await getDiscordChannel(rest, channelId));
  } else if (parsed.kind === "thread" && parsed.threadId) {
    channelId = parsed.threadId;
    await ensureThreadUnarchived(rest, await getDiscordChannel(rest, channelId));
  } else {
    channelId = parsed.channelId;
  }
  const file: DiscordFilePart = { name: params.filename, data: params.content, contentType: params.mimeType };
  let response: unknown;
  try {
    response = await rest.post(`/channels/${encodeURIComponent(channelId)}/messages`, {
      content: "",
      nonce: createDiscordMessageNonce(),
      files: [file],
    });
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "send", message: "Discord file send failed" });
  }
  const messageId =
    response && typeof response === "object" && "id" in response && typeof (response as { id?: unknown }).id === "string"
      ? (response as { id: string }).id
      : "";
  if (!messageId) {
    throw new ChannelDeliveryError("Discord file send succeeded without returning a message id", {
      outcome: "unknown", code: "unavailable",
    });
  }
  return { messageId, channelId };
}
