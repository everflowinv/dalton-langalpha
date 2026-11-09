/**
 * Discord thread creation and archived-thread re-entry.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/send-DbILKgcm.mjs`
 * (lines 267-378) and `threading-BWz5lW0p.mjs` (lines 113-193). License: MIT,
 * Copyright (c) 2025 Peter Steinberger. The OpenClaw LLM thread-title call is
 * removed; the title is derived locally from the first line / message id.
 */

import { truncateUtf16Safe } from "../../shared/text.js";
import type { RequestClient } from "./rest.js";
import { classifyDiscordDeliveryError } from "./rest.js";

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
  const body = {
    name: params.name,
    auto_archive_duration: params.autoArchiveDuration ?? DISCORD_THREAD_AUTO_ARCHIVE_MINUTES,
  };
  try {
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
  } catch (err) {
    throw classifyDiscordDeliveryError(err, { stage: "send", message: "Discord thread creation failed" });
  }
}
