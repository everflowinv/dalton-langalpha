import { homedir } from "node:os";
import { resolve } from "node:path";

export type GatewayStartupMode = "incoming" | "outgoing";

export interface GatewayConfig {
  /** Strictly `127.0.0.1` (§0.2, §5.1). */
  host: "127.0.0.1";
  /** Default `8962` (§0.2, §5.1). */
  port: number;
  /** Startup mode: `"incoming"` (default) or `"outgoing"` (notification-only, no inbound listeners/Weixin/startup recovery). */
  mode?: GatewayStartupMode;
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
): GatewayConfig & { mode: GatewayStartupMode } {
  const requestedHost = (env.GATEWAY_HOST ?? "127.0.0.1").trim();
  if (requestedHost !== "127.0.0.1") {
    throw new Error(
      `Security invariant violation: GATEWAY_HOST must be "127.0.0.1" (got "${requestedHost}")`,
    );
  }
  const rawPort = Number.parseInt(env.GATEWAY_PORT ?? "8962", 10);
  const port = Number.isFinite(rawPort) && rawPort >= 0 && rawPort <= 65535 ? rawPort : 8962;

  const serviceToken = (env.INTERNAL_SERVICE_TOKEN ?? "").trim();
  const backendUrl = (env.DALTON_BACKEND_URL ?? env.BACKEND_URL ?? "http://127.0.0.1:8000")
    .trim()
    .replace(/\/+$/, "");
  const webBaseUrl = (env.WEB_BASE_URL ?? "http://localhost:5173").trim().replace(/\/+$/, "");
  const weixinCacheDir = (
    env.DALTON_WEIXIN_CACHE_DIR ?? resolve(homedir(), ".dalton", "channel-gateway", "weixin")
  ).trim();

  const rawMode = (env.DALTON_GATEWAY_MODE ?? env.GATEWAY_MODE ?? "incoming")
    .trim()
    .toLowerCase();
  if (rawMode !== "incoming" && rawMode !== "outgoing") {
    throw new Error(
      `Invalid gateway mode "${rawMode}". Expected "incoming" or "outgoing".`,
    );
  }
  const mode: GatewayStartupMode = rawMode;

  const feishuAppId = (env.DALTON_FEISHU_APP_ID ?? "").trim();
  const feishuAppSecret = (env.DALTON_FEISHU_APP_SECRET ?? "").trim();
  const discordBotToken = (env.DALTON_DISCORD_BOT_TOKEN ?? "").trim();

  const feishuEnabled =
    mode === "outgoing" && env.DALTON_FEISHU_ENABLED === undefined
      ? Boolean(feishuAppId && feishuAppSecret)
      : parseBool(env.DALTON_FEISHU_ENABLED, false);
  const discordEnabled =
    mode === "outgoing" && env.DALTON_DISCORD_ENABLED === undefined
      ? Boolean(discordBotToken)
      : parseBool(env.DALTON_DISCORD_ENABLED, false);

  return {
    host: "127.0.0.1",
    port,
    mode,
    serviceToken,
    backendUrl,
    webBaseUrl,
    weixinCacheDir,
    colleagueDispatchEnabled: parseBool(env.DALTON_COLLEAGUE_DISPATCH_ENABLED, true),
    channels: {
      feishu: {
        enabled: feishuEnabled,
        appId: feishuAppId,
        appSecret: feishuAppSecret,
      },
      discord: {
        enabled: discordEnabled,
        botToken: discordBotToken,
      },
      weixin: {
        enabled: mode === "outgoing" ? false : parseBool(env.DALTON_WEIXIN_ENABLED, false),
        apiBaseUrl: (env.DALTON_WEIXIN_API_BASE_URL ?? "https://ilinkai.weixin.qq.com")
          .trim()
          .replace(/\/+$/, ""),
      },
    },
  };
}
