// integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs
import crypto from "node:crypto";
import {
  streamSimple,
  clampThinkingLevel,
  calculateCost,
  prepareSimpleCompletionModelForAgent,
} from "./sdk-bridge.mjs";
import { sendJson, sendOpenAiError, beginSse, writeSseData, endSse, readJsonBody } from "./http.mjs";
import {
  normalizeOpenAiMessagesToContext,
  normalizeOpenAiTools,
  normalizeToolChoice,
  normalizeResponseFormat,
  buildNonStreamingChatCompletionResponse,
  createSseDeltaTranslator,
} from "./openai-map.mjs";
import { sha256Hex } from "./journal.mjs";
import { listBrokerModels } from "./models.mjs";

const REASONING_EFFORT_MAP = {
  none: "off",
  off: "off",
  minimal: "minimal",
  low: "low",
  medium: "medium",
  high: "high",
  xhigh: "xhigh",
  max: "max",
};

const idempotencyCache = new Map();

function resolveReasoningLevel(model, rawEffort) {
  if (!rawEffort || typeof rawEffort !== "string") return undefined;
  const mapped = REASONING_EFFORT_MAP[rawEffort.trim().toLowerCase()];
  if (!mapped) return undefined;
  try {
    return clampThinkingLevel(model, mapped);
  } catch {
    return mapped === "off" ? undefined : mapped;
  }
}

function isPreparedResultOk(prepared) {
  return Boolean(
    prepared &&
      prepared.ok !== false &&
      !("error" in prepared) &&
      prepared.model &&
      typeof prepared.model === "object",
  );
}

/**
 * Extracts the bound `completionTransport` model when `prepareSimpleCompletionModelForAgent`
 * attached `Symbol("openclaw.modelLlmRuntime")` (matching OpenClaw's internal
 * `completeWithPreparedSimpleCompletionModel`), while preserving plain objects in unit tests.
 */
export function resolveStreamExecutionModel(model) {
  if (!model || typeof model !== "object") return model;
  const runtimeSym = Object.getOwnPropertySymbols(model).find(
    (s) => s.description === "openclaw.modelLlmRuntime",
  );
  if (!runtimeSym) return model;
  const binding = model[runtimeSym];
  const transport = binding?.completionTransport;
  if (!transport || typeof transport !== "object") return model;
  const bound = { ...transport };
  for (const sym of Object.getOwnPropertySymbols(transport)) {
    Object.defineProperty(bound, sym, {
      value: transport[sym],
      enumerable: false,
      configurable: true,
    });
  }
  Object.defineProperty(bound, runtimeSym, {
    value: {
      ...binding,
      completionTransport: transport,
    },
    enumerable: false,
    configurable: true,
  });
  return bound;
}

/**
 * Builds dual-compatible `toolChoice` + `onPayload` fallback so all OpenClaw transports
 * (`openai-completions`, `openai-responses`, `openai-chatgpt-responses`, `google-generative-ai`)
 * honor `tool_choice` (`"auto"`, `"none"`, `"required"`, or `{ type: "function", function: { name } }`).
 */
function buildToolChoiceStreamOptions(normalizedToolChoice) {
  if (normalizedToolChoice === undefined) return {};
  const dualToolChoice =
    typeof normalizedToolChoice === "object" &&
    normalizedToolChoice.type === "function" &&
    normalizedToolChoice.function?.name
      ? {
          type: "function",
          name: normalizedToolChoice.function.name,
          function: { name: normalizedToolChoice.function.name },
        }
      : normalizedToolChoice;

  const onPayload = (payload) => {
    if (!payload || typeof payload !== "object") return payload;
    if (payload.tool_choice !== undefined) return payload;
    const isResponsesPayload = "input" in payload && !("messages" in payload);
    const isCompletionsPayload = "messages" in payload;
    if (!isResponsesPayload && !isCompletionsPayload) return payload;
    if (typeof normalizedToolChoice === "string") {
      if (normalizedToolChoice === "none" || (Array.isArray(payload.tools) && payload.tools.length > 0)) {
        payload.tool_choice = normalizedToolChoice;
      }
      return payload;
    }
    if (
      typeof normalizedToolChoice === "object" &&
      normalizedToolChoice.type === "function" &&
      normalizedToolChoice.function?.name &&
      Array.isArray(payload.tools) &&
      payload.tools.length > 0
    ) {
      payload.tool_choice = isResponsesPayload
        ? { type: "function", name: normalizedToolChoice.function.name }
        : { type: "function", function: { name: normalizedToolChoice.function.name } };
    }
    return payload;
  };

  return {
    toolChoice: dualToolChoice,
    onPayload,
  };
}

function classifyProviderError(errMessage = "") {
  const msg = String(errMessage);
  if (/401|403|unauthorized|invalid[_\s-]?api[_\s-]?key|authentication|permission[_\s-]?denied/i.test(msg)) {
    return { status: 401, code: "provider_auth_error", type: "authentication_error" };
  }
  if (/429|rate[_\s-]?limit|quota|too many requests|resource[_\s-]?exhausted/i.test(msg)) {
    return { status: 429, code: "rate_limit_exceeded", type: "rate_limit_error" };
  }
  if (/timeout|timed out|aborted|deadline/i.test(msg)) {
    return { status: 504, code: "upstream_timeout", type: "timeout_error" };
  }
  if (/not[_\s-]?found|unknown model|unsupported model|model.*not.*found/i.test(msg)) {
    return { status: 404, code: "model_not_found", type: "invalid_request_error" };
  }
  return { status: 502, code: "upstream_provider_error", type: "api_error" };
}

function checkModelAllowlist(providerId, modelRef, pluginConfig, cfg) {
  const allowedProviders = Array.isArray(pluginConfig?.allowedProviders)
    ? pluginConfig.allowedProviders.map((s) => String(s).trim()).filter(Boolean)
    : [];
  if (allowedProviders.length > 0 && !allowedProviders.includes(providerId)) {
    return `Provider "${providerId}" is not in allowedProviders`;
  }
  const allowedModels = Array.isArray(pluginConfig?.allowedModels)
    ? pluginConfig.allowedModels.map((s) => String(s).trim()).filter(Boolean)
    : [];
  if (allowedModels.length > 0 && !allowedModels.includes(modelRef)) {
    return `Model "${modelRef}" is not in allowedModels`;
  }
  const providers = cfg?.models?.providers;
  if (providers && typeof providers === "object" && Object.keys(providers).length > 0) {
    const providerCfg = providers[providerId];
    const modelsList = Array.isArray(providerCfg?.models) ? providerCfg.models : [];
    const modelId = modelRef.slice(providerId.length + 1);
    if (!providerCfg || !modelsList.some((m) => m && String(m.id || "").trim() === modelId)) {
      return `Model "${modelRef}" is not in the configured OpenClaw catalog`;
    }
  }
  return null;
}

export async function handleChatCompletions(
  req,
  res,
  {
    cfg,
    pluginConfig = {},
    limiter,
    journal,
    replayStore,
    logger,
    deps = {},
  },
) {
  const prepareModelFn = deps.prepareSimpleCompletionModelForAgent || prepareSimpleCompletionModelForAgent;
  const streamSimpleFn = deps.streamSimple || streamSimple;
  const calculateCostFn = deps.calculateCost || calculateCost;

  const release = limiter.tryAcquire();
  if (!release) {
    return sendOpenAiError(res, 429, {
      code: "broker_overloaded",
      type: "rate_limit_error",
      message: `Dalton broker maxConcurrent (${limiter.limit}) reached`,
    });
  }

  const requestId = `chatcmpl-${crypto.randomUUID().replace(/-/g, "").slice(0, 24)}`;
  const created = Math.floor(Date.now() / 1000);
  const startMonotonic = performance.now();

  const abortController = new AbortController();
  const defaultTimeoutMs = Number(pluginConfig.defaultTimeoutMs) || 180_000;
  const maxTimeoutMs = Number(pluginConfig.maxTimeoutMs) || 600_000;
  let timeoutTimer = null;

  const onClientClose = () => {
    if (!res.writableEnded) {
      abortController.abort(new Error("client_disconnected"));
    }
  };
  req.on("aborted", onClientClose);
  res.on("close", onClientClose);

  try {
    const maxBytes = Number(pluginConfig.maxBodyBytes) || 4 * 1024 * 1024;
    const { body } = await readJsonBody(req, { maxBytes });

    const modelRef = String(body.model || "").trim();
    if (!modelRef || !modelRef.includes("/")) {
      return sendOpenAiError(res, 400, {
        code: "invalid_model",
        param: "model",
        message: 'Field "model" is required and must be in "<provider>/<model_id>" format (e.g. "xai/grok-4.7")',
      });
    }

    const providerFromRef = modelRef.split("/")[0];
    const allowErr = checkModelAllowlist(providerFromRef, modelRef, pluginConfig, cfg);
    if (allowErr) {
      return sendOpenAiError(res, 404, {
        code: "model_not_allowed",
        param: "model",
        message: allowErr,
      });
    }

    const textOnlyProviders = Array.isArray(pluginConfig?.textOnlyProviders)
      ? pluginConfig.textOnlyProviders
      : ["claude-cli-gateway", "antigravity-cli-gateway", "muse-cli-gateway"];
    if (
      textOnlyProviders.includes(providerFromRef) &&
      Array.isArray(body.tools) &&
      body.tools.length > 0
    ) {
      return sendOpenAiError(res, 400, {
        code: "tools_unsupported",
        param: "tools",
        message: `Provider "${providerFromRef}" is text-only and does not support tool calling.`,
      });
    }

    const invocationId = String(req.headers?.["x-dalton-invocation-id"] || "").trim();
    const replayOnly = String(req.headers?.["x-dalton-replay-only"] || "").trim() === "1";
    const reqHash = invocationId
      ? sha256Hex({
          model: modelRef,
          messages: body.messages,
          tools: body.tools || null,
          tool_choice: body.tool_choice || null,
        })
      : "";

    if (invocationId) {
      const existing = idempotencyCache.get(invocationId);
      if (replayOnly && !existing) {
        return sendOpenAiError(res, 404, {
          code: "invocation_not_found",
          message: `No recorded invocation found for X-Dalton-Invocation-Id=${invocationId}`,
        });
      }
      if (existing) {
        if (existing.reqHash !== reqHash) {
          return sendOpenAiError(res, 409, {
            code: "idempotency_conflict",
            type: "conflict_error",
            message: "X-Dalton-Invocation-Id reused with a different request payload.",
          });
        }
        if (existing.status === "completed" && existing.responsePayload) {
          const replayedPayload = {
            ...existing.responsePayload,
            dalton_replayed: true,
            openclaw: {
              ...(existing.responsePayload.openclaw || {}),
              replayed: true,
            },
          };
          if (!Boolean(body.stream)) {
            res.setHeader("X-Dalton-Replayed", "1");
            return sendJson(res, 200, replayedPayload);
          }
          beginSse(res, {
            "X-Dalton-Request-Id": existing.responsePayload.id || requestId,
            "X-Dalton-Replayed": "1",
          });
          writeSseData(res, {
            id: existing.responsePayload.id || requestId,
            object: "chat.completion.chunk",
            created: existing.responsePayload.created || created,
            model: modelRef,
            choices: [
              {
                index: 0,
                delta: existing.responsePayload.choices?.[0]?.message || { role: "assistant", content: "" },
                finish_reason: existing.responsePayload.choices?.[0]?.finish_reason || "stop",
              },
            ],
            usage: existing.responsePayload.usage,
            dalton_replayed: true,
          });
          endSse(res);
          return;
        }
      }
    }

    const daltonExt = body.dalton && typeof body.dalton === "object" ? body.dalton : {};
    const agentId = String(daltonExt.agent_id || pluginConfig.defaultAgentId || "main").trim();
    const wantStream = Boolean(body.stream);
    const includeUsage = body.stream_options?.include_usage !== false;

    const requestedTimeoutMs = Number(daltonExt.timeout_ms || body.timeout_ms || defaultTimeoutMs);
    const effectiveTimeoutMs = Math.max(1000, Math.min(maxTimeoutMs, requestedTimeoutMs));
    timeoutTimer = setTimeout(() => {
      abortController.abort(new Error(`broker_timeout_${effectiveTimeoutMs}ms`));
    }, effectiveTimeoutMs);

    const prepared = await prepareModelFn({
      cfg,
      agentId,
      modelRef,
    });

    if (!isPreparedResultOk(prepared)) {
      const errText = String(prepared?.error || `Failed to prepare model ${modelRef}`);
      const classified = classifyProviderError(errText);
      return sendOpenAiError(res, classified.status, {
        code: classified.code,
        type: classified.type,
        param: "model",
        message: errText,
      });
    }

    const { model, auth } = prepared;
    const sessionId = String(daltonExt.session_id || body.user || requestId);
    const context = normalizeOpenAiMessagesToContext(body.messages, {
      modelMeta: model,
      sessionId,
      replayStore,
    });
    const tools = normalizeOpenAiTools(body.tools);
    if (tools) {
      context.tools = tools;
    }

    const toolChoice = normalizeToolChoice(body.tool_choice);
    const responseFormat = normalizeResponseFormat(body.response_format);
    const reasoningEffortRaw =
      body.reasoning_effort ??
      daltonExt.reasoning_effort ??
      daltonExt.thinking_level ??
      undefined;
    const reasoning = resolveReasoningLevel(model, reasoningEffortRaw);

    const maxTokensRaw = body.max_completion_tokens ?? body.max_tokens;
    const maxTokens =
      typeof maxTokensRaw === "number" && Number.isFinite(maxTokensRaw) && maxTokensRaw > 0
        ? Math.floor(maxTokensRaw)
        : undefined;
    const temperature =
      typeof body.temperature === "number" && Number.isFinite(body.temperature)
        ? body.temperature
        : undefined;
    const stop = Array.isArray(body.stop)
      ? body.stop.map(String)
      : typeof body.stop === "string"
        ? [body.stop]
        : undefined;

    const toolChoiceOpts = buildToolChoiceStreamOptions(toolChoice);

    const streamOpts = {
      apiKey: auth?.apiKey,
      headers: auth?.headers,
      signal: abortController.signal,
      sessionId,
      ...(temperature !== undefined ? { temperature } : {}),
      ...(maxTokens !== undefined ? { maxTokens } : {}),
      ...(reasoning !== undefined ? { reasoning } : {}),
      ...(responseFormat !== undefined ? { responseFormat } : {}),
      ...(stop !== undefined ? { stop } : {}),
      ...toolChoiceOpts,
    };

    const executionModel = resolveStreamExecutionModel(model);
    const eventStream = streamSimpleFn(executionModel, context, streamOpts);

    if (!wantStream) {
      for await (const _ of eventStream) {
        // Drain stream events; final message comes from eventStream.result()
      }
      const finalMsg = await eventStream.result();
      const latencyMs = Math.round(performance.now() - startMonotonic);

      if (finalMsg?.stopReason === "error" || finalMsg?.stopReason === "aborted") {
        const errMsg = String(finalMsg.errorMessage || `Upstream generation ${finalMsg.stopReason}`);
        const classified = classifyProviderError(errMsg);
        await journal.append({
          request_id: requestId,
          endpoint: "chat/completions",
          stream: false,
          model_ref: modelRef,
          provider: model.provider,
          api: model.api,
          status: "error",
          error_code: classified.code,
          error_message: errMsg.slice(0, 500),
          latency_ms: latencyMs,
        });
        return sendOpenAiError(res, classified.status, {
          code: classified.code,
          type: classified.type,
          message: errMsg,
        });
      }

      try {
        if (finalMsg?.usage) {
          calculateCostFn(model, finalMsg.usage);
        }
      } catch {
        // Non-fatal if cost table missing
      }

      replayStore?.recordAssistantTurn?.(sessionId, finalMsg);

      const daltonMeta = {
        request_id: requestId,
        provider: model.provider,
        api: model.api,
        model_id: model.id,
        auth_mode: auth?.mode || "configured",
        profile_id: auth?.profileId || null,
        latency_ms: latencyMs,
        cost_usd: finalMsg?.usage?.cost || {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          total: 0,
        },
        stop_reason_raw: finalMsg?.stopReason || "stop",
      };

      const responsePayload = buildNonStreamingChatCompletionResponse({
        id: requestId,
        created,
        modelRef,
        assistantMsg: finalMsg,
        daltonMeta,
      });

      await journal.append({
        request_id: requestId,
        endpoint: "chat/completions",
        stream: false,
        model_ref: modelRef,
        provider: model.provider,
        api: model.api,
        auth_mode: daltonMeta.auth_mode,
        status: "ok",
        finish_reason: responsePayload.choices[0]?.finish_reason,
        usage: responsePayload.usage,
        cost_usd: daltonMeta.cost_usd,
        latency_ms: latencyMs,
        prompt_sha256: sha256Hex(body.messages),
        response_sha256: sha256Hex(responsePayload.choices[0]?.message),
        caller_meta: daltonExt,
      });

      if (invocationId) {
        idempotencyCache.set(invocationId, {
          reqHash,
          status: "completed",
          responsePayload,
          ts: Date.now(),
        });
      }

      return sendJson(res, 200, responsePayload);
    }

    // Streaming SSE branch (stream: true)
    beginSse(res, { "X-Dalton-Request-Id": requestId });
    const translator = createSseDeltaTranslator({
      id: requestId,
      created,
      modelRef,
      includeUsage,
    });

    for await (const ev of eventStream) {
      if (abortController.signal.aborted) break;
      const chunks = translator.onEvent(ev);
      for (const chunk of chunks) {
        writeSseData(res, chunk);
      }
    }

    const finalMsg = await eventStream.result();
    const latencyMs = Math.round(performance.now() - startMonotonic);

    if (finalMsg?.stopReason === "error" || finalMsg?.stopReason === "aborted") {
      const errMsg = String(finalMsg.errorMessage || `Stream ${finalMsg.stopReason}`);
      const classified = classifyProviderError(errMsg);
      writeSseData(res, {
        id: requestId,
        object: "chat.completion.chunk",
        created,
        model: modelRef,
        choices: [{ index: 0, delta: {}, finish_reason: "error" }],
        error: {
          code: classified.code,
          type: classified.type,
          message: errMsg,
        },
      });
      endSse(res);
      await journal.append({
        request_id: requestId,
        endpoint: "chat/completions",
        stream: true,
        model_ref: modelRef,
        provider: model.provider,
        api: model.api,
        status: "error",
        error_code: classified.code,
        error_message: errMsg.slice(0, 500),
        latency_ms: latencyMs,
      });
      return;
    }

    try {
      if (finalMsg?.usage) {
        calculateCostFn(model, finalMsg.usage);
      }
    } catch {
      // Non-fatal
    }

    replayStore?.recordAssistantTurn?.(sessionId, finalMsg);

    const daltonMeta = {
      request_id: requestId,
      provider: model.provider,
      api: model.api,
      model_id: model.id,
      auth_mode: auth?.mode || "configured",
      profile_id: auth?.profileId || null,
      latency_ms: latencyMs,
      cost_usd: finalMsg?.usage?.cost || {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        total: 0,
      },
      stop_reason_raw: finalMsg?.stopReason || "stop",
    };

    if (invocationId) {
      const cachedResponsePayload = buildNonStreamingChatCompletionResponse({
        id: requestId,
        created,
        modelRef,
        assistantMsg: finalMsg,
        daltonMeta,
      });
      idempotencyCache.set(invocationId, {
        reqHash,
        status: "completed",
        responsePayload: cachedResponsePayload,
        ts: Date.now(),
      });
    }

    const doneChunks = translator.onDone(finalMsg, daltonMeta);
    for (const chunk of doneChunks) {
      writeSseData(res, chunk);
    }
    endSse(res);

    await journal.append({
      request_id: requestId,
      endpoint: "chat/completions",
      stream: true,
      model_ref: modelRef,
      provider: model.provider,
      api: model.api,
      auth_mode: daltonMeta.auth_mode,
      status: "ok",
      usage: doneChunks[doneChunks.length - 1]?.usage,
      cost_usd: daltonMeta.cost_usd,
      latency_ms: latencyMs,
      prompt_sha256: sha256Hex(body.messages),
      caller_meta: daltonExt,
    });
  } catch (err) {
    const statusCode = Number(err?.statusCode) || 500;
    const code = String(err?.code || "internal_broker_error");
    const message = String(err?.message || "Unexpected broker error");
    logger?.warn?.(`[dalton-broker] chat/completions error (${code}): ${message}`);
    if (!res.headersSent) {
      sendOpenAiError(res, statusCode, { code, message });
    } else if (!res.writableEnded) {
      writeSseData(res, {
        error: { code, message, type: "api_error" },
      });
      endSse(res);
    }
  } finally {
    if (timeoutTimer) clearTimeout(timeoutTimer);
    req.off("aborted", onClientClose);
    res.off("close", onClientClose);
    release();
  }
}

/**
 * Backward-compatible `/dalton-broker/v1/invoke` adapter that translates v1 native
 * payloads (`{ model_ref, messages, system_prompt, tools, ... }`) into `handleChatCompletions`.
 */
export async function handleLegacyInvoke(req, res, ctx) {
  const maxBytes = Number(ctx.pluginConfig?.maxBodyBytes) || 4 * 1024 * 1024;
  let parsed;
  try {
    parsed = await readJsonBody(req, { maxBytes });
  } catch (err) {
    return sendOpenAiError(res, err.statusCode || 400, {
      code: err.code || "invalid_request",
      message: err.message,
    });
  }
  const b = parsed.body;
  const messages = [];
  if (typeof b.system_prompt === "string" && b.system_prompt.trim()) {
    messages.push({ role: "system", content: b.system_prompt });
  }
  if (Array.isArray(b.messages)) {
    messages.push(...b.messages);
  } else if (typeof b.prompt === "string") {
    messages.push({ role: "user", content: b.prompt });
  }

  const syntheticBody = JSON.stringify({
    model: b.model || b.model_ref,
    messages,
    stream: Boolean(b.stream),
    temperature: b.temperature,
    max_tokens: b.max_tokens,
    reasoning_effort: b.reasoning_effort || b.thinking_level,
    tools: b.tools,
    tool_choice: b.tool_choice,
    response_format: b.response_format,
    dalton: {
      agent_id: b.agent_id,
      timeout_ms: b.timeout_ms,
      ...(b.dalton || {}),
    },
  });

  const syntheticReq = Object.assign(
    (async function* () {
      yield Buffer.from(syntheticBody, "utf8");
    })(),
    {
      headers: req.headers,
      socket: req.socket,
      method: req.method,
      url: req.url,
      on: req.on.bind(req),
      off: req.off.bind(req),
    },
  );

  return handleChatCompletions(syntheticReq, res, ctx);
}
