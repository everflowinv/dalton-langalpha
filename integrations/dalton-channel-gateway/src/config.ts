import { homedir } from "node:os";
import { resolve } from "node:path";
import { loadSecureGatewayEnvFile } from "./vault-credentials.js";

export type GatewayStartupMode = "incoming" | "outgoing";
export type FeishuCredentialSource = "vault" | "env";

export interface GatewayConfig {
  /** Strictly `127.0.0.1` (§0.2, §5.1). */
  host: "127.0.0.1";
  /** Default `8962` (§0.2, §5.1). */
  port: number;
  /** Startup mode: `"incoming"` (default) or `"outgoing"` (notification-only, no inbound listeners/Weixin/startup recovery). */
  mode?: GatewayStartupMode;
  /** Explicit Feishu credential source: `"vault"` (production startup from primary admin Vault) or `"env"`. */
  feishuCredentialSource?: FeishuCredentialSource;
  /** Shared `INTERNAL_SERVICE_TOKEN` (length >= 32, §1.1). */
  serviceToken: string;
  /** Backend base URL, e.g. `http://127.0.0.1:8000`. */
  backendUrl: string;
  /** Frontend web base URL for settings/interrupt links, e.g. `https://app.ginlix.ai` or `http://localhost:5173`. */
  webBaseUrl: string;
  /** Local Weixin cache directory (`~/.dalton/channel-gateway/weixin`, mode 0700). */
  weixinCacheDir: string;
  /** Whether colleague workspace dispatch is enabled (`DALTON_COLLEAGUE_DISPATCH_ENABLED`). */
  colleagueDispatchEnabled: boolean;
  /** Enabled channel platforms. */
  channels: {
    feishu: {
      enabled: boolean;
      appId: string;
      appSecret: string;
    };
    discord: {
      enabled: boolean;
      botToken: string;
      applicationId: string;
    };
    weixin: {
      enabled: boolean;
      apiBaseUrl: string;
    };
  };
}

function parseBool(value: string | undefined, defaultValue = false): boolean {
  if (value === undefined) return defaultValue;
  const normalized = value.trim().toLowerCase();
  return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
}

export function loadConfig(
  env: NodeJS.ProcessEnv = process.env,
): GatewayConfig & {
  mode: GatewayStartupMode;
  feishuCredentialSource: FeishuCredentialSource;
} {
  const explicitCredSourceRaw = (env.DALTON_FEISHU_CREDENTIAL_SOURCE ?? "")
    .trim()
    .toLowerCase();
  if (
    explicitCredSourceRaw !== "" &&
    explicitCredSourceRaw !== "vault" &&
    explicitCredSourceRaw !== "env"
  ) {
    throw new Error(
      `Invalid DALTON_FEISHU_CREDENTIAL_SOURCE "${explicitCredSourceRaw}". Expected "vault" or "env".`,
    );
  }

  const envFilePath = (env.DALTON_CHANNEL_GATEWAY_ENV_FILE ?? "").trim();
  const fileValues = envFilePath
    ? loadSecureGatewayEnvFile(envFilePath, {
        forbidPlaintextFeishuCredentials: explicitCredSourceRaw === "vault",
      })
    : {};

  const mergedEnv: NodeJS.ProcessEnv = { ...fileValues, ...env };

  const mergedCredSourceRaw = (mergedEnv.DALTON_FEISHU_CREDENTIAL_SOURCE ?? "")
    .trim()
    .toLowerCase();
  if (
    mergedCredSourceRaw !== "" &&
    mergedCredSourceRaw !== "vault" &&
    mergedCredSourceRaw !== "env"
  ) {
    throw new Error(
      `Invalid DALTON_FEISHU_CREDENTIAL_SOURCE "${mergedCredSourceRaw}". Expected "vault" or "env".`,
    );
  }
  if (
    mergedCredSourceRaw === "vault" &&
    explicitCredSourceRaw !== "vault" &&
    envFilePath
  ) {
    // Re-check that the env file itself did not carry plaintext Feishu secrets when it enabled Vault mode.
    loadSecureGatewayEnvFile(envFilePath, {
      forbidPlaintextFeishuCredentials: true,
    });
  }

  const requestedHost = (mergedEnv.GATEWAY_HOST ?? "127.0.0.1").trim();
  if (requestedHost !== "127.0.0.1") {
    throw new Error(
      `Security invariant violation: GATEWAY_HOST must be "127.0.0.1" (got "${requestedHost}")`,
    );
  }
  const rawPort = Number.parseInt(mergedEnv.GATEWAY_PORT ?? "8962", 10);
  const port =
    Number.isFinite(rawPort) && rawPort >= 0 && rawPort <= 65535
      ? rawPort
      : 8962;

  const serviceToken = (mergedEnv.INTERNAL_SERVICE_TOKEN ?? "").trim();
  const backendUrl = (
    mergedEnv.DALTON_BACKEND_URL ??
    mergedEnv.BACKEND_URL ??
    "http://127.0.0.1:8000"
  )
    .trim()
    .replace(/\/+$/, "");
  const webBaseUrl = (mergedEnv.WEB_BASE_URL ?? "http://localhost:5173")
    .trim()
    .replace(/\/+$/, "");
  const weixinCacheDir = (
    mergedEnv.DALTON_WEIXIN_CACHE_DIR ??
    resolve(homedir(), ".dalton", "channel-gateway", "weixin")
  ).trim();

  const rawMode = (
    mergedEnv.DALTON_GATEWAY_MODE ??
    mergedEnv.GATEWAY_MODE ??
    "incoming"
  )
    .trim()
    .toLowerCase();
  if (rawMode !== "incoming" && rawMode !== "outgoing") {
    throw new Error(
      `Invalid gateway mode "${rawMode}". Expected "incoming" or "outgoing".`,
    );
  }
  const mode: GatewayStartupMode = rawMode;

  const rawEnvFeishuAppId = (mergedEnv.DALTON_FEISHU_APP_ID ?? "").trim();
  const rawEnvFeishuAppSecret = (
    mergedEnv.DALTON_FEISHU_APP_SECRET ?? ""
  ).trim();
  const discordBotToken = (mergedEnv.DALTON_DISCORD_BOT_TOKEN ?? "").trim();

  const feishuCredentialSource: FeishuCredentialSource =
    mergedCredSourceRaw === "vault"
      ? "vault"
      : mergedCredSourceRaw === "env"
        ? "env"
        : mode === "outgoing" &&
            !rawEnvFeishuAppId &&
            !rawEnvFeishuAppSecret &&
            parseBool(mergedEnv.DALTON_FEISHU_ENABLED, false)
          ? "vault"
          : "env";

  // In `"vault"` mode, environment Feishu credentials are strictly ignored so failed Vault loads never fall back to env.
  const feishuAppId =
    feishuCredentialSource === "vault" ? "" : rawEnvFeishuAppId;
  const feishuAppSecret =
    feishuCredentialSource === "vault" ? "" : rawEnvFeishuAppSecret;

  const feishuEnabled =
    feishuCredentialSource === "vault"
      ? parseBool(mergedEnv.DALTON_FEISHU_ENABLED, mode === "outgoing")
      : mode === "outgoing" && mergedEnv.DALTON_FEISHU_ENABLED === undefined
        ? Boolean(feishuAppId && feishuAppSecret)
        : parseBool(mergedEnv.DALTON_FEISHU_ENABLED, false);

  const discordEnabled =
    mode === "outgoing" && mergedEnv.DALTON_DISCORD_ENABLED === undefined
      ? Boolean(discordBotToken)
      : parseBool(mergedEnv.DALTON_DISCORD_ENABLED, false);

  return {
    host: "127.0.0.1",
    port,
    mode,
    feishuCredentialSource,
    serviceToken,
    backendUrl,
    webBaseUrl,
    weixinCacheDir,
    colleagueDispatchEnabled: parseBool(
      mergedEnv.DALTON_COLLEAGUE_DISPATCH_ENABLED,
      true,
    ),
    channels: {
      feishu: {
        enabled: feishuEnabled,
        appId: feishuAppId,
        appSecret: feishuAppSecret,
      },
      discord: {
        enabled: discordEnabled,
        botToken: discordBotToken,
        applicationId: (mergedEnv.DALTON_DISCORD_APPLICATION_ID ?? "").trim(),
      },
      weixin: {
        enabled:
          mode === "outgoing"
            ? false
            : parseBool(mergedEnv.DALTON_WEIXIN_ENABLED, false),
        apiBaseUrl: (
          mergedEnv.DALTON_WEIXIN_API_BASE_URL ??
          "https://ilinkai.weixin.qq.com"
        )
          .trim()
          .replace(/\/+$/, ""),
      },
    },
  };
}
