import { sleepWithAbort } from "../shared/async.js";
import type { SubsystemLogger } from "../shared/logger.js";
import { readStreamWithLimit, SafeFetchError } from "../shared/safe-fetch.js";
import {
  type NativeCommandPlatform,
  NativeCommandRegistrationError,
} from "./types.js";

export const FEISHU_TRUSTED_API_ORIGIN = "https://open.feishu.cn";
export const DISCORD_TRUSTED_API_ORIGIN = "https://discord.com";

export const DEFAULT_REQUEST_TIMEOUT_MS = 10_000;
export const DEFAULT_TOTAL_TIMEOUT_MS = 30_000;
export const DEFAULT_MAX_REQUESTS = 24;
export const DEFAULT_MAX_PAGES = 10;
export const DEFAULT_MAX_RATE_LIMIT_RETRIES = 3;
export const MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024;

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  return (
    normalized === "127.0.0.1" ||
    normalized === "::1" ||
    normalized === "localhost"
  );
}

/**
 * Enforces fixed trusted platform API origins in production while allowing
 * strictly loopback `http://` servers injected by unit tests.
 */
export function resolveVerifiedApiOrigin(
  trustedOrigin: string,
  testApiOrigin: string | undefined,
  platform: NativeCommandPlatform,
): string {
  if (testApiOrigin === undefined) {
    return trustedOrigin;
  }
  let parsed: URL;
  try {
    parsed = new URL(testApiOrigin.trim());
  } catch (err) {
    throw new NativeCommandRegistrationError(
      "disallowed_origin",
      `Invalid API origin override for ${platform}.`,
      { platform, cause: err },
    );
  }

  if (
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.pathname !== "" && parsed.pathname !== "/")
  ) {
    throw new NativeCommandRegistrationError(
      "disallowed_origin",
      `API origin override for ${platform} must be a bare origin without credentials, path, or query.`,
      { platform },
    );
  }

  if (parsed.origin === trustedOrigin) {
    return trustedOrigin;
  }

  if (parsed.protocol === "http:" && isLoopbackHostname(parsed.hostname)) {
    return parsed.origin;
  }

  throw new NativeCommandRegistrationError(
    "disallowed_origin",
    `Disallowed API origin "${parsed.origin}" for ${platform}; only ${trustedOrigin} or a local loopback HTTP test server is permitted.`,
    { platform },
  );
}

/**
 * Raised when an in-flight `POST` or `PATCH` loses its response (timeout,
 * socket reset, 5xx gateway error, or truncated 2xx payload), requiring a
 * follow-up platform `GET` to verify whether the write took effect.
 */
export class AmbiguousMutationError extends Error {
  readonly reason: string;

  constructor(reason: string, cause?: unknown) {
    super(reason, cause !== undefined ? { cause } : undefined);
    this.name = "AmbiguousMutationError";
    this.reason = reason;
  }
}

export interface JsonHttpResponse {
  readonly status: number;
  readonly ok: boolean;
  readonly headers: Headers;
  readonly data: unknown;
  readonly rawText: string;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseRetryAfterMs(
  headers: Headers,
  parsedBody: unknown,
  nowMs: number,
): number {
  const retryAfterHeader = headers.get("retry-after")?.trim();
  if (retryAfterHeader) {
    if (/^\d+(?:\.\d+)?$/.test(retryAfterHeader)) {
      const secs = Number.parseFloat(retryAfterHeader);
      if (Number.isFinite(secs) && secs >= 0) {
        return Math.ceil(secs * 1000);
      }
    } else {
      const parsedDate = Date.parse(retryAfterHeader);
      if (Number.isFinite(parsedDate)) {
        return Math.max(0, parsedDate - nowMs);
      }
    }
  }

  const resetAfterHeader = headers.get("x-ratelimit-reset-after")?.trim();
  if (resetAfterHeader && /^\d+(?:\.\d+)?$/.test(resetAfterHeader)) {
    const secs = Number.parseFloat(resetAfterHeader);
    if (Number.isFinite(secs) && secs >= 0) {
      return Math.ceil(secs * 1000);
    }
  }

  if (isPlainRecord(parsedBody) && typeof parsedBody.retry_after === "number") {
    if (Number.isFinite(parsedBody.retry_after) && parsedBody.retry_after >= 0) {
      return Math.ceil(parsedBody.retry_after * 1000);
    }
  }

  return 1_000;
}

async function readResponseTextBounded(
  response: Response,
  maxBytes: number,
  timeoutMs: number,
  signal: AbortSignal,
): Promise<string> {
  if (!response.body) {
    const text = await response.text();
    if (Buffer.byteLength(text, "utf8") > maxBytes) {
      throw new SafeFetchError(
        "size_exceeded",
        `Response exceeded ${maxBytes} bytes.`,
      );
    }
    return text;
  }

  const buf = await readStreamWithLimit(response.body, {
    maxBytes,
    chunkIdleTimeoutMs: timeoutMs,
    signal,
  });
  return buf.toString("utf8");
}

export interface BoundedHttpClientOptions {
  readonly platform: NativeCommandPlatform;
  readonly baseOrigin: string;
  readonly requestTimeoutMs?: number;
  readonly totalTimeoutMs?: number;
  readonly maxRequests?: number;
  readonly maxRateLimitRetries?: number;
  readonly fetchImpl?: typeof fetch;
  readonly sleepFn?: (ms: number, signal?: AbortSignal) => Promise<void>;
  readonly nowFn?: () => number;
  readonly redact: (value: string) => string;
  readonly logger?: SubsystemLogger;
}

export class BoundedHttpClient {
  private readonly platform: NativeCommandPlatform;
  private readonly baseOrigin: string;
  private readonly requestTimeoutMs: number;
  private readonly deadlineMs: number;
  private readonly maxRequests: number;
  private readonly maxRateLimitRetries: number;
  private readonly fetchImpl: typeof fetch;
  private readonly sleepFn: (ms: number, signal?: AbortSignal) => Promise<void>;
  private readonly nowFn: () => number;
  private readonly redact: (value: string) => string;
  private readonly logger?: SubsystemLogger;
  private requestCount = 0;

  constructor(options: BoundedHttpClientOptions) {
    this.platform = options.platform;
    this.baseOrigin = options.baseOrigin;
    this.nowFn = options.nowFn ?? (() => Date.now());
    const totalTimeoutMs = Math.max(
      1,
      Math.floor(options.totalTimeoutMs ?? DEFAULT_TOTAL_TIMEOUT_MS),
    );
    this.requestTimeoutMs = Math.max(
      1,
      Math.floor(options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS),
    );
    this.deadlineMs = this.nowFn() + totalTimeoutMs;
    this.maxRequests = Math.max(
      1,
      Math.floor(options.maxRequests ?? DEFAULT_MAX_REQUESTS),
    );
    this.maxRateLimitRetries = Math.max(
      0,
      Math.floor(options.maxRateLimitRetries ?? DEFAULT_MAX_RATE_LIMIT_RETRIES),
    );
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch;
    this.sleepFn = options.sleepFn ?? sleepWithAbort;
    this.redact = options.redact;
    this.logger = options.logger;
  }

  get totalRequestsMade(): number {
    return this.requestCount;
  }

  remainingBudgetMs(): number {
    return this.deadlineMs - this.nowFn();
  }

  async requestJson(params: {
    method: "GET" | "POST" | "PATCH";
    path: string;
    headers: Record<string, string>;
    body?: Record<string, unknown>;
    isMutation?: boolean;
  }): Promise<JsonHttpResponse> {
    const isMutation = Boolean(params.isMutation);

    for (
      let rateLimitAttempt = 0;
      rateLimitAttempt <= this.maxRateLimitRetries;
      rateLimitAttempt += 1
    ) {
      const remainingMs = this.remainingBudgetMs();
      if (remainingMs <= 0) {
        throw new NativeCommandRegistrationError(
          "timeout",
          `${this.platform} 命令注册超出总超时时限，已中止后续请求。`,
          { platform: this.platform },
        );
      }

      if (this.requestCount >= this.maxRequests) {
        throw new NativeCommandRegistrationError(
          "request_limit_exceeded",
          `${this.platform} 命令注册请求次数达到上限 (${this.maxRequests})，已中止后续请求。`,
          { platform: this.platform },
        );
      }

      this.requestCount += 1;
      const attemptTimeoutMs = Math.max(
        1,
        Math.min(this.requestTimeoutMs, remainingMs),
      );
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), attemptTimeoutMs);
      const url = `${this.baseOrigin}${params.path}`;
      const safePath = this.redact(params.path.split("?")[0] || params.path);

      let response: Response;
      let rawText: string;

      try {
        response = await this.fetchImpl(url, {
          method: params.method,
          headers: params.headers,
          body:
            params.body !== undefined ? JSON.stringify(params.body) : undefined,
          redirect: "manual",
          signal: controller.signal,
        });

        if (response.status >= 300 && response.status < 400) {
          try {
            await response.body?.cancel();
          } catch {
            // Ignore body cancel failure
          }
          throw new NativeCommandRegistrationError(
            "http_error",
            `${this.platform} 接口返回非预期的重定向状态 HTTP ${response.status}（禁止跨地址转发凭据）。`,
            { httpStatus: response.status, platform: this.platform },
          );
        }

        rawText = await readResponseTextBounded(
          response,
          MAX_JSON_RESPONSE_BYTES,
          attemptTimeoutMs,
          controller.signal,
        );
      } catch (err) {
        clearTimeout(timer);
        if (err instanceof NativeCommandRegistrationError) {
          throw err;
        }
        const rawErrMsg =
          err instanceof Error ? err.message : String(err);
        const safeErrMsg = this.redact(rawErrMsg);
        const timedOut =
          controller.signal.aborted ||
          (err instanceof SafeFetchError && err.code === "timeout");

        if (isMutation) {
          throw new AmbiguousMutationError(
            timedOut
              ? `${params.method} ${safePath} 请求超时 (${attemptTimeoutMs}ms)`
              : `${params.method} ${safePath} 连接中断: ${safeErrMsg}`,
          );
        }

        throw new NativeCommandRegistrationError(
          timedOut ? "timeout" : "network_error",
          timedOut
            ? `${this.platform} 请求 ${params.method} ${safePath} 超时 (${attemptTimeoutMs}ms)。`
            : `${this.platform} 请求 ${params.method} ${safePath} 网络错误: ${safeErrMsg}`,
          { platform: this.platform },
        );
      } finally {
        clearTimeout(timer);
      }

      const safeRawText = this.redact(rawText);
      let parsedData: unknown = undefined;
      let jsonValid = false;
      if (rawText.trim().length > 0) {
        try {
          parsedData = JSON.parse(rawText);
          jsonValid = true;
        } catch {
          jsonValid = false;
        }
      } else {
        jsonValid = response.status === 204;
      }

      if (response.status === 429) {
        if (isPlainRecord(parsedData) && parsedData.code === 30034) {
          throw new NativeCommandRegistrationError(
            "daily_create_limit",
            "Discord 应用命令当日创建次数已达平台上限 (code 30034)，请等待配额重置后再试。",
            { httpStatus: 429, platform: this.platform },
          );
        }

        const retryAfterMs = parseRetryAfterMs(
          response.headers,
          parsedData,
          this.nowFn(),
        );
        const remainingAfter429 = this.remainingBudgetMs();
        if (
          rateLimitAttempt >= this.maxRateLimitRetries ||
          retryAfterMs > remainingAfter429 ||
          remainingAfter429 <= 0
        ) {
          throw new NativeCommandRegistrationError(
            "rate_limited",
            `${this.platform} 接口触发速率限制 (HTTP 429, Retry-After=${retryAfterMs}ms)，超出可重试次数或剩余总时限 (${Math.max(0, remainingAfter429)}ms)。`,
            { httpStatus: 429, platform: this.platform },
          );
        }

        this.logger?.warn("HTTP 429 rate limited; waiting before retry", {
          method: params.method,
          path: safePath,
          retryAfterMs,
          attempt: rateLimitAttempt + 1,
        });
        await this.sleepFn(retryAfterMs);
        continue;
      }

      if (response.status >= 500) {
        if (isMutation) {
          throw new AmbiguousMutationError(
            `${params.method} ${safePath} 返回服务端异常 HTTP ${response.status}`,
          );
        }
        throw new NativeCommandRegistrationError(
          "http_error",
          `${this.platform} 请求 ${params.method} ${safePath} 失败 (HTTP ${response.status})${safeRawText ? `: ${safeRawText}` : ""}`,
          { httpStatus: response.status, platform: this.platform },
        );
      }

      if (response.ok && response.status !== 204 && !jsonValid) {
        if (isMutation) {
          throw new AmbiguousMutationError(
            `${params.method} ${safePath} 响应体不完整或非合法 JSON`,
          );
        }
        throw new NativeCommandRegistrationError(
          "malformed_response",
          `${this.platform} 请求 ${params.method} ${safePath} 返回非合法 JSON 响应。`,
          { httpStatus: response.status, platform: this.platform },
        );
      }

      return {
        status: response.status,
        ok: response.ok,
        headers: response.headers,
        data: parsedData,
        rawText: safeRawText,
      };
    }

    throw new NativeCommandRegistrationError(
      "rate_limited",
      `${this.platform} 接口触发速率限制 (HTTP 429)。`,
      { httpStatus: 429, platform: this.platform },
    );
  }
}

export async function collectPaginatedItems<T>(options: {
  platform: NativeCommandPlatform;
  maxPages?: number;
  fetchPage: (pageToken?: string) => Promise<{
    items: readonly T[];
    hasMore: boolean;
    nextPageToken?: string;
  }>;
}): Promise<T[]> {
  const maxPages = Math.max(
    1,
    Math.floor(options.maxPages ?? DEFAULT_MAX_PAGES),
  );
  const seenCursors = new Set<string>();
  const collected: T[] = [];
  let currentCursor: string | undefined;

  for (let page = 1; page <= maxPages; page += 1) {
    const result = await options.fetchPage(currentCursor);
    collected.push(...result.items);

    if (!result.hasMore) {
      return collected;
    }

    if (page >= maxPages) {
      throw new NativeCommandRegistrationError(
        "pagination_error",
        `${options.platform} 命令列表分页超过最大页数限制 (${maxPages})，拒绝使用不完整列表计算或写入命令。`,
        { platform: options.platform },
      );
    }

    const nextCursor =
      typeof result.nextPageToken === "string"
        ? result.nextPageToken.trim()
        : "";
    if (!nextCursor) {
      throw new NativeCommandRegistrationError(
        "pagination_error",
        `${options.platform} 命令列表返回 has_more=true 但缺少有效 page_token，拒绝使用不完整列表计算或写入命令。`,
        { platform: options.platform },
      );
    }

    if (seenCursors.has(nextCursor)) {
      throw new NativeCommandRegistrationError(
        "pagination_error",
        `${options.platform} 命令列表返回重复分页游标，拒绝使用不完整列表计算或写入命令。`,
        { platform: options.platform },
      );
    }

    seenCursors.add(nextCursor);
    currentCursor = nextCursor;
  }

  return collected;
}
