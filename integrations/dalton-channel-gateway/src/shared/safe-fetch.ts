/**
 * Safe in-memory attachment download with domain allowlist, timeout, and 20 MB cap.
 *
 * Adapted from OpenClaw media fetch helpers (`.agy/ref_openclaw_channels/feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs:1709-1899`,
 * MIT, © OpenClaw contributors) for Design §4.1 (`src/shared/safe-fetch.ts`).
 *
 * Design invariant: Gateway never writes media attachments to local disk; all
 * payloads are buffered in memory up to `MAX_ATTACHMENT_BYTES` (20 MB).
 */

import { isIP } from "node:net";
import { MAX_ATTACHMENT_BYTES } from "../types.js";

export const DEFAULT_SAFE_FETCH_TIMEOUT_MS = 30_000;
export const DEFAULT_CHUNK_IDLE_TIMEOUT_MS = 15_000;

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

export function isBlockedHostnameOrIp(hostname: string): boolean {
  const normalized = hostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
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

  const ipVersion = isIP(normalized);
  if (ipVersion === 4) {
    const parts = normalized.split(".").map((p) => Number.parseInt(p, 10));
    if (parts.length !== 4 || parts.some((n) => Number.isNaN(n))) {
      return true;
    }
    const [a, b] = parts;
    if (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 169 && b === 254) ||
      (a === 172 && b !== undefined && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 100 && b !== undefined && b >= 64 && b <= 127)
    ) {
      return true;
    }
    return false;
  }

  if (ipVersion === 6) {
    if (
      normalized === "::1" ||
      normalized === "::" ||
      normalized.startsWith("fc") ||
      normalized.startsWith("fd") ||
      normalized.startsWith("fe8") ||
      normalized.startsWith("fe9") ||
      normalized.startsWith("fea") ||
      normalized.startsWith("feb") ||
      normalized.startsWith("::ffff:127.") ||
      normalized.startsWith("::ffff:10.") ||
      normalized.startsWith("::ffff:192.168.")
    ) {
      return true;
    }
    return false;
  }

  return false;
}

export function isAllowedAttachmentHostname(
  hostname: string,
  allowedDomains: readonly string[],
): boolean {
  const host = hostname.trim().toLowerCase().replace(/\.$/, "");
  if (!host || isBlockedHostnameOrIp(host)) {
    return false;
  }
  if (allowedDomains.length === 0) {
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
 */
export async function readStreamWithLimit(
  stream: AsyncIterable<unknown> | ReadableStream<Uint8Array>,
  options: ReadStreamWithLimitOptions = {},
): Promise<Buffer> {
  const maxBytes = Math.min(
    MAX_ATTACHMENT_BYTES,
    Math.max(1, options.maxBytes ?? MAX_ATTACHMENT_BYTES),
  );
  const chunkIdleTimeoutMs =
    options.chunkIdleTimeoutMs ?? DEFAULT_CHUNK_IDLE_TIMEOUT_MS;

  const asyncIterable: AsyncIterable<unknown> =
    Symbol.asyncIterator in stream
      ? (stream as AsyncIterable<unknown>)
      : {
          async *[Symbol.asyncIterator]() {
            const reader = (stream as ReadableStream<Uint8Array>).getReader();
            try {
              while (true) {
                const { done, value } = await reader.read();
                if (done) {
                  return;
                }
                yield value;
              }
            } finally {
              reader.releaseLock();
            }
          },
        };

  const iterator = asyncIterable[Symbol.asyncIterator]();
  const chunks: Buffer[] = [];
  let totalBytes = 0;
  let exhausted = false;

  const destroySourceIfPossible = (): void => {
    const candidate = stream as { destroy?: () => void; cancel?: () => void };
    if (typeof candidate.destroy === "function") {
      candidate.destroy();
    } else if (typeof candidate.cancel === "function") {
      try {
        candidate.cancel();
      } catch {
        // Ignore cancellation errors on teardown
      }
    }
  };

  try {
    while (true) {
      if (options.signal?.aborted) {
        throw new SafeFetchError("timeout", "Attachment stream aborted");
      }

      let idleTimer: ReturnType<typeof setTimeout> | undefined;
      const nextPromise = iterator.next();
      const raced =
        chunkIdleTimeoutMs > 0
          ? await Promise.race([
              nextPromise,
              new Promise<never>((_, reject) => {
                idleTimer = setTimeout(() => {
                  destroySourceIfPossible();
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

      if (raced.done) {
        exhausted = true;
        break;
      }

      const buf = toBufferChunk(raced.value);
      totalBytes += buf.byteLength;
      if (totalBytes > maxBytes) {
        destroySourceIfPossible();
        throw new SafeFetchError(
          "size_exceeded",
          `Attachment exceeds maximum allowed size of ${maxBytes} bytes`,
        );
      }
      chunks.push(buf);
    }
  } finally {
    if (!exhausted && typeof iterator.return === "function") {
      await iterator.return().catch(() => undefined);
    }
  }

  return Buffer.concat(chunks, totalBytes);
}

export interface SafeFetchOptions {
  allowedDomains?: readonly string[];
  maxBytes?: number;
  timeoutMs?: number;
  chunkIdleTimeoutMs?: number;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export interface SafeFetchResult {
  buffer: Buffer;
  contentType: string | null;
  contentDisposition: string | null;
  status: number;
}

export async function safeFetchBuffer(
  rawUrl: string | URL,
  options: SafeFetchOptions = {},
): Promise<SafeFetchResult> {
  let parsedUrl: URL;
  try {
    parsedUrl = typeof rawUrl === "string" ? new URL(rawUrl) : new URL(rawUrl.toString());
  } catch (error) {
    throw new SafeFetchError(
      "invalid_url",
      `Invalid attachment URL: ${String(rawUrl)}`,
      { cause: error },
    );
  }

  if (parsedUrl.protocol !== "https:" && parsedUrl.protocol !== "http:") {
    throw new SafeFetchError(
      "invalid_url",
      `Unsupported attachment URL protocol: ${parsedUrl.protocol}`,
    );
  }

  if (parsedUrl.username || parsedUrl.password) {
    throw new SafeFetchError(
      "invalid_url",
      "Attachment URL must not contain embedded credentials",
    );
  }

  if (isBlockedHostnameOrIp(parsedUrl.hostname)) {
    throw new SafeFetchError(
      "blocked_host",
      `Attachment host is blocked: ${parsedUrl.hostname}`,
    );
  }

  if (
    options.allowedDomains &&
    !isAllowedAttachmentHostname(parsedUrl.hostname, options.allowedDomains)
  ) {
    throw new SafeFetchError(
      "disallowed_domain",
      `Attachment host "${parsedUrl.hostname}" is not in the allowed domain list`,
    );
  }

  const maxBytes = Math.min(
    MAX_ATTACHMENT_BYTES,
    Math.max(1, options.maxBytes ?? MAX_ATTACHMENT_BYTES),
  );
  const timeoutMs = options.timeoutMs ?? DEFAULT_SAFE_FETCH_TIMEOUT_MS;
  const fetchFn = options.fetchImpl ?? globalThis.fetch;

  const controller = new AbortController();
  const onParentAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onParentAbort, { once: true });
  const timer =
    timeoutMs > 0
      ? setTimeout(() => controller.abort(), timeoutMs)
      : undefined;

  try {
    let response: Response;
    try {
      response = await fetchFn(parsedUrl.toString(), {
        method: "GET",
        headers: options.headers,
        redirect: "follow",
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

    // If redirected, verify final URL domain as well.
    if (response.url) {
      try {
        const finalUrl = new URL(response.url);
        if (isBlockedHostnameOrIp(finalUrl.hostname)) {
          throw new SafeFetchError(
            "blocked_host",
            `Redirected attachment host is blocked: ${finalUrl.hostname}`,
          );
        }
        if (
          options.allowedDomains &&
          !isAllowedAttachmentHostname(finalUrl.hostname, options.allowedDomains)
        ) {
          throw new SafeFetchError(
            "disallowed_domain",
            `Redirected attachment host "${finalUrl.hostname}" is not in the allowed domain list`,
          );
        }
      } catch (err) {
        if (err instanceof SafeFetchError) {
          throw err;
        }
      }
    }

    if (!response.ok) {
      throw new SafeFetchError(
        "http_error",
        `Attachment download failed with HTTP ${response.status}`,
      );
    }

    const contentLengthHeader = response.headers.get("content-length");
    if (contentLengthHeader) {
      const contentLength = Number.parseInt(contentLengthHeader, 10);
      if (Number.isFinite(contentLength) && contentLength > maxBytes) {
        try {
          await response.body?.cancel();
        } catch {
          // ignore
        }
        throw new SafeFetchError(
          "size_exceeded",
          `Attachment Content-Length (${contentLength}) exceeds maximum allowed size of ${maxBytes} bytes`,
        );
      }
    }

    const buffer = response.body
      ? await readStreamWithLimit(response.body, {
          maxBytes,
          chunkIdleTimeoutMs: options.chunkIdleTimeoutMs,
          signal: controller.signal,
        })
      : Buffer.from(await response.arrayBuffer());

    if (buffer.byteLength > maxBytes) {
      throw new SafeFetchError(
        "size_exceeded",
        `Attachment size (${buffer.byteLength}) exceeds maximum allowed size of ${maxBytes} bytes`,
      );
    }

    return {
      buffer,
      contentType: response.headers.get("content-type"),
      contentDisposition: response.headers.get("content-disposition"),
      status: response.status,
    };
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    options.signal?.removeEventListener("abort", onParentAbort);
  }
}
