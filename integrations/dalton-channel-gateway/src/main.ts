import { HttpBackendClient } from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import { loadConfig } from "./config.js";
import { createGatewayApp } from "./server.js";
import type { ChannelPlatform } from "./types.js";

async function main(): Promise<void> {
  const config = loadConfig(process.env);
  if (!config.serviceToken || config.serviceToken.length < 32) {
    throw new Error(
      "INTERNAL_SERVICE_TOKEN must be set and at least 32 characters long.",
    );
  }

  const backend = new HttpBackendClient({
    baseUrl: config.backendUrl,
    serviceToken: config.serviceToken,
  });

  // Phase 3 registers live Feishu, Discord, and Weixin adapters here
  const adapters = new Map<ChannelPlatform, ChannelAdapter>();

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
