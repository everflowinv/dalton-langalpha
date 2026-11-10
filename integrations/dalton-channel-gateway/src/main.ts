import { HttpBackendClient } from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import {
  FeishuChannelAdapter,
  type FeishuChannelAdapterOptions,
} from "./channels/feishu/index.js";
import { type GatewayConfig, loadConfig } from "./config.js";
import { createGatewayApp } from "./server.js";
import type { ChannelPlatform } from "./types.js";

export function buildConfiguredAdapters(
  config: GatewayConfig,
  overrides?: {
    feishu?: Partial<FeishuChannelAdapterOptions>;
  },
): Map<ChannelPlatform, ChannelAdapter> {
  const adapters = new Map<ChannelPlatform, ChannelAdapter>();

  if (
    config.channels.feishu.enabled &&
    config.channels.feishu.appId &&
    config.channels.feishu.appSecret
  ) {
    adapters.set(
      "feishu",
      new FeishuChannelAdapter({
        appId: config.channels.feishu.appId,
        appSecret: config.channels.feishu.appSecret,
        outgoingOnly: config.mode === "outgoing",
        ...overrides?.feishu,
      }),
    );
  }

  return adapters;
}

function resolveCliModeOverride(argv: readonly string[]): "incoming" | "outgoing" | undefined {
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
      throw new Error(`Invalid --mode="${val}". Expected "incoming" or "outgoing".`);
    }
    if (arg === "--mode" && i + 1 < argv.length) {
      const val = argv[i + 1]!.trim().toLowerCase();
      if (val === "incoming" || val === "outgoing") {
        return val;
      }
      throw new Error(`Invalid --mode "${val}". Expected "incoming" or "outgoing".`);
    }
  }
  return undefined;
}

export async function main(
  env: NodeJS.ProcessEnv = process.env,
  argv: readonly string[] = process.argv.slice(2),
): Promise<void> {
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

  const backend = new HttpBackendClient({
    baseUrl: config.backendUrl,
    serviceToken: config.serviceToken,
  });

  const adapters = buildConfiguredAdapters(config);

  const app = createGatewayApp({
    config,
    backend,
    adapters,
  });

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
    process.stderr.write(
      `[dalton-channel-gateway] fatal: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`,
    );
    process.exit(1);
  });
}
