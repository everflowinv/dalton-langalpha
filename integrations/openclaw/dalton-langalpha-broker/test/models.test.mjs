// integrations/openclaw/dalton-langalpha-broker/test/models.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { listBrokerModels } from "../src/models.mjs";

test("listBrokerModels enumerates configured providers/models and filters by allowlist", async () => {
  const fakeCfg = {
    models: {
      providers: {
        xai: {
          api: "openai-responses",
          models: [
            {
              id: "grok-4.7",
              name: "Grok 4.7",
              reasoning: true,
              input: ["text", "image"],
              contextWindow: 256000,
              maxTokens: 16384,
              cost: { input: 2.0, output: 10.0, cacheRead: 0.5, cacheWrite: 0 },
            },
          ],
        },
        google: {
          api: "google-generative-ai",
          models: [
            {
              id: "gemini-3.8-flash",
              name: "Gemini 3.8 Flash",
              reasoning: true,
              input: ["text", "image"],
              contextWindow: 1000000,
              maxTokens: 8192,
              cost: { input: 0.15, output: 0.6, cacheRead: 0.04, cacheWrite: 0 },
            },
          ],
        },
      },
    },
  };

  const allModels = await listBrokerModels({
    cfg: fakeCfg,
    includeAuthCheck: true,
    prepareModelFn: async ({ modelRef }) => ({
      model: { id: modelRef },
      auth: { mode: "api-key" },
    }),
  });
  assert.equal(allModels.length, 2);
  assert.equal(allModels[0].id, "xai/grok-4.7");
  assert.equal(allModels[0].dalton_capabilities.auth_ready, true);
  assert.equal(allModels[0].dalton_capabilities.supports_tools, true);

  const filtered = await listBrokerModels({
    cfg: fakeCfg,
    pluginConfig: { allowedProviders: ["google"] },
  });
  assert.equal(filtered.length, 1);
  assert.equal(filtered[0].id, "google/gemini-3.8-flash");
});
