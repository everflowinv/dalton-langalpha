/**
 * Discord API v10 native application-command registration library.
 *
 * Verifies bot identity via `GET /api/v10/oauth2/applications/@me`, distinguishes
 * global (`/applications/{id}/commands` with `GUILD` + `BOT_DM` contexts) and
 * guild-specific (`/applications/{id}/guilds/{guild_id}/commands`) paths, and
 * performs targeted `POST` / `PATCH` mutations without bulk overwrite (`PUT`)
 * or deleting unrelated commands.
 */

import { validateDiscordSnowflake } from "./config.js";
import {
  AmbiguousMutationError,
  BoundedHttpClient,
  collectPaginatedItems,
  DISCORD_TRUSTED_API_ORIGIN,
  type JsonHttpResponse,
  resolveVerifiedApiOrigin,
} from "./http.js";
import {
  buildDiscordCreatePayload,
  DALTON_NATIVE_COMMAND_MAP,
  DALTON_NATIVE_COMMANDS,
  diffDiscordApplicationCommand,
  DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT,
  type DiscordRemoteApplicationCommand,
  encodeCommandIdPathSegment,
  isManagedNativeCommandName,
} from "./manifest.js";
import {
  createSecretAwareLogger,
  scrubNativeCommandSecrets,
} from "./redact.js";
import {
  type AppliedCommandOutcome,
  type CommonRegistrationRuntimeOptions,
  type DiscordRegistrationScope,
  type NativeCommandName,
  NativeCommandRegistrationError,
  type NativeCommandSyncResult,
  type PlannedCommandChange,
  type PreservedRemoteCommand,
} from "./types.js";

const DISCORD_API_V10_PREFIX = "/api/v10";
const DISCORD_CURRENT_APPLICATION_PATH = `${DISCORD_API_V10_PREFIX}/oauth2/applications/@me`;
const DISCORD_USER_AGENT =
  "DiscordBot (https://ginlix.ai, 0.1.0) dalton-channel-gateway/native-commands";

export interface DiscordNativeCommandSyncOptions
  extends CommonRegistrationRuntimeOptions {
  readonly botToken: string;
  readonly applicationId: string;
  /** Defaults to `{ kind: "global" }` so both Guild and Bot DM contexts are registered. */
  readonly scope?: DiscordRegistrationScope;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

interface IndexedDiscordCommands {
  readonly matchedByName: Map<
    NativeCommandName,
    { id: string; raw: DiscordRemoteApplicationCommand }
  >;
  readonly preservedCommands: PreservedRemoteCommand[];
}

function indexRemoteDiscordCommands(
  items: readonly DiscordRemoteApplicationCommand[],
): IndexedDiscordCommands {
  const matchedByName = new Map<
    NativeCommandName,
    { id: string; raw: DiscordRemoteApplicationCommand }
  >();
  const preservedCommands: PreservedRemoteCommand[] = [];

  for (const item of items) {
    if (!isPlainRecord(item)) {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        "Discord 命令列表包含非对象条目。",
        { platform: "discord" },
      );
    }

    const rawName = typeof item.name === "string" ? item.name.trim() : "";
    if (!rawName) {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        "Discord 命令列表条目缺少有效 name 字段。",
        { platform: "discord" },
      );
    }

    const cmdType =
      item.type === undefined
        ? DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT
        : item.type;
    if (typeof cmdType !== "number" || !Number.isInteger(cmdType)) {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        `Discord 命令 "${rawName}" 的 type 字段无效。`,
        { platform: "discord" },
      );
    }

    if (
      cmdType !== DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT ||
      !isManagedNativeCommandName(rawName)
    ) {
      const remoteId =
        typeof item.id === "string" && item.id.trim().length > 0
          ? item.id.trim()
          : undefined;
      preservedCommands.push({
        name: rawName,
        remoteId,
        commandType: cmdType,
      });
      continue;
    }

    if (matchedByName.has(rawName)) {
      throw new NativeCommandRegistrationError(
        "duplicate_remote_command",
        `Discord 存在重复的同名同类型 (CHAT_INPUT) 命令 "/${rawName}"，已拒绝写入以避免歧义。`,
        { platform: "discord" },
      );
    }

    encodeCommandIdPathSegment(
      item.id,
      `Discord CHAT_INPUT 命令 "/${rawName}"`,
    );
    const cleanId = (item.id as string).trim();
    matchedByName.set(rawName, { id: cleanId, raw: item });
  }

  return { matchedByName, preservedCommands };
}

function extractDiscordErrorSummary(
  resp: JsonHttpResponse,
  redact: (text: string) => string,
): { code?: number; message: string } {
  if (isPlainRecord(resp.data)) {
    const code =
      typeof resp.data.code === "number" ? resp.data.code : undefined;
    const msg =
      typeof resp.data.message === "string" ? resp.data.message : resp.rawText;
    return {
      code,
      message: redact(msg || ""),
    };
  }
  return { message: redact(resp.rawText || "") };
}

function throwForDiscordErrorResponse(params: {
  resp: JsonHttpResponse;
  scope: DiscordRegistrationScope;
  operationLabel: string;
  redact: (text: string) => string;
}): void {
  const { resp, scope, operationLabel, redact } = params;
  if (resp.ok) {
    return;
  }

  const { code, message } = extractDiscordErrorSummary(resp, redact);
  const codeSuffix = code !== undefined ? `, code=${code}` : "";
  const msgSuffix = message ? `: ${message}` : "";

  if (code === 30034) {
    throw new NativeCommandRegistrationError(
      "daily_create_limit",
      `Discord 应用命令当日创建次数已达上限 (${operationLabel}, HTTP ${resp.status}${codeSuffix})${msgSuffix}`,
      { httpStatus: resp.status, platform: "discord" },
    );
  }

  if (resp.status === 401) {
    throw new NativeCommandRegistrationError(
      "auth_error",
      `Discord Bot Token 认证失败（${operationLabel}，HTTP 401${codeSuffix}）${msgSuffix}。请检查 DALTON_DISCORD_BOT_TOKEN。`,
      { httpStatus: 401, platform: "discord" },
    );
  }

  if (resp.status === 403) {
    const scopeHint =
      scope.kind === "guild"
        ? `请确认机器人已加入目标测试服务器 (${scope.guildId}) 且授权包含 applications.commands 作用域`
        : "请确认当前 Discord 应用已开启 applications.commands 权限";
    throw new NativeCommandRegistrationError(
      "permission_denied",
      `Discord 权限不足（${operationLabel}，HTTP 403${codeSuffix}）：${scopeHint}${msgSuffix}。`,
      { httpStatus: 403, platform: "discord" },
    );
  }

  throw new NativeCommandRegistrationError(
    "http_error",
    `Discord 接口请求失败（${operationLabel}，HTTP ${resp.status}${codeSuffix}）${msgSuffix}`,
    { httpStatus: resp.status, platform: "discord" },
  );
}

export async function syncDiscordNativeCommands(
  options: DiscordNativeCommandSyncOptions,
): Promise<NativeCommandSyncResult> {
  const mode = options.mode ?? "preview";
  const botToken = options.botToken.trim();
  const rawAppId = options.applicationId.trim();

  if (!botToken || !rawAppId) {
    throw new NativeCommandRegistrationError(
      "missing_config",
      "缺少 Discord 凭据配置：请设置 DALTON_DISCORD_BOT_TOKEN 与 DALTON_DISCORD_APPLICATION_ID。",
      { platform: "discord" },
    );
  }

  if (/^bot\s+/i.test(botToken) || /\s/.test(botToken)) {
    throw new NativeCommandRegistrationError(
      "invalid_config",
      "DALTON_DISCORD_BOT_TOKEN 须为不含空白或 \"Bot \" 前缀的原始机器人令牌。",
      { platform: "discord" },
    );
  }

  const applicationId = validateDiscordSnowflake(
    rawAppId,
    "DALTON_DISCORD_APPLICATION_ID",
    "invalid_config",
  );

  const scope: DiscordRegistrationScope =
    options.scope?.kind === "guild"
      ? {
          kind: "guild",
          guildId: validateDiscordSnowflake(
            options.scope.guildId,
            "Discord 测试服务器 ID (guildId)",
            "usage_error",
          ),
        }
      : { kind: "global" };

  const scopeLabel =
    scope.kind === "guild" ? `guild:${scope.guildId}` : "global";

  const activeSecrets = new Set<string>([botToken]);
  const redact = (text: string): string =>
    scrubNativeCommandSecrets(text, activeSecrets);
  const logger = createSecretAwareLogger({
    subsystem: "native-commands/discord",
    baseLogger: options.logger,
    getSecrets: () => activeSecrets,
  });

  const baseOrigin = resolveVerifiedApiOrigin(
    DISCORD_TRUSTED_API_ORIGIN,
    options.testApiOrigin,
    "discord",
  );

  const httpClient = new BoundedHttpClient({
    platform: "discord",
    baseOrigin,
    requestTimeoutMs: options.requestTimeoutMs,
    totalTimeoutMs: options.totalTimeoutMs,
    maxRequests: options.maxRequests,
    maxRateLimitRetries: options.maxRateLimitRetries,
    fetchImpl: options.fetchImpl,
    sleepFn: options.sleepFn,
    nowFn: options.nowFn,
    redact,
    logger,
  });

  const authHeaders = {
    Authorization: `Bot ${botToken}`,
    "User-Agent": DISCORD_USER_AGENT,
  };

  // 1. Verify current bot application identity via `/api/v10/oauth2/applications/@me`
  //    (never infer application ID by base64-decoding the bot token).
  const identityResp = await httpClient.requestJson({
    method: "GET",
    path: DISCORD_CURRENT_APPLICATION_PATH,
    headers: authHeaders,
    isMutation: false,
  });

  throwForDiscordErrorResponse({
    resp: identityResp,
    scope,
    operationLabel: "查询当前 Bot 应用身份 (/oauth2/applications/@me)",
    redact,
  });

  if (!isPlainRecord(identityResp.data)) {
    throw new NativeCommandRegistrationError(
      "malformed_response",
      "Discord /oauth2/applications/@me 未返回有效 JSON 对象。",
      { httpStatus: identityResp.status, platform: "discord" },
    );
  }

  const actualAppId =
    typeof identityResp.data.id === "string"
      ? identityResp.data.id.trim()
      : "";
  const applicationName =
    typeof identityResp.data.name === "string" &&
    identityResp.data.name.trim().length > 0
      ? identityResp.data.name.trim()
      : undefined;

  if (!actualAppId) {
    throw new NativeCommandRegistrationError(
      "malformed_response",
      "Discord /oauth2/applications/@me 响应缺少应用 id 字段。",
      { httpStatus: identityResp.status, platform: "discord" },
    );
  }

  if (actualAppId !== applicationId) {
    throw new NativeCommandRegistrationError(
      "identity_mismatch",
      `Discord 应用身份不匹配：DALTON_DISCORD_APPLICATION_ID 配置为 ${applicationId}，但当前 Bot Token 所属应用 ID 为 ${actualAppId}。已拒绝读取或修改命令。`,
      { platform: "discord" },
    );
  }

  const commandsBasePath =
    scope.kind === "guild"
      ? `${DISCORD_API_V10_PREFIX}/applications/${encodeURIComponent(applicationId)}/guilds/${encodeURIComponent(scope.guildId)}/commands`
      : `${DISCORD_API_V10_PREFIX}/applications/${encodeURIComponent(applicationId)}/commands`;

  const listAllRemoteCommands = async (): Promise<
    DiscordRemoteApplicationCommand[]
  > =>
    collectPaginatedItems<DiscordRemoteApplicationCommand>({
      platform: "discord",
      maxPages: options.maxPages,
      fetchPage: async (pageToken) => {
        const path = pageToken
          ? `${commandsBasePath}?page_token=${encodeURIComponent(pageToken)}`
          : commandsBasePath;
        const resp = await httpClient.requestJson({
          method: "GET",
          path,
          headers: authHeaders,
          isMutation: false,
        });
        throwForDiscordErrorResponse({
          resp,
          scope,
          operationLabel: `读取 Discord 命令列表 (${scopeLabel})`,
          redact,
        });

        if (Array.isArray(resp.data)) {
          return {
            items: resp.data as DiscordRemoteApplicationCommand[],
            hasMore: false,
          };
        }

        if (isPlainRecord(resp.data) && Array.isArray(resp.data.items)) {
          return {
            items: resp.data.items as DiscordRemoteApplicationCommand[],
            hasMore: resp.data.has_more === true,
            nextPageToken:
              typeof resp.data.page_token === "string"
                ? resp.data.page_token
                : undefined,
          };
        }

        throw new NativeCommandRegistrationError(
          "malformed_response",
          `Discord 命令列表接口 (${commandsBasePath}) 未返回命令数组。`,
          { httpStatus: resp.status, platform: "discord" },
        );
      },
    });

  // 2. Read current commands and index by `(name, type === CHAT_INPUT)`.
  const remoteItems = await listAllRemoteCommands();
  const { matchedByName, preservedCommands } =
    indexRemoteDiscordCommands(remoteItems);

  // 3. Build targeted `POST` / `PATCH` plan (never `PUT` bulk overwrite or `DELETE`).
  const plan: PlannedCommandChange[] = DALTON_NATIVE_COMMANDS.map((spec) => {
    const existing = matchedByName.get(spec.name);
    if (!existing) {
      const createBody = buildDiscordCreatePayload(spec, scope);
      return {
        name: spec.name,
        action: "create",
        changedFields: Object.keys(createBody),
        requestMethod: "POST",
        requestPath: commandsBasePath,
        requestBody: createBody,
      };
    }

    const diff = diffDiscordApplicationCommand(existing.raw, spec, scope);
    if (!diff.needsUpdate || !diff.patchBody) {
      return {
        name: spec.name,
        action: "unchanged",
        remoteId: existing.id,
        changedFields: [],
      };
    }

    const encodedId = encodeCommandIdPathSegment(
      existing.id,
      `Discord CHAT_INPUT 命令 "/${spec.name}"`,
    );
    return {
      name: spec.name,
      action: "update",
      remoteId: existing.id,
      changedFields: diff.changedFields,
      requestMethod: "PATCH",
      requestPath: `${commandsBasePath}/${encodedId}`,
      requestBody: diff.patchBody,
    };
  });

  if (mode === "preview") {
    logger.info("Discord native command preview completed", {
      applicationId,
      scope: scopeLabel,
      createCount: plan.filter((p) => p.action === "create").length,
      updateCount: plan.filter((p) => p.action === "update").length,
      unchangedCount: plan.filter((p) => p.action === "unchanged").length,
      preservedCount: preservedCommands.length,
    });
    return {
      ok: true,
      status: "preview",
      platform: "discord",
      mode: "preview",
      scopeLabel,
      applicationId,
      applicationName,
      plan,
      preservedCommands,
      notes: [
        "当前为预览模式（未携带 --apply），未执行任何创建或修改请求。",
        scope.kind === "global"
          ? "全局命令显式启用服务器频道 (GUILD=0) 与机器人私聊 (BOT_DM=1) 上下文。"
          : `当前仅针对测试服务器 (${scope.guildId}) 预览，不包含机器人私聊全局菜单。`,
      ],
    };
  }

  // 4. Execute targeted creates/updates with post-disconnect state verification.
  const outcomes: AppliedCommandOutcome[] = [];

  for (let i = 0; i < plan.length; i += 1) {
    const item = plan[i]!;
    if (
      item.action === "unchanged" ||
      !item.requestMethod ||
      !item.requestPath ||
      !item.requestBody
    ) {
      outcomes.push({
        name: item.name,
        status: "unchanged",
        remoteId: item.remoteId,
        changedFields: [],
      });
      continue;
    }

    const spec = DALTON_NATIVE_COMMAND_MAP.get(item.name)!;

    try {
      const writeResp = await httpClient.requestJson({
        method: item.requestMethod,
        path: item.requestPath,
        headers: {
          ...authHeaders,
          "Content-Type": "application/json; charset=utf-8",
        },
        body: item.requestBody,
        isMutation: true,
      });

      throwForDiscordErrorResponse({
        resp: writeResp,
        scope,
        operationLabel: `${item.action === "create" ? "创建" : "更新"} Discord 命令 /${item.name}`,
        redact,
      });

      const respObj = isPlainRecord(writeResp.data)
        ? writeResp.data
        : undefined;
      const returnedId =
        typeof respObj?.id === "string" && respObj.id.trim().length > 0
          ? respObj.id.trim()
          : item.remoteId;

      if (!returnedId) {
        throw new AmbiguousMutationError(
          `${item.action === "create" ? "创建" : "更新"} /${item.name} 响应缺少有效命令 id`,
        );
      }

      outcomes.push({
        name: item.name,
        status: item.action === "create" ? "created" : "updated",
        remoteId: returnedId,
        changedFields: item.changedFields,
      });
    } catch (err) {
      if (!(err instanceof AmbiguousMutationError)) {
        throw err;
      }

      const safeReason = redact(err.reason);
      logger.warn(
        "Discord write response lost; reading live command list to verify state",
        {
          command: item.name,
          action: item.action,
          scope: scopeLabel,
          reason: safeReason,
        },
      );

      let verifiedId: string | undefined;
      let unconfirmedDetail: string | undefined;

      try {
        const freshItems = await listAllRemoteCommands();
        const freshIndexed = indexRemoteDiscordCommands(freshItems);
        const freshMatch = freshIndexed.matchedByName.get(item.name);
        if (freshMatch) {
          const postDiff = diffDiscordApplicationCommand(
            freshMatch.raw,
            spec,
            scope,
          );
          if (!postDiff.needsUpdate) {
            verifiedId = freshMatch.id;
          } else {
            unconfirmedDetail = `写后回查发现 "/${item.name}" 服务端字段仍未更新（差异字段: ${postDiff.changedFields.join(", ")}）`;
          }
        } else {
          unconfirmedDetail = `写后回查未在服务端命令列表中找到 "/${item.name}"`;
        }
      } catch (verifyErr) {
        const verifyMsg = redact(
          verifyErr instanceof Error ? verifyErr.message : String(verifyErr),
        );
        unconfirmedDetail = `写后状态回查失败: ${verifyMsg}`;
      }

      if (verifiedId) {
        logger.info("Discord command state verified after interrupted write", {
          command: item.name,
          commandId: verifiedId,
          scope: scopeLabel,
        });
        outcomes.push({
          name: item.name,
          status: item.action === "create" ? "created" : "updated",
          remoteId: verifiedId,
          changedFields: item.changedFields,
          verifiedAfterDisconnect: true,
        });
        continue;
      }

      const fullUnconfirmedReason = `${safeReason}；${unconfirmedDetail ?? "无法确认平台最终状态"}`;
      outcomes.push({
        name: item.name,
        status: "unconfirmed",
        remoteId: item.remoteId,
        changedFields: item.changedFields,
        unconfirmedReason: fullUnconfirmedReason,
      });
      for (let rest = i + 1; rest < plan.length; rest += 1) {
        const remaining = plan[rest]!;
        outcomes.push({
          name: remaining.name,
          status:
            remaining.action === "unchanged" ? "unchanged" : "skipped",
          remoteId: remaining.remoteId,
          changedFields: remaining.changedFields,
        });
      }

      const errorMessage = `Discord 命令 "/${item.name}" 写入结果未确认（${fullUnconfirmedReason}）。为避免盲目重复创建，已停止后续写入；可重新执行预览或 --apply 进行幂等核对。`;
      logger.error(errorMessage);
      return {
        ok: false,
        status: "unconfirmed",
        platform: "discord",
        mode: "apply",
        scopeLabel,
        applicationId,
        applicationName,
        plan,
        outcomes,
        preservedCommands,
        notes: [],
        errorMessage,
      };
    }
  }

  const notes =
    scope.kind === "global"
      ? [
          "全局命令已同步，支持服务器频道 (GUILD=0) 与机器人私聊 (BOT_DM=1)。",
        ]
      : [
          `测试服务器 (${scope.guildId}) 命令已同步；如需支持机器人私聊与全服务器菜单，请执行默认全局注册。`,
        ];

  logger.info("Discord native command sync applied", {
    applicationId,
    scope: scopeLabel,
    createdCount: outcomes.filter((o) => o.status === "created").length,
    updatedCount: outcomes.filter((o) => o.status === "updated").length,
    unchangedCount: outcomes.filter((o) => o.status === "unchanged").length,
    preservedCount: preservedCommands.length,
  });

  return {
    ok: true,
    status: "applied",
    platform: "discord",
    mode: "apply",
    scopeLabel,
    applicationId,
    applicationName,
    plan,
    outcomes,
    preservedCommands,
    notes,
  };
}
