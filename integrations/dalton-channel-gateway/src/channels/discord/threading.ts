/**
 * Discord thread creation and archived-thread re-entry.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/send-DbILKgcm.mjs`
 * (lines 267-378) and `threading-BWz5lW0p.mjs` (lines 113-193). License: MIT,
 * Copyright (c) 2025 Peter Steinberger. The OpenClaw LLM thread-title call is
 * removed; the visible title is derived locally from the first line.
 *
 * Active threads are read with `GET /guilds/{guild.id}/threads/active`
 * (`RESTGetAPIGuildThreadsResult` / `Routes.guildActiveThreads`). The parsed
 * list is a locally narrowed value: a list that is not made of thread and
 * member objects is rejected, and it is not cast to the official result type.
 * A thread started from a message uses that message's id as the thread id. A
 * standalone thread has no Discord idempotency field, so recovery uses a mark
 * derived from the delivery id, parent channel, and bot user, carried in the
 * thread name. A failed lookup before the create request is retryable and does
 * not post. A well-formed empty list before that request is the signal to post
 * once. After the request has been sent, only a positive match is reused. An
 * empty or failed lookup then stays unknown, and the durable delivery record
 * is what stops another create. A local queue rejection never leaves the
 * gateway, so that attempt stays failed and does not look the thread up.
 * A channel GET is kept only when its id is a non-empty string and every
 * present optional field has the expected type. A text channel or DM does not
 * need thread-only fields. A malformed channel body stays a read failure.
 */

import { createHash } from "node:crypto";
import type { ChannelType } from "discord-api-types/v10";
import { truncateUtf16Safe } from "../../shared/text.js";
import { ChannelDeliveryError } from "../adapter.js";
import {
  classifyDiscordDeliveryError,
  DiscordError,
  DiscordQueueError,
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
  let body: unknown;
  try {
    body = await rest.get(`/channels/${encodeURIComponent(channelId)}`);
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "read", message: "Discord channel lookup failed" });
  }
  const channel = parseDiscordThreadChannel(body);
  if (!channel) {
    throw new ChannelDeliveryError("Discord channel lookup failed", {
      outcome: "failed",
      code: "unavailable",
    });
  }
  return channel;
}

/** Unarchives a thread so a new message can be posted to it. No-op for non-threads. */
export async function ensureThreadUnarchived(rest: RequestClient, channel: DiscordChannelInfo): Promise<DiscordChannelInfo> {
  if (!channel.thread_metadata?.archived) return channel;
  try {
    const updated = parseDiscordThreadChannel(
      await rest.patch(`/channels/${encodeURIComponent(channel.id)}`, { archived: false }),
    );
    // A successful unarchive whose body is not a channel still continues from
    // the channel that was already validated, marked no longer archived.
    return updated ?? {
      ...channel,
      thread_metadata: { ...channel.thread_metadata, archived: false },
    };
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "read", message: "Discord thread unarchive failed" });
  }
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
    outcome: "unknown",
    code: "unavailable",
  });
}

/**
 * A 403/404 from a lookup before a create request is definite. Any other read
 * failure, including a timeout or 5xx, does not prove the thread is absent.
 * After a create request, the lookup result is not classified here: an
 * unconfirmed attempt stays unknown.
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

interface ActiveGuildThreadMember {
  id?: string;
  user_id?: string;
  join_timestamp: string;
  flags: number;
}

interface ActiveGuildThreadList {
  threads: DiscordChannelInfo[];
  members: ActiveGuildThreadMember[];
}

function memberConfirmsBot(
  threadId: string,
  botUserId: string,
  members: readonly ActiveGuildThreadMember[],
): boolean {
  return members.some((member) => {
    if (member.id !== threadId) return false;
    const userId = member.user_id;
    return typeof userId !== "string" || userId.length === 0 || userId === botUserId;
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function optionalInteger(value: unknown): boolean {
  return value === undefined || (typeof value === "number" && Number.isInteger(value));
}

/**
 * Copies one channel object. The id must be a non-empty string. A present
 * field of the wrong type rejects the entry. Thread type, owner, name mark,
 * and thread metadata are not required, so a text channel or DM still passes.
 */
function parseDiscordThreadChannel(value: unknown): DiscordChannelInfo | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== "string") return null;
  const id = value.id.trim();
  if (id.length === 0) return null;
  const type = value.type;
  if (type !== undefined && (typeof type !== "number" || !Number.isInteger(type))) return null;
  const guildId = value.guild_id;
  if (guildId !== undefined && typeof guildId !== "string") return null;
  const parentId = value.parent_id;
  if (parentId !== undefined && parentId !== null && typeof parentId !== "string") return null;
  const name = value.name;
  if (name !== undefined && typeof name !== "string") return null;
  const ownerId = value.owner_id;
  if (ownerId !== undefined && ownerId !== null && typeof ownerId !== "string") return null;
  if (!optionalInteger(value.flags)) return null;
  const metadata = value.thread_metadata;
  if (metadata !== undefined && !isRecord(metadata)) return null;
  if (isRecord(metadata)) {
    if (metadata.archived !== undefined && typeof metadata.archived !== "boolean") return null;
    if (metadata.locked !== undefined && typeof metadata.locked !== "boolean") return null;
    if (!optionalInteger(metadata.flags)) return null;
  }
  const thread: DiscordChannelInfo = { id };
  if (typeof type === "number") thread.type = type;
  if (typeof guildId === "string") thread.guild_id = guildId;
  if (typeof parentId === "string" || parentId === null) thread.parent_id = parentId;
  if (typeof name === "string") thread.name = name;
  if (typeof ownerId === "string" || ownerId === null) thread.owner_id = ownerId;
  if (isRecord(metadata)) {
    const archived = metadata.archived;
    const locked = metadata.locked;
    thread.thread_metadata = {
      ...(typeof archived === "boolean" ? { archived } : {}),
      ...(typeof locked === "boolean" ? { locked } : {}),
    };
  }
  return thread;
}

/** `join_timestamp` and integer `flags` are required. `id` and `user_id` stay optional. */
function parseActiveThreadMember(value: unknown): ActiveGuildThreadMember | null {
  if (!isRecord(value)) return null;
  const joinTimestamp = value.join_timestamp;
  const flags = value.flags;
  if (typeof joinTimestamp !== "string" || joinTimestamp.length === 0) return null;
  if (typeof flags !== "number" || !Number.isInteger(flags)) return null;
  const id = value.id;
  if (id !== undefined && typeof id !== "string") return null;
  const userId = value.user_id;
  if (userId !== undefined && typeof userId !== "string") return null;
  const member: ActiveGuildThreadMember = { join_timestamp: joinTimestamp, flags };
  if (typeof id === "string") member.id = id;
  if (typeof userId === "string") member.user_id = userId;
  return member;
}

function parseActiveGuildThreads(listed: unknown): ActiveGuildThreadList | null {
  if (!isRecord(listed)) return null;
  const { threads, members } = listed;
  if (!Array.isArray(threads) || !Array.isArray(members)) return null;
  const parsedThreads: DiscordChannelInfo[] = [];
  for (const entry of threads) {
    const thread = parseDiscordThreadChannel(entry);
    if (!thread) return null;
    parsedThreads.push(thread);
  }
  const parsedMembers: ActiveGuildThreadMember[] = [];
  for (const entry of members) {
    const member = parseActiveThreadMember(entry);
    if (!member) return null;
    parsedMembers.push(member);
  }
  return { threads: parsedThreads, members: parsedMembers };
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
  const located = parsed.threads.filter((thread) => locatedMarkedThread(thread, query));
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
  const channel = parseDiscordThreadChannel(listed);
  if (!channel) throw lookupNotConfirmed();
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
): Promise<DiscordChannelInfo | null> {
  const body = {
    name: params.name,
    auto_archive_duration: params.autoArchiveDuration ?? DISCORD_THREAD_AUTO_ARCHIVE_MINUTES,
  };
  const response = params.messageId
    ? await rest.post(
      `/channels/${encodeURIComponent(params.channelId)}/messages/${encodeURIComponent(params.messageId)}/threads`,
      body,
    )
    : await rest.post(`/channels/${encodeURIComponent(params.channelId)}/threads`, {
      ...body,
      type: 11, // PUBLIC_THREAD
    });
  return parseDiscordThreadChannel(response);
}

/**
 * The create request did not leave the gateway, or Discord rejected it.
 * A timeout, a lost response, and HTTP 500 and above are not definite:
 * those stay unknown and must not create again.
 */
function isDefiniteThreadPostFailure(err: unknown): boolean {
  if (err instanceof DiscordQueueError) return true;
  if (err instanceof RateLimitError) return true;
  return err instanceof DiscordError && err.status < 500;
}

/**
 * The create request was already sent. A positive match is the created thread.
 * An empty list, a 403/404/5xx, or a malformed lookup does not prove the
 * platform rejected the create, so the outcome stays unknown.
 */
async function adoptUnconfirmedThread(
  lookup: () => Promise<DiscordChannelInfo | null>,
): Promise<DiscordChannelInfo> {
  let found: DiscordChannelInfo | null = null;
  try {
    found = await lookup();
  } catch {
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
      if (created && messageCreateConfirms(created, query)) return created;
    } catch (err) {
      if (isDefiniteThreadPostFailure(err)) {
        throw classifyDiscordDeliveryError(err, {
          stage: "send",
          message: "Discord thread creation failed",
        });
      }
      // A timeout or 5xx can happen after Discord has created the thread.
      // One lookup may reuse it. Anything short of a positive match stays unknown.
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
    if (created && markedCreateConfirms(created, query)) return created;
  } catch (err) {
    if (isDefiniteThreadPostFailure(err)) {
      throw classifyDiscordDeliveryError(err, {
        stage: "send",
        message: "Discord thread creation failed",
      });
    }
    // The request was sent. A later empty active-thread list is not absence.
    return adoptUnconfirmedThread(() => findMarkedActiveThread(rest, query));
  }
  return adoptUnconfirmedThread(() => findMarkedActiveThread(rest, query));
}
