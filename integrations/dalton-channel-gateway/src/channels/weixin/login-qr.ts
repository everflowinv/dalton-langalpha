// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import { randomUUID } from "node:crypto";
import {
  type BackendClient,
  BackendHttpError,
  type WeixinLoginSessionGetResponse,
} from "../../backend-client.js";
import {
  isLeaseSafetyMarginPauseError,
  isStaleGatewayEpochError,
  StaleGatewayEpochError,
} from "../../gateway-lease.js";
import type { HostIpResolver } from "../../shared/safe-fetch.js";
import {
  fetchWeixinQrCode,
  normalizeAndValidateIlinkBaseUrl,
  pollWeixinQrStatus,
  resolveValidatedRedirectBaseUrl,
  submitWeixinQrVerifyCode,
} from "./api.js";
import {
  ILINK_DEFAULT_BASE_URL,
  ILINK_DEFAULT_BOT_TYPE,
  ILINK_DEFAULT_CDN_BASE_URL,
  OFFICIAL_ILINK_API_DOMAINS,
  type QrCodeStatusResp,
  type WeixinQrPollStatus,
  type WeixinQrPublicResult,
} from "./types.js";

const QR_SESSION_TTL_MS = 5 * 60_000;

export type WeixinQrPublicStatus =
  | "wait"
  | "scaned"
  | "verify_code"
  | "confirmed"
  | "expired"
  | "superseded";

export type WeixinQrPublicStartResponse = WeixinQrPublicResult;
export type WeixinQrPublicWaitResponse = WeixinQrPublicResult & {
  bot_generation?: number;
};

export interface ConfirmedWeixinAccount {
  accountId: string;
  ilinkBotId: string;
  botToken: string;
  baseUrl: string;
  cdnBaseUrl: string;
  botGeneration: number;
  getUpdatesBuf: string;
  ownerUserId: string;
  accountStatus?: string | null;
  accountsRevision?: number | null;
  ilinkUserId?: string | null;
  ilinkUsername?: string | null;
}

export interface WeixinQrLoginManagerOptions {
  backend: BackendClient;
  baseUrl?: string;
  cdnBaseUrl?: string;
  botType?: string;
  allowedApiDomains?: readonly string[];
  resolveHostIps?: HostIpResolver;
  checkResolvedIps?: boolean;
  fetchImpl?: typeof fetch;
  /** Dedicated control HTTP dispatcher (isolated from long-poll pool, §3.3). */
  getDispatcher?: () => unknown;
  /** Synchronous local lease check before contacting iLink (Decision #1). */
  isLeaseValid?: () => boolean;
  /**
   * Synchronous lease gate differentiating expired lease (`StaleGatewayEpochError`)
   * from `<5s` safety margin pause (`LeaseSafetyMarginPauseError`, Round 3 Decision #1).
   */
  assertLeaseValid?: (operation: string) => void;
  onAccountConfirmed?: (account: ConfirmedWeixinAccount) => Promise<void> | void;
}

function isDomainAllowed(
  hostname: string,
  allowedDomains: readonly string[],
): boolean {
  const host = hostname.toLowerCase();
  return allowedDomains.some((pattern) => {
    const clean = pattern.toLowerCase().trim().replace(/^\*\./, "");
    return host === clean || host.endsWith(`.${clean}`);
  });
}

/**
 * Allowlist of Weixin QR login error codes mapped to fixed, safe public messages
 * (Round 3 Decision #10 / §4.3). Arbitrary backend or iLink free-text `message`
 * values are NEVER forwarded to HTTP responses.
 */
export const SAFE_WEIXIN_QR_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  unauthorized: "Missing or invalid authenticated user.",
  invalid_request: "Invalid Weixin QR login request.",
  forbidden: "Weixin QR login session belongs to a different user.",
  channel_disabled: "Weixin channel is currently disabled.",
  weixin_channel_disabled: "Weixin channel is currently disabled.",
  login_session_revoked: "Weixin QR login session was superseded or expired.",
  ilink_user_already_bound: "This WeChat account is already bound to another user.",
  weixin_capacity_reached: "Weixin active account capacity limit has been reached.",
  roster_lookup_error: "Unable to verify team roster right now; please retry shortly.",
  member_not_in_roster: "Your account is not eligible for Weixin channel access.",
  external_member_forbidden: "External members are not permitted to bind Weixin.",
  ilink_invalid_qrcode: "Weixin iLink did not return a valid QR code.",
  ilink_invalid_confirm: "Weixin login confirmation response was incomplete.",
  gateway_lease_paused: "Gateway lease is renewing; please retry shortly.",
  qr_login_error: "Weixin QR login request failed.",
};

export function resolveSafeQrError(
  rawCode: string | undefined | null,
  fallbackCode = "qr_login_error",
): { code: string; message: string } {
  const cleanCode = rawCode?.trim();
  if (cleanCode && cleanCode in SAFE_WEIXIN_QR_ERROR_MESSAGES) {
    return {
      code: cleanCode,
      message: SAFE_WEIXIN_QR_ERROR_MESSAGES[cleanCode]!,
    };
  }
  const safeFallback =
    fallbackCode in SAFE_WEIXIN_QR_ERROR_MESSAGES
      ? fallbackCode
      : "qr_login_error";
  return {
    code: safeFallback,
    message: SAFE_WEIXIN_QR_ERROR_MESSAGES[safeFallback]!,
  };
}

function extractBackendErrorCode(body: unknown): string | undefined {
  if (!body || typeof body !== "object") {
    return undefined;
  }
  const obj = body as Record<string, unknown>;
  const detail =
    obj.detail && typeof obj.detail === "object"
      ? (obj.detail as Record<string, unknown>)
      : null;
  if (typeof obj.code === "string" && obj.code.trim()) {
    return obj.code.trim();
  }
  if (typeof detail?.code === "string" && detail.code.trim()) {
    return detail.code.trim();
  }
  return undefined;
}

export function sanitizeQrCodeImageContent(
  rawContent: string | undefined | null,
  allowedDomains: readonly string[] = OFFICIAL_ILINK_API_DOMAINS,
): string {
  if (!rawContent || typeof rawContent !== "string") return "";
  const trimmed = rawContent.trim();
  if (!trimmed) return "";
  if (trimmed.startsWith("data:image/")) {
    return trimmed;
  }
  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    try {
      const parsed = new URL(trimmed);
      if (parsed.protocol !== "https:") return "";
      if (parsed.username || parsed.password) return "";
      if (
        allowedDomains.length > 0 &&
        !isDomainAllowed(parsed.hostname, allowedDomains)
      ) {
        return "";
      }
      for (const key of [...parsed.searchParams.keys()]) {
        const lower = key.toLowerCase();
        if (
          lower.includes("token") ||
          lower.includes("secret") ||
          lower.includes("key") ||
          lower.includes("auth") ||
          lower.includes("password")
        ) {
          parsed.searchParams.delete(key);
        }
      }
      parsed.hash = "";
      return parsed.toString();
    } catch {
      return "";
    }
  }
  return trimmed;
}

export function shapePublicQrStartResponse(params: {
  sessionKey: string;
  status?: WeixinQrPublicStatus;
  qrcodeImgContent?: string | null;
  expiresAt?: string | null;
  loginVersion?: number;
  message?: string | null;
  allowedDomains?: readonly string[];
}): WeixinQrPublicStartResponse {
  const safeQr = sanitizeQrCodeImageContent(
    params.qrcodeImgContent,
    params.allowedDomains,
  );
  return {
    session_key: params.sessionKey,
    status: params.status ?? "wait",
    ...(safeQr ? { qrcode_img_content: safeQr, qrcode_url: safeQr } : {}),
    ...(params.expiresAt ? { expires_at: params.expiresAt } : {}),
    ...(typeof params.loginVersion === "number"
      ? { login_version: params.loginVersion }
      : {}),
    ...(params.message ? { message: params.message } : {}),
  };
}

export function shapePublicQrWaitResponse(params: {
  sessionKey: string;
  status: WeixinQrPublicStatus;
  connected: boolean;
  alreadyConnected?: boolean;
  accountId?: string | null;
  accountStatus?: string | null;
  accountsRevision?: number | null;
  pendingVerification?: boolean;
  revoked?: boolean;
  code?: string | null;
  ilinkBotId?: string | null;
  ilinkUserId?: string | null;
  ilinkUsername?: string | null;
  botGeneration?: number | null;
  expiresAt?: string | null;
  message?: string | null;
  qrcodeImgContent?: string | null;
  allowedDomains?: readonly string[];
}): WeixinQrPublicWaitResponse {
  const keepQr =
    params.status === "wait" ||
    params.status === "scaned" ||
    params.status === "verify_code";
  const safeQr = keepQr
    ? sanitizeQrCodeImageContent(params.qrcodeImgContent, params.allowedDomains)
    : "";
  return {
    session_key: params.sessionKey,
    status: params.status,
    connected: Boolean(params.connected),
    ...(params.alreadyConnected !== undefined
      ? { already_connected: params.alreadyConnected }
      : {}),
    ...(params.accountId ? { account_id: params.accountId } : {}),
    ...(params.accountStatus ? { account_status: params.accountStatus } : {}),
    ...(typeof params.accountsRevision === "number"
      ? { accounts_revision: params.accountsRevision }
      : {}),
    ...(params.pendingVerification !== undefined
      ? { pending_verification: params.pendingVerification }
      : {}),
    ...(params.revoked !== undefined ? { revoked: params.revoked } : {}),
    ...(params.code ? { code: params.code } : {}),
    ...(params.ilinkBotId ? { ilink_bot_id: params.ilinkBotId } : {}),
    ...(params.ilinkUserId ? { ilink_user_id: params.ilinkUserId } : {}),
    ...(params.ilinkUsername ? { ilink_username: params.ilinkUsername } : {}),
    ...(typeof params.botGeneration === "number"
      ? { bot_generation: params.botGeneration }
      : {}),
    ...(params.expiresAt ? { expires_at: params.expiresAt } : {}),
    ...(params.message ? { message: params.message } : {}),
    ...(safeQr ? { qrcode_img_content: safeQr, qrcode_url: safeQr } : {}),
  };
}

function sessionQrImage(session: WeixinLoginSessionGetResponse): string | null {
  return session.qrcode_img_content || session.qrcode || null;
}

export class WeixinQrLoginError extends Error {
  readonly statusCode: number;
  readonly code?: string;

  constructor(message: string, statusCode = 400, code?: string) {
    super(message);
    this.name = "WeixinQrLoginError";
    this.statusCode = statusCode;
    this.code = code;
  }
}

export class WeixinQrLoginManager {
  private readonly backend: BackendClient;
  private readonly baseUrl: string;
  private readonly cdnBaseUrl: string;
  private readonly botType: string;
  private readonly allowedApiDomains?: readonly string[];
  private readonly resolveHostIps?: HostIpResolver;
  private readonly checkResolvedIps?: boolean;
  private readonly fetchImpl?: typeof fetch;
  private readonly getDispatcher?: () => unknown;
  private readonly isLeaseValid?: () => boolean;
  private readonly assertLeaseValidFn?: (operation: string) => void;
  private readonly onAccountConfirmed?: (
    account: ConfirmedWeixinAccount,
  ) => Promise<void> | void;
  private readonly inFlightWaits = new Map<
    string,
    Promise<WeixinQrPublicWaitResponse>
  >();

  constructor(opts: WeixinQrLoginManagerOptions) {
    this.backend = opts.backend;
    this.baseUrl = (opts.baseUrl || ILINK_DEFAULT_BASE_URL).trim();
    this.cdnBaseUrl = (opts.cdnBaseUrl || ILINK_DEFAULT_CDN_BASE_URL).trim();
    this.botType = (opts.botType || ILINK_DEFAULT_BOT_TYPE).trim();
    this.allowedApiDomains = opts.allowedApiDomains;
    this.resolveHostIps = opts.resolveHostIps;
    this.checkResolvedIps = opts.checkResolvedIps;
    this.fetchImpl = opts.fetchImpl;
    this.getDispatcher = opts.getDispatcher;
    this.isLeaseValid = opts.isLeaseValid;
    this.assertLeaseValidFn = opts.assertLeaseValid;
    this.onAccountConfirmed = opts.onAccountConfirmed;
  }

  private assertLeaseValid(operation: string): void {
    if (this.assertLeaseValidFn) {
      this.assertLeaseValidFn(operation);
      return;
    }
    if (this.isLeaseValid && !this.isLeaseValid()) {
      throw new StaleGatewayEpochError(
        `Gateway lease is not locally valid before ${operation}`,
        { status: 409 },
      );
    }
  }

  private async safeResolveRedirectBaseUrl(
    redirectOrBaseUrl: string | undefined | null,
    fallbackBaseUrl: string,
  ): Promise<string> {
    if (!redirectOrBaseUrl || !redirectOrBaseUrl.trim()) {
      return normalizeAndValidateIlinkBaseUrl(
        fallbackBaseUrl,
        this.allowedApiDomains,
      );
    }
    try {
      return await resolveValidatedRedirectBaseUrl({
        redirectHost: redirectOrBaseUrl.trim(),
        baseUrl: fallbackBaseUrl,
        allowedDomains: this.allowedApiDomains,
        resolveHostIps: this.resolveHostIps,
        checkResolvedIps: this.checkResolvedIps,
      });
    } catch {
      return normalizeAndValidateIlinkBaseUrl(
        fallbackBaseUrl,
        this.allowedApiDomains,
      );
    }
  }

  /**
   * Starts QR login for a backend pre-created `starting` session (`sessionKey`,
   * `loginVersion`), fetches a QR code from iLink, and saves it via
   * `PATCH /internal/weixin/login-sessions/{session_key}` (`action: "save_qr"`).
   */
  async startQrLogin(
    userId: string,
    opts?: {
      sessionKey?: string;
      loginVersion?: number;
      expectedBotGeneration?: number;
      signal?: AbortSignal;
    },
  ): Promise<WeixinQrPublicStartResponse> {
    const cleanUserId = userId.trim();
    if (!cleanUserId) {
      throw new WeixinQrLoginError("Missing authenticated user_id", 401, "unauthorized");
    }

    this.assertLeaseValid("startQrLogin");

    const sessionKey = opts?.sessionKey?.trim() || randomUUID();

    const qrResp = await fetchWeixinQrCode({
      baseUrl: this.baseUrl,
      botType: this.botType,
      signal: opts?.signal,
      fetchImpl: this.fetchImpl,
      dispatcher: this.getDispatcher?.(),
      allowedDomains: this.allowedApiDomains,
      resolveHostIps: this.resolveHostIps,
      checkResolvedIps: this.checkResolvedIps,
      assertLeaseValid: () => this.assertLeaseValid("fetchWeixinQrCode"),
    });

    const qrcode = qrResp.qrcode?.trim();
    const qrcodeImgContent = qrResp.qrcode_img_content?.trim();
    if (!qrcode || !qrcodeImgContent) {
      const safe = resolveSafeQrError("ilink_invalid_qrcode");
      throw new WeixinQrLoginError(safe.message, 502, safe.code);
    }

    const fallbackExpiresAt = new Date(
      Date.now() + QR_SESSION_TTL_MS,
    ).toISOString();

    let saved;
    try {
      saved = await this.backend.patchWeixinLoginSession(sessionKey, {
        action: "save_qr",
        ...(typeof opts?.loginVersion === "number"
          ? { expected_login_version: opts.loginVersion }
          : {}),
        qrcode,
        qrcode_img_content: qrcodeImgContent,
        redirect_base_url: this.baseUrl,
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err) || isLeaseSafetyMarginPauseError(err)) {
        throw err;
      }
      if (err instanceof BackendHttpError) {
        const rawCode = extractBackendErrorCode(err.body);
        const safe = resolveSafeQrError(
          rawCode,
          err.status === 409 ? "login_session_revoked" : "qr_login_error",
        );
        throw new WeixinQrLoginError(safe.message, err.status, safe.code);
      }
      throw err;
    }

    if (saved.revoked || !saved.ok) {
      const safe = resolveSafeQrError(saved.code, "login_session_revoked");
      throw new WeixinQrLoginError(safe.message, 409, safe.code);
    }

    return shapePublicQrStartResponse({
      sessionKey,
      status: "wait",
      qrcodeImgContent,
      expiresAt: saved.expires_at ?? fallbackExpiresAt,
      loginVersion: saved.login_version,
      allowedDomains: this.allowedApiDomains,
    });
  }

  async waitQrLogin(
    userId: string,
    sessionKey: string,
    opts?: { signal?: AbortSignal; timeoutMs?: number },
  ): Promise<WeixinQrPublicWaitResponse> {
    const cleanUserId = userId.trim();
    const cleanSessionKey = sessionKey.trim();
    if (!cleanUserId) {
      throw new WeixinQrLoginError("Missing authenticated user_id", 401, "unauthorized");
    }
    if (!cleanSessionKey) {
      throw new WeixinQrLoginError("Missing session_key", 400, "invalid_request");
    }

    const inflightKey = `${cleanUserId}:${cleanSessionKey}`;
    const existing = this.inFlightWaits.get(inflightKey);
    if (existing) {
      return existing;
    }

    const promise = this.executeWaitQrLogin(
      cleanUserId,
      cleanSessionKey,
      opts,
    ).finally(() => {
      if (this.inFlightWaits.get(inflightKey) === promise) {
        this.inFlightWaits.delete(inflightKey);
      }
    });
    this.inFlightWaits.set(inflightKey, promise);
    return promise;
  }

  private shapeAlreadyConfirmedSession(
    session: WeixinLoginSessionGetResponse,
  ): WeixinQrPublicWaitResponse {
    const confirmed =
      session.confirmed_result && typeof session.confirmed_result === "object"
        ? session.confirmed_result
        : {};
    const accountId =
      typeof confirmed.account_id === "string"
        ? confirmed.account_id
        : session.target_account_id ?? null;
    const accountStatus =
      typeof confirmed.account_status === "string"
        ? confirmed.account_status
        : null;
    const ilinkBotId =
      typeof confirmed.ilink_bot_id === "string" ? confirmed.ilink_bot_id : null;
    const ilinkUserId =
      typeof confirmed.ilink_user_id === "string"
        ? confirmed.ilink_user_id
        : null;
    const ilinkUsername =
      typeof confirmed.ilink_username === "string"
        ? confirmed.ilink_username
        : null;
    const botGeneration =
      typeof confirmed.bot_generation === "number"
        ? confirmed.bot_generation
        : null;
    const accountsRevision =
      typeof confirmed.accounts_revision === "number"
        ? confirmed.accounts_revision
        : null;
    return shapePublicQrWaitResponse({
      sessionKey: session.session_key,
      status: "confirmed",
      connected: session.connected ?? true,
      alreadyConnected: session.already_connected ?? false,
      accountId,
      accountStatus,
      accountsRevision,
      pendingVerification: accountStatus === "pending_verification",
      ilinkBotId,
      ilinkUserId,
      ilinkUsername,
      botGeneration,
      expiresAt: session.expires_at,
    });
  }

  private validateSessionState(
    session: WeixinLoginSessionGetResponse,
    userId: string,
    sessionKey: string,
  ):
    | { valid: true }
    | { valid: false; response: WeixinQrPublicWaitResponse } {
    if (session.user_id && session.user_id !== userId) {
      throw new WeixinQrLoginError(
        "QR login session belongs to a different user",
        403,
        "forbidden",
      );
    }

    if (session.status === "confirmed") {
      return {
        valid: false,
        response: this.shapeAlreadyConfirmedSession(session),
      };
    }

    if (session.status === "superseded") {
      return {
        valid: false,
        response: shapePublicQrWaitResponse({
          sessionKey,
          status: "superseded",
          connected: false,
          revoked: true,
          code: "login_session_revoked",
          expiresAt: session.expires_at,
          message: "QR login session has been superseded.",
        }),
      };
    }

    if (session.status === "expired") {
      return {
        valid: false,
        response: shapePublicQrWaitResponse({
          sessionKey,
          status: "expired",
          connected: false,
          expiresAt: session.expires_at,
          message: "QR code has expired.",
        }),
      };
    }

    if (session.expires_at) {
      const expMs = Date.parse(session.expires_at);
      if (Number.isFinite(expMs) && expMs <= Date.now()) {
        return {
          valid: false,
          response: shapePublicQrWaitResponse({
            sessionKey,
            status: "expired",
            connected: false,
            expiresAt: session.expires_at,
            message: "QR code has expired.",
          }),
        };
      }
    }

    if (!session.qrcode) {
      return {
        valid: false,
        response: shapePublicQrWaitResponse({
          sessionKey,
          status: "expired",
          connected: false,
          message: "QR login session is missing QR code state.",
        }),
      };
    }

    return { valid: true };
  }

  private async fetchSessionOrExpired(
    sessionKey: string,
  ): Promise<
    | { found: true; session: WeixinLoginSessionGetResponse }
    | { found: false; response: WeixinQrPublicWaitResponse }
  > {
    try {
      const session = await this.backend.getWeixinLoginSession(sessionKey);
      return { found: true, session };
    } catch (err) {
      if (isStaleGatewayEpochError(err)) {
        throw err;
      }
      if (err instanceof BackendHttpError) {
        if (err.status === 404) {
          return {
            found: false,
            response: shapePublicQrWaitResponse({
              sessionKey,
              status: "expired",
              connected: false,
              message: "QR login session not found or expired.",
            }),
          };
        }
        const rawCode = extractBackendErrorCode(err.body);
        const safe = resolveSafeQrError(rawCode, "qr_login_error");
        throw new WeixinQrLoginError(safe.message, err.status, safe.code);
      }
      throw err;
    }
  }

  private async executeWaitQrLogin(
    userId: string,
    sessionKey: string,
    opts?: { signal?: AbortSignal; timeoutMs?: number },
  ): Promise<WeixinQrPublicWaitResponse> {
    this.assertLeaseValid("waitQrLogin");

    const fetched = await this.fetchSessionOrExpired(sessionKey);
    if (!fetched.found) {
      return fetched.response;
    }
    const { session } = fetched;
    const check = this.validateSessionState(session, userId, sessionKey);
    if (!check.valid) {
      return check.response;
    }

    const pollBaseUrl = await this.safeResolveRedirectBaseUrl(
      session.redirect_base_url,
      this.baseUrl,
    );

    this.assertLeaseValid("pollWeixinQrStatus");

    const statusResp = await pollWeixinQrStatus({
      baseUrl: pollBaseUrl,
      qrcode: session.qrcode!,
      timeoutMs: opts?.timeoutMs,
      signal: opts?.signal,
      fetchImpl: this.fetchImpl,
      dispatcher: this.getDispatcher?.(),
      allowedDomains: this.allowedApiDomains,
      resolveHostIps: this.resolveHostIps,
      checkResolvedIps: this.checkResolvedIps,
      assertLeaseValid: () => this.assertLeaseValid("pollWeixinQrStatus"),
    });

    return this.handleStatusPollResult(
      userId,
      sessionKey,
      session,
      pollBaseUrl,
      statusResp,
    );
  }

  async verifyQrCode(
    userId: string,
    sessionKey: string,
    verifyCode: string,
    opts?: { signal?: AbortSignal; timeoutMs?: number },
  ): Promise<WeixinQrPublicWaitResponse> {
    const cleanUserId = userId.trim();
    const cleanSessionKey = sessionKey.trim();
    const cleanCode = verifyCode.trim();
    if (!cleanUserId) {
      throw new WeixinQrLoginError("Missing authenticated user_id", 401, "unauthorized");
    }
    if (!cleanSessionKey) {
      throw new WeixinQrLoginError("Missing session_key", 400, "invalid_request");
    }
    if (!cleanCode) {
      throw new WeixinQrLoginError("Missing verify_code", 400, "invalid_request");
    }

    this.assertLeaseValid("verifyQrCode");

    const fetched = await this.fetchSessionOrExpired(cleanSessionKey);
    if (!fetched.found) {
      return fetched.response;
    }
    const { session } = fetched;
    const check = this.validateSessionState(session, cleanUserId, cleanSessionKey);
    if (!check.valid) {
      return check.response;
    }

    const pollBaseUrl = await this.safeResolveRedirectBaseUrl(
      session.redirect_base_url,
      this.baseUrl,
    );

    this.assertLeaseValid("submitWeixinQrVerifyCode");

    const statusResp = await submitWeixinQrVerifyCode({
      baseUrl: pollBaseUrl,
      qrcode: session.qrcode!,
      verifyCode: cleanCode,
      timeoutMs: opts?.timeoutMs,
      signal: opts?.signal,
      fetchImpl: this.fetchImpl,
      dispatcher: this.getDispatcher?.(),
      allowedDomains: this.allowedApiDomains,
      resolveHostIps: this.resolveHostIps,
      checkResolvedIps: this.checkResolvedIps,
      assertLeaseValid: () => this.assertLeaseValid("submitWeixinQrVerifyCode"),
    });

    return this.handleStatusPollResult(
      cleanUserId,
      cleanSessionKey,
      session,
      pollBaseUrl,
      statusResp,
    );
  }

  private async patchSessionUpdate(
    sessionKey: string,
    session: WeixinLoginSessionGetResponse,
    status: WeixinQrPublicStatus,
    redirectBaseUrl: string,
  ): Promise<{ revoked: boolean; code?: string; message?: string }> {
    try {
      const upd = await this.backend.patchWeixinLoginSession(sessionKey, {
        action: "update",
        expected_login_version: session.login_version,
        status,
        redirect_base_url: redirectBaseUrl,
      });
      if (upd.revoked || !upd.ok) {
        const safe = resolveSafeQrError(upd.code, "login_session_revoked");
        return {
          revoked: true,
          code: safe.code,
          message: safe.message,
        };
      }
      return { revoked: false };
    } catch (err) {
      if (isStaleGatewayEpochError(err) || isLeaseSafetyMarginPauseError(err)) {
        throw err;
      }
      if (err instanceof BackendHttpError && err.status === 409) {
        const rawCode = extractBackendErrorCode(err.body);
        const safe = resolveSafeQrError(rawCode, "login_session_revoked");
        return {
          revoked: true,
          code: safe.code,
          message: safe.message,
        };
      }
      throw err;
    }
  }

  private async handleStatusPollResult(
    userId: string,
    sessionKey: string,
    session: WeixinLoginSessionGetResponse,
    currentPollBaseUrl: string,
    statusResp: QrCodeStatusResp,
  ): Promise<WeixinQrPublicWaitResponse> {
    const nextRedirectBaseUrl = statusResp.redirect_host
      ? await this.safeResolveRedirectBaseUrl(
          statusResp.redirect_host,
          currentPollBaseUrl,
        )
      : currentPollBaseUrl;

    const rawStatus = (statusResp.status ?? "wait") as
      | WeixinQrPollStatus
      | "need_verify"
      | "need_verifycode";
    const normalizedStatus: WeixinQrPublicStatus =
      rawStatus === "confirmed"
        ? "confirmed"
        : rawStatus === "expired"
          ? "expired"
          : rawStatus === "verify_code" ||
              rawStatus === "need_verify" ||
              rawStatus === "need_verifycode"
            ? "verify_code"
            : rawStatus === "scaned" || rawStatus === "scaned_but_redirect"
              ? "scaned"
              : "wait";

    if (normalizedStatus === "wait") {
      if (nextRedirectBaseUrl !== currentPollBaseUrl) {
        const upd = await this.patchSessionUpdate(
          sessionKey,
          session,
          "wait",
          nextRedirectBaseUrl,
        );
        if (upd.revoked) {
          return shapePublicQrWaitResponse({
            sessionKey,
            status: "superseded",
            connected: false,
            revoked: true,
            code: upd.code,
            message: upd.message,
          });
        }
      }
      return shapePublicQrWaitResponse({
        sessionKey,
        status: "wait",
        connected: false,
        expiresAt: session.expires_at,
        qrcodeImgContent: sessionQrImage(session),
        allowedDomains: this.allowedApiDomains,
      });
    }

    if (normalizedStatus === "scaned" || normalizedStatus === "verify_code") {
      const upd = await this.patchSessionUpdate(
        sessionKey,
        session,
        normalizedStatus,
        nextRedirectBaseUrl,
      );
      if (upd.revoked) {
        return shapePublicQrWaitResponse({
          sessionKey,
          status: "superseded",
          connected: false,
          revoked: true,
          code: upd.code,
          message: upd.message,
        });
      }
      return shapePublicQrWaitResponse({
        sessionKey,
        status: normalizedStatus,
        connected: false,
        expiresAt: session.expires_at,
        qrcodeImgContent: sessionQrImage(session),
        allowedDomains: this.allowedApiDomains,
        message:
          normalizedStatus === "verify_code"
            ? "Please enter the verification code shown in WeChat."
            : "QR code scanned. Please confirm login in WeChat.",
      });
    }

    if (normalizedStatus === "expired") {
      await this.patchSessionUpdate(
        sessionKey,
        session,
        "expired",
        nextRedirectBaseUrl,
      );
      return shapePublicQrWaitResponse({
        sessionKey,
        status: "expired",
        connected: false,
        expiresAt: session.expires_at,
        message: "QR code has expired.",
      });
    }

    // Confirmed state: validate credentials before committing atomically via PATCH confirm.
    const botToken = statusResp.bot_token?.trim();
    const ilinkBotId = statusResp.ilink_bot_id?.trim();
    if (!botToken || !ilinkBotId) {
      throw new WeixinQrLoginError(
        "Weixin login confirmed response is missing bot_token or ilink_bot_id",
        502,
        "ilink_invalid_confirm",
      );
    }

    const confirmedBaseUrl = statusResp.baseurl
      ? await this.safeResolveRedirectBaseUrl(
          statusResp.baseurl,
          nextRedirectBaseUrl,
        )
      : nextRedirectBaseUrl;

    let confirmRes;
    try {
      confirmRes = await this.backend.patchWeixinLoginSession(sessionKey, {
        action: "confirm",
        expected_login_version: session.login_version,
        expected_bot_generation: session.expected_bot_generation,
        ilink_bot_id: ilinkBotId,
        bot_token: botToken,
        base_url: confirmedBaseUrl,
        cdn_base_url: this.cdnBaseUrl,
        ilink_user_id: statusResp.ilink_user_id?.trim() || undefined,
        ilink_username: statusResp.ilink_username?.trim() || undefined,
      });
    } catch (err) {
      if (isStaleGatewayEpochError(err) || isLeaseSafetyMarginPauseError(err)) {
        throw err;
      }
      if (err instanceof BackendHttpError) {
        const rawCode = extractBackendErrorCode(err.body);
        const safe = resolveSafeQrError(
          rawCode,
          err.status === 409 ? "login_session_revoked" : "qr_login_error",
        );
        throw new WeixinQrLoginError(safe.message, err.status, safe.code);
      }
      throw err;
    }

    if (confirmRes.revoked || !confirmRes.ok) {
      const safe = resolveSafeQrError(confirmRes.code, "login_session_revoked");
      return shapePublicQrWaitResponse({
        sessionKey,
        status: "superseded",
        connected: false,
        revoked: true,
        code: safe.code,
        message: safe.message,
      });
    }

    const confirmedAccountId =
      confirmRes.account_id?.trim() || session.target_account_id?.trim() || "";
    const confirmedGeneration =
      typeof confirmRes.bot_generation === "number"
        ? confirmRes.bot_generation
        : session.expected_bot_generation + 1;
    // Contract §3.2 places account status in `status` ("active" | "pending_verification"),
    // while backend `confirm_weixin_login_session` returns `status: "confirmed"` + `account_status`.
    // Support both so contract-only mocks and live backend behave identically.
    const resolvedAccountStatus: "active" | "pending_verification" =
      confirmRes.account_status === "pending_verification" ||
      confirmRes.status === "pending_verification"
        ? "pending_verification"
        : "active";

    if (this.onAccountConfirmed && confirmedAccountId) {
      await this.onAccountConfirmed({
        accountId: confirmedAccountId,
        ilinkBotId: confirmRes.ilink_bot_id?.trim() || ilinkBotId,
        botToken,
        baseUrl: confirmedBaseUrl,
        cdnBaseUrl: this.cdnBaseUrl,
        botGeneration: confirmedGeneration,
        getUpdatesBuf: "",
        ownerUserId: userId,
        accountStatus: resolvedAccountStatus,
        accountsRevision: confirmRes.accounts_revision ?? null,
        ilinkUserId:
          confirmRes.ilink_user_id ?? statusResp.ilink_user_id?.trim() ?? null,
        ilinkUsername:
          confirmRes.ilink_username ?? statusResp.ilink_username?.trim() ?? null,
      });
    }

    return shapePublicQrWaitResponse({
      sessionKey,
      status: "confirmed",
      connected: confirmRes.connected ?? true,
      alreadyConnected: confirmRes.already_connected ?? false,
      accountId: confirmedAccountId || null,
      accountStatus: resolvedAccountStatus,
      accountsRevision: confirmRes.accounts_revision ?? null,
      pendingVerification: resolvedAccountStatus === "pending_verification",
      ilinkBotId: confirmRes.ilink_bot_id ?? ilinkBotId,
      ilinkUserId:
        confirmRes.ilink_user_id ?? statusResp.ilink_user_id?.trim() ?? null,
      ilinkUsername:
        confirmRes.ilink_username ?? statusResp.ilink_username?.trim() ?? null,
      botGeneration: confirmedGeneration,
      expiresAt: session.expires_at,
    });
  }
}
