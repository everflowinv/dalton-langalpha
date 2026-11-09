/**
 * Weixin CDN AES-128-ECB PKCS#7 cryptography, dual key parsing, magic-byte MIME validation,
 * and bounded in-memory CDN download/upload helpers.
 *
 * Adapted from `@tencent-weixin/openclaw-weixin` (`src/cdn/aes-ecb.ts`, `src/cdn/pic-decrypt.ts`,
 * `src/cdn/cdn-url.ts`, `src/cdn/cdn-upload.ts`, `src/cdn/upload.ts`, MIT, Copyright (c) 2026 Tencent).
 */

import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import path from "node:path";
import {
  type HostIpResolver,
  MAX_CIPHERTEXT_ATTACHMENT_BYTES,
  SafeFetchError,
  safeFetchBuffer,
  safeFetchResponse,
  validateSafeFetchTargetUrl,
  validateSafeTargetUrlWithDns,
} from "../../shared/safe-fetch.js";
import { MAX_ATTACHMENT_BYTES } from "../../types.js";
import {
  getWeixinUploadUrl,
  parseRetryAfterMs,
  WeixinApiError,
  type WeixinHttpCommonOptions,
} from "./api.js";
import {
  ILINK_DEFAULT_CDN_BASE_URL,
  OFFICIAL_WEIXIN_CDN_DOMAINS,
  UploadMediaType,
  type WeixinPersistedAttachmentMeta,
} from "./types.js";

export const WEIXIN_CDN_UPLOAD_MAX_RETRIES = 3;
export const WEIXIN_CDN_DOWNLOAD_TIMEOUT_MS = 30_000;
export const WEIXIN_CDN_UPLOAD_TIMEOUT_MS = 45_000;

/**
 * Computes AES-128-ECB ciphertext byte size with PKCS#7 padding (`ceil((size + 1) / 16) * 16`).
 */
export function aesEcbPaddedSize(plaintextSize: number): number {
  if (!Number.isInteger(plaintextSize) || plaintextSize < 0) {
    throw new RangeError(`Invalid plaintext size: ${plaintextSize}`);
  }
  return Math.ceil((plaintextSize + 1) / 16) * 16;
}

export function encryptAes128Ecb(plaintext: Buffer, key: Buffer): Buffer {
  if (key.byteLength !== 16) {
    throw new RangeError(
      `AES-128-ECB key must be 16 bytes, got ${key.byteLength}`,
    );
  }
  const cipher = createCipheriv("aes-128-ecb", key, null);
  return Buffer.concat([cipher.update(plaintext), cipher.final()]);
}

export function decryptAes128Ecb(ciphertext: Buffer, key: Buffer): Buffer {
  if (key.byteLength !== 16) {
    throw new RangeError(
      `AES-128-ECB key must be 16 bytes, got ${key.byteLength}`,
    );
  }
  if (ciphertext.byteLength === 0 || ciphertext.byteLength % 16 !== 0) {
    throw new RangeError(
      `AES-128-ECB ciphertext length (${ciphertext.byteLength}) must be a positive multiple of 16`,
    );
  }
  const decipher = createDecipheriv("aes-128-ecb", key, null);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

/**
 * Parses an inbound Weixin AES-128 key into a 16-byte `Buffer`:
 * 1. `aesKeyHex`: 32-char hex string (`image_item.aeskey`) -> 16 raw bytes.
 * 2. `aesKeyBase64`: base64 string (`media.aes_key`) -> either:
 *    - 16 raw bytes (base64 of 16 binary bytes), OR
 *    - 32 ASCII hex chars (base64 of 32 hex chars) -> 16 raw bytes.
 */
export function parseInboundAesKey(params: {
  aesKeyHex?: string;
  aesKeyBase64?: string;
}): Buffer {
  const hexRaw = params.aesKeyHex?.trim();
  if (hexRaw) {
    if (!/^[0-9a-fA-F]{32}$/.test(hexRaw)) {
      throw new Error(
        `Invalid inbound hex aeskey length/characters (expected 32 hex chars)`,
      );
    }
    return Buffer.from(hexRaw, "hex");
  }

  const b64Raw = params.aesKeyBase64?.trim();
  if (!b64Raw) {
    throw new Error("Missing inbound AES key");
  }
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(b64Raw) || b64Raw.length % 4 !== 0) {
    throw new Error("Invalid base64 encoding for inbound aes_key");
  }
  const decoded = Buffer.from(b64Raw, "base64");
  if (decoded.byteLength === 16) {
    return decoded;
  }
  if (decoded.byteLength === 32) {
    const ascii = decoded.toString("ascii");
    if (/^[0-9a-fA-F]{32}$/.test(ascii)) {
      return Buffer.from(ascii, "hex");
    }
  }
  throw new Error(
    `aes_key must decode to 16 raw bytes or 32-char hex string, got ${decoded.byteLength} bytes`,
  );
}

/**
 * Encodes an outbound 16-byte key (or 32-char hex string) for `sendmessage`'s `media.aes_key`:
 * iLink outbound protocol requires base64 of the 32-character lowercase hex ASCII string,
 * NOT base64 of the 16 raw binary bytes.
 */
export function encodeOutboundAesKeyBase64(keyOrHex: Buffer | string): string {
  const hexStr =
    typeof keyOrHex === "string"
      ? keyOrHex.trim().toLowerCase()
      : keyOrHex.toString("hex").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(hexStr)) {
    throw new Error("Outbound AES key must be 16 bytes (32 hex chars)");
  }
  return Buffer.from(hexStr, "utf-8").toString("base64");
}

export function detectImageMimeFromMagicBytes(buf: Buffer): string | null {
  if (buf.byteLength >= 8) {
    if (
      buf[0] === 0x89 &&
      buf[1] === 0x50 &&
      buf[2] === 0x4e &&
      buf[3] === 0x47 &&
      buf[4] === 0x0d &&
      buf[5] === 0x0a &&
      buf[6] === 0x1a &&
      buf[7] === 0x0a
    ) {
      return "image/png";
    }
  }
  if (buf.byteLength >= 3) {
    if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) {
      return "image/jpeg";
    }
  }
  if (buf.byteLength >= 6) {
    const sig = buf.subarray(0, 6).toString("ascii");
    if (sig === "GIF87a" || sig === "GIF89a") {
      return "image/gif";
    }
  }
  if (buf.byteLength >= 12) {
    const riff = buf.subarray(0, 4).toString("ascii");
    const webp = buf.subarray(8, 12).toString("ascii");
    if (riff === "RIFF" && webp === "WEBP") {
      return "image/webp";
    }
  }
  if (buf.byteLength >= 2 && buf[0] === 0x42 && buf[1] === 0x4d) {
    return "image/bmp";
  }
  return null;
}

const EXTENSION_TO_MIME: Readonly<Record<string, string>> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".bmp": "image/bmp",
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".csv": "text/csv",
  ".json": "application/json",
  ".zip": "application/zip",
  ".doc": "application/msword",
  ".docx":
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xls": "application/vnd.ms-excel",
  ".xlsx":
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".ppt": "application/vnd.ms-powerpoint",
  ".pptx":
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".mp4": "video/mp4",
  ".mov": "video/quicktime",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
};

export function inferMimeFromFilename(filename: string): string {
  const ext = path.extname(filename.trim()).toLowerCase();
  return EXTENSION_TO_MIME[ext] ?? "application/octet-stream";
}

export function isImageAttachment(filename: string, mimeType?: string): boolean {
  if (mimeType && mimeType.trim().toLowerCase().startsWith("image/")) {
    return true;
  }
  const inferred = inferMimeFromFilename(filename);
  return inferred.startsWith("image/");
}

export function normalizeAndValidateCdnBaseUrl(
  rawCdnBaseUrl: string | undefined,
  allowedCdnDomains: readonly string[] = OFFICIAL_WEIXIN_CDN_DOMAINS,
): string {
  const candidate = (
    rawCdnBaseUrl?.trim() || ILINK_DEFAULT_CDN_BASE_URL
  ).replace(/\/+$/, "");
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch (err) {
    throw new SafeFetchError("invalid_url", `Invalid Weixin CDN base URL: ${candidate}`, {
      cause: err,
    });
  }
  validateSafeFetchTargetUrl(parsed, allowedCdnDomains, false, true);
  return parsed.toString().replace(/\/+$/, "");
}

export function resolveValidatedCdnDownloadUrl(params: {
  encryptedQueryParam?: string;
  fullUrl?: string;
  cdnBaseUrl?: string;
  allowedCdnDomains?: readonly string[];
}): string {
  const allowedDomains = params.allowedCdnDomains ?? OFFICIAL_WEIXIN_CDN_DOMAINS;
  const full = params.fullUrl?.trim();
  if (full) {
    let parsed: URL;
    try {
      parsed = new URL(full);
    } catch (err) {
      throw new SafeFetchError("invalid_url", "Invalid Weixin CDN full_url", {
        cause: err,
      });
    }
    validateSafeFetchTargetUrl(parsed, allowedDomains, false, true);
    return parsed.toString();
  }

  const eqp = params.encryptedQueryParam?.trim();
  if (!eqp) {
    throw new SafeFetchError(
      "invalid_url",
      "Missing Weixin CDN encrypt_query_param or full_url",
    );
  }
  const base = normalizeAndValidateCdnBaseUrl(params.cdnBaseUrl, allowedDomains);
  const urlStr = `${base}/download?encrypted_query_param=${encodeURIComponent(eqp)}`;
  const parsed = new URL(urlStr);
  validateSafeFetchTargetUrl(parsed, allowedDomains, false, true);
  return parsed.toString();
}

export function resolveValidatedCdnUploadUrl(params: {
  uploadParam?: string;
  uploadFullUrl?: string;
  filekey: string;
  cdnBaseUrl?: string;
  allowedCdnDomains?: readonly string[];
}): string {
  const allowedDomains = params.allowedCdnDomains ?? OFFICIAL_WEIXIN_CDN_DOMAINS;
  const full = params.uploadFullUrl?.trim();
  if (full) {
    let parsed: URL;
    try {
      parsed = new URL(full);
    } catch (err) {
      throw new SafeFetchError(
        "invalid_url",
        "Invalid Weixin CDN upload_full_url",
        { cause: err },
      );
    }
    validateSafeFetchTargetUrl(parsed, allowedDomains, false, true);
    return parsed.toString();
  }

  const uploadParam = params.uploadParam?.trim();
  if (!uploadParam) {
    throw new SafeFetchError(
      "invalid_url",
      "Missing Weixin CDN upload_param or upload_full_url",
    );
  }
  const base = normalizeAndValidateCdnBaseUrl(params.cdnBaseUrl, allowedDomains);
  const urlStr = `${base}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(params.filekey)}`;
  const parsed = new URL(urlStr);
  validateSafeFetchTargetUrl(parsed, allowedDomains, false, true);
  return parsed.toString();
}

export interface WeixinCdnDownloadOptions {
  cdnBaseUrl?: string;
  allowedCdnDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  timeoutMs?: number;
  chunkIdleTimeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

/**
 * Downloads and (when AES key is present) decrypts an inbound Weixin attachment in memory.
 * Never sends Bot or Service tokens to CDN. Enforces 20 MiB plaintext / 20 MiB + 16 ciphertext cap,
 * declared-vs-actual size verification, and image magic-byte verification.
 */
export async function downloadWeixinCdnAttachment(
  meta: WeixinPersistedAttachmentMeta,
  options: WeixinCdnDownloadOptions = {},
): Promise<{
  buffer: Buffer;
  filename: string;
  mimeType: string;
}> {
  if (
    meta.sizeBytes !== undefined &&
    Number.isFinite(meta.sizeBytes) &&
    meta.sizeBytes > MAX_ATTACHMENT_BYTES
  ) {
    throw new SafeFetchError(
      "size_exceeded",
      `Declared attachment size (${meta.sizeBytes}) exceeds 20 MiB limit`,
    );
  }

  const allowedCdnDomains =
    options.allowedCdnDomains ?? OFFICIAL_WEIXIN_CDN_DOMAINS;
  const downloadUrl = resolveValidatedCdnDownloadUrl({
    encryptedQueryParam: meta.encryptQueryParam,
    fullUrl: meta.fullUrl,
    cdnBaseUrl: options.cdnBaseUrl,
    allowedCdnDomains,
  });

  const hasAesKey = Boolean(meta.aesKeyHex?.trim() || meta.aesKeyBase64?.trim());
  const aesKey = hasAesKey
    ? parseInboundAesKey({
        aesKeyHex: meta.aesKeyHex,
        aesKeyBase64: meta.aesKeyBase64,
      })
    : null;

  const maxDownloadBytes = aesKey
    ? MAX_CIPHERTEXT_ATTACHMENT_BYTES
    : MAX_ATTACHMENT_BYTES;

  const expectedCiphertextBytes =
    aesKey &&
    meta.sizeBytes !== undefined &&
    Number.isFinite(meta.sizeBytes) &&
    meta.sizeBytes > 0
      ? meta.kind === "image"
        ? meta.sizeBytes
        : aesEcbPaddedSize(meta.sizeBytes)
      : undefined;

  const fetched = await safeFetchBuffer(downloadUrl, {
    allowedDomains: allowedCdnDomains,
    requireHttps: true,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    maxBytes: maxDownloadBytes,
    declaredSizeBytes: expectedCiphertextBytes ?? meta.sizeBytes,
    timeoutMs: options.timeoutMs ?? WEIXIN_CDN_DOWNLOAD_TIMEOUT_MS,
    chunkIdleTimeoutMs: options.chunkIdleTimeoutMs,
    // Strictly isolated CDN headers: no Authorization or X-Service-Token
    headers: {},
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });

  const plaintext = aesKey
    ? decryptAes128Ecb(fetched.buffer, aesKey)
    : fetched.buffer;

  if (plaintext.byteLength > MAX_ATTACHMENT_BYTES) {
    throw new SafeFetchError(
      "size_exceeded",
      `Decrypted attachment size (${plaintext.byteLength}) exceeds 20 MiB limit`,
    );
  }

  if (
    meta.kind === "file" &&
    meta.sizeBytes !== undefined &&
    Number.isFinite(meta.sizeBytes) &&
    meta.sizeBytes >= 0 &&
    plaintext.byteLength !== meta.sizeBytes
  ) {
    throw new SafeFetchError(
      "size_exceeded",
      `Decrypted file size (${plaintext.byteLength}) does not match declared file len (${meta.sizeBytes})`,
    );
  }

  let mimeType = meta.mimeType ?? inferMimeFromFilename(meta.filename);
  let filename = meta.filename;
  if (meta.kind === "image") {
    const detectedMime = detectImageMimeFromMagicBytes(plaintext);
    if (!detectedMime) {
      throw new Error(
        "Decrypted Weixin image payload does not match any supported image magic bytes",
      );
    }
    mimeType = detectedMime;
    const ext =
      detectedMime === "image/png"
        ? ".png"
        : detectedMime === "image/gif"
          ? ".gif"
          : detectedMime === "image/webp"
            ? ".webp"
            : detectedMime === "image/bmp"
              ? ".bmp"
              : ".jpg";
    if (!path.extname(filename)) {
      filename = `${filename}${ext}`;
    }
  }

  return {
    buffer: plaintext,
    filename,
    mimeType,
  };
}

export interface WeixinUploadedMediaDescriptor {
  filekey: string;
  downloadEncryptedQueryParam: string;
  aeskeyHex: string;
  aeskeyBase64: string;
  rawsize: number;
  filesize: number;
  rawfilemd5: string;
  mediaType: number;
}

export interface UploadBufferToWeixinCdnOptions extends WeixinHttpCommonOptions {
  buffer: Buffer;
  toUserId: string;
  mediaType: number;
  cdnBaseUrl?: string;
  allowedCdnDomains?: readonly string[];
  maxUploadAttempts?: number;
  precomputedKeys?: {
    filekey?: string;
    aesKey?: Buffer;
  };
}

async function sleepWithAbort(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) {
    return;
  }
  if (signal?.aborted) {
    throw new Error("Aborted");
  }
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error("Aborted"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Encrypts an in-memory `Buffer` with AES-128-ECB, requests an upload URL from iLink,
 * and uploads the ciphertext to Weixin CDN.
 *
 * Across CDN upload retries, reuses the exact same `filekey`, `aesKey`, and `ciphertext`
 * so partial retries never generate duplicate encryption state.
 */
export async function uploadBufferToWeixinCdn(
  options: UploadBufferToWeixinCdnOptions,
): Promise<WeixinUploadedMediaDescriptor> {
  const { buffer, toUserId } = options;
  if (buffer.byteLength === 0) {
    throw new Error("Cannot upload empty media buffer to Weixin CDN");
  }
  if (buffer.byteLength > MAX_ATTACHMENT_BYTES) {
    throw new SafeFetchError(
      "size_exceeded",
      `Outbound attachment size (${buffer.byteLength}) exceeds 20 MiB limit`,
    );
  }

  const rawsize = buffer.byteLength;
  const rawfilemd5 = createHash("md5").update(buffer).digest("hex");
  const filesize = aesEcbPaddedSize(rawsize);
  const filekey =
    options.precomputedKeys?.filekey ?? randomBytes(16).toString("hex");
  const aesKey = options.precomputedKeys?.aesKey ?? randomBytes(16);
  const aeskeyHex = aesKey.toString("hex").toLowerCase();
  const aeskeyBase64 = encodeOutboundAesKeyBase64(aeskeyHex);
  const ciphertext = encryptAes128Ecb(buffer, aesKey);

  const uploadUrlResp = await getWeixinUploadUrl({
    baseUrl: options.baseUrl,
    botToken: options.botToken,
    timeoutMs: options.timeoutMs,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    allowedDomains: options.allowedDomains,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    filekey,
    media_type: options.mediaType,
    to_user_id: toUserId,
    rawsize,
    rawfilemd5,
    filesize,
    no_need_thumb: true,
    aeskey: aeskeyHex,
  });

  const allowedCdnDomains =
    options.allowedCdnDomains ?? OFFICIAL_WEIXIN_CDN_DOMAINS;
  const cdnUploadUrl = resolveValidatedCdnUploadUrl({
    uploadParam: uploadUrlResp.upload_param,
    uploadFullUrl: uploadUrlResp.upload_full_url,
    filekey,
    cdnBaseUrl: options.cdnBaseUrl,
    allowedCdnDomains,
  });

  await validateSafeTargetUrlWithDns(new URL(cdnUploadUrl), {
    allowedDomains: allowedCdnDomains,
    requireHttps: true,
    isRedirect: false,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
  });

  const maxAttempts = Math.max(
    1,
    options.maxUploadAttempts ?? WEIXIN_CDN_UPLOAD_MAX_RETRIES,
  );
  let lastError: unknown;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    if (options.signal?.aborted) {
      throw new Error("Weixin CDN upload aborted");
    }
    try {
      const res = await safeFetchResponse(cdnUploadUrl, {
        method: "POST",
        // Isolated CDN headers: never include Authorization or X-Service-Token
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Length": String(ciphertext.byteLength),
        },
        body: ciphertext,
        allowedDomains: allowedCdnDomains,
        requireHttps: true,
        resolveHostIps: options.resolveHostIps,
        checkResolvedIps: options.checkResolvedIps,
        timeoutMs: options.timeoutMs ?? WEIXIN_CDN_UPLOAD_TIMEOUT_MS,
        signal: options.signal,
        fetchImpl: options.fetchImpl,
      });

      if (res.status === 429) {
        const retryAfterMs = parseRetryAfterMs(res.headers.get("retry-after"));
        await res.body?.cancel().catch(() => undefined);
        throw new WeixinApiError({
          kind: "rate_limited",
          endpoint: "cdn/upload",
          message: "Weixin CDN upload rate limited (HTTP 429)",
          status: 429,
          retryAfterMs,
          requestSent: true,
        });
      }

      if (res.status >= 400 && res.status < 500) {
        const errMsg =
          res.headers.get("x-error-message") ?? (await res.text());
        throw new WeixinApiError({
          kind: "http_error",
          endpoint: "cdn/upload",
          message: `Weixin CDN upload client error HTTP ${res.status}: ${errMsg.slice(0, 200)}`,
          status: res.status,
          requestSent: true,
        });
      }

      if (!res.ok) {
        const errMsg =
          res.headers.get("x-error-message") ?? (await res.text());
        throw new Error(
          `Weixin CDN upload server error HTTP ${res.status}: ${errMsg.slice(0, 200)}`,
        );
      }

      const downloadParam = res.headers.get("x-encrypted-param")?.trim();
      await res.body?.cancel().catch(() => undefined);
      if (!downloadParam) {
        throw new Error(
          "Weixin CDN upload response is missing x-encrypted-param header",
        );
      }

      return {
        filekey,
        downloadEncryptedQueryParam: downloadParam,
        aeskeyHex,
        aeskeyBase64,
        rawsize,
        filesize,
        rawfilemd5,
        mediaType: options.mediaType,
      };
    } catch (err) {
      lastError = err;
      if (
        err instanceof WeixinApiError &&
        (err.kind === "http_error" || err.kind === "rate_limited")
      ) {
        throw err;
      }
      if ( err instanceof SafeFetchError && err.code !== "timeout") {
        throw err;
      }
      if (attempt >= maxAttempts || options.signal?.aborted) {
        break;
      }
      await sleepWithAbort(200 * attempt, options.signal);
    }
  }

  throw lastError instanceof Error
    ? lastError
    : new Error(`Weixin CDN upload failed: ${String(lastError)}`);
}

export { UploadMediaType };
