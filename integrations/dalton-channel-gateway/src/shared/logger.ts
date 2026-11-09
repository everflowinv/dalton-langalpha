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
  /^(?:text|content|body|raw_body|message_body|prompt|secret|app_secret|appsecret|token|service_token|access_token|refresh_token|bot_token|authorization|x-service-token|password|cookie|buffer|file_bytes|audio_bytes)$/i;

function sanitizeValue(value: unknown, depth = 0): unknown {
  if (value === null || value === undefined) {
    return value;
  }
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    return `[RedactedBuffer:${value.byteLength}B]`;
  }
  if (typeof value === "string") {
    if (value.length > 512) {
      return `${value.slice(0, 128)}...[truncated:${value.length}]`;
    }
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
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
  return String(value);
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
    const safeMeta = sanitizeLogMeta(meta);
    const metaSuffix =
      safeMeta && Object.keys(safeMeta).length > 0 ? ` ${JSON.stringify(safeMeta)}` : "";
    const line = `[${subsystem}] ${message}${metaSuffix}`;
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
