/**
 * Feishu Open Platform v7 native slash-command registration library.
 *
 * Endpoint and payload semantics follow `larksuite/cli`
 * (`shortcuts/application/slash_command_create.go`, `slash_command_common.go`,
 * `slash_command_resolve.go`), Copyright (c) 2026 Lark Technologies Pte. Ltd.,
 * licensed under the MIT License.
 */

import {
  AmbiguousMutationError,
  BoundedHttpClient,
  collectPaginatedItems,
  FEISHU_TRUSTED_API_ORIGIN,
  type JsonHttpResponse,
  resolveVerifiedApiOrigin,
} from "./http.js";
import {
  buildFeishuCreatePayload,
  DALTON_NATIVE_COMMAND_MAP,
  DALTON_NATIVE_COMMANDS,
  diffFeishuSlashCommand,
  encodeCommandIdPathSegment,
  FEISHU_CLIENT_CACHE_NOTE,
  FEISHU_SLASH_COMMAND_BASE_PATH,
  type FeishuRemoteSlashCommandItem,
  isManagedNativeCommandName,
} from "./manifest.js";
import {
  createSecretAwareLogger,
  MAX_STRUCTURED_STRING_LENGTH,
  sanitizeNativeCommandSyncResult,
  sanitizeRegistrationError,
  scrubNativeCommandSecrets,
} from "./redact.js";
import {
  type AppliedCommandOutcome,
  type CommonRegistrationRuntimeOptions,
  type NativeCommandDefinition,
  type NativeCommandName,
  NativeCommandRegistrationError,
  type NativeCommandSyncResult,
  type PlannedCommandChange,
  type PreservedRemoteCommand,
} from "./types.js";

const FEISHU_TENANT_TOKEN_PATH =
  "/open-apis/auth/v3/tenant_access_token/internal";
const FEISHU_USER_AGENT = "dalton-channel-gateway/native-commands";

const FEISHU_AUTH_ERROR_CODES = new Set([
  10003,
  10014,
  99991661,
  99991663,
  99991664,
  99991668,
]);

const FEISHU_PERMISSION_ERROR_CODES = new Set([
  99991401,
  99991672,
  99991679,
]);

export interface FeishuNativeCommandSyncOptions
  extends CommonRegistrationRuntimeOptions {
  readonly appId: string;
  readonly appSecret: string;
  readonly expectedAppId?: string;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Registers any non-empty `tenant_access_token` present at top level or under
 * `data` into `activeSecrets` before error formatting or identity validation,
 * without treating unvalidated tokens as usable credentials.
 */
function registerCandidateTenantTokens(
  payload: unknown,
  activeSecrets: Set<string>,
): void {
  if (!isPlainRecord(payload)) {
    return;
  }
  if (typeof payload.tenant_access_token === "string") {
    const topToken = payload.tenant_access_token.trim();
    if (topToken.length > 0) {
      activeSecrets.add(topToken);
    }
  }
  if (
    isPlainRecord(payload.data) &&
    typeof payload.data.tenant_access_token === "string"
  ) {
    const nestedToken = payload.data.tenant_access_token.trim();
    if (nestedToken.length > 0) {
      activeSecrets.add(nestedToken);
    }
  }
}

function isFeishuPermissionMessage(msg: string): boolean {
  return /(?:scope|permission|forbidden|access\s+denied|insufficient|unauthorized|app_slash_command)/i.test(
    msg,
  );
}

interface IndexedFeishuCommands {
  readonly matchedByName: Map<
    NativeCommandName,
    { id: string; raw: FeishuRemoteSlashCommandItem }
  >;
  readonly preservedCommands: PreservedRemoteCommand[];
}

function indexRemoteFeishuCommands(
  items: readonly FeishuRemoteSlashCommandItem[],
  redact: (text: string) => string,
): IndexedFeishuCommands {
  const matchedByName = new Map<
    NativeCommandName,
    { id: string; raw: FeishuRemoteSlashCommandItem }
  >();
  const preservedCommands: PreservedRemoteCommand[] = [];

  for (const item of items) {
    if (!isPlainRecord(item)) {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        "飞书命令列表包含非对象条目。",
        { platform: "feishu" },
      );
    }
    const rawName = typeof item.command === "string" ? item.command.trim() : "";
    if (!rawName) {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        "飞书命令列表条目缺少有效 command 名称。",
        { platform: "feishu" },
      );
    }

    if (!isManagedNativeCommandName(rawName)) {
      const remoteId =
        typeof item.command_id === "string" && item.command_id.trim().length > 0
          ? redact(item.command_id.trim())
          : undefined;
      preservedCommands.push({
        name: redact(rawName),
        remoteId,
      });
      continue;
    }

    if (matchedByName.has(rawName)) {
      throw new NativeCommandRegistrationError(
        "duplicate_remote_command",
        `飞书应用存在重复的同名斜杠命令 "/${rawName}"，已拒绝写入以避免歧义。`,
        { platform: "feishu" },
      );
    }

    // Validate ID shape and path-segment safety.
    encodeCommandIdPathSegment(
      item.command_id,
      `飞书斜杠命令 "/${rawName}"`,
    );
    const cleanId = (item.command_id as string).trim();
    matchedByName.set(rawName, { id: cleanId, raw: item });
  }

  return { matchedByName, preservedCommands };
}

function throwForFeishuApiFailure(params: {
  resp: JsonHttpResponse;
  requiredScope:
    | "application:app_slash_command:read"
    | "application:app_slash_command:write";
  operationLabel: string;
  redact: (text: string) => string;
  isMutation?: boolean;
}): Record<string, unknown> {
  const { resp, requiredScope, operationLabel, redact, isMutation } = params;
  const body = isPlainRecord(resp.data) ? resp.data : undefined;
  const code = typeof body?.code === "number" ? body.code : undefined;
  const rawMsg = typeof body?.msg === "string" ? body.msg : resp.rawText;
  const safeMsg = redact(rawMsg || "");

  if (
    resp.status === 401 ||
    (code !== undefined && FEISHU_AUTH_ERROR_CODES.has(code))
  ) {
    throw new NativeCommandRegistrationError(
      "auth_error",
      `飞书认证失败（${operationLabel}，HTTP ${resp.status}${code !== undefined ? `, code=${code}` : ""}）${safeMsg ? `: ${safeMsg}` : ""}。请检查 DALTON_FEISHU_APP_ID 与 DALTON_FEISHU_APP_SECRET。`,
      { httpStatus: resp.status, platform: "feishu" },
    );
  }

  if (
    resp.status === 403 ||
    (code !== undefined && FEISHU_PERMISSION_ERROR_CODES.has(code)) ||
    ((!resp.ok || (code !== undefined && code !== 0)) &&
      isFeishuPermissionMessage(safeMsg))
  ) {
    throw new NativeCommandRegistrationError(
      "permission_denied",
      `飞书权限不足（${operationLabel}${code !== undefined ? `, code=${code}` : ""}）：请为当前 Dalton 飞书应用开通 ${requiredScope} 权限并发布应用版本${safeMsg ? `（${safeMsg}）` : ""}。`,
      { httpStatus: resp.status, platform: "feishu" },
    );
  }

  if (!resp.ok || (code !== undefined && code !== 0)) {
    throw new NativeCommandRegistrationError(
      "http_error",
      `飞书接口调用失败（${operationLabel}，HTTP ${resp.status}${code !== undefined ? `, code=${code}` : ""}）${safeMsg ? `: ${safeMsg}` : ""}`,
      { httpStatus: resp.status, platform: "feishu" },
    );
  }

  if (!body || typeof code !== "number") {
    if (isMutation) {
      throw new AmbiguousMutationError(
        `飞书接口（${operationLabel}）返回结构缺少合法 code=0 字段`,
      );
    }
    throw new NativeCommandRegistrationError(
      "malformed_response",
      `飞书接口（${operationLabel}）未返回包含合法 code=0 的 JSON 对象。`,
      { httpStatus: resp.status, platform: "feishu" },
    );
  }

  return body;
}

function verifyFeishuWriteResponsePayload(params: {
  body: Record<string, unknown>;
  action: "create" | "update";
  expectedRemoteId?: string;
  spec: NativeCommandDefinition;
}): string {
  const { body, action, expectedRemoteId, spec } = params;
  const actionVerb = action === "create" ? "创建" : "更新";
  const rawData = body.data;

  if (rawData !== undefined && rawData !== null && !isPlainRecord(rawData)) {
    throw new AmbiguousMutationError(
      `${actionVerb} /${spec.name} 响应的 data 字段结构异常`,
    );
  }

  const dataObj = isPlainRecord(rawData) ? rawData : undefined;
  const rawCandidateId =
    dataObj?.command_id !== undefined && dataObj.command_id !== null
      ? dataObj.command_id
      : body.command_id;

  let returnedId: string | undefined;
  if (rawCandidateId !== undefined && rawCandidateId !== null) {
    try {
      encodeCommandIdPathSegment(
        rawCandidateId,
        `飞书斜杠命令 "/${spec.name}"`,
      );
    } catch {
      throw new AmbiguousMutationError(
        `${actionVerb} /${spec.name} 响应包含无效 command_id`,
      );
    }
    returnedId = (rawCandidateId as string).trim();
    if (
      action === "update" &&
      expectedRemoteId !== undefined &&
      returnedId !== expectedRemoteId
    ) {
      throw new AmbiguousMutationError(
        `更新 /${spec.name} 响应的 command_id 与目标命令不一致`,
      );
    }
  }

  if (
    dataObj?.command !== undefined &&
    dataObj.command !== null &&
    (typeof dataObj.command !== "string" ||
      dataObj.command.trim() !== spec.name)
  ) {
    throw new AmbiguousMutationError(
      `${actionVerb} /${spec.name} 响应的 command 名称与目标不一致`,
    );
  }

  if (
    dataObj &&
    ("description" in dataObj ||
      (spec.iconKey !== undefined && "icon" in dataObj))
  ) {
    const respDiff = diffFeishuSlashCommand(
      dataObj as FeishuRemoteSlashCommandItem,
      spec,
    );
    if (respDiff.needsUpdate) {
      throw new AmbiguousMutationError(
        `${actionVerb} /${spec.name} 响应未反映期望更新字段（差异字段: ${respDiff.changedFields.join(", ")}）`,
      );
    }
  }

  if (action === "create") {
    if (!returnedId) {
      throw new AmbiguousMutationError(
        `创建 /${spec.name} 响应未包含有效 command_id`,
      );
    }
    return returnedId;
  }

  // Per `larksuite/cli` (`slash_command_update.go`), Feishu PATCH succeeds with
  // `code === 0` even when `data` is nil/empty; fall back to `expectedRemoteId`
  // once `code === 0` and any returned fields have been validated.
  const effectiveId = returnedId ?? expectedRemoteId;
  if (!effectiveId) {
    throw new AmbiguousMutationError(
      `更新 /${spec.name} 缺少可确认的 command_id`,
    );
  }
  return effectiveId;
}

export async function syncFeishuNativeCommands(
  options: FeishuNativeCommandSyncOptions,
): Promise<NativeCommandSyncResult> {
  const mode = options.mode ?? "preview";
  const appId = options.appId.trim();
  const appSecret = options.appSecret.trim();

  if (!appId || !appSecret) {
    throw new NativeCommandRegistrationError(
      "missing_config",
      "缺少飞书凭据配置：请设置 DALTON_FEISHU_APP_ID 与 DALTON_FEISHU_APP_SECRET。",
      { platform: "feishu" },
    );
  }
  if (/\s/.test(appId)) {
    throw new NativeCommandRegistrationError(
      "invalid_config",
      "DALTON_FEISHU_APP_ID 包含非法空白字符。",
      { platform: "feishu" },
    );
  }
  if (
    options.expectedAppId !== undefined &&
    options.expectedAppId.trim() !== appId
  ) {
    throw new NativeCommandRegistrationError(
      "identity_mismatch",
      `飞书应用身份不匹配：配置 DALTON_FEISHU_APP_ID (${appId}) 与期望应用 ID (${options.expectedAppId.trim()}) 不一致，已拒绝执行。`,
      { platform: "feishu" },
    );
  }

  const activeSecrets = new Set<string>([appSecret]);
  const redact = (text: string): string =>
    scrubNativeCommandSecrets(
      text,
      activeSecrets,
      MAX_STRUCTURED_STRING_LENGTH,
    );
  // Sanitize an outbound copy with `activeSecrets` (including dynamic
  // `tenant_access_token`) only at return boundaries so internal HTTP calls
  // continue using real remote command IDs and preserved fields.
  const finalizeResult = (
    rawResult: NativeCommandSyncResult,
  ): NativeCommandSyncResult =>
    sanitizeNativeCommandSyncResult(rawResult, activeSecrets);
  const logger = createSecretAwareLogger({
    subsystem: "native-commands/feishu",
    baseLogger: options.logger,
    getSecrets: () => activeSecrets,
  });

  try {
    const baseOrigin = resolveVerifiedApiOrigin(
      FEISHU_TRUSTED_API_ORIGIN,
      options.testApiOrigin,
      "feishu",
    );

    const httpClient = new BoundedHttpClient({
      platform: "feishu",
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

    // 1. Obtain tenant_access_token strictly bound to the configured `appId`.
    const tokenResp = await httpClient.requestJson({
      method: "POST",
      path: FEISHU_TENANT_TOKEN_PATH,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": FEISHU_USER_AGENT,
      },
      body: {
        app_id: appId,
        app_secret: appSecret,
      },
      isMutation: false,
      onParsedJson: (parsed) =>
        registerCandidateTenantTokens(parsed, activeSecrets),
    });

    // Ensure both top-level and `data.tenant_access_token` (even if distinct)
    // are scrubbed before any early error message or identity check runs.
    registerCandidateTenantTokens(tokenResp.data, activeSecrets);

    const tokenBody = isPlainRecord(tokenResp.data) ? tokenResp.data : undefined;
    const tokenDataObj = isPlainRecord(tokenBody?.data)
      ? tokenBody.data
      : undefined;
    const tokenCode =
      typeof tokenBody?.code === "number" ? tokenBody.code : undefined;
    const tokenMsg = redact(
      typeof tokenBody?.msg === "string" ? tokenBody.msg : tokenResp.rawText,
    );

    if (tokenResp.status === 403) {
      throw new NativeCommandRegistrationError(
        "permission_denied",
        `飞书获取 tenant_access_token 被拒绝 (HTTP 403)${tokenMsg ? `: ${tokenMsg}` : ""}。`,
        { httpStatus: 403, platform: "feishu" },
      );
    }

    if (!tokenResp.ok || (tokenCode !== undefined && tokenCode !== 0)) {
      throw new NativeCommandRegistrationError(
        "auth_error",
        `飞书获取 tenant_access_token 失败（HTTP ${tokenResp.status}${tokenCode !== undefined ? `, code=${tokenCode}` : ""}）${tokenMsg ? `: ${tokenMsg}` : ""}。请核对 DALTON_FEISHU_APP_ID 与 DALTON_FEISHU_APP_SECRET。`,
        { httpStatus: tokenResp.status, platform: "feishu" },
      );
    }

    if (!tokenBody || typeof tokenCode !== "number") {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        "飞书认证接口未返回合法的 code=0 响应结构。",
        { httpStatus: tokenResp.status, platform: "feishu" },
      );
    }

    const topAppId =
      typeof tokenBody.app_id === "string" ? tokenBody.app_id.trim() : "";
    const nestedAppId =
      typeof tokenDataObj?.app_id === "string"
        ? tokenDataObj.app_id.trim()
        : "";
    for (const candidateAppId of [topAppId, nestedAppId]) {
      if (candidateAppId && candidateAppId !== appId) {
        throw new NativeCommandRegistrationError(
          "identity_mismatch",
          `飞书凭据返回的应用身份 (${redact(candidateAppId)}) 与配置 DALTON_FEISHU_APP_ID (${appId}) 不一致，已拒绝执行。`,
          { platform: "feishu" },
        );
      }
    }

    const topTenantToken =
      typeof tokenBody.tenant_access_token === "string"
        ? tokenBody.tenant_access_token.trim()
        : "";
    const nestedTenantToken =
      typeof tokenDataObj?.tenant_access_token === "string"
        ? tokenDataObj.tenant_access_token.trim()
        : "";
    const rawTenantToken = topTenantToken || nestedTenantToken;

    if (!rawTenantToken) {
      throw new NativeCommandRegistrationError(
        "malformed_response",
        "飞书认证接口未返回有效 tenant_access_token。",
        { httpStatus: tokenResp.status, platform: "feishu" },
      );
    }

    const authHeaders = {
      Authorization: `Bearer ${rawTenantToken}`,
      "User-Agent": FEISHU_USER_AGENT,
    };

    const listAllRemoteCommands = async (): Promise<
      FeishuRemoteSlashCommandItem[]
    > =>
      collectPaginatedItems<FeishuRemoteSlashCommandItem>({
        platform: "feishu",
        maxPages: options.maxPages,
        fetchPage: async (pageToken) => {
          const path = pageToken
            ? `${FEISHU_SLASH_COMMAND_BASE_PATH}?page_token=${encodeURIComponent(pageToken)}`
            : FEISHU_SLASH_COMMAND_BASE_PATH;
          const resp = await httpClient.requestJson({
            method: "GET",
            path,
            headers: authHeaders,
            isMutation: false,
          });
          const body = throwForFeishuApiFailure({
            resp,
            requiredScope: "application:app_slash_command:read",
            operationLabel: "读取斜杠命令列表",
            redact,
          });
          const dataObj =
            body.data === undefined || body.data === null ? {} : body.data;
          if (!isPlainRecord(dataObj)) {
            throw new NativeCommandRegistrationError(
              "malformed_response",
              "飞书斜杠命令列表响应缺少对象类型的 data 字段。",
              { httpStatus: resp.status, platform: "feishu" },
            );
          }
          const rawItems = dataObj.items;
          if (
            rawItems !== undefined &&
            rawItems !== null &&
            !Array.isArray(rawItems)
          ) {
            throw new NativeCommandRegistrationError(
              "malformed_response",
              "飞书斜杠命令列表响应的 data.items 不是数组。",
              { httpStatus: resp.status, platform: "feishu" },
            );
          }
          return {
            items: (rawItems ?? []) as FeishuRemoteSlashCommandItem[],
            hasMore: dataObj.has_more === true,
            nextPageToken:
              typeof dataObj.page_token === "string"
                ? dataObj.page_token
                : undefined,
          };
        },
      });

    // 2. Read complete current command list across all pages before diffing.
    const remoteItems = await listAllRemoteCommands();
    const { matchedByName, preservedCommands } = indexRemoteFeishuCommands(
      remoteItems,
      redact,
    );

    // 3. Build idempotent plan for fixed Dalton commands.
    const plan: PlannedCommandChange[] = DALTON_NATIVE_COMMANDS.map((spec) => {
      const existing = matchedByName.get(spec.name);
      if (!existing) {
        return {
          name: spec.name,
          action: "create",
          changedFields: ["command", "description.default_value"],
          requestMethod: "POST",
          requestPath: FEISHU_SLASH_COMMAND_BASE_PATH,
          requestBody: buildFeishuCreatePayload(spec),
        };
      }

      const diff = diffFeishuSlashCommand(existing.raw, spec);
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
        `飞书斜杠命令 "/${spec.name}"`,
      );
      return {
        name: spec.name,
        action: "update",
        remoteId: existing.id,
        changedFields: diff.changedFields,
        requestMethod: "PATCH",
        requestPath: `${FEISHU_SLASH_COMMAND_BASE_PATH}/${encodedId}`,
        requestBody: diff.patchBody,
      };
    });

    if (mode === "preview") {
      logger.info("Feishu native command preview completed", {
        createCount: plan.filter((p) => p.action === "create").length,
        updateCount: plan.filter((p) => p.action === "update").length,
        unchangedCount: plan.filter((p) => p.action === "unchanged").length,
        preservedCount: preservedCommands.length,
      });
      return finalizeResult({
        ok: true,
        status: "preview",
        platform: "feishu",
        mode: "preview",
        scopeLabel: "app",
        plan,
        preservedCommands,
        notes: [
          "当前为预览模式（未携带 --apply），未执行任何创建或修改请求。",
        ],
      });
    }

    // 4. Apply planned mutations with post-disconnect state verification and
    //    partial-outcome preservation on explicit failures.
    const outcomes: AppliedCommandOutcome[] = [];
    let wroteAny = false;

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

        const body = throwForFeishuApiFailure({
          resp: writeResp,
          requiredScope: "application:app_slash_command:write",
          operationLabel: `${item.action === "create" ? "创建" : "更新"}斜杠命令 /${item.name}`,
          redact,
          isMutation: true,
        });

        const confirmedId = verifyFeishuWriteResponsePayload({
          body,
          action: item.action,
          expectedRemoteId: item.remoteId,
          spec,
        });

        outcomes.push({
          name: item.name,
          status: item.action === "create" ? "created" : "updated",
          remoteId: confirmedId,
          changedFields: item.changedFields,
        });
        wroteAny = true;
      } catch (err) {
        if (!(err instanceof AmbiguousMutationError)) {
          const regErr = sanitizeRegistrationError(
            err,
            activeSecrets,
            "feishu",
          );
          outcomes.push({
            name: item.name,
            status: "failed",
            remoteId: item.remoteId,
            changedFields: item.changedFields,
            errorCode: regErr.code,
            failureReason: regErr.message,
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

          const errorMessage = wroteAny
            ? `飞书命令 "/${item.name}" 执行失败（${regErr.message}）。此前已完成的命令变更已保留，后续未执行命令已跳过。`
            : `飞书命令 "/${item.name}" 执行失败（${regErr.message}）；后续未执行命令已跳过。`;
          logger.error(errorMessage, {
            command: item.name,
            code: regErr.code,
          });
          return finalizeResult({
            ok: false,
            status: "failed",
            platform: "feishu",
            mode: "apply",
            scopeLabel: "app",
            plan,
            outcomes,
            preservedCommands,
            notes: wroteAny ? [FEISHU_CLIENT_CACHE_NOTE] : [],
            errorCode: regErr.code,
            errorMessage,
          });
        }

        const safeReason = redact(err.reason);
        logger.warn(
          "Feishu write response ambiguous or lost; reading live command list to verify state",
          {
            command: item.name,
            action: item.action,
            reason: safeReason,
          },
        );

        let verifiedId: string | undefined;
        let unconfirmedDetail: string | undefined;

        try {
          const freshItems = await listAllRemoteCommands();
          const freshIndexed = indexRemoteFeishuCommands(freshItems, redact);
          const freshMatch = freshIndexed.matchedByName.get(item.name);
          if (freshMatch) {
            const postDiff = diffFeishuSlashCommand(freshMatch.raw, spec);
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
          logger.info(
            "Feishu command state verified via live list after ambiguous write response",
            {
              command: item.name,
              commandId: verifiedId,
            },
          );
          outcomes.push({
            name: item.name,
            status: item.action === "create" ? "created" : "updated",
            remoteId: verifiedId,
            changedFields: item.changedFields,
            verifiedAfterDisconnect: true,
          });
          wroteAny = true;
          continue;
        }

        const fullUnconfirmedReason = `${safeReason}；${unconfirmedDetail ?? "无法确认平台最终状态"}`;
        outcomes.push({
          name: item.name,
          status: "unconfirmed",
          remoteId: item.remoteId,
          changedFields: item.changedFields,
          errorCode: "unconfirmed_write",
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

        const errorMessage = `飞书命令 "/${item.name}" 写入结果未确认（${fullUnconfirmedReason}）。为避免盲目重复创建，已停止后续写入；可重新执行预览或 --apply 进行幂等核对。`;
        logger.error(errorMessage);
        return finalizeResult({
          ok: false,
          status: "unconfirmed",
          platform: "feishu",
          mode: "apply",
          scopeLabel: "app",
          plan,
          outcomes,
          preservedCommands,
          notes: wroteAny ? [FEISHU_CLIENT_CACHE_NOTE] : [],
          errorCode: "unconfirmed_write",
          errorMessage,
        });
      }
    }

    const notes = wroteAny ? [FEISHU_CLIENT_CACHE_NOTE] : [];
    logger.info("Feishu native command sync applied", {
      createdCount: outcomes.filter((o) => o.status === "created").length,
      updatedCount: outcomes.filter((o) => o.status === "updated").length,
      unchangedCount: outcomes.filter((o) => o.status === "unchanged").length,
      preservedCount: preservedCommands.length,
    });

    return finalizeResult({
      ok: true,
      status: "applied",
      platform: "feishu",
      mode: "apply",
      scopeLabel: "app",
      plan,
      outcomes,
      preservedCommands,
      notes,
    });
  } catch (err) {
    throw sanitizeRegistrationError(err, activeSecrets, "feishu");
  }
}
