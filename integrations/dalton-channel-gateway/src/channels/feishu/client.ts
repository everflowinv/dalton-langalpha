/**
 * Feishu SDK client, WebSocket client, event dispatcher, and multipart upload fix.
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/client-wwr8PGPH.mjs`
 * (lines 103–153, 161–170, 222–331).
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 1 (`src/channels/feishu/client.ts`):
 * - Retains `normalizeMultipartUploadData`, `createFeishuClient`,
 *   `createFeishuWSClient`, and `createEventDispatcher`.
 * - Excludes `createFeishuHttpInstance`'s `captureChannelReadAuthority`,
 *   `captureEffectAuthority`, and `getFeishuProxyAgent`.
 */

import * as Lark from "@larksuiteoapi/node-sdk";
import {
  createSubsystemLogger,
  redactCredentialString,
  type SubsystemLogger,
} from "../../shared/logger.js";

export type FeishuDomain = "feishu" | "lark" | (string & {});

export interface FeishuCredentials {
  accountId?: string;
  appId: string;
  appSecret: string;
  encryptKey?: string;
  verificationToken?: string;
  domain?: FeishuDomain;
  httpTimeoutMs?: number;
  logger?: SubsystemLogger;
}

export interface FeishuWSClientOptions extends FeishuCredentials {
  onReady?: () => void;
  onError?: (error: Error) => void;
  onReconnecting?: () => void;
  onReconnected?: () => void;
}

export const DEFAULT_FEISHU_HTTP_TIMEOUT_MS = 30_000;
export const FEISHU_USER_AGENT = "dalton-channel-gateway/feishu";

const defaultFeishuLogger = createSubsystemLogger("feishu/sdk");

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function resolveFeishuDomain(
  domain: FeishuDomain | undefined,
): Lark.Domain | string {
  if (!domain || domain === "feishu") {
    return Lark.Domain.Feishu;
  }
  if (domain === "lark") {
    return Lark.Domain.Lark;
  }
  return domain.replace(/\/+$/, "");
}

function bufferToBlobPart(value: Buffer): Uint8Array<ArrayBuffer> {
  const copy = new Uint8Array(value.byteLength);
  copy.set(new Uint8Array(value.buffer, value.byteOffset, value.byteLength));
  return copy;
}

function coerceBinaryFieldsToBuffer(
  record: Record<string, unknown>,
): Record<string, unknown> {
  let normalized: Record<string, unknown> | undefined;
  for (const [key, value] of Object.entries(record)) {
    let nextValue = value;
    if (Buffer.isBuffer(value)) {
      nextValue = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    } else if (ArrayBuffer.isView(value)) {
      nextValue = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    } else if (value instanceof ArrayBuffer) {
      nextValue = Buffer.from(value);
    }
    if (nextValue !== value || normalized) {
      normalized ??= { ...record };
      normalized[key] = nextValue;
    }
  }
  return normalized ?? record;
}

/**
 * Converts multipart upload request payloads (`POST /open-apis/im/v1/images` and
 * `POST /open-apis/im/v1/files`) containing `Buffer` / binary fields into native
 * `FormData` + `Blob` instances and strips pre-set `Content-Type` headers,
 * matching `.agy/ref_openclaw_channels/feishu/dist/.setup/client-wwr8PGPH.mjs:103-153`.
 * Also normalizes `Uint8Array` / `ArrayBuffer` fields into `Buffer` when called
 * on a plain data record.
 */
export function normalizeMultipartUploadData(opts: unknown): unknown {
  if (!isRecord(opts)) {
    return opts;
  }

  // Request-options shape: `{ method, url, data, headers, ... }`
  if (typeof opts.url === "string" && isRecord(opts.data)) {
    const coercedData = coerceBinaryFieldsToBuffer(opts.data);
    const pathname = (() => {
      try {
        return new URL(opts.url, "https://open.feishu.cn").pathname;
      } catch {
        return opts.url;
      }
    })();
    const methodUpper =
      typeof opts.method === "string" ? opts.method.toUpperCase() : "";
    const isMultipartUpload =
      methodUpper === "POST" &&
      (pathname === "/open-apis/im/v1/images" ||
        pathname === "/open-apis/im/v1/files");
    if (
      !isMultipartUpload ||
      typeof FormData === "undefined" ||
      typeof Blob === "undefined"
    ) {
      return coercedData === opts.data ? opts : { ...opts, data: coercedData };
    }
    if ((opts.data as unknown) instanceof FormData) {
      return opts;
    }
    const entries = Object.entries(coercedData).filter(
      ([, value]) => value !== undefined && value !== null,
    );
    if (!entries.some(([, value]) => Buffer.isBuffer(value))) {
      return coercedData === opts.data ? opts : { ...opts, data: coercedData };
    }
    const formData = new FormData();
    const fileName =
      typeof coercedData.file_name === "string" && coercedData.file_name.trim()
        ? coercedData.file_name.trim()
        : "upload.bin";
    for (const [key, value] of entries) {
      if (Buffer.isBuffer(value)) {
        formData.append(key, new Blob([bufferToBlobPart(value)]), fileName);
        continue;
      }
      if (
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean"
      ) {
        formData.append(key, String(value));
        continue;
      }
      return coercedData === opts.data ? opts : { ...opts, data: coercedData };
    }
    const headers = isRecord(opts.headers) ? { ...opts.headers } : undefined;
    if (headers) {
      delete headers["Content-Type"];
      delete headers["content-type"];
    }
    return {
      ...opts,
      ...(headers ? { headers } : {}),
      data: formData,
    };
  }

  return coerceBinaryFieldsToBuffer(opts);
}

function scrubLiteralSecrets(
  text: string,
  appId: string,
  appSecret: string,
): string {
  let out = redactCredentialString(text);
  if (appSecret && out.includes(appSecret)) {
    out = out.split(appSecret).join("[REDACTED]");
  }
  if (appId && out.includes(appId)) {
    out = out.split(appId).join("[REDACTED_APP_ID]");
  }
  return out;
}

function sanitizeSdkLogEntry(
  entry: unknown,
  appId: string,
  appSecret: string,
  depth = 0,
): unknown {
  if (entry === null || entry === undefined) {
    return entry;
  }
  if (typeof entry === "string") {
    return scrubLiteralSecrets(entry, appId, appSecret);
  }
  if (typeof entry === "number" || typeof entry === "boolean") {
    return entry;
  }
  if (entry instanceof Error) {
    return {
      name: entry.name,
      message: scrubLiteralSecrets(entry.message, appId, appSecret),
    };
  }
  if (depth >= 3) {
    return "[Truncated]";
  }
  if (Array.isArray(entry)) {
    return entry
      .slice(0, 10)
      .map((item) => sanitizeSdkLogEntry(item, appId, appSecret, depth + 1));
  }
  if (isRecord(entry)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(entry)) {
      const lower = k.toLowerCase();
      if (
        lower === "config" ||
        lower === "request" ||
        lower === "headers" ||
        lower.includes("secret") ||
        lower.includes("token") ||
        lower === "app_id" ||
        lower === "appid" ||
        lower === "authorization"
      ) {
        out[k] = "[REDACTED]";
      } else if (lower === "response" && isRecord(v)) {
        out.response = {
          status: v.status,
          statusText: v.statusText,
          data: isRecord(v.data)
            ? {
                code: v.data.code,
                msg:
                  typeof v.data.msg === "string"
                    ? scrubLiteralSecrets(v.data.msg, appId, appSecret)
                    : undefined,
              }
            : undefined,
        };
      } else if (lower === "data" && typeof v === "string") {
        out[k] = scrubLiteralSecrets(v, appId, appSecret);
      } else {
        out[k] = sanitizeSdkLogEntry(v, appId, appSecret, depth + 1);
      }
    }
    return out;
  }
  return scrubLiteralSecrets(String(entry), appId, appSecret);
}

/**
 * Creates a `@larksuiteoapi/node-sdk` compatible `Lark.Logger` that routes SDK
 * logs through `src/shared/logger.ts` while stripping `appId`, `appSecret`,
 * `tenant_access_token`, and Axios request `config`/`data` objects (`codex-1`, `grok-9`).
 */
export function createFeishuSdkLogger(options: {
  appId: string;
  appSecret: string;
  logger?: SubsystemLogger;
}): Lark.Logger {
  const targetLogger = options.logger ?? defaultFeishuLogger;
  const { appId, appSecret } = options;

  const formatArgs = (args: unknown[]): { message: string; meta?: Record<string, unknown> } => {
    const sanitized = args.map((item) =>
      sanitizeSdkLogEntry(item, appId, appSecret),
    );
    const stringParts: string[] = [];
    const details: unknown[] = [];
    for (const part of sanitized) {
      if (typeof part === "string") {
        stringParts.push(part);
      } else {
        details.push(part);
      }
    }
    const message =
      stringParts.join(" ").trim() || "Feishu SDK log event";
    return details.length > 0 ? { message, meta: { details } } : { message };
  };

  return {
    error: (...msg: unknown[]) => {
      const { message, meta } = formatArgs(msg);
      targetLogger.error(message, meta);
    },
    warn: (...msg: unknown[]) => {
      const { message, meta } = formatArgs(msg);
      targetLogger.warn(message, meta);
    },
    info: (...msg: unknown[]) => {
      const { message, meta } = formatArgs(msg);
      targetLogger.info(message, meta);
    },
    debug: (...msg: unknown[]) => {
      const { message, meta } = formatArgs(msg);
      targetLogger.debug(message, meta);
    },
    trace: (...msg: unknown[]) => {
      const { message, meta } = formatArgs(msg);
      targetLogger.debug(message, meta);
    },
  };
}

export function createFeishuHttpInstance(options?: {
  defaultTimeoutMs?: number;
}): Lark.HttpInstance {
  const defaultTimeoutMs =
    options?.defaultTimeoutMs ?? DEFAULT_FEISHU_HTTP_TIMEOUT_MS;
  // The SDK's default instance unwraps `response.data` in an interceptor, so at runtime it
  // already satisfies HttpInstance; its declared axios types do not say so.
  const base = Lark.defaultHttpInstance as unknown as Lark.HttpInstance;

  return {
    request: ((opts: Record<string, unknown>) => {
      const configuredTimeout =
        typeof opts.timeout === "number" &&
        Number.isFinite(opts.timeout) &&
        opts.timeout > 0
          ? opts.timeout
          : defaultTimeoutMs;
      const headers = {
        ...((opts.headers as Record<string, string> | undefined) ?? {}),
        "User-Agent": FEISHU_USER_AGENT,
      };
      const normalizedOpts = normalizeMultipartUploadData({
        ...opts,
        headers,
        timeout: configuredTimeout,
      }) as Record<string, unknown>;
      return base.request(normalizedOpts as Parameters<typeof base.request>[0]);
    }) as Lark.HttpInstance["request"],
    get: (url, opts) => base.get(url, opts),
    post: (url, data, opts) =>
      base.post(url, normalizeMultipartUploadData(data), opts),
    put: (url, data, opts) =>
      base.put(url, normalizeMultipartUploadData(data), opts),
    patch: (url, data, opts) =>
      base.patch(url, normalizeMultipartUploadData(data), opts),
    delete: (url, opts) => base.delete(url, opts),
    head: (url, opts) => base.head(url, opts),
    options: (url, opts) => base.options(url, opts),
  };
}

interface CachedClientEntry {
  client: Lark.Client;
  config: {
    appId: string;
    appSecret: string;
    domain?: FeishuDomain;
    httpTimeoutMs: number;
  };
}

const clientCache = new Map<string, CachedClientEntry>();

export function createFeishuClient(creds: FeishuCredentials): Lark.Client {
  const {
    accountId = "default",
    appId,
    appSecret,
    domain,
    httpTimeoutMs = DEFAULT_FEISHU_HTTP_TIMEOUT_MS,
    logger,
  } = creds;

  if (!appId || !appSecret) {
    throw new Error(
      `Feishu credentials not configured for account "${accountId}"`,
    );
  }

  const cached = clientCache.get(accountId);
  if (
    !logger &&
    cached &&
    cached.config.appId === appId &&
    cached.config.appSecret === appSecret &&
    cached.config.domain === domain &&
    cached.config.httpTimeoutMs === httpTimeoutMs
  ) {
    return cached.client;
  }

  const sdkLogger = createFeishuSdkLogger({ appId, appSecret, logger });
  const client = new Lark.Client({
    appId,
    appSecret,
    appType: Lark.AppType.SelfBuild,
    domain: resolveFeishuDomain(domain),
    logger: sdkLogger,
    loggerLevel: Lark.LoggerLevel.warn,
    httpInstance: createFeishuHttpInstance({ defaultTimeoutMs: httpTimeoutMs }),
  });

  if (!logger) {
    clientCache.set(accountId, {
      client,
      config: {
        appId,
        appSecret,
        domain,
        httpTimeoutMs,
      },
    });
  }

  return client;
}

export function createFeishuWSClient(
  creds: FeishuWSClientOptions,
): Lark.WSClient {
  const {
    accountId = "default",
    appId,
    appSecret,
    domain,
    httpTimeoutMs = DEFAULT_FEISHU_HTTP_TIMEOUT_MS,
    logger,
    onReady,
    onError,
    onReconnecting,
    onReconnected,
  } = creds;
  if (!appId || !appSecret) {
    throw new Error(
      `Feishu credentials not configured for account "${accountId}"`,
    );
  }
  const sdkLogger = createFeishuSdkLogger({ appId, appSecret, logger });
  return new Lark.WSClient({
    appId,
    appSecret,
    domain: resolveFeishuDomain(domain),
    logger: sdkLogger,
    loggerLevel: Lark.LoggerLevel.warn,
    httpInstance: createFeishuHttpInstance({ defaultTimeoutMs: httpTimeoutMs }),
    wsConfig: {
      pingTimeout: 3,
    },
    onReady,
    onError,
    onReconnecting,
    onReconnected,
  });
}

export function createEventDispatcher(
  creds: Pick<FeishuCredentials, "encryptKey" | "verificationToken">,
): Lark.EventDispatcher {
  return new Lark.EventDispatcher({
    encryptKey: creds.encryptKey,
    verificationToken: creds.verificationToken,
  });
}

export function clearClientCache(): void {
  clientCache.clear();
}
