import { HttpBackendClient } from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import {
  FeishuChannelAdapter,
  type FeishuChannelAdapterOptions,
} from "./channels/feishu/index.js";
import {
  WeixinChannelAdapter,
  type WeixinChannelAdapterOptions,
} from "./channels/weixin/index.js";
import { type GatewayConfig, loadConfig } from "./config.js";
import { createGatewayApp } from "./server.js";
import type { ChannelPlatform } from "./types.js";

export function buildConfiguredAdapters(
  config: GatewayConfig,
  overrides?: {
    feishu?: Partial<FeishuChannelAdapterOptions>;
    weixin?: Partial<WeixinChannelAdapterOptions>;
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
        ...overrides?.feishu,
      }),
    );
  }

  if (config.channels.weixin.enabled) {
    adapters.set(
      "weixin",
      new WeixinChannelAdapter({
        baseUrl: config.channels.weixin.apiBaseUrl,
        cdnBaseUrl: config.channels.weixin.cdnBaseUrl,
        ...overrides?.weixin,
      }),
    );
  }

  return adapters;
}

export async function main(env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const config = loadConfig(env);
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
