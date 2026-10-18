// integrations/openclaw/dalton-langalpha-broker/src/models.mjs
import { prepareSimpleCompletionModelForAgent } from "./sdk-bridge.mjs";

const TOOL_CAPABLE_APIS = new Set([
  "openai-completions",
  "openai-responses",
  "openai-chatgpt-responses",
  "azure-openai-responses",
  "anthropic-messages",
  "google-generative-ai",
  "google-vertex",
  "bedrock-converse-stream",
]);

function isPreparedResultOk(prepared) {
  return Boolean(
    prepared &&
      prepared.ok !== false &&
      !("error" in prepared) &&
      prepared.model &&
      typeof prepared.model === "object",
  );
}

function isAllowedModel(providerId, modelRef, pluginConfig) {
  const allowedProviders = Array.isArray(pluginConfig?.allowedProviders)
    ? pluginConfig.allowedProviders.map((s) => String(s).trim()).filter(Boolean)
    : [];
  if (allowedProviders.length > 0 && !allowedProviders.includes(providerId)) {
    return false;
  }
  const allowedModels = Array.isArray(pluginConfig?.allowedModels)
    ? pluginConfig.allowedModels.map((s) => String(s).trim()).filter(Boolean)
    : [];
  if (allowedModels.length > 0 && !allowedModels.includes(modelRef)) {
    return false;
  }
  return true;
}

const authCheckCache = new Map();
const AUTH_CACHE_TTL_MS = 60_000;

export async function listBrokerModels({
  cfg,
  pluginConfig = {},
  agentId = "main",
  includeAuthCheck = false,
  prepareModelFn = prepareSimpleCompletionModelForAgent,
}) {
  const providersObj = cfg?.models?.providers;
  if (!providersObj || typeof providersObj !== "object") {
    return [];
  }

  const created = Math.floor(Date.now() / 1000);
  const rawEntries = [];

  for (const [providerId, providerCfg] of Object.entries(providersObj)) {
    if (!providerCfg || typeof providerCfg !== "object") continue;
    const providerApi = String(providerCfg.api || "openai-completions");
    const modelsList = Array.isArray(providerCfg.models) ? providerCfg.models : [];

    for (const modelEntry of modelsList) {
      if (!modelEntry || typeof modelEntry !== "object" || !modelEntry.id) continue;
      const modelId = String(modelEntry.id).trim();
      if (!modelId) continue;
      const modelRef = `${providerId}/${modelId}`;

      if (!isAllowedModel(providerId, modelRef, pluginConfig)) {
        continue;
      }

      const api = String(modelEntry.api || providerApi);
      const reasoning = Boolean(modelEntry.reasoning);
      const inputModalities = Array.isArray(modelEntry.input)
        ? modelEntry.input.map(String)
        : ["text"];
      const cost = modelEntry.cost && typeof modelEntry.cost === "object"
        ? {
            input: Number(modelEntry.cost.input ?? 0),
            output: Number(modelEntry.cost.output ?? 0),
            cacheRead: Number(modelEntry.cost.cacheRead ?? 0),
            cacheWrite: Number(modelEntry.cost.cacheWrite ?? 0),
          }
        : { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

      rawEntries.push({
        providerId,
        modelId,
        modelRef,
        modelEntry,
        api,
        reasoning,
        inputModalities,
        cost,
      });
    }
  }

  const useCache = prepareModelFn === prepareSimpleCompletionModelForAgent;
  const now = Date.now();

  const items = await Promise.all(
    rawEntries.map(async (entry) => {
      let authReady = null;
      let authMode = null;
      if (includeAuthCheck) {
        const cacheKey = `${agentId}:${entry.modelRef}`;
        const cached = useCache ? authCheckCache.get(cacheKey) : null;
        if (cached && now - cached.ts < AUTH_CACHE_TTL_MS) {
          authReady = cached.authReady;
          authMode = cached.authMode;
        } else {
          try {
            const prepared = await prepareModelFn({
              cfg,
              agentId,
              modelRef: entry.modelRef,
            });
            const ok = isPreparedResultOk(prepared);
            authReady = ok;
            authMode = ok ? String(prepared.auth?.mode || "configured") : null;
          } catch {
            authReady = false;
          }
          if (useCache) {
            authCheckCache.set(cacheKey, { ts: Date.now(), authReady, authMode });
          }
        }
      }

      return {
        id: entry.modelRef,
        object: "model",
        created,
        owned_by: entry.providerId,
        dalton_capabilities: {
          provider: entry.providerId,
          model_id: entry.modelId,
          name: String(entry.modelEntry.name || entry.modelId),
          api: entry.api,
          reasoning: entry.reasoning,
          supports_tools: TOOL_CAPABLE_APIS.has(entry.api),
          supports_streaming: true,
          input_modalities: entry.inputModalities,
          context_window: Number(entry.modelEntry.contextWindow ?? 0) || null,
          max_tokens: Number(entry.modelEntry.maxTokens ?? 0) || null,
          cost_per_million_usd: entry.cost,
          ...(includeAuthCheck ? { auth_ready: authReady, auth_mode: authMode } : {}),
        },
      };
    }),
  );

  return items;
}
