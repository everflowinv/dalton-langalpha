/**
 * Structured gateway logger with sensitive payload redaction.
 *
 * Design §4.1 (`src/shared/logger.ts`) & §5.3:
 * Logs must never emit user message bodies, attachment contents, or secrets/tokens.
 */

export type LogLevel = "debug" | "info" | "warn" | "error";

export interface SubsystemLogger {
  readonly subsystem: string;
  debug(message: string, meta?: Record<string, unknown>): void;
  info(message: string, meta?: Record<string, unknown>): void;
  warn(message: string, meta?: Record<string, unknown>): void;
  error(message: string, meta?: Record<string, unknown>): void;
  child(subName: string): SubsystemLogger;
}

export interface LoggerSink {
  debug?(line: string): void;
  info(line: string): void;
  warn(line: string): void;
  error(line: string): void;
}

const SENSITIVE_KEY_PATTERN =
  /(?:^(?:text|content|body|raw_body|message_body|prompt|buffer|file_bytes|audio_bytes|errmsg)$|secret|token|password|authorization|cookie|api[_-]?key|app[_-]?secret|app[_-]?id|tenant[_-]?access[_-]?token|bearer|credential|qrcode|qr_code|verify[_-]?code|aes[_-]?key|aeskey|encrypt[_-]?query[_-]?param|encryptqueryparam|cdn[_-]?key|typing[_-]?ticket|upload[_-]?param)/i;

const MAX_LOG_STRING_LENGTH = 512;

/**
 * Returns only an 8-character prefix of a login session key for safe structured logs (§4.3).
 */
export function formatSafeSessionKeyPrefix(sessionKey: string | undefined | null): string {
  const trimmed = (sessionKey ?? "").trim();
  if (!trimmed) {
    return "";
  }
  return trimmed.slice(0, 8);
}

const HTTP_STATUS_ERROR_CODE_RE = /^http_[1-5]\d{2}$/;

/**
 * Fixed whitelist of known safe error codes / class names that may appear raw in structured logs.
 * Any unknown or synthetic code/name is normalized to `"unknown_error"` (§4.3 / R5 #7).
 */
export const KNOWN_LOG_ERROR_CODES: ReadonlySet<string> = new Set([
  // Backend / contract deny & operational codes
  "roster_lookup_error",
  "preflight_deadline_exceeded",
  "stale_gateway_epoch",
  "gateway_lease_safety_margin",
  "gateway_lease_paused",
  "gateway_starting",
  "gateway_stopping",
  "weixin_snapshot_not_ready",
  "weixin_bot_generation_mismatch",
  "weixin_bot_generation_superseded",
  "weixin_account_rebound",
  "weixin_session_expired",
  "weixin_context_token_stale",
  "missing_weixin_context_token",
  "weixin_context_token_missing",
  "invalid_reconcile_snapshot",
  "session_generation_superseded",
  "source_generation_unknown",
  "member_not_in_roster",
  "weixin_channel_disabled",
  "external_access_disabled",
  "not_linked",
  "not_allowed",
  "unavailable",
  "rate_limited",
  "target_not_found",
  "invalid_request",
  "forbidden",
  "internal_error",
  "delivery_uncertain",
  "in_flight",
  "artifact_invalid",
  "file_not_sent",
  // QR login codes
  "login_session_revoked",
  "ilink_user_already_bound",
  "weixin_capacity_full",
  "weixin_capacity_reached",
  "qr_login_error",
  "qr_start_failed",
  "qr_poll_failed",
  "qr_expired",
  "verify_code_failed",
  // Weixin API / safe-fetch kinds & codes
  "session_expired",
  "api_error",
  "http_error",
  "network_error",
  "timeout",
  "preflight_timeout",
  "size_exceeded",
  "ssrf_blocked",
  "redirect_blocked",
  // Node.js / Undici system error codes
  "ECONNREFUSED",
  "ECONNRESET",
  "ETIMEDOUT",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EPIPE",
  "ECONNABORTED",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_BODY_TIMEOUT",
  "UND_ERR_SOCKET",
  "UND_ERR_ABORTED",
  // Standard / gateway Error class names
  "AbortError",
  "TimeoutError",
  "TypeError",
  "Error",
  "BackendHttpError",
  "WeixinApiError",
  "SafeFetchError",
  "ChannelDeliveryError",
  "StaleGatewayEpochError",
  "LeaseSafetyMarginPauseError",
  "WeixinQrLoginError",
]);

/**
 * Normalizes an error / deny code against `KNOWN_LOG_ERROR_CODES`.
 * Unknown or arbitrary strings always collapse to `"unknown_error"`.
 */
export function sanitizeLogErrorCode(rawCode: unknown): string {
  if (typeof rawCode === "string") {
    const trimmed = rawCode.trim();
    if (
      KNOWN_LOG_ERROR_CODES.has(trimmed) ||
      HTTP_STATUS_ERROR_CODE_RE.test(trimmed)
    ) {
      return trimmed;
    }
  }
  return "unknown_error";
}

/**
 * Extracts strictly whitelisted error code / HTTP status metadata from an arbitrary error,
 * never emitting free-text `err.message`, `String(err)`, or unknown/synthetic error names (§4.3 / R5 #7).
 */
export function extractSafeLogErrorFields(
  err: unknown,
): { error_code: string; http_status?: number } {
  const out: { error_code: string; http_status?: number } = {
    error_code: "unknown_error",
  };
  if (typeof err === "object" && err !== null) {
    const rec = err as {
      code?: unknown;
      denyCode?: unknown;
      kind?: unknown;
      status?: unknown;
      statusCode?: unknown;
      name?: unknown;
      body?: unknown;
      cause?: unknown;
    };
    const rawStatus =
      typeof rec.status === "number"
        ? rec.status
        : typeof rec.statusCode === "number"
          ? rec.statusCode
          : undefined;
    if (
      rawStatus !== undefined &&
      Number.isInteger(rawStatus) &&
      rawStatus >= 100 &&
      rawStatus <= 599
    ) {
      out.http_status = rawStatus;
    }
    const bodyCode =
      typeof rec.body === "object" && rec.body !== null && "code" in rec.body
        ? (rec.body as { code?: unknown }).code
        : undefined;
    const causeCode =
      typeof rec.cause === "object" && rec.cause !== null && "code" in rec.cause
        ? (rec.cause as { code?: unknown }).code
        : undefined;
    for (const candidate of [
      rec.denyCode,
      bodyCode,
      rec.code,
      causeCode,
      rec.kind,
      rec.name,
    ]) {
      const sanitized = sanitizeLogErrorCode(candidate);
      if (sanitized !== "unknown_error") {
        out.error_code = sanitized;
        return out;
      }
    }
    if (out.http_status !== undefined) {
      out.error_code = `http_${out.http_status}`;
    }
  }
  return out;
}

/**
 * Scrubs credential-like substrings (Bearer tokens, JSON/query secret fields,
 * URL userinfo, Feishu tenant/app tokens) and truncates long strings so raw
 * secrets or oversized payloads never reach stdout/stderr.
 */
export function redactCredentialString(
  value: string,
  maxLength: number = MAX_LOG_STRING_LENGTH,
): string {
  if (!value) {
    return value;
  }
  const scrubbed = value
    // URL userinfo: https://user:secret@host -> https://[REDACTED]@host
    .replace(
      /\b([a-z][a-z0-9+.-]*:\/\/)([^/\s:@]+:[^/\s@]+)@/gi,
      "$1[REDACTED]@",
    )
    // Authorization: Bearer / Bot <token>
    .replace(/\b((?:Bearer|Bot)\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]")
    // Discord interaction/webhook URL tokens: /interactions/<id>/<token> or /webhooks/<id>/<token>
    .replace(
      /(\/(?:interactions|webhooks)\/[^/\s?#]+\/)(?!\[REDACTED\]|\[redacted\])[^/\s?#"']+/gi,
      "$1[REDACTED]",
    )
    // JSON key-value pairs for sensitive keys
    .replace(
      /("(?:app_secret|appSecret|app_id|appId|tenant_access_token|user_access_token|access_token|refresh_token|client_secret|api_key|apiKey|password|secret|token|bot_token|botToken|context_token|contextToken|weixin_context_token|typing_ticket|qrcode|qrcode_img_content|qrcode_url|verify_code|aes_key|aeskey|aesKeyHex|aesKeyBase64|encrypt_query_param|encryptQueryParam|upload_param|authorization)"\s*:\s*)"[^"]*"/gi,
      '$1"[REDACTED]"',
    )
    // Query/header/key=value or key: value pairs for sensitive keys
    .replace(
      /\b((?:app_secret|appSecret|app_id|appId|tenant_access_token|user_access_token|access_token|refresh_token|client_secret|api_key|apiKey|password|secret|token|bot_token|botToken|context_token|contextToken|weixin_context_token|typing_ticket|qrcode|qrcode_img_content|qrcode_url|verify_code|aes_key|aeskey|aesKeyHex|aesKeyBase64|encrypt_query_param|encryptQueryParam|encrypted_query_param|upload_param)\s*[=:]\s*)(?:[^\s,&"'}]+)/gi,
      "$1[REDACTED]",
    )
    // Feishu tenant/user access tokens (e.g. t-g104...)
    .replace(/\b(t-[a-zA-Z0-9_-]{8,})\b/g, "[REDACTED]");

  if (scrubbed.length > maxLength) {
    return `${scrubbed.slice(0, 128)}...[truncated:${scrubbed.length}]`;
  }
  return scrubbed;
}

function sanitizeValue(value: unknown, depth = 0): unknown {
  if (value === null || value === undefined) {
    return value;
  }
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    return `[RedactedBuffer:${value.byteLength}B]`;
  }
  if (typeof value === "string") {
    return redactCredentialString(value);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (value instanceof Error) {
    return {
      name: value.name,
      message: redactCredentialString(value.message),
      ...("code" in value && value.code !== undefined ? { code: value.code } : {}),
    };
  }
  if (depth >= 4) {
    return "[MaxDepth]";
  }
  if (Array.isArray(value)) {
    return value.slice(0, 20).map((entry) => sanitizeValue(entry, depth + 1));
  }
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (SENSITIVE_KEY_PATTERN.test(k)) {
        out[k] = "[REDACTED]";
      } else {
        out[k] = sanitizeValue(v, depth + 1);
      }
    }
    return out;
  }
  return redactCredentialString(String(value));
}

export function sanitizeLogMeta(
  meta?: Record<string, unknown>,
): Record<string, unknown> | undefined {
  if (!meta) {
    return undefined;
  }
  const sanitized = sanitizeValue(meta, 0);
  return typeof sanitized === "object" && sanitized !== null && !Array.isArray(sanitized)
    ? (sanitized as Record<string, unknown>)
    : undefined;
}

const defaultSink: LoggerSink = {
  debug: (line) => console.debug(line),
  info: (line) => console.info(line),
  warn: (line) => console.warn(line),
  error: (line) => console.error(line),
};

export function createSubsystemLogger(
  subsystem: string,
  options?: { sink?: LoggerSink; enableDebug?: boolean },
): SubsystemLogger {
  const sink = options?.sink ?? defaultSink;
  const enableDebug = options?.enableDebug ?? false;

  const emit = (level: LogLevel, message: string, meta?: Record<string, unknown>): void => {
    if (level === "debug" && !enableDebug) {
      return;
    }
    const safeMessage = redactCredentialString(message);
    const safeMeta = sanitizeLogMeta(meta);
    const metaSuffix =
      safeMeta && Object.keys(safeMeta).length > 0 ? ` ${JSON.stringify(safeMeta)}` : "";
    const line = `[${subsystem}] ${safeMessage}${metaSuffix}`;
    if (level === "debug") {
      (sink.debug ?? sink.info)(line);
    } else if (level === "info") {
      sink.info(line);
    } else if (level === "warn") {
      sink.warn(line);
    } else {
      sink.error(line);
    }
  };

  return {
    subsystem,
    debug: (message, meta) => emit("debug", message, meta),
    info: (message, meta) => emit("info", message, meta),
    warn: (message, meta) => emit("warn", message, meta),
    error: (message, meta) => emit("error", message, meta),
    child: (subName: string) =>
      createSubsystemLogger(`${subsystem}/${subName}`, options),
  };
}
