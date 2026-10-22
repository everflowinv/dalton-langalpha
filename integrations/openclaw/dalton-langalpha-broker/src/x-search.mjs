// integrations/openclaw/dalton-langalpha-broker/src/x-search.mjs
import { readJsonBody, sendJson } from "./http.mjs";
import {
  createXSearchTool as defaultCreateXSearchTool,
  prepareSimpleCompletionModelForAgent as defaultPrepareModel,
} from "./sdk-bridge.mjs";

const MAX_X_SEARCH_BODY_BYTES = 64 * 1024;
const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_TIMEOUT_MS = 120_000;
const MAX_HANDLES = 20;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

const PERMISSION_ERROR_CODES = new Set([
  "missing_xai_api_key",
  "PROVIDER_PERMISSION_DENIED",
  "AUTH_REQUIRED",
  "AUTH_INVALID",
  "AUTH_EXPIRED",
  "UNAUTHORIZED",
  "FORBIDDEN",
]);

const RATE_LIMIT_ERROR_CODES = new Set([
  "PROVIDER_RATE_LIMITED",
  "RATE_LIMITED",
  "rate_limited",
  "too_many_requests",
]);

const INVALID_REQUEST_ERROR_CODES = new Set([
  "INVALID_REQUEST",
  "invalid_request",
  "MISSING_FIELD",
  "UNKNOWN_FIELD",
]);

function isValidIsoDate(value) {
  if (typeof value !== "string" || !DATE_RE.test(value)) {
    return false;
  }
  const [y, m, d] = value.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return (
    dt.getUTCFullYear() === y &&
    dt.getUTCMonth() === m - 1 &&
    dt.getUTCDate() === d
  );
}

function normalizeHandleList(raw, fieldName) {
  if (raw === undefined || raw === null) {
    return undefined;
  }
  if (!Array.isArray(raw)) {
    throw Object.assign(new Error(`${fieldName} must be an array of strings`), {
      code: "INVALID_REQUEST",
      statusCode: 400,
    });
  }
  const cleaned = [];
  for (const item of raw) {
    if (typeof item !== "string") {
      throw Object.assign(new Error(`${fieldName} entries must be strings`), {
        code: "INVALID_REQUEST",
        statusCode: 400,
      });
    }
    const handle = item.trim().replace(/^@+/, "").trim();
    if (!handle) {
      throw Object.assign(new Error(`${fieldName} entries must not be empty`), {
        code: "INVALID_REQUEST",
        statusCode: 400,
      });
    }
    cleaned.push(handle);
  }
  if (cleaned.length > MAX_HANDLES) {
    throw Object.assign(
      new Error(`${fieldName} cannot exceed ${MAX_HANDLES} handles`),
      {
        code: "INVALID_REQUEST",
        statusCode: 400,
      },
    );
  }
  return cleaned.length > 0 ? cleaned : undefined;
}

function extractPayloadFromToolResult(result) {
  if (!result || typeof result !== "object") {
    return {};
  }
  if (result.details && typeof result.details === "object") {
    return result.details;
  }
  if (Array.isArray(result.content) && result.content.length > 0) {
    const first = result.content[0];
    if (first && typeof first.text === "string") {
      try {
        const parsed = JSON.parse(first.text);
        if (parsed && typeof parsed === "object") {
          return parsed;
        }
      } catch {
        // ignore parse failure
      }
    }
  }
  return result;
}

/**
 * Classify provider errors strictly by HTTP status code or stable error code.
 * Never scans free-form error messages for natural-language substrings.
 */
export function classifyProviderError(errCode, httpStatus) {
  const normalizedCode = typeof errCode === "string" ? errCode.trim() : "";
  const statusNum = Number.isInteger(httpStatus) ? httpStatus : null;

  if (
    PERMISSION_ERROR_CODES.has(normalizedCode) ||
    statusNum === 401 ||
    statusNum === 403
  ) {
    return {
      status: 403,
      code: "PROVIDER_PERMISSION_DENIED",
      message: "xAI authentication or permission rejected",
    };
  }
  if (RATE_LIMIT_ERROR_CODES.has(normalizedCode) || statusNum === 429) {
    return {
      status: 429,
      code: "PROVIDER_RATE_LIMITED",
      message: "xAI rate limit exceeded",
    };
  }
  if (INVALID_REQUEST_ERROR_CODES.has(normalizedCode) || statusNum === 400) {
    return {
      status: 400,
      code: "INVALID_REQUEST",
      message: "xAI rejected request parameters",
    };
  }
  return {
    status: 502,
    code: "PROVIDER_ERROR",
    message: "xAI x_search provider error",
  };
}

export async function handleXSearch(req, res, ctx) {
  const { cfg, pluginConfig, limiter, modelAuth, logger, deps = {} } = ctx;

  let body;
  try {
    const parsed = await readJsonBody(req, { maxBytes: MAX_X_SEARCH_BODY_BYTES });
    body = parsed.body;
  } catch (err) {
    const status = err.statusCode || 400;
    const code = err.code === "payload_too_large" ? "FRAME_TOO_LARGE" : "INVALID_REQUEST";
    sendJson(res, status, {
      ok: false,
      error: { code, message: err.message || "Invalid JSON request body" },
    });
    return;
  }

  const rawQuery = body.query;
  if (typeof rawQuery !== "string" || !rawQuery.trim()) {
    sendJson(res, 400, {
      ok: false,
      error: { code: "INVALID_REQUEST", message: "query is required" },
    });
    return;
  }
  const query = rawQuery.trim();

  let allowedHandles;
  let excludedHandles;
  try {
    const rawAllowed =
      body.allowed_x_handles !== undefined ? body.allowed_x_handles : body.handles;
    allowedHandles = normalizeHandleList(rawAllowed, "allowed_x_handles");
    excludedHandles = normalizeHandleList(
      body.excluded_x_handles,
      "excluded_x_handles",
    );
  } catch (err) {
    sendJson(res, err.statusCode || 400, {
      ok: false,
      error: { code: err.code || "INVALID_REQUEST", message: err.message },
    });
    return;
  }

  if (allowedHandles && excludedHandles) {
    sendJson(res, 400, {
      ok: false,
      error: {
        code: "INVALID_REQUEST",
        message: "allowed_x_handles and excluded_x_handles cannot be combined",
      },
    });
    return;
  }

  const fromDate =
    body.from_date !== undefined && body.from_date !== null
      ? body.from_date
      : body.fromDate;
  const toDate =
    body.to_date !== undefined && body.to_date !== null
      ? body.to_date
      : body.toDate;

  if (fromDate !== undefined && fromDate !== null && !isValidIsoDate(fromDate)) {
    sendJson(res, 400, {
      ok: false,
      error: {
        code: "INVALID_REQUEST",
        message: "from_date must be a valid YYYY-MM-DD date",
      },
    });
    return;
  }
  if (toDate !== undefined && toDate !== null && !isValidIsoDate(toDate)) {
    sendJson(res, 400, {
      ok: false,
      error: {
        code: "INVALID_REQUEST",
        message: "to_date must be a valid YYYY-MM-DD date",
      },
    });
    return;
  }
  if (fromDate && toDate && fromDate > toDate) {
    sendJson(res, 400, {
      ok: false,
      error: {
        code: "INVALID_REQUEST",
        message: "from_date must not be later than to_date",
      },
    });
    return;
  }

  const rawTimeout =
    body.timeoutMs !== undefined ? body.timeoutMs : DEFAULT_TIMEOUT_MS;
  if (
    typeof rawTimeout !== "number" ||
    !Number.isInteger(rawTimeout) ||
    rawTimeout < 1
  ) {
    sendJson(res, 400, {
      ok: false,
      error: {
        code: "INVALID_REQUEST",
        message: "timeoutMs must be a positive integer",
      },
    });
    return;
  }
  const timeoutMs = Math.min(rawTimeout, MAX_TIMEOUT_MS);

  const release = limiter.tryAcquire();
  if (!release) {
    sendJson(res, 503, {
      ok: false,
      error: {
        code: "bridge_busy",
        message: "OpenClaw broker concurrency limit reached",
      },
    });
    return;
  }

  try {
    const prepareModelFn =
      deps.prepareSimpleCompletionModelForAgent || defaultPrepareModel;
    const createToolFn = deps.createXSearchTool || defaultCreateXSearchTool;

    const resolveXaiKey = async () => {
      if (typeof modelAuth?.resolveApiKeyForProvider === "function") {
        try {
          const resolved = await modelAuth.resolveApiKeyForProvider({
            provider: "xai",
            cfg,
          });
          const key =
            typeof resolved === "string"
              ? resolved.trim()
              : String(resolved?.apiKey ?? "").trim();
          if (key) return key;
        } catch {
          // fallback to prepareSimpleCompletionModelForAgent
        }
      }
      if (typeof prepareModelFn === "function") {
        try {
          const xSearchCfg = cfg?.plugins?.entries?.xai?.config?.xSearch;
          const rawModel =
            typeof xSearchCfg?.model === "string" && xSearchCfg.model.trim()
              ? xSearchCfg.model.trim()
              : "grok-4-1-fast-non-reasoning";
          const modelRef = rawModel.startsWith("xai/")
            ? rawModel
            : `xai/${rawModel}`;
          const prepared = await prepareModelFn({
            cfg,
            agentId: pluginConfig?.defaultAgentId || "main",
            modelRef,
          });
          const key = String(prepared?.apiKey ?? "").trim();
          if (key) return key;
        } catch {
          // ignore
        }
      }
      return undefined;
    };

    const auth = {
      hasAuthForProvider(providerId) {
        return String(providerId || "").trim().toLowerCase() === "xai";
      },
      async resolveApiKeyForProvider(providerId) {
        if (String(providerId || "").trim().toLowerCase() !== "xai") {
          return undefined;
        }
        return await resolveXaiKey();
      },
    };

    const xSearchTool = createToolFn({
      config: cfg,
      runtimeConfig: cfg,
      auth,
    });

    if (!xSearchTool || typeof xSearchTool.execute !== "function") {
      sendJson(res, 503, {
        ok: false,
        error: {
          code: "TOOL_UNAVAILABLE",
          message: "OpenClaw x_search tool is disabled or unavailable",
        },
      });
      return;
    }

    const toolArgs = { query };
    if (allowedHandles) toolArgs.allowed_x_handles = allowedHandles;
    if (excludedHandles) toolArgs.excluded_x_handles = excludedHandles;
    if (fromDate) toolArgs.from_date = fromDate;
    if (toDate) toolArgs.to_date = toDate;

    const controller = new AbortController();
    const timer = setTimeout(
      () => controller.abort(new Error("x_search timed out")),
      timeoutMs,
    );
    let result;
    try {
      result = await xSearchTool.execute(
        `dalton-x-search-${Date.now()}`,
        toolArgs,
        controller.signal,
      );
    } finally {
      clearTimeout(timer);
    }

    const payload = extractPayloadFromToolResult(result);
    if (result?.isError || payload?.error) {
      const errCode = String(payload?.error || "x_search_failed");
      const rawStatus =
        Number.isInteger(payload?.status)
          ? payload.status
          : Number.isInteger(payload?.statusCode)
            ? payload.statusCode
            : null;
      const { status, code, message } = classifyProviderError(errCode, rawStatus);
      sendJson(res, status, {
        ok: false,
        error: { code, message },
      });
      return;
    }

    sendJson(res, 200, {
      ok: true,
      provider: String(payload?.provider || "xai"),
      result,
    });
  } catch (err) {
    const errType = String(err?.name || "Error");
    const errCode = typeof err?.code === "string" ? err.code : "";
    const rawStatus =
      Number.isInteger(err?.status)
        ? err.status
        : Number.isInteger(err?.statusCode)
          ? err.statusCode
          : null;
    logger?.warn?.(
      `[dalton-broker] x-search failed: type=${errType} code=${errCode || "-"} status=${rawStatus ?? "-"}`,
    );
    const { status, code, message } = classifyProviderError(errCode, rawStatus);
    sendJson(res, status, {
      ok: false,
      error: { code, message: `${message} (${errType})` },
    });
  } finally {
    release();
  }
}
