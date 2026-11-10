/**
 * Discord thread creation and archived-thread re-entry.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/send-DbILKgcm.mjs`
 * (lines 267-378) and `threading-BWz5lW0p.mjs` (lines 113-193). License: MIT,
 * Copyright (c) 2025 Peter Steinberger. The OpenClaw LLM thread-title call is
 * removed; the title is derived locally from the first line / message id.
 */

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

export function sanitizeDiscordThreadName(raw: string, fallbackId?: string): string {
  const firstLine = (raw ?? "").split("\n").map((l) => l.trim()).find((l) => l.length > 0) ?? "";
  const cleaned = firstLine.replace(/^[#>\-\*\s]+/, "").replace(/\s+/g, " ").trim();
  const name = truncateUtf16Safe(cleaned, DISCORD_THREAD_NAME_LIMIT).trim();
  if (name) return name;
  const fallback = (fallbackId ?? "").trim();
  return fallback ? truncateUtf16Safe(`Thread ${fallback}`, DISCORD_THREAD_NAME_LIMIT) : "Thread";
}

export interface DiscordChannelInfo {
  id: string;
  type?: number;
  guild_id?: string;
  parent_id?: string;
  name?: string;
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

function threadRecordMatchesName(thread: DiscordChannelInfo, name: string): boolean {
  return typeof thread.id === "string" && thread.id.trim().length > 0 && thread.name === name;
}

/**
 * Lists active threads in the parent channel. One exact name match is adopted.
 * Several matches, or a lookup that does not prove the thread is absent, throw
 * `failed` and must not create another thread. A confirmed empty list returns null.
 */
async function findActiveThreadByExactName(
  rest: RequestClient,
  channelId: string,
  name: string,
): Promise<DiscordChannelInfo | null> {
  let listed: unknown;
  try {
    listed = await rest.get(`/channels/${encodeURIComponent(channelId)}/threads/active`);
  } catch (err) {
    const classified = classifyDiscordDeliveryError(err, {
      stage: "read",
      message: "Discord thread lookup failed",
    });
    if (classified.code === "target_not_found" || classified.code === "not_allowed") {
      throw classified;
    }
    throw new ChannelDeliveryError(
      "Discord thread lookup did not confirm whether the thread exists",
      { outcome: "failed", code: "unavailable" },
    );
  }
  const threads =
    listed && typeof listed === "object" && Array.isArray((listed as { threads?: unknown }).threads)
      ? (listed as { threads: DiscordChannelInfo[] }).threads
      : null;
  if (!threads) {
    throw new ChannelDeliveryError(
      "Discord thread lookup did not confirm whether the thread exists",
      { outcome: "failed", code: "unavailable" },
    );
  }
  const matches = threads.filter((thread) => threadRecordMatchesName(thread, name));
  if (matches.length > 1) {
    throw new ChannelDeliveryError("Discord thread lookup matched more than one thread", {
      outcome: "failed",
      code: "unavailable",
    });
  }
  return matches[0] ?? null;
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

/** After an unconfirmed thread POST, adopt one verified active thread. Does not POST again. */
async function adoptUnconfirmedThread(
  rest: RequestClient,
  channelId: string,
  name: string,
): Promise<DiscordChannelInfo> {
  let found: DiscordChannelInfo | null;
  try {
    found = await findActiveThreadByExactName(rest, channelId, name);
  } catch (err) {
    if (
      err instanceof ChannelDeliveryError &&
      (err.code === "target_not_found" || err.code === "not_allowed")
    ) {
      throw err;
    }
    throw new ChannelDeliveryError("Discord thread creation was not confirmed", {
      outcome: "failed",
      code: "unavailable",
    });
  }
  if (!found?.id) {
    throw new ChannelDeliveryError("Discord thread creation was not confirmed", {
      outcome: "failed",
      code: "unavailable",
    });
  }
  return found;
}

function isDefiniteThreadPostFailure(err: unknown): boolean {
  if (err instanceof RateLimitError) return true;
  return err instanceof DiscordError && err.status < 500;
}

export async function createThreadDiscord(
  rest: RequestClient,
  params: {
    channelId: string;
    /** When set, creates a thread attached to this message; otherwise a standalone public thread. */
    messageId?: string | null;
    name: string;
    autoArchiveDuration?: number;
  },
): Promise<DiscordChannelInfo> {
  const existing = await findActiveThreadByExactName(rest, params.channelId, params.name);
  if (existing?.id) return existing;
  try {
    const created = await postDiscordThread(rest, params);
    if (created?.id) return created;
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
    return adoptUnconfirmedThread(rest, params.channelId, params.name);
  }
  return adoptUnconfirmedThread(rest, params.channelId, params.name);
}
