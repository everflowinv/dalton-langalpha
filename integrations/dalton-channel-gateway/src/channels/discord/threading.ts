/**
 * Discord thread creation and archived-thread re-entry.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/send-DbILKgcm.mjs`
 * (lines 267-378) and `threading-BWz5lW0p.mjs` (lines 113-193). License: MIT,
 * Copyright (c) 2025 Peter Steinberger. The OpenClaw LLM thread-title call is
 * removed; the visible title is derived locally from the first line.
 *
 * Active threads are read with `GET /guilds/{guild.id}/threads/active`
 * (`RESTGetAPIGuildThreadsResult` / `Routes.guildActiveThreads`). A thread
 * started from a message uses that message's id as the thread id. A standalone
 * thread has no Discord idempotency field, so recovery uses a mark derived from
 * the delivery id, parent channel, and bot user, carried in the thread name.
 */

import { createHash } from "node:crypto";
import type {
  APIThreadMember,
  ChannelType,
  RESTGetAPIGuildThreadsResult,
} from "discord-api-types/v10";
import { truncateUtf16Safe } from "../../shared/text.js";
import { ChannelDeliveryError } from "../adapter.js";
import {
  classifyDiscordDeliveryError,
  DiscordError,
  RateLimitError,
  type RequestClient,
} from "./rest.js";

export const DISCORD_THREAD_NAME_LIMIT = 100;
export const DISCORD_THREAD_AUTO_ARCHIVE_MINUTES = 60;
export const DISCORD_THREAD_REQUEST_MARK_PREFIX = "[#dlt:";

const DISCORD_THREAD_REQUEST_VERSION = "dalton-discord-thread-v1";
const DISCORD_THREAD_REQUEST_MARK_HASH_CHARS = 22;

type DiscordThreadType =
  | ChannelType.AnnouncementThread
  | ChannelType.PublicThread
  | ChannelType.PrivateThread;

export function sanitizeDiscordThreadName(raw: string, fallbackId?: string): string {
  const firstLine = (raw ?? "").split("\n").map((l) => l.trim()).find((l) => l.length > 0) ?? "";
  const cleaned = firstLine.replace(/^[#>\-\*\s]+/, "").replace(/\s+/g, " ").trim();
  const name = truncateUtf16Safe(cleaned, DISCORD_THREAD_NAME_LIMIT).trim();
  if (name) return name;
  const fallback = (fallbackId ?? "").trim();
  return fallback ? truncateUtf16Safe(`Thread ${fallback}`, DISCORD_THREAD_NAME_LIMIT) : "Thread";
}

/**
 * Stable recovery mark for one standalone thread. The same delivery id, parent
 * channel, and bot always produce the same mark. A display title is not part
 * of the input.
 */
export function discordThreadRequestMark(params: {
  deliveryKey: string;
  parentChannelId: string;
  botUserId: string;
}): string {
  const deliveryKey = params.deliveryKey.trim();
  const parentChannelId = params.parentChannelId.trim();
  const botUserId = params.botUserId.trim();
  if (!deliveryKey || !parentChannelId || !botUserId) {
    throw new ChannelDeliveryError(
      "Discord standalone thread recovery requires the delivery id, parent channel, and bot user",
      { outcome: "failed", code: "unavailable" },
    );
  }
  const digest = createHash("sha256")
    .update(
      [
        DISCORD_THREAD_REQUEST_VERSION,
        parentChannelId,
        botUserId,
        deliveryKey,
      ].join("\0"),
      "utf8",
    )
    .digest("base64url");
  const mark = `${DISCORD_THREAD_REQUEST_MARK_PREFIX}${digest.slice(0, DISCORD_THREAD_REQUEST_MARK_HASH_CHARS)}]`;
  if (mark.length >= DISCORD_THREAD_NAME_LIMIT) {
    throw new ChannelDeliveryError(
      "Discord thread request mark exceeds the thread name limit",
      { outcome: "failed", code: "unavailable" },
    );
  }
  return mark;
}

/** Keeps the request mark intact inside Discord's 100 UTF-16 code unit name limit. */
export function discordStandaloneThreadName(displayName: string, mark: string): string {
  if (mark.length === 0 || mark.length >= DISCORD_THREAD_NAME_LIMIT) {
    throw new ChannelDeliveryError(
      "Discord thread request mark exceeds the thread name limit",
      { outcome: "failed", code: "unavailable" },
    );
  }
  const room = DISCORD_THREAD_NAME_LIMIT - mark.length - 1;
  const prefix = room > 0 ? truncateUtf16Safe(displayName, room).trimEnd() : "";
  const name = prefix ? `${prefix} ${mark}` : mark;
  if (name.length > DISCORD_THREAD_NAME_LIMIT || !name.endsWith(mark)) {
    throw new ChannelDeliveryError(
      "Discord thread request mark exceeds the thread name limit",
      { outcome: "failed", code: "unavailable" },
    );
  }
  return name;
}

export interface DiscordChannelInfo {
  id: string;
  type?: number;
  guild_id?: string;
  parent_id?: string | null;
  name?: string;
  owner_id?: string | null;
  thread_metadata?: { archived?: boolean; locked?: boolean };
}

export async function getDiscordChannel(rest: RequestClient, channelId: string): Promise<DiscordChannelInfo> {
  try {
    return (await rest.get(`/channels/${encodeURIComponent(channelId)}`)) as DiscordChannelInfo;
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "read", message: "Discord channel lookup failed" });
  }
}

/** Unarchives a thread so a new message can be posted to it. No-op for non-threads. */
export async function ensureThreadUnarchived(rest: RequestClient, channel: DiscordChannelInfo): Promise<DiscordChannelInfo> {
  if (channel.thread_metadata?.archived) {
    try {
      const updated = (await rest.patch(`/channels/${encodeURIComponent(channel.id)}`, { archived: false })) as DiscordChannelInfo;
      return updated && typeof updated === "object" ? updated : { ...channel, thread_metadata: { ...channel.thread_metadata, archived: false } };
    } catch (err) {
      throw classifyDiscordDeliveryError(err, { stage: "read", message: "Discord thread unarchive failed" });
    }
  }
  return channel;
}

function isDiscordThreadType(type: number | undefined): type is DiscordThreadType {
  return type === 10 || type === 11 || type === 12;
}

function lookupNotConfirmed(): ChannelDeliveryError {
  return new ChannelDeliveryError(
    "Discord thread lookup did not confirm whether the thread exists",
    { outcome: "failed", code: "unavailable" },
  );
}

function creationNotConfirmed(): ChannelDeliveryError {
  return new ChannelDeliveryError("Discord thread creation was not confirmed", {
    outcome: "failed",
    code: "unavailable",
  });
}

/**
 * A 403/404 from a lookup is definite. Any other read failure, including a
 * timeout or 5xx, does not prove the thread is absent.
 */
function throwThreadLookupFailure(err: unknown): never {
  const classified = classifyDiscordDeliveryError(err, {
    stage: "read",
    message: "Discord thread lookup failed",
  });
  if (classified.code === "target_not_found" || classified.code === "not_allowed") {
    throw classified;
  }
  throw lookupNotConfirmed();
}

function activeGuildThreadsPath(guildId: string): `/guilds/${string}/threads/active` {
  return `/guilds/${encodeURIComponent(guildId)}/threads/active`;
}

interface ResolvedThreadParent {
  parentChannelId: string;
  guildId: string;
}

/**
 * The thread parent is the text channel. A category id on that channel is not
 * a thread parent. A thread id is resolved to its own parent channel and guild.
 */
async function resolveDiscordThreadParent(
  rest: RequestClient,
  channelId: string,
): Promise<ResolvedThreadParent> {
  const channel = await getDiscordChannel(rest, channelId);
  const source = isDiscordThreadType(channel.type) ? null : channel;
  const parent = source ?? (await (async () => {
    const parentId = channel.parent_id?.trim() ?? "";
    if (!parentId) throw lookupNotConfirmed();
    return getDiscordChannel(rest, parentId);
  })());
  const parentChannelId = parent.id?.trim() ?? "";
  const guildId = parent.guild_id?.trim() ?? "";
  if (!parentChannelId || !guildId || isDiscordThreadType(parent.type)) {
    throw lookupNotConfirmed();
  }
  return { parentChannelId, guildId };
}

function nameCarriesRequestMark(name: string | undefined, mark: string): boolean {
  return typeof name === "string" && name.endsWith(mark);
}

function memberConfirmsBot(
  threadId: string,
  botUserId: string,
  members: APIThreadMember[],
): boolean {
  return members.some((member) => {
    if (!isRecord(member) || member.id !== threadId) return false;
    const userId = member.user_id;
    return typeof userId !== "string" || userId.length === 0 || userId === botUserId;
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseActiveGuildThreads(listed: unknown): RESTGetAPIGuildThreadsResult | null {
  if (!isRecord(listed)) return null;
  const { threads, members } = listed;
  if (!Array.isArray(threads) || !Array.isArray(members)) return null;
  if (!threads.every(isRecord) || !members.every(isRecord)) return null;
  return listed as RESTGetAPIGuildThreadsResult;
}

interface MarkedThreadQuery {
  guildId: string;
  parentChannelId: string;
  botUserId: string;
  mark: string;
}

/** Parent, guild, thread type, and request mark. A display title is not an identity. */
function locatedMarkedThread(thread: DiscordChannelInfo, query: MarkedThreadQuery): boolean {
  const threadId = thread.id?.trim() ?? "";
  if (!threadId || !isDiscordThreadType(thread.type)) return false;
  if (thread.parent_id !== query.parentChannelId) return false;
  if (thread.guild_id !== query.guildId) return false;
  return nameCarriesRequestMark(thread.name, query.mark);
}

/**
 * Lists active threads in the parent channel's guild and keeps only threads
 * whose parent, guild, bot owner, membership, and request mark all match.
 * Threads from other channels in that guild are not candidates. A matching
 * mark whose owner or membership cannot be verified is not absence, so this
 * does not create another thread.
 */
async function findMarkedActiveThread(
  rest: RequestClient,
  query: MarkedThreadQuery,
): Promise<DiscordChannelInfo | null> {
  let listed: unknown;
  try {
    listed = await rest.get(activeGuildThreadsPath(query.guildId));
  } catch (err) {
    throwThreadLookupFailure(err);
  }
  const parsed = parseActiveGuildThreads(listed);
  if (!parsed) throw lookupNotConfirmed();
  const located = (parsed.threads as DiscordChannelInfo[]).filter((thread) =>
    locatedMarkedThread(thread, query),
  );
  if (located.some((thread) => (thread.owner_id ?? "").trim().length === 0)) {
    throw lookupNotConfirmed();
  }
  const owned = located.filter((thread) => thread.owner_id === query.botUserId);
  const confirmed = owned.filter((thread) =>
    memberConfirmsBot(thread.id.trim(), query.botUserId, parsed.members),
  );
  if (confirmed.length !== owned.length) throw lookupNotConfirmed();
  if (confirmed.length > 1) {
    throw new ChannelDeliveryError("Discord thread lookup matched more than one thread", {
      outcome: "failed",
      code: "unavailable",
    });
  }
  return confirmed[0] ?? null;
}

interface MessageThreadQuery {
  messageId: string;
  parentChannelId: string;
  guildId: string;
}

function messageThreadMatches(channel: DiscordChannelInfo, query: MessageThreadQuery): boolean {
  return channel.id === query.messageId
    && isDiscordThreadType(channel.type)
    && channel.parent_id === query.parentChannelId
    && channel.guild_id === query.guildId;
}

/**
 * A thread started from a message has that message's id. 404 means this
 * message has no thread. A channel that exists but does not match the parent,
 * guild, or thread type is a different resource and is not absence.
 */
async function findThreadStartedFromMessage(
  rest: RequestClient,
  query: MessageThreadQuery,
): Promise<DiscordChannelInfo | null> {
  let listed: unknown;
  try {
    listed = await rest.get(`/channels/${encodeURIComponent(query.messageId)}`);
  } catch (err) {
    const classified = classifyDiscordDeliveryError(err, {
      stage: "read",
      message: "Discord thread lookup failed",
    });
    if (classified.code === "target_not_found") return null;
    if (classified.code === "not_allowed") throw classified;
    throw lookupNotConfirmed();
  }
  const channel = listed as DiscordChannelInfo;
  if (!channel || typeof channel !== "object" || typeof channel.id !== "string") {
    throw lookupNotConfirmed();
  }
  if (!messageThreadMatches(channel, query)) {
    throw new ChannelDeliveryError("Discord thread lookup did not match the requested thread", {
      outcome: "failed",
      code: "unavailable",
    });
  }
  return channel;
}

async function postDiscordThread(
  rest: RequestClient,
  params: {
    channelId: string;
    messageId?: string | null;
    name: string;
    autoArchiveDuration?: number;
  },
): Promise<DiscordChannelInfo> {
  const body = {
    name: params.name,
    auto_archive_duration: params.autoArchiveDuration ?? DISCORD_THREAD_AUTO_ARCHIVE_MINUTES,
  };
  if (params.messageId) {
    return (await rest.post(
      `/channels/${encodeURIComponent(params.channelId)}/messages/${encodeURIComponent(params.messageId)}/threads`,
      body,
    )) as DiscordChannelInfo;
  }
  return (await rest.post(`/channels/${encodeURIComponent(params.channelId)}/threads`, {
    ...body,
    type: 11, // PUBLIC_THREAD
  })) as DiscordChannelInfo;
}

function isDefiniteThreadPostFailure(err: unknown): boolean {
  if (err instanceof RateLimitError) return true;
  return err instanceof DiscordError && err.status < 500;
}

async function adoptUnconfirmedThread(
  lookup: () => Promise<DiscordChannelInfo | null>,
): Promise<DiscordChannelInfo> {
  let found: DiscordChannelInfo | null;
  try {
    found = await lookup();
  } catch (err) {
    if (err instanceof ChannelDeliveryError) throw err;
    throw creationNotConfirmed();
  }
  if (!found?.id) throw creationNotConfirmed();
  return found;
}

function messageCreateConfirms(channel: DiscordChannelInfo, query: MessageThreadQuery): boolean {
  return messageThreadMatches(channel, query);
}

function markedCreateConfirms(channel: DiscordChannelInfo, query: MarkedThreadQuery): boolean {
  const threadId = channel.id?.trim() ?? "";
  if (!threadId || !isDiscordThreadType(channel.type)) return false;
  if (channel.parent_id !== query.parentChannelId || channel.guild_id !== query.guildId) return false;
  if (channel.owner_id !== query.botUserId) return false;
  return nameCarriesRequestMark(channel.name, query.mark);
}

export async function createThreadDiscord(
  rest: RequestClient,
  params: {
    channelId: string;
    /** When set, the thread id is this message id. Equal titles with different message ids are different requests. */
    messageId?: string | null;
    name: string;
    autoArchiveDuration?: number;
    /** Stable delivery id. Required for a standalone thread. Not a display title. */
    deliveryKey?: string | null;
    /** Bot user that must own a standalone thread before that thread can be reused. */
    botUserId?: string | null;
  },
): Promise<DiscordChannelInfo> {
  const parent = await resolveDiscordThreadParent(rest, params.channelId);
  const messageId = params.messageId?.trim() || null;
  if (messageId) {
    const query: MessageThreadQuery = {
      messageId,
      parentChannelId: parent.parentChannelId,
      guildId: parent.guildId,
    };
    const existing = await findThreadStartedFromMessage(rest, query);
    if (existing?.id) return existing;
    const postName = params.name;
    try {
      const created = await postDiscordThread(rest, {
        channelId: parent.parentChannelId,
        messageId,
        name: postName,
        autoArchiveDuration: params.autoArchiveDuration,
      });
      if (messageCreateConfirms(created, query)) return created;
    } catch (err) {
      if (isDefiniteThreadPostFailure(err)) {
        throw classifyDiscordDeliveryError(err, {
          stage: "send",
          message: "Discord thread creation failed",
        });
      }
      // A timeout or 5xx can happen after Discord has already created the thread.
      // That is not a message send: look the thread up instead of marking the
      // claim unknown or posting a second thread.
      return adoptUnconfirmedThread(() => findThreadStartedFromMessage(rest, query));
    }
    return adoptUnconfirmedThread(() => findThreadStartedFromMessage(rest, query));
  }

  const deliveryKey = params.deliveryKey?.trim() ?? "";
  const botUserId = params.botUserId?.trim() ?? "";
  if (!deliveryKey || !botUserId) {
    throw new ChannelDeliveryError(
      "Discord standalone thread recovery requires the delivery id, parent channel, and bot user",
      { outcome: "failed", code: "unavailable" },
    );
  }
  const mark = discordThreadRequestMark({
    deliveryKey,
    parentChannelId: parent.parentChannelId,
    botUserId,
  });
  const query: MarkedThreadQuery = {
    guildId: parent.guildId,
    parentChannelId: parent.parentChannelId,
    botUserId,
    mark,
  };
  const existing = await findMarkedActiveThread(rest, query);
  if (existing?.id) return existing;
  const postName = discordStandaloneThreadName(params.name, mark);
  try {
    const created = await postDiscordThread(rest, {
      channelId: parent.parentChannelId,
      name: postName,
      autoArchiveDuration: params.autoArchiveDuration,
    });
    if (markedCreateConfirms(created, query)) return created;
  } catch (err) {
    if (isDefiniteThreadPostFailure(err)) {
      throw classifyDiscordDeliveryError(err, {
        stage: "send",
        message: "Discord thread creation failed",
      });
    }
    return adoptUnconfirmedThread(() => findMarkedActiveThread(rest, query));
  }
  return adoptUnconfirmedThread(() => findMarkedActiveThread(rest, query));
}
