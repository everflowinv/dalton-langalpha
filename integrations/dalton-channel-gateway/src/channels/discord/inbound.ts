/**
 * Discord `MESSAGE_CREATE` inbound parsing and attachment download.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/transcripts-source-EMrQb132.mjs`
 * (lines 1-22, 135-429: `DISCORD_MEDIA_SSRF_POLICY`, `resolveDiscordMediaClassification`,
 * `fetchDiscordMedia`, `resolveDiscordMessageText`, `resolveDiscordMentions`). License: MIT,
 * Copyright (c) 2025 Peter Steinberger. OpenClaw local media storage (`saveRemoteMedia`)
 * and the Typing indicator are removed; downloads go through `src/shared/safe-fetch.ts`
 * into memory only, and the bot token is never sent to CDN or redirect targets.
 */

import { SafeFetchError, safeFetchBuffer, type SafeFetchOptions } from "../../shared/safe-fetch.js";
import { MAX_ATTACHMENT_BYTES, type InboundAttachmentDescriptor, type InboundChannelEvent } from "../../types.js";
import { classifyDiscordDeliveryError, type RequestClient } from "./rest.js";

export const DISCORD_MEDIA_SSRF_POLICY = {
  hostnameAllowlist: [
    "cdn.discordapp.com",
    "media.discordapp.net",
    "*.discordapp.com",
    "*.discordapp.net",
  ],
} as const;

export const DISCORD_CHANNEL_TYPES = {
  GuildText: 0, DM: 1, GuildAnnouncement: 5, AnnouncementThread: 10,
  PublicThread: 11, PrivateThread: 12, GuildForum: 15, GuildMedia: 16,
} as const;

export function isDiscordThreadChannelType(type: number | undefined): boolean {
  return type === 10 || type === 11 || type === 12;
}

export interface DiscordChannelContext {
  channelId: string;
  type?: number;
  guildId?: string;
  parentId?: string;
  name?: string;
}

export interface DiscordMessageLike {
  id?: string;
  channel_id?: string;
  guild_id?: string;
  content?: string;
  author?: { id?: string; username?: string; global_name?: string; bot?: boolean };
  webhook_id?: string;
  mentions?: Array<{ id?: string; username?: string; global_name?: string }>;
  mention_everyone?: boolean;
  attachments?: Array<{
    id?: string; filename?: string; content_type?: string; size?: number;
    url?: string; proxy_url?: string;
  }>;
  thread?: { id?: string; parent_id?: string };
  [key: string]: unknown;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function resolveDiscordMentions(text: string, message: DiscordMessageLike, botUserId?: string): string {
  if (!text.includes("<")) return text;
  let out = text;
  const botId = botUserId?.trim();
  const mentions = Array.isArray(message.mentions) ? message.mentions : [];
  for (const user of mentions) {
    const userId = typeof user?.id === "string" ? user.id.trim() : "";
    if (!userId) continue;
    if (botId && userId === botId) continue;
    const label = user.global_name || user.username || userId;
    out = out.replace(new RegExp(`<@!?${escapeRegExp(userId)}>`, "g"), () => `@${label}`);
  }
  return out;
}

export function isBotMentioned(message: DiscordMessageLike, botUserId?: string): boolean {
  const botId = botUserId?.trim();
  if (!botId) return false;
  if (!Array.isArray(message.mentions)) return false;
  return message.mentions.some((m) => typeof m?.id === "string" && m.id.trim() === botId);
}

export function stripBotMention(text: string, botUserId?: string): string {
  const botId = botUserId?.trim();
  if (!botId) return text.trim();
  return text
    .replace(new RegExp(`[ \\t]*<@!?${escapeRegExp(botId)}>[ \\t]*`, "g"), " ")
    .replace(/^[ \t]+|[ \t]+$/gm, "")
    .trim();
}

export function extractDiscordAttachments(
  message: DiscordMessageLike,
): InboundAttachmentDescriptor[] {
  const channelId = typeof message.channel_id === "string" ? message.channel_id : "";
  const messageId = typeof message.id === "string" ? message.id : "";
  const out: InboundAttachmentDescriptor[] = [];
  for (const att of message.attachments ?? []) {
    if (!att.id) continue;
    const filename = att.filename?.trim() || `${att.id}.bin`;
    out.push({
      id: att.id,
      filename,
      mimeType: att.content_type?.trim() || guessMimeFromFilename(filename),
      ...(typeof att.size === "number" && Number.isFinite(att.size) ? { sizeBytes: att.size } : {}),
      ...(att.url ? { url: att.url } : {}),
      ...(att.proxy_url ? { proxyUrl: att.proxy_url } : {}),
      ...(channelId ? { channelId } : {}),
      ...(messageId ? { messageId } : {}),
    });
  }
  return out;
}

function guessMimeFromFilename(filename: string): string {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".gif")) return "image/gif";
  if (lower.endsWith(".webp")) return "image/webp";
  if (lower.endsWith(".pdf")) return "application/pdf";
  if (lower.endsWith(".txt") || lower.endsWith(".md")) return "text/plain";
  return "application/octet-stream";
}

export function buildDiscordThreadScopeKey(params: {
  chatType: "dm" | "group";
  platformUserId: string;
  guildId?: string;
  channelId: string;
  parentId?: string;
  isThread?: boolean;
}): string {
  if (params.chatType === "dm") return `dm:${params.platformUserId}`;
  const parent = params.isThread && params.parentId ? params.parentId : params.channelId;
  const base = params.guildId ? `${params.guildId}/${parent}` : parent;
  return params.isThread ? `chat:${base}:thread:${params.channelId}` : `chat:${base}`;
}

export function buildDiscordChatAddress(params: {
  chatType: "dm" | "group";
  guildId?: string;
  channelId: string;
  parentId?: string;
  isThread?: boolean;
}): string {
  if (params.chatType === "dm") return "discord:@me";
  const parent = params.isThread && params.parentId ? params.parentId : params.channelId;
  return params.guildId ? `discord:${params.guildId}/${parent}` : `discord:${parent}`;
}

/**
 * Parses a raw Gateway `MESSAGE_CREATE` payload into an `InboundChannelEvent`.
 * Returns `null` for bot/webhook/own messages and malformed payloads.
 * `channelContext` resolves thread parents and channel types (from the
 * adapter's GUILD_CREATE/CHANNEL_CREATE cache or a REST channel lookup).
 */
export function parseDiscordInboundMessage(
  raw: unknown,
  options: { botUserId?: string; channelContext?: DiscordChannelContext } = {},
): InboundChannelEvent | null {
  if (!isRecord(raw)) return null;
  const message = raw as DiscordMessageLike;
  const author = message.author;
  if (!author?.id) return null;
  if (author.bot) return null;
  if (message.webhook_id) return null;
  const botUserId = options.botUserId?.trim();
  if (botUserId && author.id === botUserId) return null;

  const messageId = typeof message.id === "string" ? message.id.trim() : "";
  const channelId = typeof message.channel_id === "string" ? message.channel_id.trim() : "";
  if (!messageId || !channelId) return null;

  const guildId = typeof message.guild_id === "string" && message.guild_id ? message.guild_id : undefined;
  const ctx = options.channelContext;
  const channelType = ctx?.type;
  const isThread = isDiscordThreadChannelType(channelType) || Boolean(message.thread?.id && message.thread.id === channelId);
  const isDm = !guildId || channelType === DISCORD_CHANNEL_TYPES.DM;
  const chatType: "dm" | "group" = isDm ? "dm" : "group";
  const parentId =
    ctx?.parentId ??
    (isRecord(message.thread) && typeof message.thread.parent_id === "string" ? message.thread.parent_id : undefined);

  const rawText = typeof message.content === "string" ? message.content : "";
  const mentioned = isBotMentioned(message, botUserId);
  const stripped = mentioned ? stripBotMention(rawText, botUserId) : rawText;
  const text = resolveDiscordMentions(stripped, message, botUserId).trim();

  const chatIdForScope = isThread ? (parentId ?? channelId) : channelId;
  const attachments = extractDiscordAttachments(message);

  return {
    platform: "discord",
    platformUserId: author.id,
    platformUsername: author.global_name || author.username || undefined,
    chatType,
    chatAddress: buildDiscordChatAddress({ chatType, guildId, channelId, parentId, isThread }),
    chatId: chatType === "dm" ? channelId : guildId ? `${guildId}/${chatIdForScope}` : chatIdForScope,
    ...(ctx?.name ? { chatName: ctx.name } : {}),
    threadScopeKey: buildDiscordThreadScopeKey({
      chatType, platformUserId: author.id, guildId, channelId, parentId, isThread,
    }),
    messageId,
    text,
    isGroupTrigger: chatType === "group" ? mentioned : undefined,
    ...(attachments.length > 0 ? { attachments } : {}),
    rawPayload: { discord_message: message as Record<string, unknown> },
  };
}

function resolveAttachmentChannelId(
  descriptor: InboundAttachmentDescriptor,
  event: InboundChannelEvent,
): string {
  if (descriptor.channelId?.trim()) return descriptor.channelId.trim();
  const rawMessage = event.rawPayload?.discord_message;
  if (
    isRecord(rawMessage) &&
    typeof rawMessage.channel_id === "string" &&
    rawMessage.channel_id.trim()
  ) {
    return rawMessage.channel_id.trim();
  }
  const threadMatch = /:thread:([^:/\s]+)$/.exec(event.threadScopeKey ?? "");
  if (threadMatch?.[1]) return threadMatch[1];
  return lastSegment(event.chatId) ?? event.chatId;
}

async function fetchFreshAttachmentFromMessage(
  rest: RequestClient,
  descriptor: InboundAttachmentDescriptor,
  event: InboundChannelEvent,
): Promise<{ url: string; filename?: string; mimeType?: string }> {
  const channelId = resolveAttachmentChannelId(descriptor, event);
  const messageId = (descriptor.messageId ?? event.messageId ?? "").trim();
  if (!channelId || !messageId) {
    throw classifyDiscordDeliveryError(
      new Error("Discord attachment lookup requires channelId and messageId"),
      { stage: "read", message: "Discord attachment message lookup failed" },
    );
  }
  let fetched: DiscordMessageLike;
  try {
    fetched = (await rest.get(
      `/channels/${encodeURIComponent(channelId)}/messages/${encodeURIComponent(messageId)}`,
    )) as DiscordMessageLike;
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "read", message: "Discord attachment message lookup failed" });
  }
  const fresh = (fetched?.attachments ?? []).find((a) => a.id === descriptor.id);
  const freshUrl = fresh?.url ?? fresh?.proxy_url;
  if (!fresh || !freshUrl) {
    throw classifyDiscordDeliveryError(
      new Error(`Discord attachment ${descriptor.id} is no longer available on message ${messageId}`),
      { stage: "read", message: "Discord attachment download failed" },
    );
  }
  return {
    url: freshUrl,
    ...(fresh.filename ? { filename: fresh.filename } : {}),
    ...(fresh.content_type ? { mimeType: fresh.content_type } : {}),
  };
}

/**
 * Downloads one inbound attachment into memory. Uses the descriptor's signed
 * CDN URL when present; if absent or expired, re-fetches the original message
 * through the REST client by saved channel/message/attachment ids to obtain a
 * fresh URL. The bot token is only sent to the Discord API, never to CDN or
 * redirect targets.
 */
export async function downloadDiscordAttachment(
  rest: RequestClient,
  descriptor: InboundAttachmentDescriptor,
  event: InboundChannelEvent,
  options: { fetchImpl?: SafeFetchOptions["fetchImpl"]; timeoutMs?: number; maxBytes?: number } = {},
): Promise<{ content: Buffer; filename: string; mimeType: string }> {
  const maxBytes = options.maxBytes ?? MAX_ATTACHMENT_BYTES;
  if (descriptor.sizeBytes !== undefined && descriptor.sizeBytes > maxBytes) {
    throw classifyDiscordDeliveryError(new Error(`Attachment exceeds maximum allowed size of ${maxBytes} bytes`), {
      stage: "read", message: "Discord attachment download failed",
    });
  }
  let url = descriptor.url ?? descriptor.proxyUrl;
  let filename = descriptor.filename;
  let mimeType = descriptor.mimeType;
  let refreshedFromRest = false;

  if (!url) {
    const fresh = await fetchFreshAttachmentFromMessage(rest, descriptor, event);
    url = fresh.url;
    if (fresh.filename) filename = fresh.filename;
    if (fresh.mimeType) mimeType = fresh.mimeType;
    refreshedFromRest = true;
  }

  const fetchFromCdn = async (targetUrl: string) =>
    safeFetchBuffer(targetUrl, {
      allowedDomains: DISCORD_MEDIA_SSRF_POLICY.hostnameAllowlist,
      maxBytes,
      timeoutMs: options.timeoutMs ?? 30_000,
      fetchImpl: options.fetchImpl,
      // Deliberately no Authorization header: CDN URLs are signed and the bot
      // token must not leak to CDN or redirect targets (validated per hop).
    });

  let result: Awaited<ReturnType<typeof safeFetchBuffer>>;
  try {
    result = await fetchFromCdn(url);
  } catch (err) {
    if (!refreshedFromRest && err instanceof SafeFetchError && err.code === "http_error") {
      const fresh = await fetchFreshAttachmentFromMessage(rest, descriptor, event);
      url = fresh.url;
      if (fresh.filename) filename = fresh.filename;
      if (fresh.mimeType) mimeType = fresh.mimeType;
      result = await fetchFromCdn(url);
    } else {
      throw err;
    }
  }

  return {
    content: result.buffer,
    filename,
    mimeType: result.contentType?.split(";")[0]?.trim() || mimeType,
  };
}

function lastSegment(chatId: string): string | null {
  const parts = chatId.split("/").filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1]! : null;
}
