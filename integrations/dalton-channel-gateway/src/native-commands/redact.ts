/**
 * Credential scrubbing and logger wrapper for native command registration.
 *
 * Extends `redactCredentialString` with Discord `Bot <token>` headers, Discord
 * bot token patterns, and exact literal scrubbing for active credentials
 * (`appSecret`, `botToken`, and dynamically acquired `tenant_access_token`) so
 * secrets never surface in thrown errors, HTTP snippets, or log sinks.
 */

import {
  createSubsystemLogger,
  type LoggerSink,
  redactCredentialString,
  type SubsystemLogger,
} from "../shared/logger.js";
import {
  type NativeCommandPlatform,
  NativeCommandRegistrationError,
} from "./types.js";

const DISCORD_BOT_AUTH_PATTERN = /\b(Bot\s+)[A-Za-z0-9._~+/=-]+/gi;
const DISCORD_TOKEN_SHAPE_PATTERN =
  /\b[MN][A-Za-z\d]{20,}\.[\w-]{6}\.[\w-]{25,}\b/g;

function collectSecretVariants(secrets: Iterable<string>): string[] {
  const variants = new Set<string>();
  for (const raw of secrets) {
    if (typeof raw !== "string") {
      continue;
    }
    const candidates = [raw, raw.trim()];
    for (const candidate of candidates) {
      if (candidate.length === 0) {
        continue;
      }
      variants.add(candidate);
      try {
        const encoded = encodeURIComponent(candidate);
        if (encoded.length > 0) {
          variants.add(encoded);
        }
      } catch {
        // Ignore URI encoding failures on malformed surrogates
      }
      const jsonEscaped = JSON.stringify(candidate).slice(1, -1);
      if (jsonEscaped.length > 0) {
        variants.add(jsonEscaped);
      }
    }
  }
  // Replace longer secrets first so partial overlaps cannot leave fragments.
  return [...variants].sort((a, b) => b.length - a.length);
}

export function scrubNativeCommandSecrets(
  value: string,
  secrets: Iterable<string> = [],
  maxLength = 512,
): string {
  if (!value) {
    return value;
  }

  let out = value;
  for (const secret of collectSecretVariants(secrets)) {
    if (out.includes(secret)) {
      out = out.split(secret).join("[REDACTED]");
    }
  }

  out = out
    .replace(DISCORD_BOT_AUTH_PATTERN, "$1[REDACTED]")
    .replace(DISCORD_TOKEN_SHAPE_PATTERN, "[REDACTED]");

  return redactCredentialString(out, maxLength);
}

function scrubMetaValue(
  value: unknown,
  secrets: readonly string[],
  depth = 0,
): unknown {
  if (value === null || value === undefined) {
    return value;
  }
  if (typeof value === "string") {
    return scrubNativeCommandSecrets(value, secrets);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (value instanceof Error) {
    return {
      name: value.name,
      message: scrubNativeCommandSecrets(value.message, secrets),
      ...("code" in value && value.code !== undefined
        ? { code: value.code }
        : {}),
    };
  }
  if (depth >= 4) {
    return "[MaxDepth]";
  }
  if (Array.isArray(value)) {
    return value
      .slice(0, 20)
      .map((item) => scrubMetaValue(item, secrets, depth + 1));
  }
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (
        /(?:secret|token|password|authorization|cookie|api[_-]?key|bearer|credential)/i.test(
          k,
        )
      ) {
        out[k] = "[REDACTED]";
      } else {
        out[k] = scrubMetaValue(v, secrets, depth + 1);
      }
    }
    return out;
  }
  return scrubNativeCommandSecrets(String(value), secrets);
}

export function createStderrLoggerSink(
  stderr: { write(chunk: string): unknown } = process.stderr,
): LoggerSink {
  const writeLine = (line: string): void => {
    stderr.write(`${line}\n`);
  };
  return {
    debug: writeLine,
    info: writeLine,
    warn: writeLine,
    error: writeLine,
  };
}

export function sanitizeRegistrationError(
  error: unknown,
  secrets: Iterable<string>,
  platform: NativeCommandPlatform,
): NativeCommandRegistrationError {
  const secretList = [...secrets];
  if (error instanceof NativeCommandRegistrationError) {
    const safeMsg = scrubNativeCommandSecrets(error.message, secretList, 2048);
    const sanitized = new NativeCommandRegistrationError(error.code, safeMsg, {
      httpStatus: error.httpStatus,
      platform: error.platform ?? platform,
    });
    if (sanitized.stack) {
      sanitized.stack = scrubNativeCommandSecrets(
        sanitized.stack,
        secretList,
        4096,
      );
    }
    return sanitized;
  }

  const rawMsg = error instanceof Error ? error.message : String(error);
  const safeMsg = scrubNativeCommandSecrets(rawMsg, secretList, 2048);
  const wrapped = new NativeCommandRegistrationError("network_error", safeMsg, {
    platform,
  });
  if (wrapped.stack) {
    wrapped.stack = scrubNativeCommandSecrets(wrapped.stack, secretList, 4096);
  }
  return wrapped;
}

export function createSecretAwareLogger(options: {
  subsystem: string;
  baseLogger?: SubsystemLogger;
  getSecrets: () => Iterable<string>;
}): SubsystemLogger {
  const target =
    options.baseLogger ??
    createSubsystemLogger(options.subsystem, {
      sink: createStderrLoggerSink(process.stderr),
    });

  const sanitizeCall = (
    message: string,
    meta?: Record<string, unknown>,
  ): { safeMessage: string; safeMeta?: Record<string, unknown> } => {
    const secrets = [...options.getSecrets()];
    const safeMessage = scrubNativeCommandSecrets(message, secrets);
    if (!meta) {
      return { safeMessage };
    }
    const scrubbed = scrubMetaValue(meta, secrets, 0);
    return {
      safeMessage,
      safeMeta:
        typeof scrubbed === "object" &&
        scrubbed !== null &&
        !Array.isArray(scrubbed)
          ? (scrubbed as Record<string, unknown>)
          : undefined,
    };
  };

  return {
    subsystem: target.subsystem,
    debug(message, meta) {
      const { safeMessage, safeMeta } = sanitizeCall(message, meta);
      target.debug(safeMessage, safeMeta);
    },
    info(message, meta) {
      const { safeMessage, safeMeta } = sanitizeCall(message, meta);
      target.info(safeMessage, safeMeta);
    },
    warn(message, meta) {
      const { safeMessage, safeMeta } = sanitizeCall(message, meta);
      target.warn(safeMessage, safeMeta);
    },
    error(message, meta) {
      const { safeMessage, safeMeta } = sanitizeCall(message, meta);
      target.error(safeMessage, safeMeta);
    },
    child(subName) {
      return createSecretAwareLogger({
        subsystem: `${options.subsystem}/${subName}`,
        baseLogger: target.child(subName),
        getSecrets: options.getSecrets,
      });
    },
  };
}
