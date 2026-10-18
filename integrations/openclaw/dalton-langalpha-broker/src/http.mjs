// integrations/openclaw/dalton-langalpha-broker/src/http.mjs

export const DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024; // 4 MiB

export function sendJson(res, statusCode, payload, extraHeaders = {}) {
  if (res.writableEnded) return;
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  res.end(body);
}

export function sendOpenAiError(res, statusCode, { code, message, type = "invalid_request_error", param = null, details = undefined }) {
  return sendJson(res, statusCode, {
    error: {
      message: String(message || "Broker error"),
      type,
      param,
      code: String(code || "broker_error"),
      ...(details !== undefined ? { details } : {}),
    },
  });
}

export function beginSse(res, extraHeaders = {}) {
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  if (typeof res.flushHeaders === "function") {
    res.flushHeaders();
  }
}

export function writeSseData(res, payload) {
  if (res.writableEnded || res.destroyed) return false;
  const serialized = typeof payload === "string" ? payload : JSON.stringify(payload);
  return res.write(`data: ${serialized}\n\n`);
}

export function endSse(res) {
  if (res.writableEnded || res.destroyed) return;
  res.write("data: [DONE]\n\n");
  res.end();
}

export async function readJsonBody(req, { maxBytes = DEFAULT_MAX_BODY_BYTES } = {}) {
  const chunks = [];
  let total = 0;

  for await (const chunk of req) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buf.length;
    if (total > maxBytes) {
      const err = new Error(`Request body exceeds maxBytes (${maxBytes})`);
      err.statusCode = 413;
      err.code = "payload_too_large";
      throw err;
    }
    chunks.push(buf);
  }

  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) {
    const err = new Error("Empty JSON request body");
    err.statusCode = 400;
    err.code = "invalid_request";
    throw err;
  }

  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      const err = new Error("Request body must be a JSON object");
      err.statusCode = 400;
      err.code = "invalid_request";
      throw err;
    }
    return { raw, body: parsed };
  } catch (parseErr) {
    if (parseErr.statusCode) throw parseErr;
    const err = new Error(`Malformed JSON body: ${parseErr.message}`);
    err.statusCode = 400;
    err.code = "invalid_request";
    throw err;
  }
}
