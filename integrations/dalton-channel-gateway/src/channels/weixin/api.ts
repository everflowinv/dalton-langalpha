/**
 * Weixin iLink Bot HTTP JSON client with header isolation, official host validation,
 * lossless uint64 ID parsing, and business ret/errcode classification.
 *
 * Adapted from `@tencent-weixin/openclaw-weixin` (`src/api/api.ts`, MIT, Copyright (c) 2026 Tencent).
 */

import crypto from "node:crypto";
import {
  isLeaseSafetyMarginPauseError,
  isStaleGatewayEpochError,
} from "../../gateway-lease.js";
import {
  type HostIpResolver,
  SafeFetchError,
  safeFetchResponse,
  validateSafeFetchTargetUrl,
  validateSafeTargetUrlWithDns,
} from "../../shared/safe-fetch.js";
import {
  type BaseInfo,
  type GetConfigReq,
  type GetConfigResp,
  type GetUpdatesReq,
  type GetUpdatesResp,
  type GetUploadUrlReq,
  type GetUploadUrlResp,
  ILINK_CHANNEL_VERSION,
  ILINK_DEFAULT_BASE_URL,
  ILINK_DEFAULT_BOT_TYPE,
  OFFICIAL_ILINK_API_DOMAINS,
  type QrCodeStartResp,
  type QrCodeStatusResp,
  type SendMessageReq,
  type SendMessageResp,
  type SendTypingReq,
  type SendTypingResp,
  SESSION_EXPIRED_ERRCODE,
} from "./types.js";

export const DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000;
export const DEFAULT_API_TIMEOUT_MS = 15_000;
export const DEFAULT_QR_POLL_TIMEOUT_MS = 35_000;
export const DEFAULT_RETRY_AFTER_MS = 5_000;
export const MAX_RETRY_AFTER_MS = 60_000;

export type WeixinApiErrorKind =
  | "session_expired"
  | "rate_limited"
  | "business_error"
  | "http_error"
  | "network_error";

export class WeixinApiError extends Error {
  readonly kind: WeixinApiErrorKind;
  readonly endpoint: string;
  readonly status?: number;
  readonly ret?: number;
  readonly errcode?: number;
  readonly retryAfterMs?: number;
  readonly requestSent: boolean;

  constructor(params: {
    kind: WeixinApiErrorKind;
    endpoint: string;
    message: string;
    status?: number;
    ret?: number;
    errcode?: number;
    retryAfterMs?: number;
    requestSent: boolean;
    cause?: unknown;
  }) {
    super(params.message, params.cause ? { cause: params.cause } : undefined);
    this.name = "WeixinApiError";
    this.kind = params.kind;
    this.endpoint = params.endpoint;
    this.status = params.status;
    this.ret = params.ret;
    this.errcode = params.errcode;
    this.retryAfterMs = params.retryAfterMs;
    this.requestSent = params.requestSent;
  }
}

const UINT64_ID_JSON_RE =
  /("(?:message_id|msg_id|svr_id)"\s*:\s*)(-?\d+)(?=\s*[,}\]\r\n])|"(?:\\.|[^"\\])*"/g;

/**
 * Parses iLink JSON while converting uint64 integer literals on `message_id`,
 * `msg_id`, and `svr_id` into exact decimal strings before `JSON.parse`.
 */
export function parseWeixinApiJson<T>(rawText: string): T {
  const normalized = rawText.replace(
    UINT64_ID_JSON_RE,
    (match, keyPrefix?: string, intLiteral?: string) => {
      if (keyPrefix !== undefined && intLiteral !== undefined) {
        return `${keyPrefix}"${intLiteral}"`;
      }
      return match;
    },
  );
  return JSON.parse(normalized) as T;
}

export function buildBaseInfo(): BaseInfo {
  return { channel_version: ILINK_CHANNEL_VERSION };
}

export function randomWechatUin(): string {
  const uint32 = crypto.randomBytes(4).readUInt32BE(0);
  return Buffer.from(String(uint32), "utf-8").toString("base64");
}

/**
 * Builds isolated HTTP headers for iLink API requests.
 * Never includes backend `X-Service-Token` or `X-User-Id`.
 */
export function buildIlinkApiHeaders(opts: {
  botToken?: string;
  body?: string;
  extraHeaders?: Record<string, string>;
}): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    AuthorizationType: "ilink_bot_token",
    "X-WECHAT-UIN": randomWechatUin(),
  };
  if (opts.body !== undefined) {
    headers["Content-Length"] = String(Buffer.byteLength(opts.body, "utf-8"));
  }
  if (opts.botToken?.trim()) {
    headers.Authorization = `Bearer ${opts.botToken.trim()}`;
  }
  if (opts.extraHeaders) {
    for (const [k, v] of Object.entries(opts.extraHeaders)) {
      const lower = k.trim().toLowerCase();
      if (lower === "x-service-token" || lower === "x-user-id") {
        continue;
      }
      headers[k] = v;
    }
  }
  return headers;
}

export function isSessionExpiredCode(
  ret?: number,
  errcode?: number,
): boolean {
  return (
    ret === SESSION_EXPIRED_ERRCODE || errcode === SESSION_EXPIRED_ERRCODE
  );
}

export function parseRetryAfterMs(
  retryAfterHeader: string | null | undefined,
  defaultMs = DEFAULT_RETRY_AFTER_MS,
  maxMs = MAX_RETRY_AFTER_MS,
): number {
  const raw = retryAfterHeader?.trim();
  if (!raw) {
    return Math.min(maxMs, Math.max(100, defaultMs));
  }
  if (/^\d+$/.test(raw)) {
    const seconds = Number.parseInt(raw, 10);
    return Math.min(maxMs, Math.max(100, seconds * 1000));
  }
  const parsedDate = Date.parse(raw);
  if (Number.isFinite(parsedDate)) {
    const deltaMs = parsedDate - Date.now();
    if (deltaMs > 0) {
      return Math.min(maxMs, Math.max(100, deltaMs));
    }
    return 100;
  }
  return Math.min(maxMs, Math.max(100, defaultMs));
}

export function sanitizeIlinkEndpointForLog(endpoint: string): string {
  const qIdx = endpoint.indexOf("?");
  return qIdx >= 0 ? endpoint.slice(0, qIdx) : endpoint;
}

export function assertIlinkBusinessOk(
  payload: { ret?: unknown; errcode?: unknown; errmsg?: unknown },
  endpoint: string,
  requestSent = true,
): void {
  const safeEndpoint = sanitizeIlinkEndpointForLog(endpoint);
  const rawRet = payload.ret;
  const rawErrcode = payload.errcode;
  const ret =
    typeof rawRet === "number" && Number.isFinite(rawRet) ? rawRet : undefined;
  const errcode =
    typeof rawErrcode === "number" && Number.isFinite(rawErrcode)
      ? rawErrcode
      : undefined;
  const hasRetError = rawRet !== undefined && (ret === undefined || ret !== 0);
  const hasErrcodeError =
    rawErrcode !== undefined && (errcode === undefined || errcode !== 0);
  if (!hasRetError && !hasErrcodeError) {
    return;
  }
  const expired = isSessionExpiredCode(ret, errcode);
  const retLabel = ret !== undefined ? String(ret) : rawRet === undefined ? "0" : "non_numeric";
  const errcodeLabel =
    errcode !== undefined ? String(errcode) : rawErrcode === undefined ? "0" : "non_numeric";
  throw new WeixinApiError({
    kind: expired ? "session_expired" : "business_error",
    endpoint: safeEndpoint,
    // §4.3: never emit raw iLink errmsg text or unvalidated non-numeric fields into logs/errors
    message: `${safeEndpoint} business error: ret=${retLabel} errcode=${errcodeLabel}`,
    ret,
    errcode,
    requestSent,
  });
}

export function normalizeAndValidateIlinkBaseUrl(
  rawBaseUrl: string | undefined,
  allowedDomains: readonly string[] = OFFICIAL_ILINK_API_DOMAINS,
): string {
  const candidate = (rawBaseUrl?.trim() || ILINK_DEFAULT_BASE_URL).replace(
    /\/+$/,
    "",
  );
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch (err) {
    throw new SafeFetchError("invalid_url", `Invalid iLink base URL: ${candidate}`, {
      cause: err,
    });
  }
  validateSafeFetchTargetUrl(parsed, allowedDomains, false, true);
  return parsed.toString().replace(/\/+$/, "");
}

export async function resolveValidatedRedirectBaseUrl(params: {
  redirectHost?: string;
  baseUrl?: string;
  allowedDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
}): Promise<string> {
  const allowedDomains = params.allowedDomains ?? OFFICIAL_ILINK_API_DOMAINS;
  let candidateStr: string;
  const rawRedirect = params.redirectHost?.trim();
  if (rawRedirect) {
    candidateStr = /^https?:\/\//i.test(rawRedirect)
      ? rawRedirect
      : `https://${rawRedirect}`;
  } else {
    candidateStr = params.baseUrl?.trim() || ILINK_DEFAULT_BASE_URL;
  }
  let parsed: URL;
  try {
    parsed = new URL(candidateStr);
  } catch (err) {
    throw new SafeFetchError("invalid_url", "Invalid iLink redirect host URL", {
      cause: err,
    });
  }
  await validateSafeTargetUrlWithDns(parsed, {
    allowedDomains,
    requireHttps: true,
    isRedirect: true,
    resolveHostIps: params.resolveHostIps,
    checkResolvedIps: params.checkResolvedIps,
  });
  return parsed.toString().replace(/\/+$/, "");
}

export interface WeixinHttpCommonOptions {
  baseUrl?: string;
  botToken?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  dispatcher?: unknown;
  allowedDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  /** Synchronous lease check invoked after DNS resolution and immediately before HTTP dispatch (Round 3 Decision #1). */
  assertLeaseValid?: () => void;
}

async function executeIlinkJsonRequest<T extends { ret?: number; errcode?: number; errmsg?: string }>(params: {
  method: "GET" | "POST";
  endpoint: string;
  baseUrl?: string;
  botToken?: string;
  bodyObj?: unknown;
  extraHeaders?: Record<string, string>;
  timeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  dispatcher?: unknown;
  allowedDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  assertLeaseValid?: () => void;
}): Promise<T> {
  const safeEndpoint = sanitizeIlinkEndpointForLog(params.endpoint);
  const allowedDomains = params.allowedDomains ?? OFFICIAL_ILINK_API_DOMAINS;
  const validatedBase = normalizeAndValidateIlinkBaseUrl(
    params.baseUrl,
    allowedDomains,
  );
  const url = new URL(params.endpoint.replace(/^\/+/, ""), `${validatedBase}/`);
  await validateSafeTargetUrlWithDns(url, {
    allowedDomains,
    requireHttps: true,
    isRedirect: false,
    resolveHostIps: params.resolveHostIps,
    checkResolvedIps: params.checkResolvedIps,
  });
  // Round 3 Decision #1: re-verify local lease after DNS resolution and before building/dispatching HTTP request.
  params.assertLeaseValid?.();

  const bodyStr =
    params.bodyObj !== undefined ? JSON.stringify(params.bodyObj) : undefined;
  const headers = buildIlinkApiHeaders({
    botToken: params.botToken,
    body: bodyStr,
    extraHeaders: params.extraHeaders,
  });

  let requestDispatched = false;
  const wrappedFetch: typeof fetch = async (input, init) => {
    params.assertLeaseValid?.();
    requestDispatched = true;
    const fn = params.fetchImpl ?? globalThis.fetch;
    return fn(input, init);
  };

  let response: Response;
  try {
    response = await safeFetchResponse(url, {
      method: params.method,
      headers,
      body: bodyStr,
      allowedDomains,
      requireHttps: true,
      resolveHostIps: params.resolveHostIps,
      checkResolvedIps: params.checkResolvedIps,
      timeoutMs: params.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
      signal: params.signal,
      fetchImpl: wrappedFetch,
      dispatcher: params.dispatcher,
    });
  } catch (err) {
    if (
      err instanceof WeixinApiError ||
      isStaleGatewayEpochError(err) ||
      isLeaseSafetyMarginPauseError(err)
    ) {
      throw err;
    }
    throw new WeixinApiError({
      kind: "network_error",
      endpoint: safeEndpoint,
      message: `${safeEndpoint} network failure: ${err instanceof Error ? err.message : String(err)}`,
      requestSent: requestDispatched,
      cause: err,
    });
  }

  let rawText: string;
  try {
    rawText = await response.text();
  } catch (err) {
    throw new WeixinApiError({
      kind: "network_error",
      endpoint: safeEndpoint,
      message: `${safeEndpoint} response body read failed: ${err instanceof Error ? err.message : String(err)}`,
      status: response.status,
      requestSent: requestDispatched,
      cause: err,
    });
  }
  if (response.status === 429) {
    const retryAfterMs = parseRetryAfterMs(response.headers.get("retry-after"));
    throw new WeixinApiError({
      kind: "rate_limited",
      endpoint: safeEndpoint,
      message: `${safeEndpoint} rate limited (HTTP 429)`,
      status: 429,
      retryAfterMs,
      requestSent: requestDispatched,
    });
  }

  if (!response.ok) {
    throw new WeixinApiError({
      kind: "http_error",
      endpoint: safeEndpoint,
      // §4.3: never emit raw HTTP response body or query credentials into logs or error strings
      message: `${safeEndpoint} HTTP ${response.status}`,
      status: response.status,
      requestSent: requestDispatched,
    });
  }

  let parsed: T;
  try {
    parsed = parseWeixinApiJson<T>(rawText);
  } catch (err) {
    throw new WeixinApiError({
      kind: "http_error",
      endpoint: safeEndpoint,
      message: `${safeEndpoint} returned invalid JSON`,
      status: response.status,
      requestSent: requestDispatched,
      cause: err,
    });
  }

  if (
    parsed === null ||
    parsed === undefined ||
    typeof parsed !== "object" ||
    Array.isArray(parsed)
  ) {
    throw new WeixinApiError({
      kind: "http_error",
      endpoint: safeEndpoint,
      message: `${safeEndpoint} returned a non-object JSON body`,
      status: response.status,
      requestSent: requestDispatched,
    });
  }

  assertIlinkBusinessOk(parsed, safeEndpoint, requestDispatched);
  return parsed;
}

export async function fetchWeixinQrCode(
  options: WeixinHttpCommonOptions & { botType?: string } = {},
): Promise<QrCodeStartResp> {
  const botType = options.botType?.trim() || ILINK_DEFAULT_BOT_TYPE;
  return executeIlinkJsonRequest<QrCodeStartResp>({
    method: "GET",
    endpoint: `ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(botType)}`,
    baseUrl: options.baseUrl,
    timeoutMs: options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    dispatcher: options.dispatcher,
    allowedDomains: options.allowedDomains,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    assertLeaseValid: options.assertLeaseValid,
  });
}

export async function pollWeixinQrStatus(
  options: WeixinHttpCommonOptions & { qrcode: string },
): Promise<QrCodeStatusResp> {
  const qrcode = options.qrcode.trim();
  if (!qrcode) {
    throw new Error("qrcode is required for pollWeixinQrStatus");
  }
  try {
    return await executeIlinkJsonRequest<QrCodeStatusResp>({
      method: "GET",
      endpoint: `ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(qrcode)}`,
      baseUrl: options.baseUrl,
      extraHeaders: { "iLink-App-ClientVersion": "1" },
      timeoutMs: options.timeoutMs ?? DEFAULT_QR_POLL_TIMEOUT_MS,
      signal: options.signal,
      fetchImpl: options.fetchImpl,
      dispatcher: options.dispatcher,
      allowedDomains: options.allowedDomains,
      resolveHostIps: options.resolveHostIps,
      checkResolvedIps: options.checkResolvedIps,
      assertLeaseValid: options.assertLeaseValid,
    });
  } catch (err) {
    if (
      err instanceof WeixinApiError &&
      err.kind === "network_error" &&
      err.cause instanceof SafeFetchError &&
      err.cause.code === "timeout" &&
      !options.signal?.aborted
    ) {
      return { ret: 0, status: "wait" };
    }
    throw err;
  }
}

export async function submitWeixinQrVerifyCode(
  options: WeixinHttpCommonOptions & { qrcode: string; verifyCode: string },
): Promise<QrCodeStatusResp> {
  const qrcode = options.qrcode.trim();
  const verifyCode = options.verifyCode.trim();
  if (!qrcode || !verifyCode) {
    throw new Error("qrcode and verifyCode are required");
  }
  return executeIlinkJsonRequest<QrCodeStatusResp>({
    method: "POST",
    endpoint: "ilink/bot/verify_qrcode_code",
    baseUrl: options.baseUrl,
    extraHeaders: { "iLink-App-ClientVersion": "1" },
    bodyObj: {
      qrcode,
      verify_code: verifyCode,
      base_info: buildBaseInfo(),
    },
    timeoutMs: options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    dispatcher: options.dispatcher,
    allowedDomains: options.allowedDomains,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    assertLeaseValid: options.assertLeaseValid,
  });
}

export async function getWeixinUpdates(
  options: WeixinHttpCommonOptions & { getUpdatesBuf?: string },
): Promise<GetUpdatesResp> {
  const reqBody: GetUpdatesReq = {
    get_updates_buf: options.getUpdatesBuf ?? "",
    base_info: buildBaseInfo(),
  };
  try {
    return await executeIlinkJsonRequest<GetUpdatesResp>({
      method: "POST",
      endpoint: "ilink/bot/getupdates",
      baseUrl: options.baseUrl,
      botToken: options.botToken,
      bodyObj: reqBody,
      timeoutMs: options.timeoutMs ?? DEFAULT_LONG_POLL_TIMEOUT_MS,
      signal: options.signal,
      fetchImpl: options.fetchImpl,
      dispatcher: options.dispatcher,
      allowedDomains: options.allowedDomains,
      resolveHostIps: options.resolveHostIps,
      checkResolvedIps: options.checkResolvedIps,
      assertLeaseValid: options.assertLeaseValid,
    });
  } catch (err) {
    if (
      err instanceof WeixinApiError &&
      err.kind === "network_error" &&
      err.cause instanceof SafeFetchError &&
      err.cause.code === "timeout" &&
      !options.signal?.aborted
    ) {
      return {
        ret: 0,
        msgs: [],
        get_updates_buf: options.getUpdatesBuf ?? "",
      };
    }
    throw err;
  }
}

export async function getWeixinUploadUrl(
  options: WeixinHttpCommonOptions & GetUploadUrlReq,
): Promise<GetUploadUrlResp> {
  const {
    baseUrl,
    botToken,
    timeoutMs,
    signal,
    fetchImpl,
    dispatcher,
    allowedDomains,
    resolveHostIps,
    checkResolvedIps,
    assertLeaseValid,
    ...uploadFields
  } = options;
  return executeIlinkJsonRequest<GetUploadUrlResp>({
    method: "POST",
    endpoint: "ilink/bot/getuploadurl",
    baseUrl,
    botToken,
    bodyObj: {
      ...uploadFields,
      base_info: buildBaseInfo(),
    },
    timeoutMs: timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    signal,
    fetchImpl,
    dispatcher,
    allowedDomains,
    resolveHostIps,
    checkResolvedIps,
    assertLeaseValid,
  });
}

export async function sendWeixinMessage(
  options: WeixinHttpCommonOptions & { body: SendMessageReq },
): Promise<SendMessageResp> {
  return executeIlinkJsonRequest<SendMessageResp>({
    method: "POST",
    endpoint: "ilink/bot/sendmessage",
    baseUrl: options.baseUrl,
    botToken: options.botToken,
    bodyObj: {
      ...options.body,
      base_info: options.body.base_info ?? buildBaseInfo(),
    },
    timeoutMs: options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    dispatcher: options.dispatcher,
    allowedDomains: options.allowedDomains,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    assertLeaseValid: options.assertLeaseValid,
  });
}

export async function getWeixinConfig(
  options: WeixinHttpCommonOptions & {
    ilinkUserId: string;
    contextToken?: string;
  },
): Promise<GetConfigResp> {
  const reqBody: GetConfigReq = {
    ilink_user_id: options.ilinkUserId.trim(),
    ...(options.contextToken?.trim()
      ? { context_token: options.contextToken.trim() }
      : {}),
    base_info: buildBaseInfo(),
  };
  return executeIlinkJsonRequest<GetConfigResp>({
    method: "POST",
    endpoint: "ilink/bot/getconfig",
    baseUrl: options.baseUrl,
    botToken: options.botToken,
    bodyObj: reqBody,
    timeoutMs: options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    dispatcher: options.dispatcher,
    allowedDomains: options.allowedDomains,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    assertLeaseValid: options.assertLeaseValid,
  });
}

export async function sendWeixinTyping(
  options: WeixinHttpCommonOptions & {
    ilinkUserId: string;
    typingTicket: string;
    status: number;
  },
): Promise<SendTypingResp> {
  const reqBody: SendTypingReq = {
    ilink_user_id: options.ilinkUserId.trim(),
    typing_ticket: options.typingTicket.trim(),
    status: options.status,
    base_info: buildBaseInfo(),
  };
  return executeIlinkJsonRequest<SendTypingResp>({
    method: "POST",
    endpoint: "ilink/bot/sendtyping",
    baseUrl: options.baseUrl,
    botToken: options.botToken,
    bodyObj: reqBody,
    timeoutMs: options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    dispatcher: options.dispatcher,
    allowedDomains: options.allowedDomains,
    resolveHostIps: options.resolveHostIps,
    checkResolvedIps: options.checkResolvedIps,
    assertLeaseValid: options.assertLeaseValid,
  });
}
