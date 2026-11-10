/**
 * Safe in-memory attachment download with domain allowlist, timeout, and 20 MB cap.
 *
 * Adapted from OpenClaw media fetch helpers (`.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs:1709-1899`,
 * MIT, © OpenClaw contributors) for Design §4.1 (`src/shared/safe-fetch.ts`).
 *
 * Design invariant: Gateway never writes media attachments to local disk; all
 * payloads are buffered in memory up to `MAX_ATTACHMENT_BYTES` (20 MB).
 */

import { lookup as dnsLookup } from "node:dns/promises";
import { isIP } from "node:net";
import { MAX_ATTACHMENT_BYTES } from "../types.js";

export const MAX_CIPHERTEXT_ATTACHMENT_BYTES = MAX_ATTACHMENT_BYTES + 16;
export const DEFAULT_SAFE_FETCH_TIMEOUT_MS = 30_000;
export const DEFAULT_CHUNK_IDLE_TIMEOUT_MS = 15_000;
export const MAX_SAFE_FETCH_REDIRECTS = 5;

export type HostIpResolver = (hostname: string) => Promise<readonly string[]>;

export async function defaultResolveHostIps(
  hostname: string,
): Promise<readonly string[]> {
  const normalized = hostname
    .trim()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "");
  if (isIP(normalized) !== 0) {
    return [normalized];
  }
  const records = await dnsLookup(normalized, { all: true, verbatim: true });
  return records.map((r) => r.address);
}

export class SafeFetchError extends Error {
  readonly code:
    | "invalid_url"
    | "disallowed_domain"
    | "blocked_host"
    | "size_exceeded"
    | "http_error"
    | "timeout";

  constructor(
    code: SafeFetchError["code"],
    message: string,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "SafeFetchError";
    this.code = code;
  }
}

function parseIpv4Octets(text: string): [number, number, number, number] | null {
  const parts = text.split(".");
  if (parts.length !== 4) {
    return null;
  }
  const octets: number[] = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) {
      return null;
    }
    const n = Number.parseInt(part, 10);
    if (!Number.isInteger(n) || n < 0 || n > 255) {
      return null;
    }
    octets.push(n);
  }
  return [octets[0]!, octets[1]!, octets[2]!, octets[3]!];
}

function isBlockedIpv4Octets([a, b, c]: [number, number, number, number]): boolean {
  return (
    a === 0 || // 0.0.0.0/8
    a === 10 || // 10.0.0.0/8 RFC1918
    a === 127 || // 127.0.0.0/8 loopback
    (a === 169 && b === 254) || // 169.254.0.0/16 link-local / cloud metadata
    (a === 172 && b >= 16 && b <= 31) || // 172.16.0.0/12 RFC1918
    (a === 192 && b === 168) || // 192.168.0.0/16 RFC1918
    (a === 100 && b >= 64 && b <= 127) || // 100.64.0.0/10 CGNAT RFC6598
    (a === 192 && b === 0 && (c === 0 || c === 2)) || // 192.0.0.0/24, 192.0.2.0/24
    (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || // 198.18.0.0/15, 198.51.100.0/24
    (a === 203 && b === 0 && c === 113) || // 203.0.113.0/24
    a >= 224 // 224.0.0.0/4 multicast & 240.0.0.0/4 reserved/broadcast
  );
}

function extractIpv4FromMappedIpv6(
  normalizedIpv6: string,
): [number, number, number, number] | null {
  const mappedMatch =
    /^(?:::ffff:(?:0+:)?|(?:0+:){5}ffff:|0*:0*:0*:0*:0*:0*ffff:)(.+)$/i.exec(
      normalizedIpv6,
    );
  const tail = mappedMatch?.[1]?.trim();
  if (!tail) {
    return null;
  }
  const dotted = parseIpv4Octets(tail);
  if (dotted) {
    return dotted;
  }
  const hexMatch = /^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/i.exec(tail);
  if (!hexMatch) {
    return null;
  }
  const hi = Number.parseInt(hexMatch[1]!, 16);
  const lo = Number.parseInt(hexMatch[2]!, 16);
  return [(hi >> 8) & 0xff, hi & 0xff, (lo >> 8) & 0xff, lo & 0xff];
}

export function isBlockedHostnameOrIp(hostname: string): boolean {
  const normalized = hostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "");
  if (!normalized) {
    return true;
  }
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized.endsWith(".local") ||
    normalized.endsWith(".internal")
  ) {
    return true;
  }

  const ipv4Octets = parseIpv4Octets(normalized);
  if (ipv4Octets) {
    return isBlockedIpv4Octets(ipv4Octets);
  }

  const ipVersion = isIP(normalized);
  if (ipVersion === 4) {
    return true;
  }

  if (ipVersion === 6 || normalized.includes(":")) {
    if (
      normalized === "::1" ||
      normalized === "::" ||
      /^0*(?::0*){1,7}$/.test(normalized) ||
      /^0*(?::0*){0,6}:0*1$/.test(normalized) ||
      /^f[cd][0-9a-f]{0,2}:/i.test(normalized) ||
      /^fe[89ab][0-9a-f]?:/i.test(normalized) ||
      /^ff[0-9a-f]{0,2}:/i.test(normalized) ||
      /^2001:0*db8:/i.test(normalized)
    ) {
      return true;
    }
    const mappedOctets = extractIpv4FromMappedIpv6(normalized);
    if (mappedOctets) {
      return isBlockedIpv4Octets(mappedOctets);
    }
    if (normalized.includes("ffff:")) {
      return true;
    }
    return false;
  }

  return false;
}

export function isAllowedAttachmentHostname(
  hostname: string,
  allowedDomains?: readonly string[],
): boolean {
  const host = hostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "");
  if (!host || isBlockedHostnameOrIp(host)) {
    return false;
  }
  if (!allowedDomains || allowedDomains.length === 0) {
    return false;
  }
  for (const rawDomain of allowedDomains) {
    const domain = rawDomain
      .trim()
      .toLowerCase()
      .replace(/^\*\./, "")
      .replace(/^\./, "")
      .replace(/\.$/, "");
    if (!domain) {
      continue;
    }
    if (host === domain || host.endsWith(`.${domain}`)) {
      return true;
    }
  }
  return false;
}

export interface ReadStreamWithLimitOptions {
  maxBytes?: number;
  chunkIdleTimeoutMs?: number;
  signal?: AbortSignal;
}

function toBufferChunk(chunk: unknown): Buffer {
  if (Buffer.isBuffer(chunk)) {
    return chunk;
  }
  if (chunk instanceof Uint8Array) {
    return Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  }
  if (chunk instanceof ArrayBuffer) {
    return Buffer.from(chunk);
  }
  if (typeof chunk === "string") {
    return Buffer.from(chunk);
  }
  throw new TypeError(`Unsupported stream chunk type: ${typeof chunk}`);
}

/**
 * Reads an AsyncIterable / ReadableStream into an in-memory `Buffer` while
 * enforcing `maxBytes` (default 20 MB) and per-chunk idle timeout.
 * Cancels the active reader/iterator cleanly without unhandled rejections.
 */
export async function readStreamWithLimit(
  stream: AsyncIterable<unknown> | ReadableStream<Uint8Array>,
  options: ReadStreamWithLimitOptions = {},
): Promise<Buffer> {
  const maxBytes = Math.min(
    MAX_CIPHERTEXT_ATTACHMENT_BYTES,
    Math.max(1, options.maxBytes ?? MAX_ATTACHMENT_BYTES),
  );
  const chunkIdleTimeoutMs =
    options.chunkIdleTimeoutMs ?? DEFAULT_CHUNK_IDLE_TIMEOUT_MS;

  const isWebReadable =
    typeof (stream as ReadableStream<Uint8Array>).getReader === "function";

  let readNext: () => Promise<IteratorResult<unknown>>;
  let cancelSource: () => Promise<void>;
  let releaseSource: () => Promise<void>;

  if (isWebReadable) {
    const reader = (stream as ReadableStream<Uint8Array>).getReader();
    readNext = () => reader.read();
    cancelSource = async () => {
      try {
        await reader.cancel();
      } catch {
        // Ignore cancellation errors on aborted/errored streams
      }
    };
    releaseSource = async () => {
      try {
        reader.releaseLock();
      } catch {
        // Ignore lock release errors
      }
    };
  } else {
    const iterator = (stream as AsyncIterable<unknown>)[Symbol.asyncIterator]();
    readNext = () => iterator.next();
    cancelSource = async () => {
      try {
        const destroyable = stream as { destroy?: () => void };
        if (typeof destroyable.destroy === "function") {
          destroyable.destroy();
        }
      } catch {
        // Ignore destroy errors
      }
      try {
        if (typeof iterator.return === "function") {
          await Promise.race([
            iterator.return(),
            new Promise<void>((resolve) => {
              const timer = setTimeout(resolve, 50);
              timer.unref?.();
            }),
          ]);
        }
      } catch {
        // Ignore iterator closure errors
      }
    };
    releaseSource = async () => {
      try {
        if (typeof iterator.return === "function") {
          await iterator.return();
        }
      } catch {
        // Ignore iterator closure errors
      }
    };
  }

  const chunks: Buffer[] = [];
  let totalBytes = 0;
  let exhausted = false;
  let cancelled = false;

  const safeCancel = async (): Promise<void> => {
    if (cancelled) {
      return;
    }
    cancelled = true;
    await cancelSource();
  };

  let abortedBySignal = Boolean(options.signal?.aborted);
  const onAbort = (): void => {
    abortedBySignal = true;
    void safeCancel();
  };
  if (options.signal && !options.signal.aborted) {
    options.signal.addEventListener("abort", onAbort);
  }

  try {
    while (true) {
      if (options.signal?.aborted || abortedBySignal) {
        await safeCancel();
        throw new SafeFetchError("timeout", "Attachment stream aborted");
      }

      let idleTimer: ReturnType<typeof setTimeout> | undefined;
      const nextPromise = readNext();
      // Attach a no-op rejection handler immediately so if idle timeout wins
      // Promise.race and the pending read later rejects, no unhandled rejection occurs.
      nextPromise.catch(() => undefined);

      let timedOut = false;
      const raced =
        chunkIdleTimeoutMs > 0
          ? await Promise.race([
              nextPromise,
              new Promise<never>((_, reject) => {
                idleTimer = setTimeout(() => {
                  timedOut = true;
                  reject(
                    new SafeFetchError(
                      "timeout",
                      `Attachment download stalled: no data received for ${chunkIdleTimeoutMs}ms`,
                    ),
                  );
                }, chunkIdleTimeoutMs);
              }),
            ]).finally(() => {
              if (idleTimer !== undefined) {
                clearTimeout(idleTimer);
              }
            })
          : await nextPromise;

      if (timedOut) {
        await safeCancel();
      }

      if (options.signal?.aborted || abortedBySignal) {
        await safeCancel();
        throw new SafeFetchError("timeout", "Attachment stream aborted");
      }

      if (raced.done) {
        exhausted = true;
        break;
      }

      const buf = toBufferChunk(raced.value);
      totalBytes += buf.byteLength;
      if (totalBytes > maxBytes) {
        await safeCancel();
        throw new SafeFetchError(
          "size_exceeded",
          `Attachment exceeds maximum allowed size of ${maxBytes} bytes`,
        );
      }
      chunks.push(buf);
    }
  } catch (err) {
    await safeCancel();
    if (options.signal?.aborted || abortedBySignal) {
      if (err instanceof SafeFetchError && err.code === "timeout") {
        throw err;
      }
      throw new SafeFetchError("timeout", "Attachment stream aborted", {
        cause: err,
      });
    }
    throw err;
  } finally {
    if (options.signal) {
      options.signal.removeEventListener("abort", onAbort);
    }
    if (!exhausted && !cancelled) {
      await safeCancel();
    }
    await releaseSource();
  }

  return Buffer.concat(chunks, totalBytes);
}

export interface SafeFetchOptions {
  allowedDomains?: readonly string[];
  requireHttps?: boolean;
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  maxBytes?: number;
  declaredSizeBytes?: number;
  expectedExactBytes?: number;
  timeoutMs?: number;
  chunkIdleTimeoutMs?: number;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export interface SafeFetchRequestOptions {
  method?: "GET" | "POST" | "PUT";
  allowedDomains?: readonly string[];
  requireHttps?: boolean;
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  headers?: Record<string, string>;
  body?: BodyInit | Buffer | Uint8Array | string;
  timeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  maxRedirects?: number;
  maxBytes?: number;
}

export interface SafeFetchResult {
  buffer: Buffer;
  contentType: string | null;
  contentDisposition: string | null;
  status: number;
  headers: Headers;
}

const SENSITIVE_REDIRECT_HEADER_NAMES = new Set([
  "authorization",
  "cookie",
  "proxy-authorization",
  "x-service-token",
  "x-user-id",
  "x-wechat-uin",
]);

function stripSensitiveHeadersOnCrossOrigin(
  headers: Record<string, string> | undefined,
  initialOrigin: string,
  targetOrigin: string,
): Record<string, string> | undefined {
  if (!headers || initialOrigin === targetOrigin) {
    return headers;
  }
  const filtered: Record<string, string> = {};
  for (const [k, v] of Object.entries(headers)) {
    if (!SENSITIVE_REDIRECT_HEADER_NAMES.has(k.trim().toLowerCase())) {
      filtered[k] = v;
    }
  }
  return filtered;
}

async function cancelResponseBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // Ignore body cancellation errors
  }
}

export function validateSafeFetchTargetUrl(
  targetUrl: URL,
  allowedDomains: readonly string[] | undefined,
  isRedirect: boolean,
  requireHttps = false,
): void {
  const prefix = isRedirect ? "Redirected attachment" : "Attachment";
  if (requireHttps) {
    if (targetUrl.protocol !== "https:") {
      throw new SafeFetchError(
        "invalid_url",
        `${prefix} URL must use HTTPS: ${targetUrl.protocol}`,
      );
    }
  } else if (targetUrl.protocol !== "https:" && targetUrl.protocol !== "http:") {
    throw new SafeFetchError(
      "invalid_url",
      `Unsupported ${prefix.toLowerCase()} URL protocol: ${targetUrl.protocol}`,
    );
  }

  if (targetUrl.username || targetUrl.password) {
    throw new SafeFetchError(
      "invalid_url",
      `${prefix} URL must not contain embedded credentials`,
    );
  }

  if (isBlockedHostnameOrIp(targetUrl.hostname)) {
    throw new SafeFetchError(
      "blocked_host",
      `${prefix} host is blocked: ${targetUrl.hostname}`,
    );
  }

  if (!isAllowedAttachmentHostname(targetUrl.hostname, allowedDomains)) {
    throw new SafeFetchError(
      "disallowed_domain",
      `${prefix} host "${targetUrl.hostname}" is not in the allowed domain list`,
    );
  }
}

export async function validateSafeTargetUrlWithDns(
  targetUrl: URL,
  options: {
    allowedDomains?: readonly string[];
    requireHttps?: boolean;
    isRedirect?: boolean;
    resolveHostIps?: HostIpResolver;
    checkResolvedIps?: boolean;
  },
): Promise<void> {
  const isRedirect = Boolean(options.isRedirect);
  validateSafeFetchTargetUrl(
    targetUrl,
    options.allowedDomains,
    isRedirect,
    Boolean(options.requireHttps),
  );

  const resolver =
    options.resolveHostIps ??
    (options.checkResolvedIps ? defaultResolveHostIps : undefined);
  if (resolver) {
    let resolvedIps: readonly string[];
    try {
      resolvedIps = await resolver(targetUrl.hostname);
    } catch (error) {
      throw new SafeFetchError(
        "blocked_host",
        `Failed to resolve host "${targetUrl.hostname}"`,
        { cause: error },
      );
    }
    if (!resolvedIps || resolvedIps.length === 0) {
      throw new SafeFetchError(
        "blocked_host",
        `Host "${targetUrl.hostname}" resolved to no IP addresses`,
      );
    }
    for (const ip of resolvedIps) {
      if (isBlockedHostnameOrIp(ip)) {
        throw new SafeFetchError(
          "blocked_host",
          `Host "${targetUrl.hostname}" resolved to blocked IP address "${ip}"`,
        );
      }
    }
  }
}

export async function safeFetchResponse(
  rawUrl: string | URL,
  options: SafeFetchRequestOptions = {},
): Promise<Response> {
  let currentUrl: URL;
  try {
    currentUrl =
      typeof rawUrl === "string"
        ? new URL(rawUrl)
        : new URL(rawUrl.toString());
  } catch (error) {
    throw new SafeFetchError("invalid_url", "Invalid request URL", {
      cause: error,
    });
  }

  await validateSafeTargetUrlWithDns(currentUrl, {
    allowedDomains: options.allowedDomains,
    requireHttps: options.requireHttps,
    isRedirect: false,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
  });

  const initialOrigin = currentUrl.origin;
  const timeoutMs = options.timeoutMs ?? DEFAULT_SAFE_FETCH_TIMEOUT_MS;
  const fetchFn = options.fetchImpl ?? globalThis.fetch;
  const maxRedirects = options.maxRedirects ?? MAX_SAFE_FETCH_REDIRECTS;
  let method = options.method ?? "GET";
  let body = options.body;

  const controller = new AbortController();
  const onParentAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onParentAbort, { once: true });
  if (options.signal?.aborted) {
    controller.abort();
  }
  const timer =
    timeoutMs > 0
      ? setTimeout(() => controller.abort(), timeoutMs)
      : undefined;

  try {
    let redirectHops = 0;
    while (true) {
      if (controller.signal.aborted) {
        throw new SafeFetchError(
          "timeout",
          `Request timed out or aborted after ${timeoutMs}ms`,
        );
      }

      const effectiveHeaders = stripSensitiveHeadersOnCrossOrigin(
        options.headers,
        initialOrigin,
        currentUrl.origin,
      );

      let response: Response;
      try {
        response = await fetchFn(currentUrl.toString(), {
          method,
          headers: effectiveHeaders,
          body: body as BodyInit | undefined,
          redirect: "manual",
          signal: controller.signal,
        });
      } catch (error) {
        if (controller.signal.aborted) {
          throw new SafeFetchError(
            "timeout",
            `Request timed out after ${timeoutMs}ms`,
            { cause: error },
          );
        }
        throw error;
      }

      if (response.status >= 300 && response.status < 400) {
        const locationHeader = response.headers.get("location")?.trim();
        await cancelResponseBody(response);

        if (!locationHeader) {
          throw new SafeFetchError(
            "http_error",
            `Redirect response HTTP ${response.status} is missing Location header`,
          );
        }

        if (redirectHops >= maxRedirects) {
          throw new SafeFetchError(
            "http_error",
            `Request exceeded maximum redirect hops (${maxRedirects})`,
          );
        }

        let nextUrl: URL;
        try {
          nextUrl = new URL(locationHeader, currentUrl);
        } catch (error) {
          throw new SafeFetchError(
            "invalid_url",
            "Invalid redirect Location URL",
            { cause: error },
          );
        }

        await validateSafeTargetUrlWithDns(nextUrl, {
          allowedDomains: options.allowedDomains,
          requireHttps: options.requireHttps,
          isRedirect: true,
          resolveHostIps: options.resolveHostIps,
          checkResolvedIps: options.checkResolvedIps,
        });

        if (
          response.status === 301 ||
          response.status === 302 ||
          response.status === 303
        ) {
          if (method !== "GET") {
            method = "GET";
            body = undefined;
          }
        }
        currentUrl = nextUrl;
        redirectHops += 1;
        continue;
      }

      const maxBytes = Math.min(
        MAX_CIPHERTEXT_ATTACHMENT_BYTES,
        Math.max(1, options.maxBytes ?? MAX_CIPHERTEXT_ATTACHMENT_BYTES),
      );
      let payload: Buffer;
      try {
        payload = response.body
          ? await readStreamWithLimit(response.body, {
              maxBytes,
              chunkIdleTimeoutMs: 0,
              signal: controller.signal,
            })
          : Buffer.alloc(0);
      } catch (error) {
        await cancelResponseBody(response);
        if (error instanceof SafeFetchError) {
          throw error;
        }
        if (controller.signal.aborted) {
          throw new SafeFetchError(
            "timeout",
            `Request timed out or aborted after ${timeoutMs}ms`,
            { cause: error },
          );
        }
        throw error;
      }
      return new Response(new Uint8Array(payload), {
        status: response.status,
        statusText: response.statusText,
        headers: new Headers(response.headers),
      });
    }
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    options.signal?.removeEventListener("abort", onParentAbort);
  }
}

export async function safeFetchBuffer(
  rawUrl: string | URL,
  options: SafeFetchOptions = {},
): Promise<SafeFetchResult> {
  let currentUrl: URL;
  try {
    currentUrl =
      typeof rawUrl === "string"
        ? new URL(rawUrl)
        : new URL(rawUrl.toString());
  } catch (error) {
    throw new SafeFetchError("invalid_url", "Invalid attachment URL", {
      cause: error,
    });
  }

  await validateSafeTargetUrlWithDns(currentUrl, {
    allowedDomains: options.allowedDomains,
    requireHttps: options.requireHttps,
    isRedirect: false,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
  });

  const maxBytes = Math.min(
    MAX_CIPHERTEXT_ATTACHMENT_BYTES,
    Math.max(1, options.maxBytes ?? MAX_ATTACHMENT_BYTES),
  );
  if (
    options.declaredSizeBytes !== undefined &&
    Number.isFinite(options.declaredSizeBytes) &&
    options.declaredSizeBytes > maxBytes
  ) {
    throw new SafeFetchError(
      "size_exceeded",
      `Declared attachment size (${options.declaredSizeBytes}) exceeds maximum allowed size of ${maxBytes} bytes`,
    );
  }

  const initialOrigin = currentUrl.origin;
  const timeoutMs = options.timeoutMs ?? DEFAULT_SAFE_FETCH_TIMEOUT_MS;
  const fetchFn = options.fetchImpl ?? globalThis.fetch;

  const controller = new AbortController();
  const onParentAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onParentAbort, { once: true });
  if (options.signal?.aborted) {
    controller.abort();
  }
  const timer =
    timeoutMs > 0
      ? setTimeout(() => controller.abort(), timeoutMs)
      : undefined;

  try {
    let response: Response;
    let redirectHops = 0;

    while (true) {
      const effectiveHeaders = stripSensitiveHeadersOnCrossOrigin(
        options.headers,
        initialOrigin,
        currentUrl.origin,
      );
      try {
        response = await fetchFn(currentUrl.toString(), {
          method: "GET",
          headers: effectiveHeaders,
          redirect: "manual",
          signal: controller.signal,
        });
      } catch (error) {
        if (controller.signal.aborted) {
          throw new SafeFetchError(
            "timeout",
            `Attachment download timed out after ${timeoutMs}ms`,
            { cause: error },
          );
        }
        throw error;
      }

      if (response.status >= 300 && response.status < 400) {
        const locationHeader = response.headers.get("location")?.trim();
        await cancelResponseBody(response);

        if (!locationHeader) {
          throw new SafeFetchError(
            "http_error",
            `Redirect response HTTP ${response.status} is missing Location header`,
          );
        }

        if (redirectHops >= MAX_SAFE_FETCH_REDIRECTS) {
          throw new SafeFetchError(
            "http_error",
            `Attachment download exceeded maximum redirect hops (${MAX_SAFE_FETCH_REDIRECTS})`,
          );
        }

        let nextUrl: URL;
        try {
          nextUrl = new URL(locationHeader, currentUrl);
        } catch (error) {
          throw new SafeFetchError(
            "invalid_url",
            "Invalid redirect Location URL",
            { cause: error },
          );
        }

        await validateSafeTargetUrlWithDns(nextUrl, {
          allowedDomains: options.allowedDomains,
          requireHttps: options.requireHttps,
          isRedirect: true,
          resolveHostIps: options.resolveHostIps,
          checkResolvedIps: options.checkResolvedIps,
        });
        currentUrl = nextUrl;
        redirectHops += 1;
        continue;
      }

      break;
    }

    if (!response.ok) {
      await cancelResponseBody(response);
      throw new SafeFetchError(
        "http_error",
        `Attachment download failed with HTTP ${response.status}`,
      );
    }

    const contentLengthHeader = response.headers.get("content-length");
    let contentLengthValue: number | undefined;
    if (contentLengthHeader) {
      const contentLength = Number.parseInt(contentLengthHeader, 10);
      if (Number.isFinite(contentLength) && contentLength >= 0) {
        contentLengthValue = contentLength;
        if (contentLength > maxBytes) {
          await cancelResponseBody(response);
          throw new SafeFetchError(
            "size_exceeded",
            `Attachment Content-Length (${contentLength}) exceeds maximum allowed size of ${maxBytes} bytes`,
          );
        }
        if (
          options.expectedExactBytes !== undefined &&
          contentLength !== options.expectedExactBytes
        ) {
          await cancelResponseBody(response);
          throw new SafeFetchError(
            "size_exceeded",
            `Attachment Content-Length (${contentLength}) does not match expected size (${options.expectedExactBytes})`,
          );
        }
      }
    }

    const buffer = response.body
      ? await readStreamWithLimit(response.body, {
          maxBytes,
          chunkIdleTimeoutMs: options.chunkIdleTimeoutMs,
          signal: controller.signal,
        })
      : Buffer.alloc(0);

    if (buffer.byteLength > maxBytes) {
      throw new SafeFetchError(
        "size_exceeded",
        `Attachment size (${buffer.byteLength}) exceeds maximum allowed size of ${maxBytes} bytes`,
      );
    }
    if (
      contentLengthValue !== undefined &&
      buffer.byteLength !== contentLengthValue
    ) {
      throw new SafeFetchError(
        "size_exceeded",
        `Attachment actual size (${buffer.byteLength}) does not match Content-Length (${contentLengthValue})`,
      );
    }
    if (
      options.expectedExactBytes !== undefined &&
      buffer.byteLength !== options.expectedExactBytes
    ) {
      throw new SafeFetchError(
        "size_exceeded",
        `Attachment actual size (${buffer.byteLength}) does not match declared size (${options.expectedExactBytes})`,
      );
    }

    return {
      buffer,
      contentType: response.headers.get("content-type"),
      contentDisposition: response.headers.get("content-disposition"),
      status: response.status,
      headers: response.headers,
    };
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    options.signal?.removeEventListener("abort", onParentAbort);
  }
}
