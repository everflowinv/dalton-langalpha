import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { SubsystemLogger } from "../shared/logger.js";
import {
  DALTON_DISCORD_BOT_TOKEN_ENV,
  DALTON_FEISHU_APP_SECRET_ENV,
  loadDiscordNativeCommandCredentials,
  loadFeishuNativeCommandCredentials,
  validateDiscordSnowflake,
} from "./config.js";
import { syncDiscordNativeCommands } from "./discord-registrar.js";
import { syncFeishuNativeCommands } from "./feishu-registrar.js";
import { scrubNativeCommandSecrets } from "./redact.js";
import {
  type DiscordRegistrationScope,
  exitCodeForNativeCommandError,
  type NativeCommandPlatform,
  NativeCommandRegistrationError,
  type NativeCommandSyncResult,
} from "./types.js";

export interface ParsedNativeCommandsCliArgs {
  readonly help: boolean;
  readonly platform?: NativeCommandPlatform;
  readonly mode: "preview" | "apply";
  readonly discordScope: DiscordRegistrationScope;
  readonly json: boolean;
}

export const NATIVE_COMMANDS_CLI_HELP = `用法:
  dalton-native-commands --platform <feishu|discord> [--apply] [--guild-id <ID>] [--json]

说明:
  管理 Dalton 渠道网关的固定原生命令菜单（main、workspace、new、stop）。
  默认仅读取平台当前命令并输出不含凭据的创建/修改/无需修改预览；
  仅当显式传入 --apply 时才向平台写入变更。

选项:
  -p, --platform <feishu|discord>  目标平台（必填，也可作为首个位置参数传入）
      --apply                      执行创建/修改写入（默认关闭，仅预览）
      --dry-run, --preview         显式指定只读预览模式（不可与 --apply 同时使用）
      --scope <global|guild>       Discord 注册范围；默认为 global（同时支持服务器与机器人私聊）
      --guild-id <SNOWFLAKE>       Discord 指定测试服务器 ID（隐含 --scope guild）
      --json                       以 JSON 格式输出结果
  -h, --help                       显示帮助信息并退出

环境变量（仅读取 Dalton 网关专用配置）:
  飞书:
    DALTON_FEISHU_APP_ID           飞书自建应用 App ID
    DALTON_FEISHU_APP_SECRET       飞书自建应用 App Secret
  Discord:
    DALTON_DISCORD_BOT_TOKEN       Discord 机器人 Token
    DALTON_DISCORD_APPLICATION_ID  Discord 应用 ID（执行前通过 /oauth2/applications/@me 校验）

退出码:
  0  预览完成或变更已确认生效
  1  网络、限流、分页或远端命令冲突错误
  2  命令行参数错误
  3  配置缺失、认证失败、应用身份不匹配或平台权限不足
  4  写入请求连接中断且回查未能确认最终状态
`;

function parsePlatformValue(raw: string): NativeCommandPlatform {
  const normalized = raw.trim().toLowerCase();
  if (normalized === "feishu" || normalized === "discord") {
    return normalized;
  }
  throw new NativeCommandRegistrationError(
    "usage_error",
    `不支持的平台 "${raw}"；请指定 feishu 或 discord。`,
  );
}

export function parseNativeCommandsCliArgs(
  argv: readonly string[],
): ParsedNativeCommandsCliArgs {
  let help = false;
  let flagPlatform: NativeCommandPlatform | undefined;
  let positionalPlatform: NativeCommandPlatform | undefined;
  let sawApply = false;
  let sawPreview = false;
  let explicitScope: "global" | "guild" | undefined;
  let rawGuildId: string | undefined;
  let json = false;

  const consumeValue = (
    flagName: string,
    index: number,
    inlineValue?: string,
  ): { value: string; nextIndex: number } => {
    if (inlineValue !== undefined) {
      if (!inlineValue.trim()) {
        throw new NativeCommandRegistrationError(
          "usage_error",
          `参数 ${flagName} 需要非空值。`,
        );
      }
      return { value: inlineValue, nextIndex: index };
    }
    const next = argv[index + 1];
    if (next === undefined || next.startsWith("-")) {
      throw new NativeCommandRegistrationError(
        "usage_error",
        `参数 ${flagName} 缺少对应取值。`,
      );
    }
    return { value: next, nextIndex: index + 1 };
  };

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    if (token === "-h" || token === "--help") {
      help = true;
      continue;
    }
    if (token === "--apply") {
      sawApply = true;
      continue;
    }
    if (token === "--dry-run" || token === "--preview") {
      sawPreview = true;
      continue;
    }
    if (token === "--json") {
      json = true;
      continue;
    }

    if (
      token === "-p" ||
      token === "--platform" ||
      token.startsWith("--platform=")
    ) {
      const inline = token.startsWith("--platform=")
        ? token.slice("--platform=".length)
        : undefined;
      const { value, nextIndex } = consumeValue("--platform", i, inline);
      i = nextIndex;
      const parsed = parsePlatformValue(value);
      if (flagPlatform && flagPlatform !== parsed) {
        throw new NativeCommandRegistrationError(
          "usage_error",
          `重复且冲突的 --platform 参数（${flagPlatform} 与 ${parsed}）。`,
        );
      }
      flagPlatform = parsed;
      continue;
    }

    if (token === "--scope" || token.startsWith("--scope=")) {
      const inline = token.startsWith("--scope=")
        ? token.slice("--scope=".length)
        : undefined;
      const { value, nextIndex } = consumeValue("--scope", i, inline);
      i = nextIndex;
      const normalized = value.trim().toLowerCase();
      if (normalized !== "global" && normalized !== "guild") {
        throw new NativeCommandRegistrationError(
          "usage_error",
          `无效的 --scope 取值 "${value}"；仅支持 global 或 guild。`,
        );
      }
      if (explicitScope && explicitScope !== normalized) {
        throw new NativeCommandRegistrationError(
          "usage_error",
          "重复且冲突的 --scope 参数。",
        );
      }
      explicitScope = normalized;
      continue;
    }

    if (
      token === "--guild-id" ||
      token === "--guild" ||
      token.startsWith("--guild-id=") ||
      token.startsWith("--guild=")
    ) {
      const inline = token.startsWith("--guild-id=")
        ? token.slice("--guild-id=".length)
        : token.startsWith("--guild=")
          ? token.slice("--guild=".length)
          : undefined;
      const { value, nextIndex } = consumeValue("--guild-id", i, inline);
      i = nextIndex;
      if (rawGuildId !== undefined && rawGuildId !== value.trim()) {
        throw new NativeCommandRegistrationError(
          "usage_error",
          "重复且冲突的 --guild-id 参数。",
        );
      }
      rawGuildId = value.trim();
      continue;
    }

    if (token.startsWith("-")) {
      throw new NativeCommandRegistrationError(
        "usage_error",
        `未知命令行选项 "${token}"（使用 --help 查看支持的选项）。`,
      );
    }

    if (positionalPlatform === undefined) {
      positionalPlatform = parsePlatformValue(token);
      continue;
    }

    throw new NativeCommandRegistrationError(
      "usage_error",
      `多余的位置参数 "${token}"。`,
    );
  }

  if (help) {
    return {
      help: true,
      mode: "preview",
      discordScope: { kind: "global" },
      json,
    };
  }

  if (sawApply && sawPreview) {
    throw new NativeCommandRegistrationError(
      "usage_error",
      "--apply 与 --dry-run/--preview 不能同时使用。",
    );
  }

  if (
    flagPlatform &&
    positionalPlatform &&
    flagPlatform !== positionalPlatform
  ) {
    throw new NativeCommandRegistrationError(
      "usage_error",
      `位置参数平台 (${positionalPlatform}) 与 --platform (${flagPlatform}) 不一致。`,
    );
  }

  const platform = flagPlatform ?? positionalPlatform;
  if (!platform) {
    throw new NativeCommandRegistrationError(
      "usage_error",
      "必须通过 --platform <feishu|discord> 指定目标平台（使用 --help 查看说明）。",
    );
  }

  if (platform === "feishu") {
    if (explicitScope !== undefined || rawGuildId !== undefined) {
      throw new NativeCommandRegistrationError(
        "usage_error",
        "飞书斜杠命令按当前应用注册，不支持 --scope 或 --guild-id 参数。",
      );
    }
    return {
      help: false,
      platform,
      mode: sawApply ? "apply" : "preview",
      discordScope: { kind: "global" },
      json,
    };
  }

  if (explicitScope === "global" && rawGuildId !== undefined) {
    throw new NativeCommandRegistrationError(
      "usage_error",
      "--scope global 不能与 --guild-id 同时使用。",
    );
  }

  if (explicitScope === "guild" && rawGuildId === undefined) {
    throw new NativeCommandRegistrationError(
      "usage_error",
      "指定 --scope guild 时必须同时提供 --guild-id <SNOWFLAKE>。",
    );
  }

  const discordScope: DiscordRegistrationScope =
    rawGuildId !== undefined
      ? {
          kind: "guild",
          guildId: validateDiscordSnowflake(
            rawGuildId,
            "--guild-id",
            "usage_error",
          ),
        }
      : { kind: "global" };

  return {
    help: false,
    platform,
    mode: sawApply ? "apply" : "preview",
    discordScope,
    json,
  };
}

const ACTION_LABELS = {
  create: "创建",
  update: "修改",
  unchanged: "无需修改",
} as const;

const OUTCOME_LABELS = {
  created: "已创建",
  updated: "已修改",
  unchanged: "无需修改",
  unconfirmed: "未确认",
  skipped: "已跳过",
} as const;

export function formatNativeCommandSyncResult(
  result: NativeCommandSyncResult,
): string {
  const lines: string[] = [];
  const modeText =
    result.mode === "preview" ? "预览（只读，未写入）" : "执行（--apply）";
  lines.push(`平台: ${result.platform}`);
  lines.push(`模式: ${modeText}`);
  lines.push(`范围: ${result.scopeLabel}`);
  if (result.applicationId) {
    lines.push(
      `应用: ${result.applicationName ? `${result.applicationName} (${result.applicationId})` : result.applicationId}`,
    );
  }
  lines.push("");

  if (result.mode === "preview" || !result.outcomes) {
    lines.push("命令变更预览:");
    for (const item of result.plan) {
      const label = ACTION_LABELS[item.action];
      const idPart = item.remoteId ? ` (id: ${item.remoteId})` : "";
      const fieldsPart =
        item.action !== "unchanged" && item.changedFields.length > 0
          ? ` — 字段: ${item.changedFields.join(", ")}`
          : "";
      lines.push(`  [${label}] /${item.name}${idPart}${fieldsPart}`);
    }
  } else {
    lines.push("命令执行结果:");
    for (const outcome of result.outcomes) {
      const label = OUTCOME_LABELS[outcome.status];
      const idPart = outcome.remoteId ? ` (id: ${outcome.remoteId})` : "";
      const verifiedPart = outcome.verifiedAfterDisconnect
        ? " [连接中断后回查确认]"
        : "";
      const detailPart = outcome.unconfirmedReason
        ? ` — ${outcome.unconfirmedReason}`
        : outcome.status === "updated" && outcome.changedFields.length > 0
          ? ` — 字段: ${outcome.changedFields.join(", ")}`
          : "";
      lines.push(
        `  [${label}] /${outcome.name}${idPart}${verifiedPart}${detailPart}`,
      );
    }
  }

  lines.push("");
  if (result.preservedCommands.length === 0) {
    lines.push("其他未管理命令: 0 个");
  } else {
    const names = result.preservedCommands
      .map((cmd) =>
        cmd.commandType !== undefined && cmd.commandType !== 1
          ? `${cmd.name}(type=${cmd.commandType})`
          : `/${cmd.name}`,
      )
      .join(", ");
    lines.push(
      `保留其他未管理命令 (${result.preservedCommands.length} 个): ${names}`,
    );
  }

  for (const note of result.notes) {
    lines.push(`提示: ${note}`);
  }

  return `${lines.join("\n")}\n`;
}

export interface RunNativeCommandsCliOptions {
  readonly env?: NodeJS.ProcessEnv;
  readonly stdout?: { write(chunk: string): unknown };
  readonly stderr?: { write(chunk: string): unknown };
  readonly logger?: SubsystemLogger;
  /** Loopback-only HTTP test origin; never exposed via CLI flags or environment variables. */
  readonly testApiOrigin?: string;
  readonly requestTimeoutMs?: number;
  readonly totalTimeoutMs?: number;
  readonly maxRequests?: number;
  readonly maxPages?: number;
  readonly maxRateLimitRetries?: number;
  readonly fetchImpl?: typeof fetch;
  readonly sleepFn?: (ms: number, signal?: AbortSignal) => Promise<void>;
  readonly nowFn?: () => number;
}

export async function runNativeCommandsCli(
  argv: readonly string[],
  options: RunNativeCommandsCliOptions = {},
): Promise<number> {
  const env = options.env ?? process.env;
  const stdout = options.stdout ?? process.stdout;
  const stderr = options.stderr ?? process.stderr;

  const envSecrets = [
    env[DALTON_FEISHU_APP_SECRET_ENV] ?? "",
    env[DALTON_DISCORD_BOT_TOKEN_ENV] ?? "",
  ].filter((s) => s.length > 0);

  let wantJson = argv.includes("--json");

  try {
    const parsed = parseNativeCommandsCliArgs(argv);
    wantJson = parsed.json;

    if (parsed.help) {
      stdout.write(NATIVE_COMMANDS_CLI_HELP);
      return 0;
    }

    let result: NativeCommandSyncResult;
    if (parsed.platform === "feishu") {
      const creds = loadFeishuNativeCommandCredentials(env);
      result = await syncFeishuNativeCommands({
        appId: creds.appId,
        appSecret: creds.appSecret,
        mode: parsed.mode,
        logger: options.logger,
        testApiOrigin: options.testApiOrigin,
        requestTimeoutMs: options.requestTimeoutMs,
        totalTimeoutMs: options.totalTimeoutMs,
        maxRequests: options.maxRequests,
        maxPages: options.maxPages,
        maxRateLimitRetries: options.maxRateLimitRetries,
        fetchImpl: options.fetchImpl,
        sleepFn: options.sleepFn,
        nowFn: options.nowFn,
      });
    } else {
      const creds = loadDiscordNativeCommandCredentials(env);
      result = await syncDiscordNativeCommands({
        botToken: creds.botToken,
        applicationId: creds.applicationId,
        scope: parsed.discordScope,
        mode: parsed.mode,
        logger: options.logger,
        testApiOrigin: options.testApiOrigin,
        requestTimeoutMs: options.requestTimeoutMs,
        totalTimeoutMs: options.totalTimeoutMs,
        maxRequests: options.maxRequests,
        maxPages: options.maxPages,
        maxRateLimitRetries: options.maxRateLimitRetries,
        fetchImpl: options.fetchImpl,
        sleepFn: options.sleepFn,
        nowFn: options.nowFn,
      });
    }

    if (wantJson) {
      stdout.write(
        `${scrubNativeCommandSecrets(JSON.stringify(result, null, 2), envSecrets, 65_536)}\n`,
      );
    } else {
      stdout.write(
        scrubNativeCommandSecrets(
          formatNativeCommandSyncResult(result),
          envSecrets,
          65_536,
        ),
      );
    }

    if (!result.ok || result.status === "unconfirmed") {
      if (result.errorMessage) {
        stderr.write(
          `[dalton-native-commands] error: ${scrubNativeCommandSecrets(result.errorMessage, envSecrets, 2048)}\n`,
        );
      }
      return 4;
    }

    return 0;
  } catch (err) {
    const exitCode = exitCodeForNativeCommandError(err);
    const rawMsg = err instanceof Error ? err.message : String(err);
    const safeMsg = scrubNativeCommandSecrets(rawMsg, envSecrets, 2048);
    const errorCode =
      err instanceof NativeCommandRegistrationError
        ? err.code
        : "unexpected_error";

    if (wantJson) {
      stdout.write(
        `${JSON.stringify(
          {
            ok: false,
            status: "error",
            code: errorCode,
            errorMessage: safeMsg,
          },
          null,
          2,
        )}\n`,
      );
    } else {
      stderr.write(`[dalton-native-commands] error (${errorCode}): ${safeMsg}\n`);
    }
    return exitCode;
  }
}

export function isCliExecutedDirectly(
  importMetaUrl: string,
  argv1: string | undefined = process.argv[1],
): boolean {
  if (!argv1) {
    return false;
  }
  const modulePath = fileURLToPath(importMetaUrl);
  try {
    return realpathSync(resolve(argv1)) === realpathSync(modulePath);
  } catch {
    return resolve(argv1) === resolve(modulePath);
  }
}
