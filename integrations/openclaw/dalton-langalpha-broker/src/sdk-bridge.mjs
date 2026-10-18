// integrations/openclaw/dalton-langalpha-broker/src/sdk-bridge.mjs
import Module from "node:module";
import os from "node:os";
import path from "node:path";

const req = Module.createRequire(import.meta.url);

let cachedLlm = null;
let cachedSimpleRuntime = null;

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
