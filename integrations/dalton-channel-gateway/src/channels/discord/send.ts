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

import { randomBytes } from "node:crypto";
import { ChannelDeliveryError } from "../adapter.js";
import { isDiscordThreadChannelType } from "./inbound.js";
import { classifyDiscordDeliveryError, type DiscordFilePart, type RequestClient } from "./rest.js";
import { createThreadDiscord, ensureThreadUnarchived, getDiscordChannel, sanitizeDiscordThreadName } from "./threading.js";

export const DISCORD_MAX_NONCE_CHARS = 25;

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
  // Discord v10 limits `nonce` to at most 25 characters (`RESTPostAPIChannelMessageJSONBody`).
  // 12 random bytes encoded as 24 hex characters stay within the limit while providing
  // 96 bits of entropy; reusing the same request body on a 429 retry keeps the nonce identical.
  return randomBytes(12).toString("hex");
}

export interface SendMessageDiscordParams {
  targetAddress: string;
  recipientPlatformUserId?: string | null;
  text: string;
  replyToMessageId?: string | null;
  newThread?: boolean;
  threadScopeKey?: string | null;
  /** Stable delivery id. Standalone thread recovery reuses it across retries. */
  deliveryKey?: string | null;
  /** Current bot user. A standalone thread is adopted only when this user owns it. */
  botUserId?: string | null;
  /** Already stored thread. Skips creation and does not consult the active-thread list. */
  confirmedThreadAddress?: string | null;
  /** Persist a confirmed thread before the message POST. Failure skips the message. */
  onConfirmedThread?: (confirmed: { address: string }) => Promise<void>;
  /** Resolves (and caches) a DM channel id for a recipient user id. */
  resolveDmChannelId?: (recipientUserId: string) => Promise<string>;
}

export interface SendMessageDiscordResult {
  messageId: string;
  channelId: string;
  resolvedAddress: string;
  nonce: string;
}

/**
 * Post into the thread this delivery already confirmed.
 * The active-thread list is not a substitute for that stored id.
 */
async function routeConfirmedDiscordThread(
  rest: RequestClient,
  params: { confirmedAddress: string; authorized: ParsedDiscordTarget },
): Promise<{ channelId: string; resolvedAddress: string }> {
  const confirmed = parseDiscordTarget(params.confirmedAddress);
  if (
    !confirmed ||
    confirmed.kind !== "thread" ||
    !confirmed.threadId ||
    !confirmed.guildId ||
    !confirmed.parentChannelId
  ) {
    throw new ChannelDeliveryError(
      "Stored Discord thread address is not a thread in the authorized channel",
      { outcome: "failed", code: "unavailable" },
    );
  }
  const authorized = params.authorized;
  if (
    authorized.kind !== "channel" ||
    !authorized.guildId ||
    authorized.guildId !== confirmed.guildId ||
    authorized.channelId !== confirmed.parentChannelId
  ) {
    throw new ChannelDeliveryError(
      "Stored Discord thread does not match the authorized parent channel",
      { outcome: "failed", code: "not_allowed" },
    );
  }
  const channel = await getDiscordChannel(rest, confirmed.threadId);
  if (
    channel.id !== confirmed.threadId ||
    channel.guild_id !== confirmed.guildId ||
    channel.parent_id !== confirmed.parentChannelId ||
    !isDiscordThreadChannelType(channel.type)
  ) {
    throw new ChannelDeliveryError(
      "Discord confirmed thread does not match the stored destination",
      { outcome: "failed", code: "unavailable" },
    );
  }
  await ensureThreadUnarchived(rest, channel);
  return { channelId: confirmed.threadId, resolvedAddress: params.confirmedAddress };
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
  const confirmedAddress = params.confirmedThreadAddress?.trim() || "";
  let channelId: string;
  let resolvedAddress = params.targetAddress;

  if (confirmedAddress) {
    const routed = await routeConfirmedDiscordThread(rest, {
      confirmedAddress,
      authorized: parsed,
    });
    channelId = routed.channelId;
    resolvedAddress = routed.resolvedAddress;
  } else if (parsed.kind === "dm") {
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

  if (
    params.newThread &&
    !confirmedAddress &&
    parsed.kind !== "dm" &&
    !scopeThreadId &&
    parsed.kind !== "thread"
  ) {
    const messageId = params.replyToMessageId?.trim() || null;
    const name = sanitizeDiscordThreadName(params.text, messageId ?? undefined);
    const thread = await createThreadDiscord(rest, {
      channelId,
      messageId,
      name,
      deliveryKey: params.deliveryKey,
      botUserId: params.botUserId,
    });
    channelId = thread.id;
    resolvedAddress = formatDiscordThreadAddress({
      guildId: thread.guild_id ?? parsed.guildId,
      parentChannelId: thread.parent_id ?? parsed.channelId,
      threadId: thread.id,
    });
    if (params.onConfirmedThread) {
      try {
        await params.onConfirmedThread({ address: resolvedAddress });
      } catch (err) {
        if (err instanceof ChannelDeliveryError) throw err;
        throw new ChannelDeliveryError("Discord confirmed thread was not stored", {
          outcome: "unknown",
          code: "unavailable",
        });
      }
    }
  }

  const nonce = createDiscordMessageNonce();
  const body: Record<string, unknown> = {
    content: params.text,
    nonce,
    allowed_mentions: { parse: [] },
  };
  if (params.replyToMessageId && !params.newThread && !confirmedAddress) {
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
