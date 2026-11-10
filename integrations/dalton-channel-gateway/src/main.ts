import { HttpBackendClient } from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import {
  DiscordChannelAdapter,
  type DiscordChannelAdapterOptions,
} from "./channels/discord/index.js";
import {
  FeishuChannelAdapter,
  type FeishuChannelAdapterOptions,
} from "./channels/feishu/index.js";
import {
  type FeishuCredentialSource,
  type GatewayConfig,
  type GatewayStartupMode,
  loadConfig,
} from "./config.js";
import { createGatewayApp, type GatewayApp } from "./server.js";
import {
  createSubsystemLogger,
  type SubsystemLogger,
} from "./shared/logger.js";
import type { ChannelPlatform } from "./types.js";
import {
  loadFeishuCredentialsFromVault,
  VaultBootstrapError,
} from "./vault-credentials.js";

export function buildConfiguredAdapters(
  config: GatewayConfig,
  overrides?: {
    feishu?: Partial<FeishuChannelAdapterOptions>;
    discord?: Partial<DiscordChannelAdapterOptions>;
  },
): Map<ChannelPlatform, ChannelAdapter> {
  const adapters = new Map<ChannelPlatform, ChannelAdapter>();

  const effectiveFeishuAppId = (
    overrides?.feishu?.appId ?? config.channels.feishu.appId
  ).trim();
  const effectiveFeishuAppSecret = (
    overrides?.feishu?.appSecret ?? config.channels.feishu.appSecret
  ).trim();

  if (
    config.channels.feishu.enabled &&
    effectiveFeishuAppId &&
    effectiveFeishuAppSecret
  ) {
    adapters.set(
      "feishu",
      new FeishuChannelAdapter({
        ...overrides?.feishu,
        appId: effectiveFeishuAppId,
        appSecret: effectiveFeishuAppSecret,
        outgoingOnly:
          overrides?.feishu?.outgoingOnly ?? config.mode === "outgoing",
      }),
    );
  }

  if (config.channels.discord.enabled && config.channels.discord.botToken) {
    adapters.set(
      "discord",
      new DiscordChannelAdapter({
        botToken: config.channels.discord.botToken,
        ...(config.channels.discord.applicationId
          ? { applicationId: config.channels.discord.applicationId }
          : {}),
        ...overrides?.discord,
      }),
    );
  }

  return adapters;
}

function resolveCliModeOverride(
  argv: readonly string[],
): "incoming" | "outgoing" | undefined {
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]!;
    if (arg === "--outgoing-only") {
      return "outgoing";
    }
    if (arg.startsWith("--mode=")) {
      const val = arg.slice("--mode=".length).trim().toLowerCase();
      if (val === "incoming" || val === "outgoing") {
        return val;
      }
      throw new Error(
        `Invalid --mode="${val}". Expected "incoming" or "outgoing".`,
      );
    }
    if (arg === "--mode" && i + 1 < argv.length) {
      const val = argv[i + 1]!.trim().toLowerCase();
      if (val === "incoming" || val === "outgoing") {
        return val;
      }
      throw new Error(
        `Invalid --mode "${val}". Expected "incoming" or "outgoing".`,
      );
    }
  }
  return undefined;
}

export interface PrepareGatewayRuntimeOptions {
  env?: NodeJS.ProcessEnv;
  argv?: readonly string[];
  fetchImpl?: typeof fetch;
  logger?: SubsystemLogger;
  vaultTimeoutMs?: number;
  adapterOverrides?: {
    feishu?: Partial<FeishuChannelAdapterOptions>;
  };
}

export interface PreparedGatewayRuntime {
  config: GatewayConfig & {
    mode: GatewayStartupMode;
    feishuCredentialSource: FeishuCredentialSource;
  };
  backend: HttpBackendClient;
  adapters: Map<ChannelPlatform, ChannelAdapter>;
  app: GatewayApp;
}

/**
 * Resolve configuration, load dedicated Feishu credentials from Dalton Vault when
 * `feishuCredentialSource === "vault"` BEFORE creating adapters or reporting readiness,
 * and assemble the `GatewayApp` instance. Fails closed without fallback on Vault errors.
 */
export async function prepareGatewayRuntime(
  options: PrepareGatewayRuntimeOptions = {},
): Promise<PreparedGatewayRuntime> {
  const env = options.env ?? process.env;
  const argv = options.argv ?? [];
  const logger =
    options.logger ?? createSubsystemLogger("dalton-channel-gateway/startup");

  const cliMode = resolveCliModeOverride(argv);
  const effectiveEnv = cliMode
    ? { ...env, DALTON_GATEWAY_MODE: cliMode }
    : env;

  const config = loadConfig(effectiveEnv);
  if (!config.serviceToken || config.serviceToken.length < 32) {
    throw new Error(
      "INTERNAL_SERVICE_TOKEN must be set and at least 32 characters long.",
    );
  }

  let feishuVaultOverrides:
    | { appId: string; appSecret: string }
    | undefined;

  if (
    config.channels.feishu.enabled &&
    config.feishuCredentialSource === "vault"
  ) {
    try {
      feishuVaultOverrides = await loadFeishuCredentialsFromVault({
        backendUrl: config.backendUrl,
        serviceToken: config.serviceToken,
        fetchImpl: options.fetchImpl,
        timeoutMs: options.vaultTimeoutMs,
        logger,
      });
    } catch (err) {
      if (err instanceof VaultBootstrapError) {
        logger.error("Failed to bootstrap dedicated Feishu credentials from Vault", {
          code: err.code,
          ...(err.missing.length > 0 ? { missing: [...err.missing] } : {}),
        });
      } else {
        logger.error("Failed to bootstrap dedicated Feishu credentials from Vault", {
          code: "vault_unavailable",
        });
      }
      throw err;
    }
  }

  const backend = new HttpBackendClient({
    baseUrl: config.backendUrl,
    serviceToken: config.serviceToken,
    ...(options.fetchImpl ? { fetchFn: options.fetchImpl } : {}),
  });

  const adapters = buildConfiguredAdapters(config, {
    ...options.adapterOverrides,
    feishu: {
      ...options.adapterOverrides?.feishu,
      ...(feishuVaultOverrides
        ? {
            appId: feishuVaultOverrides.appId,
            appSecret: feishuVaultOverrides.appSecret,
          }
        : {}),
    },
  });

  const app = createGatewayApp({
    config,
    backend,
    adapters,
  });

  return {
    config,
    backend,
    adapters,
    app,
  };
}

export async function main(
  env: NodeJS.ProcessEnv = process.env,
  argv: readonly string[] = process.argv.slice(2),
): Promise<void> {
  const { app } = await prepareGatewayRuntime({ env, argv });

  const { host, port } = await app.start();
  process.stdout.write(
    `[dalton-channel-gateway] listening on http://${host}:${port}\n`,
  );

  const shutdown = async () => {
    await app.stop();
    process.exit(0);
  };

  process.on("SIGINT", () => void shutdown());
  process.on("SIGTERM", () => void shutdown());
}

const isDirectExecution =
  process.argv[1] !== undefined &&
  (process.argv[1].endsWith("main.ts") ||
    process.argv[1].endsWith("index.mjs"));

if (isDirectExecution) {
  main().catch((err) => {
    const safeMessage =
      err instanceof Error ? err.message : "Gateway startup failed";
    process.stderr.write(
      `[dalton-channel-gateway] fatal: ${safeMessage}\n`,
    );
    process.exit(1);
  });
}
