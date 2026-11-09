/**
 * Shared fixed native command manifest (`main`, `workspace`, `new`, `stop`) and
 * platform payload builders / idempotent comparators for Feishu and Discord.
 *
 * Feishu slash-command request/path conventions are adapted from `larksuite/cli`
 * (`shortcuts/application/slash_command_create.go`, `slash_command_common.go`,
 * `slash_command_resolve.go`), Copyright (c) 2026 Lark Technologies Pte. Ltd.,
 * licensed under the MIT License.
 */

import {
  type DiscordRegistrationScope,
  type NativeCommandDefinition,
  type NativeCommandName,
  NativeCommandRegistrationError,
} from "./types.js";

export const FEISHU_SLASH_COMMAND_BASE_PATH =
  "/open-apis/application/v7/app_slash_commands";

export const FEISHU_CLIENT_CACHE_NOTE =
  "飞书服务端命令已更新；飞书客户端命令面板约有 5 分钟本地缓存，列表接口会立即反映最新状态。";

export const DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT = 1;
export const DISCORD_COMMAND_OPTION_TYPE_STRING = 3;
export const DISCORD_INTEGRATION_TYPE_GUILD_INSTALL = 0;
export const DISCORD_INTERACTION_CONTEXT_GUILD = 0;
export const DISCORD_INTERACTION_CONTEXT_BOT_DM = 1;

export const DISCORD_GLOBAL_INTEGRATION_TYPES: readonly number[] = [
  DISCORD_INTEGRATION_TYPE_GUILD_INSTALL,
];

export const DISCORD_GLOBAL_CONTEXTS: readonly number[] = [
  DISCORD_INTERACTION_CONTEXT_GUILD,
  DISCORD_INTERACTION_CONTEXT_BOT_DM,
];

/**
 * Canonical fixed native commands shared across channels.
 * Workspace names are never statically registered here; Discord `workspace`
 * exposes a single optional string argument with `autocomplete: true`.
 */
export const DALTON_NATIVE_COMMANDS: readonly NativeCommandDefinition[] = [
  {
    name: "main",
    description: "返回 Chief of Staff",
    options: [],
  },
  {
    name: "workspace",
    description: "查看可访问的工作区",
    options: [
      {
        name: "workspace",
        description: "要切换的工作区名称，留空查看可访问列表",
        type: "string",
        required: false,
        autocomplete: true,
      },
    ],
  },
  {
    name: "new",
    description: "在当前工作区新建会话",
    options: [],
  },
  {
    name: "stop",
    description: "停止当前任务",
    options: [],
  },
] as const;

export const DALTON_NATIVE_COMMAND_MAP: ReadonlyMap<
  string,
  NativeCommandDefinition
> = new Map(DALTON_NATIVE_COMMANDS.map((cmd) => [cmd.name, cmd]));

export function isManagedNativeCommandName(
  value: string,
): value is NativeCommandName {
  return DALTON_NATIVE_COMMAND_MAP.has(value);
}

export function validateNativeCommandName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) {
    throw new NativeCommandRegistrationError(
      "invalid_config",
      "Native command name must not be empty.",
    );
  }
  if (trimmed.startsWith("/")) {
    throw new NativeCommandRegistrationError(
      "invalid_config",
      `Native command name "${trimmed}" must not start with "/".`,
    );
  }
  return trimmed;
}

/**
 * Validates and percent-encodes a remote command ID before interpolating it
 * into a `PATCH .../{command_id}` URL segment.
 */
export function encodeCommandIdPathSegment(
  rawId: unknown,
  label: string,
): string {
  if (typeof rawId !== "string") {
    throw new NativeCommandRegistrationError(
      "invalid_remote_command",
      `${label} is missing a valid string command ID.`,
    );
  }
  const trimmed = rawId.trim();
  if (
    !trimmed ||
    trimmed === "." ||
    trimmed === ".." ||
    trimmed.includes("/") ||
    trimmed.includes("\\") ||
    /\s/.test(trimmed)
  ) {
    throw new NativeCommandRegistrationError(
      "invalid_remote_command",
      `${label} returned an invalid command ID.`,
    );
  }
  return encodeURIComponent(trimmed);
}

// ---------------------------------------------------------------------------
// Feishu `/open-apis/application/v7/app_slash_commands` Payload & Diff
// ---------------------------------------------------------------------------

export interface FeishuRemoteSlashCommandItem {
  command_id?: unknown;
  command?: unknown;
  description?: unknown;
  icon?: unknown;
  [key: string]: unknown;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractNonEmptyStringMap(
  value: unknown,
): Record<string, string> | undefined {
  if (!isPlainRecord(value)) {
    return undefined;
  }
  const entries: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(value)) {
    if (typeof v === "string" && k.trim().length > 0 && v.trim().length > 0) {
      entries.push([k, v]);
    }
  }
  if (entries.length === 0) {
    return undefined;
  }
  return Object.fromEntries(entries);
}

function areStringMapsEqual(
  a: Readonly<Record<string, string>> | undefined,
  b: Readonly<Record<string, string>> | undefined,
): boolean {
  const aKeys = a ? Object.keys(a).sort() : [];
  const bKeys = b ? Object.keys(b).sort() : [];
  if (aKeys.length !== bKeys.length) {
    return false;
  }
  for (let i = 0; i < aKeys.length; i += 1) {
    const key = aKeys[i]!;
    if (key !== bKeys[i] || a![key] !== b![key]) {
      return false;
    }
  }
  return true;
}

/**
 * Builds Feishu v7 slash-command request body (`POST` includes `command`;
 * `PATCH` omits `command` so only mutable fields are updated).
 * `icon` sits at the top level beside `description`.
 */
export function buildFeishuSlashCommandBody(params: {
  command?: string;
  description: string;
  i18n?: Readonly<Record<string, string>>;
  iconKey?: string;
}): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (params.command !== undefined && params.command !== "") {
    body.command = validateNativeCommandName(params.command);
  }
  const i18nEntries =
    params.i18n && Object.keys(params.i18n).length > 0
      ? { ...params.i18n }
      : undefined;
  if (params.description !== "" || i18nEntries) {
    const desc: Record<string, unknown> = {};
    if (params.description !== "") {
      desc.default_value = params.description;
    }
    if (i18nEntries) {
      desc.i18n = i18nEntries;
    }
    body.description = desc;
  }
  if (params.iconKey !== undefined && params.iconKey !== "") {
    body.icon = { icon_key: params.iconKey };
  }
  return body;
}

export function buildFeishuCreatePayload(
  spec: NativeCommandDefinition,
): Record<string, unknown> {
  return buildFeishuSlashCommandBody({
    command: spec.name,
    description: spec.description,
    i18n: spec.descriptionI18n,
    iconKey: spec.iconKey,
  });
}

export function diffFeishuSlashCommand(
  existing: FeishuRemoteSlashCommandItem,
  spec: NativeCommandDefinition,
): {
  needsUpdate: boolean;
  changedFields: string[];
  patchBody?: Record<string, unknown>;
} {
  const changedFields: string[] = [];
  const descObj = isPlainRecord(existing.description)
    ? existing.description
    : undefined;
  const existingDefault =
    typeof descObj?.default_value === "string"
      ? descObj.default_value
      : undefined;

  if (existingDefault !== spec.description) {
    changedFields.push("description.default_value");
  }

  const existingI18n = extractNonEmptyStringMap(descObj?.i18n);
  if (
    spec.descriptionI18n !== undefined &&
    !areStringMapsEqual(existingI18n, spec.descriptionI18n)
  ) {
    changedFields.push("description.i18n");
  }

  const iconObj = isPlainRecord(existing.icon) ? existing.icon : undefined;
  const existingIconKey =
    typeof iconObj?.icon_key === "string" ? iconObj.icon_key : undefined;
  if (spec.iconKey !== undefined && existingIconKey !== spec.iconKey) {
    changedFields.push("icon.icon_key");
  }

  if (changedFields.length === 0) {
    return { needsUpdate: false, changedFields: [] };
  }

  // Preserve unmanaged existing `description.i18n` if `description` is patched,
  // because Feishu replaces the `i18n` map when `description` is rewritten.
  const effectiveI18n =
    spec.descriptionI18n !== undefined ? spec.descriptionI18n : existingI18n;

  const patchBody = buildFeishuSlashCommandBody({
    description: spec.description,
    i18n: effectiveI18n,
    iconKey: spec.iconKey,
  });

  return {
    needsUpdate: true,
    changedFields,
    patchBody,
  };
}

// ---------------------------------------------------------------------------
// Discord API v10 Application Commands Payload & Diff
// ---------------------------------------------------------------------------

export interface DiscordRemoteCommandOption {
  type?: unknown;
  name?: unknown;
  description?: unknown;
  required?: unknown;
  autocomplete?: unknown;
  choices?: unknown;
  options?: unknown;
  min_length?: unknown;
  max_length?: unknown;
  name_localizations?: unknown;
  description_localizations?: unknown;
  [key: string]: unknown;
}

export interface DiscordRemoteApplicationCommand {
  id?: unknown;
  type?: unknown;
  application_id?: unknown;
  guild_id?: unknown;
  name?: unknown;
  description?: unknown;
  options?: unknown;
  integration_types?: unknown;
  contexts?: unknown;
  name_localizations?: unknown;
  description_localizations?: unknown;
  version?: unknown;
  [key: string]: unknown;
}

function normalizeNumberSet(value: unknown): number[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const nums = new Set<number>();
  for (const item of value) {
    if (typeof item !== "number" || !Number.isInteger(item)) {
      return null;
    }
    nums.add(item);
  }
  return [...nums].sort((a, b) => a - b);
}

function areNumberSetsEqual(
  actual: unknown,
  expected: readonly number[],
): boolean {
  const normalizedActual = normalizeNumberSet(actual);
  if (!normalizedActual) {
    return false;
  }
  const normalizedExpected = [...new Set(expected)].sort((a, b) => a - b);
  if (normalizedActual.length !== normalizedExpected.length) {
    return false;
  }
  for (let i = 0; i < normalizedActual.length; i += 1) {
    if (normalizedActual[i] !== normalizedExpected[i]) {
      return false;
    }
  }
  return true;
}

function buildDiscordOptionPayload(
  specOpt: NativeCommandDefinition["options"][number],
  existingOpt?: DiscordRemoteCommandOption,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    name: specOpt.name,
    description: specOpt.description,
    type: DISCORD_COMMAND_OPTION_TYPE_STRING,
    required: specOpt.required,
    autocomplete: specOpt.autocomplete,
  };
  // Preserve unmanaged localization maps on an existing option of the same name.
  if (existingOpt && existingOpt.name === specOpt.name) {
    const nameLocalizations = extractNonEmptyStringMap(
      existingOpt.name_localizations,
    );
    if (nameLocalizations) {
      payload.name_localizations = nameLocalizations;
    }
    const descriptionLocalizations = extractNonEmptyStringMap(
      existingOpt.description_localizations,
    );
    if (descriptionLocalizations) {
      payload.description_localizations = descriptionLocalizations;
    }
  }
  return payload;
}

function doDiscordOptionsMatch(
  existingOptions: unknown,
  specOptions: NativeCommandDefinition["options"],
): boolean {
  if (specOptions.length === 0) {
    return (
      existingOptions === undefined ||
      existingOptions === null ||
      (Array.isArray(existingOptions) && existingOptions.length === 0)
    );
  }
  if (
    !Array.isArray(existingOptions) ||
    existingOptions.length !== specOptions.length
  ) {
    return false;
  }

  for (let i = 0; i < specOptions.length; i += 1) {
    const specOpt = specOptions[i]!;
    const rawOpt = existingOptions[i];
    if (!isPlainRecord(rawOpt)) {
      return false;
    }
    const existingRequired =
      rawOpt.required === undefined ? false : rawOpt.required;
    const existingAutocomplete =
      rawOpt.autocomplete === undefined ? false : rawOpt.autocomplete;
    const hasChoices =
      rawOpt.choices !== undefined &&
      rawOpt.choices !== null &&
      (!Array.isArray(rawOpt.choices) || rawOpt.choices.length > 0);
    const hasNestedOptions =
      rawOpt.options !== undefined &&
      rawOpt.options !== null &&
      (!Array.isArray(rawOpt.options) || rawOpt.options.length > 0);
    const hasLengthBounds =
      (rawOpt.min_length !== undefined && rawOpt.min_length !== null) ||
      (rawOpt.max_length !== undefined && rawOpt.max_length !== null);

    if (
      rawOpt.name !== specOpt.name ||
      rawOpt.description !== specOpt.description ||
      rawOpt.type !== DISCORD_COMMAND_OPTION_TYPE_STRING ||
      existingRequired !== specOpt.required ||
      existingAutocomplete !== specOpt.autocomplete ||
      hasChoices ||
      hasNestedOptions ||
      hasLengthBounds
    ) {
      return false;
    }
  }

  return true;
}

export function buildDiscordCreatePayload(
  spec: NativeCommandDefinition,
  scope: DiscordRegistrationScope,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: validateNativeCommandName(spec.name),
    type: DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT,
    description: spec.description,
  };

  if (scope.kind === "global") {
    body.integration_types = [...DISCORD_GLOBAL_INTEGRATION_TYPES];
    body.contexts = [...DISCORD_GLOBAL_CONTEXTS];
  }

  if (spec.options.length > 0) {
    body.options = spec.options.map((opt) => buildDiscordOptionPayload(opt));
  }

  return body;
}

export function diffDiscordApplicationCommand(
  existing: DiscordRemoteApplicationCommand,
  spec: NativeCommandDefinition,
  scope: DiscordRegistrationScope,
): {
  needsUpdate: boolean;
  changedFields: string[];
  patchBody?: Record<string, unknown>;
} {
  const changedFields: string[] = [];
  const patchBody: Record<string, unknown> = {};

  if (existing.description !== spec.description) {
    changedFields.push("description");
    patchBody.description = spec.description;
  }

  if (scope.kind === "global") {
    if (
      !areNumberSetsEqual(
        existing.integration_types,
        DISCORD_GLOBAL_INTEGRATION_TYPES,
      )
    ) {
      changedFields.push("integration_types");
      patchBody.integration_types = [...DISCORD_GLOBAL_INTEGRATION_TYPES];
    }
    if (!areNumberSetsEqual(existing.contexts, DISCORD_GLOBAL_CONTEXTS)) {
      changedFields.push("contexts");
      patchBody.contexts = [...DISCORD_GLOBAL_CONTEXTS];
    }
  }

  if (!doDiscordOptionsMatch(existing.options, spec.options)) {
    changedFields.push("options");
    if (spec.options.length === 0) {
      patchBody.options = [];
    } else {
      const existingArr = Array.isArray(existing.options)
        ? existing.options
        : [];
      patchBody.options = spec.options.map((opt) => {
        const candidate = existingArr.find(
          (item) => isPlainRecord(item) && item.name === opt.name,
        );
        return buildDiscordOptionPayload(
          opt,
          candidate as DiscordRemoteCommandOption | undefined,
        );
      });
    }
  }

  if (changedFields.length === 0) {
    return { needsUpdate: false, changedFields: [] };
  }

  return {
    needsUpdate: true,
    changedFields,
    patchBody,
  };
}
