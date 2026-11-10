/**
 * Standalone Dalton environment configuration reader for native command registration.
 *
 * Strictly reads Dalton gateway environment variable names (`DALTON_FEISHU_*`,
 * `DALTON_DISCORD_*`) and never inspects OpenClaw files, OpenClaw environment
 * variables, or `.env` files on disk.
 */

import { NativeCommandRegistrationError } from "./types.js";

export const DALTON_FEISHU_APP_ID_ENV = "DALTON_FEISHU_APP_ID";
export const DALTON_FEISHU_APP_SECRET_ENV = "DALTON_FEISHU_APP_SECRET";
export const DALTON_DISCORD_BOT_TOKEN_ENV = "DALTON_DISCORD_BOT_TOKEN";
export const DALTON_DISCORD_APPLICATION_ID_ENV =
  "DALTON_DISCORD_APPLICATION_ID";

const DISCORD_SNOWFLAKE_PATTERN = /^\d{5,22}$/;

export interface FeishuNativeCommandCredentials {
  readonly appId: string;
  readonly appSecret: string;
}

export interface DiscordNativeCommandCredentials {
  readonly botToken: string;
  readonly applicationId: string;
}

export function validateDiscordSnowflake(
  value: string,
  fieldLabel: string,
  errorCode: "invalid_config" | "usage_error" = "invalid_config",
): string {
  const trimmed = value.trim();
  if (!DISCORD_SNOWFLAKE_PATTERN.test(trimmed)) {
    throw new NativeCommandRegistrationError(
      errorCode,
      `${fieldLabel} 格式无效（须为 5–22 位纯数字 Discord Snowflake ID）。`,
    );
  }
  return trimmed;
}

export function loadFeishuNativeCommandCredentials(
  env: NodeJS.ProcessEnv = process.env,
): FeishuNativeCommandCredentials {
  const appId = (env[DALTON_FEISHU_APP_ID_ENV] ?? "").trim();
  const appSecret = (env[DALTON_FEISHU_APP_SECRET_ENV] ?? "").trim();

  const missing: string[] = [];
  if (!appId) {
    missing.push(DALTON_FEISHU_APP_ID_ENV);
  }
  if (!appSecret) {
    missing.push(DALTON_FEISHU_APP_SECRET_ENV);
  }
  if (missing.length > 0) {
    throw new NativeCommandRegistrationError(
      "missing_config",
      `缺少飞书凭据配置：请设置环境变量 ${missing.join("、")}（仅使用 Dalton 网关配置名称，不读取 OpenClaw 配置）。`,
      { platform: "feishu" },
    );
  }

  if (/\s/.test(appId)) {
    throw new NativeCommandRegistrationError(
      "invalid_config",
      `${DALTON_FEISHU_APP_ID_ENV} 包含非法空白字符。`,
      { platform: "feishu" },
    );
  }

  return { appId, appSecret };
}

export function loadDiscordNativeCommandCredentials(
  env: NodeJS.ProcessEnv = process.env,
): DiscordNativeCommandCredentials {
  const botToken = (env[DALTON_DISCORD_BOT_TOKEN_ENV] ?? "").trim();
  const rawApplicationId = (
    env[DALTON_DISCORD_APPLICATION_ID_ENV] ?? ""
  ).trim();

  const missing: string[] = [];
  if (!botToken) {
    missing.push(DALTON_DISCORD_BOT_TOKEN_ENV);
  }
  if (!rawApplicationId) {
    missing.push(DALTON_DISCORD_APPLICATION_ID_ENV);
  }
  if (missing.length > 0) {
    throw new NativeCommandRegistrationError(
      "missing_config",
      `缺少 Discord 凭据配置：请设置环境变量 ${missing.join("、")}（仅使用 Dalton 网关配置名称，不读取 OpenClaw 配置）。`,
      { platform: "discord" },
    );
  }

  if (/^bot\s+/i.test(botToken) || /\s/.test(botToken)) {
    throw new NativeCommandRegistrationError(
      "invalid_config",
      `${DALTON_DISCORD_BOT_TOKEN_ENV} 须为不含空白或 "Bot " 前缀的原始机器人令牌。`,
      { platform: "discord" },
    );
  }

  const applicationId = validateDiscordSnowflake(
    rawApplicationId,
    DALTON_DISCORD_APPLICATION_ID_ENV,
    "invalid_config",
  );

  return { botToken, applicationId };
}
