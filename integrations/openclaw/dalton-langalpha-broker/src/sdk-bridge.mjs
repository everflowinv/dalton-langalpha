// integrations/openclaw/dalton-langalpha-broker/src/sdk-bridge.mjs
import Module from "node:module";
import os from "node:os";
import path from "node:path";

const req = Module.createRequire(import.meta.url);

let cachedLlm = null;
let cachedSimpleRuntime = null;
let cachedXSearch = null;

function loadSdkSubpath(subpath) {
  try {
    return req(`openclaw/plugin-sdk/${subpath}`);
  } catch {
    const fallbackPath = path.join(
      os.homedir(),
      ".openclaw",
      "tools",
      "node",
      "lib",
      "node_modules",
      "openclaw",
      "dist",
      "plugin-sdk",
      `${subpath}.js`,
    );
    return req(fallbackPath);
  }
}

function getLlmSdk() {
  if (!cachedLlm) {
    cachedLlm = loadSdkSubpath("llm");
  }
  return cachedLlm;
}

function getSimpleRuntimeSdk() {
  if (!cachedSimpleRuntime) {
    cachedSimpleRuntime = loadSdkSubpath("simple-completion-runtime");
  }
  return cachedSimpleRuntime;
}

export function streamSimple(model, context, options) {
  return getLlmSdk().streamSimple(model, context, options);
}

export function clampThinkingLevel(model, level) {
  return getLlmSdk().clampThinkingLevel(model, level);
}

export function calculateCost(model, usage) {
  return getLlmSdk().calculateCost(model, usage);
}

export function prepareSimpleCompletionModelForAgent(params) {
  return getSimpleRuntimeSdk().prepareSimpleCompletionModelForAgent(params);
}

function getXSearchModule() {
  if (!cachedXSearch) {
    try {
      cachedXSearch = req("openclaw/dist/extensions/xai/x-search.js");
    } catch {
      const candidates = [
        path.resolve(
          path.dirname(process.execPath),
          "..",
          "lib",
          "node_modules",
          "openclaw",
          "dist",
          "extensions",
          "xai",
          "x-search.js",
        ),
        path.join(
          os.homedir(),
          ".openclaw",
          "tools",
          "node",
          "lib",
          "node_modules",
          "openclaw",
          "dist",
          "extensions",
          "xai",
          "x-search.js",
        ),
      ];
      let lastErr;
      for (const candidate of candidates) {
        try {
          cachedXSearch = req(candidate);
          break;
        } catch (err) {
          lastErr = err;
        }
      }
      if (!cachedXSearch) {
        throw lastErr;
      }
    }
  }
  return cachedXSearch;
}

export function createXSearchTool(options) {
  return getXSearchModule().createXSearchTool(options);
}

const XAI_PROVIDER_IDS = new Set(["xai", "x-ai"]);
const XAI_GROK_OAUTH_BASE_URL = "https://cli-chat-proxy.grok.com/v1";
let warnedMissingClientVersion = false;
let warnedMissingModelId = false;

/**
 * Mirrors OpenClaw `isXaiProviderId` (`extensions/xai/provider-id.ts`).
 */
export function isXaiProviderId(provider) {
  return typeof provider === "string" && XAI_PROVIDER_IDS.has(provider.trim().toLowerCase());
}

/**
 * Mirrors OpenClaw `isXaiGrokProxyBaseUrl` (`extensions/xai/provider-catalog.ts`).
 */
export function isXaiGrokProxyBaseUrl(baseUrl) {
  return URL.parse(String(baseUrl ?? ""))?.href.replace(/\/+$/u, "") === XAI_GROK_OAUTH_BASE_URL;
}

/**
 * Mirrors OpenClaw `createXaiGrokOAuthHeadersWrapper` (`extensions/xai/stream.ts`).
 *
 * `prepareSimpleCompletionModelForAgent` (`openclaw/plugin-sdk/simple-completion-runtime`)
 * delegates to `prepareModelForSimpleCompletion`, which only invokes
 * `plugin.wrapSimpleCompletionStreamFn`. The OpenClaw xAI plugin only registers
 * `wrapStreamFn` (`wrapXaiProviderStream` in `extensions/xai/stream.ts`) and
 * `openclaw/plugin-sdk/*` does not expose provider `wrapStreamFn`, so we apply
 * `createXaiGrokOAuthHeadersWrapper` directly before `streamSimple`.
 */
export function createXaiGrokOAuthHeadersWrapper(baseStreamFn, clientVersion, logger) {
  const underlying = baseStreamFn ?? streamSimple;
  const normalizedClientVersion = String(clientVersion ?? "").trim();
  return (model, context, options) => {
    if (!isXaiProviderId(model?.provider) || !isXaiGrokProxyBaseUrl(model?.baseUrl)) {
      return underlying(model, context, options);
    }
    if (!normalizedClientVersion) {
      if (!warnedMissingClientVersion) {
        warnedMissingClientVersion = true;
        logger?.warn?.(
          "[dalton-broker] 缺 OpenClaw 运行时版本，Grok 代理会返回 426，已原样透传",
        );
      }
      return underlying(model, context, options);
    }
    const normalizedModelId = String(model?.id ?? "").trim();
    if (!normalizedModelId) {
      if (!warnedMissingModelId) {
        warnedMissingModelId = true;
        logger?.warn?.(
          "[dalton-broker] xAI Grok 代理调用缺 model.id，跳过请求头注入并原样透传",
        );
      }
      return underlying(model, context, options);
    }
    const headers = new Headers(options?.headers);
    headers.set("X-XAI-Token-Auth", "xai-grok-cli");
    headers.set("x-grok-client-version", normalizedClientVersion);
    headers.set("x-grok-model-override", normalizedModelId);
    return underlying(model, context, {
      ...options,
      headers: Object.fromEntries(headers.entries()),
    });
  };
}

