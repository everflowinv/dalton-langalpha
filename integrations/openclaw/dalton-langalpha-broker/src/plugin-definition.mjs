// integrations/openclaw/dalton-langalpha-broker/src/plugin-definition.mjs
import os from "node:os";
import path from "node:path";
import { verifyLoopbackAndOrigin, verifyBearerToken, ensureScopedTokenFile } from "./auth.mjs";
import { sendJson, sendOpenAiError } from "./http.mjs";
import { listBrokerModels } from "./models.mjs";
import { handleChatCompletions, handleLegacyInvoke } from "./invoke.mjs";
import { createBrokerJournal } from "./journal.mjs";
import { createConcurrencyLimiter } from "./limits.mjs";
import { createReplayStore } from "./replay-state.mjs";

export const PLUGIN_ID = "dalton-langalpha-broker";
export const PLUGIN_VERSION = "0.2.0";
export const DEFAULT_ROUTE_PREFIX = "/dalton-broker/v1";

function normalizeRoutePrefix(rawPrefix) {
  const trimmed = String(rawPrefix || DEFAULT_ROUTE_PREFIX).trim();
  const withLeading = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeading.replace(/\/+$/, "");
}

function resolveDefaultStateDir(api, ctx) {
  const normalizeBaseStateDir = (baseDir) => {
    const resolved = path.resolve(String(baseDir));
    return path.basename(resolved) === ".openclaw"
      ? path.join(resolved, "state", "plugins", PLUGIN_ID)
      : path.join(resolved, "plugins", PLUGIN_ID);
  };
  if (ctx?.stateDir) {
    return normalizeBaseStateDir(ctx.stateDir);
  }
  try {
    const baseStateDir = api?.runtime?.state?.resolveStateDir?.();
    if (baseStateDir) {
      return normalizeBaseStateDir(baseStateDir);
    }
  } catch {
    // fallback below
  }
  return path.join(os.homedir(), ".openclaw", "state", "plugins", PLUGIN_ID);
}

export function createPluginDefinition(deps = {}) {
  return {
    id: PLUGIN_ID,
    name: "Dalton LangAlpha Model Broker v2",
    version: PLUGIN_VERSION,
    description:
      "Loopback OpenAI-compatible chat/completions + models broker backed by OpenClaw runtime auth, streaming, tool calls, and cost metadata.",

    register(api) {
      const pluginConfig =
        api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
      if (pluginConfig.enabled === false) {
        api.logger?.info?.(`[${PLUGIN_ID}] disabled via pluginConfig.enabled=false`);
        return;
      }

      const routePrefix = normalizeRoutePrefix(pluginConfig.routePrefix);
      const limiter = createConcurrencyLimiter(pluginConfig.maxConcurrent ?? 8);
      const replayStore = createReplayStore();

      const brokerState = {
        startedAt: null,
        stateDir: null,
        tokenState: { token: "", tokenPath: "" },
        journal: { append: async () => {}, journalDir: "" },
        initPromise: null,
      };

      async function ensureInitialized(ctx) {
        if (brokerState.startedAt && brokerState.tokenState.token) {
          return brokerState;
        }
        if (brokerState.initPromise) {
          return brokerState.initPromise;
        }
        brokerState.initPromise = (async () => {
          const stateDir = resolveDefaultStateDir(api, ctx);
          const tokenState = await ensureScopedTokenFile({
            stateDir,
            explicitTokenPath: pluginConfig.authTokenPath,
          });
          const journalDir = pluginConfig.journalDir
            ? path.resolve(pluginConfig.journalDir)
            : path.join(stateDir, "journal");
          const journal = await createBrokerJournal({ journalDir });

          brokerState.startedAt = new Date().toISOString();
          brokerState.stateDir = stateDir;
          brokerState.tokenState = tokenState;
          brokerState.journal = journal;
          return brokerState;
        })();
        try {
          return await brokerState.initPromise;
        } finally {
          brokerState.initPromise = null;
        }
      }

      api.registerService({
        id: `${PLUGIN_ID}-service`,
        async start(ctx) {
          const initialized = await ensureInitialized(ctx);
          ctx.logger?.info?.(
            `[${PLUGIN_ID}] v${PLUGIN_VERSION} started on ${routePrefix}/* (tokenPath=${initialized.tokenState.tokenPath}, journalDir=${initialized.journal.journalDir})`,
          );
        },
        async stop(ctx) {
          ctx.logger?.info?.(`[${PLUGIN_ID}] service stopped`);
        },
      });

      api.registerHttpRoute({
        path: `${routePrefix}/`,
        auth: "plugin",
        match: "prefix",
        handler: async (req, res) => {
          const rawUrl = String(req.url || "/");
          const parsedUrl = new URL(rawUrl, "http://127.0.0.1");
          const pathname = parsedUrl.pathname.replace(/\/+$/, "");

          if (!pathname.startsWith(routePrefix)) {
            return false;
          }

          await ensureInitialized();

          // 1. Strict loopback + no-proxy-header verification on EVERY route
          const loopCheck = verifyLoopbackAndOrigin(req);
          if (!loopCheck.ok) {
            sendOpenAiError(res, loopCheck.status, {
              code: loopCheck.code,
              type: "permission_error",
              message: loopCheck.message,
            });
            return true;
          }

          const subPath = pathname.slice(routePrefix.length) || "/";
          const method = String(req.method || "GET").toUpperCase();

          // 2. Unauthenticated loopback health probe
          if (method === "GET" && subPath === "/health") {
            const currentCfg = api.runtime?.config?.current?.() || api.config || {};
            const models = await listBrokerModels({
              cfg: currentCfg,
              pluginConfig,
              agentId: pluginConfig.defaultAgentId || "main",
              includeAuthCheck: false,
            });
            sendJson(res, 200, {
              ok: true,
              status: "ok",
              plugin_id: PLUGIN_ID,
              version: PLUGIN_VERSION,
              route_prefix: routePrefix,
              started_at: brokerState.startedAt,
              active_requests: limiter.activeCount,
              max_concurrent: limiter.limit,
              configured_models_count: models.length,
              token_configured: Boolean(brokerState.tokenState.token),
            });
            return true;
          }

          // 3. Bearer token verification for all other endpoints
          const bearerCheck = verifyBearerToken(req, brokerState.tokenState.token);
          if (!bearerCheck.ok) {
            sendOpenAiError(res, bearerCheck.status, {
              code: bearerCheck.code,
              type: "authentication_error",
              message: bearerCheck.message,
            });
            return true;
          }

          const currentCfg = api.runtime?.config?.current?.() || api.config || {};

          // 4. GET /dalton-broker/v1/models
          if (method === "GET" && subPath === "/models") {
            const includeAuthCheck = parsedUrl.searchParams.get("detail") === "1";
            const data = await listBrokerModels({
              cfg: currentCfg,
              pluginConfig,
              agentId: pluginConfig.defaultAgentId || "main",
              includeAuthCheck,
              ...(deps.prepareSimpleCompletionModelForAgent
                ? { prepareModelFn: deps.prepareSimpleCompletionModelForAgent }
                : {}),
            });
            sendJson(res, 200, {
              object: "list",
              data,
            });
            return true;
          }

          // 5. POST /dalton-broker/v1/chat/completions
          if (method === "POST" && subPath === "/chat/completions") {
            await handleChatCompletions(req, res, {
              cfg: currentCfg,
              pluginConfig,
              limiter,
              journal: brokerState.journal,
              replayStore,
              logger: api.logger,
              deps,
            });
            return true;
          }

          // 6. POST /dalton-broker/v1/invoke (backward-compatible v1 endpoint)
          if (method === "POST" && subPath === "/invoke") {
            await handleLegacyInvoke(req, res, {
              cfg: currentCfg,
              pluginConfig,
              limiter,
              journal: brokerState.journal,
              replayStore,
              logger: api.logger,
              deps,
            });
            return true;
          }

          sendOpenAiError(res, 404, {
            code: "route_not_found",
            type: "invalid_request_error",
            message: `Unknown broker route: ${method} ${pathname}`,
          });
          return true;
        },
      });
    },
  };
}
