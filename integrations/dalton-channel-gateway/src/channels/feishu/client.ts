/**
 * Feishu SDK client, WebSocket client, event dispatcher, and multipart upload fix.
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/client-wwr8PGPH.mjs`
 * (lines 161–170, 222–331).
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 1 (`src/channels/feishu/client.ts`):
 * - Retains `normalizeMultipartUploadData`, `createFeishuClient`,
 *   `createFeishuWSClient`, and `createEventDispatcher`.
 * - Excludes `createFeishuHttpInstance`'s `captureChannelReadAuthority`,
 *   `captureEffectAuthority`, and `getFeishuProxyAgent`.
 */

import * as Lark from "@larksuiteoapi/node-sdk";

export type FeishuDomain = "feishu" | "lark" | (string & {});

export interface FeishuCredentials {
  accountId?: string;
  appId: string;
  appSecret: string;
  encryptKey?: string;
  verificationToken?: string;
  domain?: FeishuDomain;
  httpTimeoutMs?: number;
}

export const DEFAULT_FEISHU_HTTP_TIMEOUT_MS = 30_000;
export const FEISHU_USER_AGENT = "dalton-channel-gateway/feishu";

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

/**
 * Normalizes `Buffer` / `Uint8Array` / `ArrayBuffer` fields inside multipart
 * upload request `data` into concrete `Buffer` instances before `form-data`
 * serializes them, preventing `[object Object]` corruption in `@larksuiteoapi/node-sdk`.
 *
 * Source: `client-wwr8PGPH.mjs:222-246`.
 */
export function normalizeMultipartUploadData(data: unknown): unknown {
  if (!isRecord(data)) {
    return data;
  }
  let normalized: Record<string, unknown> | undefined;
  for (const [key, value] of Object.entries(data)) {
    let nextValue = value;
    if (Buffer.isBuffer(value)) {
      nextValue = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    } else if (ArrayBuffer.isView(value)) {
      nextValue = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    } else if (value instanceof ArrayBuffer) {
      nextValue = Buffer.from(value);
    }
    if (nextValue !== value || normalized) {
      normalized ??= { ...data };
      normalized[key] = nextValue;
    }
  }
  return normalized ?? data;
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
      return base.request({
        ...opts,
        data: normalizeMultipartUploadData(opts.data),
        headers,
        timeout: configuredTimeout,
      } as Parameters<typeof base.request>[0]);
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
  } = creds;

  if (!appId || !appSecret) {
    throw new Error(
      `Feishu credentials not configured for account "${accountId}"`,
    );
  }

  const cached = clientCache.get(accountId);
  if (
    cached &&
    cached.config.appId === appId &&
    cached.config.appSecret === appSecret &&
    cached.config.domain === domain &&
    cached.config.httpTimeoutMs === httpTimeoutMs
  ) {
    return cached.client;
  }

  const client = new Lark.Client({
    appId,
    appSecret,
    appType: Lark.AppType.SelfBuild,
    domain: resolveFeishuDomain(domain),
    httpInstance: createFeishuHttpInstance({ defaultTimeoutMs: httpTimeoutMs }),
  });

  clientCache.set(accountId, {
    client,
    config: {
      appId,
      appSecret,
      domain,
      httpTimeoutMs,
    },
  });

  return client;
}

export function createFeishuWSClient(creds: FeishuCredentials): Lark.WSClient {
  const { accountId = "default", appId, appSecret, domain } = creds;
  if (!appId || !appSecret) {
    throw new Error(
      `Feishu credentials not configured for account "${accountId}"`,
    );
  }
  return new Lark.WSClient({
    appId,
    appSecret,
    domain: resolveFeishuDomain(domain),
    loggerLevel: Lark.LoggerLevel.info,
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
