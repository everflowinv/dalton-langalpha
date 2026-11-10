import { parseSlashCommand, type ParsedSlashCommand } from "../../commands.js";
import { isManagedNativeCommandName } from "../../native-commands/manifest.js";
import type { NativeCommandName } from "../../native-commands/types.js";
import { makeInboundRequestKey } from "../../uuid.js";
import {
  buildDiscordChatAddress,
  buildDiscordThreadScopeKey,
  isDiscordThreadChannelType,
  type DiscordChannelContext,
} from "./inbound.js";

export const DISCORD_INTERACTION_TYPE_PING = 1;
export const DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND = 2;
export const DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE = 4;

export const DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT = 1;
export const DISCORD_COMMAND_OPTION_TYPE_STRING = 3;

export type ParsedSessionSlashCommand = Extract<ParsedSlashCommand, { kind: "session" }>;

export interface DiscordRawInteractionOption {
  name?: unknown;
  type?: unknown;
  value?: unknown;
  focused?: unknown;
  options?: unknown;
}

export interface DiscordRawInteractionData {
  id?: unknown;
  name?: unknown;
  type?: unknown;
  options?: unknown;
}

export interface DiscordRawInteractionPayload {
  id?: unknown;
  application_id?: unknown;
  type?: unknown;
  token?: unknown;
  version?: unknown;
  guild_id?: unknown;
  channel_id?: unknown;
  channel?: unknown;
  member?: unknown;
  user?: unknown;
  data?: unknown;
}

export interface ParsedDiscordInteractionBase {
  interactionId: string;
  applicationId: string;
  interactionToken: string;
  commandName: NativeCommandName;
  platformUserId: string;
  platformUsername?: string;
  chatType: "dm" | "group";
  chatAddress: string;
  chatId: string;
  chatName?: string;
  threadScopeKey: string;
  guildId?: string;
  channelId: string;
  parentId?: string;
  isThread: boolean;
  /**
   * True when stage-1 pre-defer validation accepted a guild interaction whose thread parent
   * or channel type was omitted from the raw payload and must be resolved/verified via REST
   * before any command effect is executed.
   */
  needsChannelResolution: boolean;
  sanitizedRawPayload: Record<string, unknown>;
}

export interface ParsedDiscordCommandInteraction extends ParsedDiscordInteractionBase {
  kind: "command";
  interactionType: typeof DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND;
  requestKey: string;
  workspaceArg?: string;
  parsedCommand: ParsedSessionSlashCommand;
}

export interface ParsedDiscordAutocompleteInteraction extends ParsedDiscordInteractionBase {
  kind: "autocomplete";
  interactionType: typeof DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE;
  focusedOptionName: "workspace";
  query: string;
}

export type ParsedDiscordInteraction =
  | ParsedDiscordCommandInteraction
  | ParsedDiscordAutocompleteInteraction;

export type DiscordInteractionRejectReason =
  | "invalid_payload"
  | "unverified_application_identity"
  | "application_id_mismatch"
  | "missing_interaction_id"
  | "missing_interaction_token"
  | "unsupported_interaction_type"
  | "invalid_command_data"
  | "unsupported_command_type"
  | "unsupported_command_name"
  | "invalid_command_options"
  | "missing_guild_member"
  | "missing_dm_user"
  | "user_identity_mismatch"
  | "bot_user_ignored"
  | "missing_channel_id"
  | "channel_guild_mismatch"
  | "invalid_dm_channel"
  | "missing_thread_parent";

export type ParseDiscordInteractionResult =
  | { ok: true; interaction: ParsedDiscordInteraction }
  | { ok: false; reason: DiscordInteractionRejectReason; detail?: string };

export interface ParseDiscordInteractionOptions {
  /**
   * Application ID verified against `/api/v10/oauth2/applications/@me` and `DALTON_DISCORD_APPLICATION_ID`.
   * Must NEVER be a fallback to `botUserId`.
   */
  verifiedApplicationId?: string | null;
  botUserId?: string;
  resolveChannelInfo?: (channelId: string) => Promise<DiscordChannelContext | null>;
  /**
   * When true, allows stage-1 validation to succeed if a guild thread's `parent_id` or channel `type`
   * is omitted from the raw payload and not yet cached, setting `needsChannelResolution = true`
   * so the adapter can defer privately within the 3-second budget before resolving the channel via REST.
   * Before executing any command effect, stage-2 validation must run with `allowUnresolvedThreadParent: false`.
   */
  allowUnresolvedThreadParent?: boolean;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function mapDiscordNativeCommandToSlashCommand(
  commandName: NativeCommandName,
  workspaceArg?: string,
): ParsedSessionSlashCommand | null {
  let parsed: ParsedSlashCommand | null = null;
  switch (commandName) {
    case "main":
      parsed = parseSlashCommand("/main");
      break;
    case "new":
      parsed = parseSlashCommand("/new");
      break;
    case "stop":
      parsed = parseSlashCommand("/stop");
      break;
    case "workspace": {
      if (workspaceArg !== undefined && /[\r\n]/.test(workspaceArg)) {
        return null;
      }
      const trimmed = (workspaceArg ?? "").trim();
      if (!trimmed) {
        parsed = parseSlashCommand("/workspace");
      } else {
        parsed = parseSlashCommand(`/workspace ${trimmed}`);
      }
      break;
    }
    default:
      return null;
  }
  if (!parsed || parsed.kind !== "session") {
    return null;
  }
  return parsed;
}

export async function parseDiscordInteractionCreate(
  raw: unknown,
  options: ParseDiscordInteractionOptions,
): Promise<ParseDiscordInteractionResult> {
  if (!isRecord(raw)) {
    return { ok: false, reason: "invalid_payload", detail: "Interaction payload is not an object" };
  }

  const verifiedAppId = typeof options.verifiedApplicationId === "string"
    ? options.verifiedApplicationId.trim()
    : "";
  if (!verifiedAppId) {
    return {
      ok: false,
      reason: "unverified_application_identity",
      detail: "Bot application identity is not verified",
    };
  }

  const interactionId = typeof raw.id === "string" ? raw.id.trim() : "";
  if (!interactionId) {
    return { ok: false, reason: "missing_interaction_id" };
  }

  const payloadAppId = typeof raw.application_id === "string" ? raw.application_id.trim() : "";
  if (!payloadAppId || payloadAppId !== verifiedAppId) {
    return {
      ok: false,
      reason: "application_id_mismatch",
      detail: `Interaction application_id ${payloadAppId || "<empty>"} does not match verified application_id ${verifiedAppId}`,
    };
  }

  const interactionToken = typeof raw.token === "string" ? raw.token.trim() : "";
  if (!interactionToken) {
    return { ok: false, reason: "missing_interaction_token" };
  }

  const interactionType = raw.type;
  if (
    interactionType !== DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND &&
    interactionType !== DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE
  ) {
    return {
      ok: false,
      reason: "unsupported_interaction_type",
      detail: `Unsupported interaction type: ${String(interactionType)}`,
    };
  }

  if (!isRecord(raw.data)) {
    return { ok: false, reason: "invalid_command_data" };
  }
  const data = raw.data as DiscordRawInteractionData;
  if (data.type !== undefined && data.type !== DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT) {
    return {
      ok: false,
      reason: "unsupported_command_type",
      detail: `Unsupported application command type: ${String(data.type)}`,
    };
  }

  const rawCommandName = typeof data.name === "string" ? data.name.trim().toLowerCase() : "";
  if (!isManagedNativeCommandName(rawCommandName)) {
    return {
      ok: false,
      reason: "unsupported_command_name",
      detail: `Unsupported native command name: ${rawCommandName || "<empty>"}`,
    };
  }
  const commandName: NativeCommandName = rawCommandName;

  if (data.options !== undefined && !Array.isArray(data.options)) {
    return { ok: false, reason: "invalid_command_options", detail: "data.options must be an array" };
  }
  const rawOptions = (data.options as DiscordRawInteractionOption[] | undefined) ?? [];

  let workspaceOptionValue: string | undefined;
  let workspaceOptionFocused = false;
  if (commandName !== "workspace") {
    if (rawOptions.length > 0) {
      return {
        ok: false,
        reason: "invalid_command_options",
        detail: `Command /${commandName} does not accept options`,
      };
    }
    if (interactionType === DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE) {
      return {
        ok: false,
        reason: "invalid_command_options",
        detail: `Command /${commandName} does not support autocomplete`,
      };
    }
  } else {
    if (rawOptions.length > 1) {
      return {
        ok: false,
        reason: "invalid_command_options",
        detail: "Command /workspace accepts at most one option",
      };
    }
    if (rawOptions.length === 1) {
      const opt = rawOptions[0];
      if (!isRecord(opt)) {
        return { ok: false, reason: "invalid_command_options", detail: "Option must be an object" };
      }
      if (opt.name !== "workspace") {
        return {
          ok: false,
          reason: "invalid_command_options",
          detail: `Unexpected option name on /workspace: ${String(opt.name)}`,
        };
      }
      if (opt.type !== DISCORD_COMMAND_OPTION_TYPE_STRING) {
        return {
          ok: false,
          reason: "invalid_command_options",
          detail: `Option workspace must have string type (3), got ${String(opt.type)}`,
        };
      }
      if (typeof opt.value !== "string") {
        return {
          ok: false,
          reason: "invalid_command_options",
          detail: "Option workspace value must be a string",
        };
      }
      if (opt.focused !== undefined && typeof opt.focused !== "boolean") {
        return {
          ok: false,
          reason: "invalid_command_options",
          detail: "Option focused flag must be a boolean",
        };
      }
      workspaceOptionValue = opt.value;
      workspaceOptionFocused = opt.focused === true;
    } else if (interactionType === DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE) {
      return {
        ok: false,
        reason: "invalid_command_options",
        detail: "Autocomplete on /workspace requires the workspace option",
      };
    }
  }

  // Validate guild / DM context and user / member relationship
  const rawGuildId = raw.guild_id;
  if (rawGuildId !== undefined && rawGuildId !== null && typeof rawGuildId !== "string") {
    return { ok: false, reason: "invalid_payload", detail: "guild_id must be a string when present" };
  }
  const guildId = typeof rawGuildId === "string" ? rawGuildId.trim() : "";
  const isGuild = guildId.length > 0;
  const chatType: "dm" | "group" = isGuild ? "group" : "dm";

  let userObj: Record<string, unknown> | null = null;
  if (isGuild) {
    if (!isRecord(raw.member) || !isRecord(raw.member.user)) {
      return {
        ok: false,
        reason: "missing_guild_member",
        detail: "Guild interaction must carry member.user",
      };
    }
    userObj = raw.member.user;
    if (isRecord(raw.user) && typeof raw.user.id === "string" && raw.user.id !== userObj.id) {
      return {
        ok: false,
        reason: "user_identity_mismatch",
        detail: "Interaction member.user.id and user.id mismatch",
      };
    }
  } else {
    if (raw.member !== undefined && raw.member !== null) {
      return {
        ok: false,
        reason: "invalid_dm_channel",
        detail: "DM interaction must not carry guild member object",
      };
    }
    if (!isRecord(raw.user)) {
      return {
        ok: false,
        reason: "missing_dm_user",
        detail: "DM interaction must carry user object",
      };
    }
    userObj = raw.user;
  }

  const platformUserId = typeof userObj.id === "string" ? userObj.id.trim() : "";
  if (!platformUserId) {
    return {
      ok: false,
      reason: isGuild ? "missing_guild_member" : "missing_dm_user",
      detail: "Interaction user id is empty",
    };
  }
  if (userObj.bot === true || (options.botUserId && platformUserId === options.botUserId)) {
    return { ok: false, reason: "bot_user_ignored" };
  }
  const platformUsername =
    (typeof userObj.global_name === "string" && userObj.global_name.trim()) ||
    (typeof userObj.username === "string" && userObj.username.trim()) ||
    undefined;

  // Validate channel / guild / thread relationship
  const channelObj = isRecord(raw.channel) ? raw.channel : undefined;
  const rawChannelId = typeof raw.channel_id === "string" ? raw.channel_id.trim() : "";
  const embeddedChannelId =
    channelObj && typeof channelObj.id === "string" ? channelObj.id.trim() : "";
  const channelId = rawChannelId || embeddedChannelId;
  if (!channelId) {
    return { ok: false, reason: "missing_channel_id" };
  }
  if (rawChannelId && embeddedChannelId && rawChannelId !== embeddedChannelId) {
    return {
      ok: false,
      reason: "missing_channel_id",
      detail: "Interaction channel_id and channel.id mismatch",
    };
  }

  const channelGuildId =
    channelObj && typeof channelObj.guild_id === "string" ? channelObj.guild_id.trim() : "";
  if (channelObj && channelObj.guild_id !== undefined && channelObj.guild_id !== null) {
    if (!isGuild || channelGuildId !== guildId) {
      return {
        ok: false,
        reason: "channel_guild_mismatch",
        detail: "Interaction channel.guild_id does not match guild_id",
      };
    }
  }

  const embeddedChannelType =
    channelObj && typeof channelObj.type === "number" ? channelObj.type : undefined;
  let parentId =
    channelObj && typeof channelObj.parent_id === "string" && channelObj.parent_id.trim()
      ? channelObj.parent_id.trim()
      : undefined;
  let chatName =
    channelObj && typeof channelObj.name === "string" && channelObj.name.trim()
      ? channelObj.name.trim()
      : undefined;
  const hasThreadMetadata = Boolean(channelObj && isRecord(channelObj.thread_metadata));
  let isThread = isDiscordThreadChannelType(embeddedChannelType) || hasThreadMetadata;
  let needsChannelResolution = false;

  if (!isGuild) {
    if (isThread || parentId || (embeddedChannelType !== undefined && embeddedChannelType !== 1)) {
      return {
        ok: false,
        reason: "invalid_dm_channel",
        detail: "DM interaction channel cannot be a guild or thread channel",
      };
    }
  } else {
    if (parentId && parentId === channelId) {
      return {
        ok: false,
        reason: "missing_thread_parent",
        detail: "Thread interaction parent_id cannot equal channel_id",
      };
    }

    let resolvedInfo: DiscordChannelContext | null = null;
    const needsLookup = embeddedChannelType === undefined || (isThread && !parentId);
    if (needsLookup && options.resolveChannelInfo) {
      resolvedInfo = await options.resolveChannelInfo(channelId);
      if (resolvedInfo) {
        if (resolvedInfo.channelId && resolvedInfo.channelId !== channelId) {
          return {
            ok: false,
            reason: "missing_channel_id",
            detail: "Resolved channelId does not match interaction channel_id",
          };
        }
        if ((resolvedInfo.guildId || !channelGuildId) && resolvedInfo.guildId !== guildId) {
          return {
            ok: false,
            reason: "channel_guild_mismatch",
            detail: "Resolved channel guildId does not match interaction guild_id",
          };
        }
        if (
          parentId &&
          resolvedInfo.parentId &&
          resolvedInfo.parentId.trim() &&
          resolvedInfo.parentId.trim() !== parentId
        ) {
          return {
            ok: false,
            reason: "missing_thread_parent",
            detail: "Resolved thread parentId does not match embedded parent_id",
          };
        }
        if (isDiscordThreadChannelType(resolvedInfo.type)) {
          isThread = true;
        }
        if (!parentId && resolvedInfo.parentId) {
          parentId = resolvedInfo.parentId.trim() || undefined;
        }
        if (!chatName && resolvedInfo.name) {
          chatName = resolvedInfo.name;
        }
      }
    }

    const effectiveChannelType =
      embeddedChannelType ??
      (typeof resolvedInfo?.type === "number" ? resolvedInfo.type : undefined);
    if (effectiveChannelType === 1 || effectiveChannelType === 3) {
      return {
        ok: false,
        reason: "channel_guild_mismatch",
        detail: "Guild interaction channel cannot have DM channel type",
      };
    }

    if (isThread) {
      if (!parentId || parentId === channelId) {
        if (options.allowUnresolvedThreadParent && !parentId) {
          needsChannelResolution = true;
        } else {
          return {
            ok: false,
            reason: "missing_thread_parent",
            detail: "Thread interaction is missing a valid parent channel id",
          };
        }
      }
    }
    if (effectiveChannelType === undefined) {
      if (options.allowUnresolvedThreadParent) {
        needsChannelResolution = true;
      } else {
        return {
          ok: false,
          reason: "missing_channel_id",
          detail: "Guild interaction channel type could not be resolved",
        };
      }
    }
  }

  const chatAddress = buildDiscordChatAddress({
    chatType,
    guildId: isGuild ? guildId : undefined,
    channelId,
    parentId,
    isThread,
  });
  const effectiveChannelForChatId = isThread && parentId ? parentId : channelId;
  const chatId = chatType === "dm" ? "@me" : `${guildId}/${effectiveChannelForChatId}`;
  const threadScopeKey = buildDiscordThreadScopeKey({
    chatType,
    platformUserId,
    guildId: isGuild ? guildId : undefined,
    channelId,
    parentId,
    isThread,
  });

  const sanitizedRawPayload: Record<string, unknown> = {
    native_interaction: true,
    discord_interaction: {
      id: interactionId,
      application_id: verifiedAppId,
      type: interactionType,
      command_name: commandName,
      guild_id: isGuild ? guildId : null,
      channel_id: channelId,
      parent_id: isThread && parentId ? parentId : null,
      is_thread: isThread,
      user_id: platformUserId,
    },
  };

  if (interactionType === DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE) {
    if (commandName !== "workspace" || (rawOptions.length === 1 && !workspaceOptionFocused)) {
      return {
        ok: false,
        reason: "invalid_command_options",
        detail: "Autocomplete interaction requires focused workspace option",
      };
    }
    return {
      ok: true,
      interaction: {
        kind: "autocomplete",
        interactionType: DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND_AUTOCOMPLETE,
        interactionId,
        applicationId: verifiedAppId,
        interactionToken,
        commandName,
        focusedOptionName: "workspace",
        query: workspaceOptionValue ?? "",
        platformUserId,
        platformUsername,
        chatType,
        chatAddress,
        chatId,
        chatName,
        threadScopeKey,
        guildId: isGuild ? guildId : undefined,
        channelId,
        parentId: isThread ? parentId : undefined,
        isThread,
        needsChannelResolution,
        sanitizedRawPayload,
      },
    };
  }

  const parsedCommand = mapDiscordNativeCommandToSlashCommand(commandName, workspaceOptionValue);
  if (!parsedCommand) {
    return {
      ok: false,
      reason: commandName === "workspace" ? "invalid_command_options" : "unsupported_command_name",
      detail:
        commandName === "workspace"
          ? "Invalid workspace option value"
          : `Failed to map native command /${commandName}`,
    };
  }

  return {
    ok: true,
    interaction: {
      kind: "command",
      interactionType: DISCORD_INTERACTION_TYPE_APPLICATION_COMMAND,
      interactionId,
      applicationId: verifiedAppId,
      interactionToken,
      commandName,
      requestKey: makeInboundRequestKey("discord", interactionId),
      workspaceArg: workspaceOptionValue,
      parsedCommand,
      platformUserId,
      platformUsername,
      chatType,
      chatAddress,
      chatId,
      chatName,
      threadScopeKey,
      guildId: isGuild ? guildId : undefined,
      channelId,
      parentId: isThread ? parentId : undefined,
      isThread,
      needsChannelResolution,
      sanitizedRawPayload,
    },
  };
}
