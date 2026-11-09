// Portions adapted from @tencent-weixin/openclaw-weixin (MIT License, Copyright (c) 2026 Tencent).

import { randomUUID } from "node:crypto";
import type {
  BackendClient,
  I9WeixinStateGetResponse,
} from "../../backend-client.js";
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
  | "expired";

export type WeixinQrPublicStartResponse = WeixinQrPublicResult;
export type WeixinQrPublicWaitResponse = WeixinQrPublicResult & {
  bot_generation?: number;
};

export interface ConfirmedWeixinAccount {
  ilinkBotId: string;
  botToken: string;
  baseUrl: string;
  cdnBaseUrl: string;
  botGeneration: number;
  getUpdatesBuf: string;
  ownerUserId: string;
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
    ...(params.message ? { message: params.message } : {}),
  };
}

export function shapePublicQrWaitResponse(params: {
  sessionKey: string;
  status: WeixinQrPublicStatus;
  connected: boolean;
  ilinkBotId?: string | null;
  ilinkUserId?: string | null;
  ilinkUsername?: string | null;
  botGeneration?: number | null;
  expiresAt?: string | null;
  message?: string | null;
}): WeixinQrPublicWaitResponse {
  return {
    session_key: params.sessionKey,
    status: params.status,
    connected: Boolean(params.connected),
    ...(params.ilinkBotId ? { ilink_bot_id: params.ilinkBotId } : {}),
    ...(params.ilinkUserId ? { ilink_user_id: params.ilinkUserId } : {}),
    ...(params.ilinkUsername ? { ilink_username: params.ilinkUsername } : {}),
    ...(typeof params.botGeneration === "number"
      ? { bot_generation: params.botGeneration }
      : {}),
    ...(params.expiresAt ? { expires_at: params.expiresAt } : {}),
    ...(params.message ? { message: params.message } : {}),
  };
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
    this.onAccountConfirmed = opts.onAccountConfirmed;
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

  async startQrLogin(
    userId: string,
    opts?: { signal?: AbortSignal },
  ): Promise<WeixinQrPublicStartResponse> {
    const cleanUserId = userId.trim();
    if (!cleanUserId) {
      throw new WeixinQrLoginError("Missing authenticated user_id", 401);
    }

    const qrResp = await fetchWeixinQrCode({
      baseUrl: this.baseUrl,
      botType: this.botType,
      signal: opts?.signal,
      fetchImpl: this.fetchImpl,
      allowedDomains: this.allowedApiDomains,
      resolveHostIps: this.resolveHostIps,
      checkResolvedIps: this.checkResolvedIps,
    });

    const qrcode = qrResp.qrcode?.trim();
    const qrcodeImgContent = qrResp.qrcode_img_content?.trim();
    if (!qrcode || !qrcodeImgContent) {
      throw new WeixinQrLoginError(
        "Weixin iLink did not return a valid QR code",
        502,
      );
    }

    const sessionKey = randomUUID();
    const expiresAt = new Date(Date.now() + QR_SESSION_TTL_MS).toISOString();

    const saved = await this.backend.putWeixinState({
      login_session_action: "start",
      login_session_key: sessionKey,
      login_owner_user_id: cleanUserId,
      login_status: "wait",
      login_qrcode: qrcode,
      login_qrcode_img_content: qrcodeImgContent,
      login_redirect_base_url: this.baseUrl,
      login_expires_at: expiresAt,
    });

    if (saved.stale_session || !saved.updated) {
      throw new WeixinQrLoginError(
        saved.reason === "admin_required"
          ? "Only admin users can start Weixin QR login."
          : "Failed to initialize Weixin QR login session.",
        saved.reason === "admin_required" ? 403 : 409,
      );
    }

    return shapePublicQrStartResponse({
      sessionKey,
      status: "wait",
      qrcodeImgContent,
      expiresAt,
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
      throw new WeixinQrLoginError("Missing authenticated user_id", 401);
    }
    if (!cleanSessionKey) {
      throw new WeixinQrLoginError("Missing session_key", 400);
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

  private validateSessionState(
    state: I9WeixinStateGetResponse,
    userId: string,
    sessionKey: string,
  ):
    | { valid: true }
    | { valid: false; response: WeixinQrPublicWaitResponse } {
    if (!state.login_session_key || state.login_session_key !== sessionKey) {
      return {
        valid: false,
        response: shapePublicQrWaitResponse({
          sessionKey,
          status: "expired",
          connected: false,
          message: "QR login session has been superseded or expired.",
        }),
      };
    }

    if (state.login_owner_user_id && state.login_owner_user_id !== userId) {
      throw new WeixinQrLoginError(
        "QR login session belongs to a different user",
        403,
      );
    }

    if (state.login_status === "confirmed") {
      const confirmed = state.login_confirmed_result;
      return {
        valid: false,
        response: shapePublicQrWaitResponse({
          sessionKey,
          status: "confirmed",
          connected: true,
          ilinkBotId:
            (typeof confirmed?.ilink_bot_id === "string"
              ? confirmed.ilink_bot_id
              : null) ?? state.ilink_bot_id,
          ilinkUserId:
            (typeof confirmed?.ilink_user_id === "string"
              ? confirmed.ilink_user_id
              : null) ?? state.ilink_user_id,
          ilinkUsername:
            (typeof confirmed?.ilink_username === "string"
              ? confirmed.ilink_username
              : null) ?? state.ilink_username,
          botGeneration:
            (typeof confirmed?.bot_generation === "number"
              ? confirmed.bot_generation
              : null) ?? state.bot_generation,
          expiresAt: state.login_expires_at,
        }),
      };
    }

    if (state.login_status === "expired") {
      return {
        valid: false,
        response: shapePublicQrWaitResponse({
          sessionKey,
          status: "expired",
          connected: false,
          expiresAt: state.login_expires_at,
          message: "QR code has expired.",
        }),
      };
    }

    if (state.login_expires_at) {
      const expMs = Date.parse(state.login_expires_at);
      if (Number.isFinite(expMs) && expMs <= Date.now()) {
        return {
          valid: false,
          response: shapePublicQrWaitResponse({
            sessionKey,
            status: "expired",
            connected: false,
            expiresAt: state.login_expires_at,
            message: "QR code has expired.",
          }),
        };
      }
    }

    if (!state.login_qrcode) {
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

  private async executeWaitQrLogin(
    userId: string,
    sessionKey: string,
    opts?: { signal?: AbortSignal; timeoutMs?: number },
  ): Promise<WeixinQrPublicWaitResponse> {
    const state = await this.backend.getWeixinState({ include_token: false });
    const check = this.validateSessionState(state, userId, sessionKey);
    if (!check.valid) {
      return check.response;
    }

    const pollBaseUrl = await this.safeResolveRedirectBaseUrl(
      state.login_redirect_base_url,
      this.baseUrl,
    );

    const statusResp = await pollWeixinQrStatus({
      baseUrl: pollBaseUrl,
      qrcode: state.login_qrcode!,
      timeoutMs: opts?.timeoutMs,
      signal: opts?.signal,
      fetchImpl: this.fetchImpl,
      allowedDomains: this.allowedApiDomains,
      resolveHostIps: this.resolveHostIps,
      checkResolvedIps: this.checkResolvedIps,
    });

    return this.handleStatusPollResult(
      userId,
      sessionKey,
      state,
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
      throw new WeixinQrLoginError("Missing authenticated user_id", 401);
    }
    if (!cleanSessionKey) {
      throw new WeixinQrLoginError("Missing session_key", 400);
    }
    if (!cleanCode) {
      throw new WeixinQrLoginError("Missing verify_code", 400);
    }

    const state = await this.backend.getWeixinState({ include_token: false });
    const check = this.validateSessionState(state, cleanUserId, cleanSessionKey);
    if (!check.valid) {
      return check.response;
    }

    const pollBaseUrl = await this.safeResolveRedirectBaseUrl(
      state.login_redirect_base_url,
      this.baseUrl,
    );

    const statusResp = await submitWeixinQrVerifyCode({
      baseUrl: pollBaseUrl,
      qrcode: state.login_qrcode!,
      verifyCode: cleanCode,
      timeoutMs: opts?.timeoutMs,
      signal: opts?.signal,
      fetchImpl: this.fetchImpl,
      allowedDomains: this.allowedApiDomains,
      resolveHostIps: this.resolveHostIps,
      checkResolvedIps: this.checkResolvedIps,
    });

    return this.handleStatusPollResult(
      cleanUserId,
      sessionKey,
      state,
      pollBaseUrl,
      statusResp,
    );
  }

  private async handleStatusPollResult(
    userId: string,
    sessionKey: string,
    initialState: I9WeixinStateGetResponse,
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
        const upd = await this.backend.putWeixinState({
          login_session_action: "update",
          login_session_key: sessionKey,
          login_owner_user_id: userId,
          login_version: initialState.login_version ?? undefined,
          expected_bot_generation:
            initialState.login_expected_bot_generation ?? undefined,
          login_status: "wait",
          login_redirect_base_url: nextRedirectBaseUrl,
        });
        if (upd.stale_session) {
          return shapePublicQrWaitResponse({
            sessionKey,
            status: "expired",
            connected: false,
            message: "QR login session was superseded.",
          });
        }
      }
      return shapePublicQrWaitResponse({
        sessionKey,
        status: "wait",
        connected: false,
        expiresAt: initialState.login_expires_at,
      });
    }

    if (normalizedStatus === "scaned" || normalizedStatus === "verify_code") {
      const upd = await this.backend.putWeixinState({
        login_session_action: "update",
        login_session_key: sessionKey,
        login_owner_user_id: userId,
        login_version: initialState.login_version ?? undefined,
        expected_bot_generation:
          initialState.login_expected_bot_generation ?? undefined,
        login_status: normalizedStatus,
        login_redirect_base_url: nextRedirectBaseUrl,
      });
      if (upd.stale_session) {
        return shapePublicQrWaitResponse({
          sessionKey,
          status: "expired",
          connected: false,
          message: "QR login session was superseded.",
        });
      }
      return shapePublicQrWaitResponse({
        sessionKey,
        status: normalizedStatus,
        connected: false,
        expiresAt: initialState.login_expires_at,
        message:
          normalizedStatus === "verify_code"
            ? "Please enter the verification code shown in WeChat."
            : "QR code scanned. Please confirm login in WeChat.",
      });
    }

    if (normalizedStatus === "expired") {
      await this.backend.putWeixinState({
        login_session_action: "update",
        login_session_key: sessionKey,
        login_owner_user_id: userId,
        login_version: initialState.login_version ?? undefined,
        expected_bot_generation:
          initialState.login_expected_bot_generation ?? undefined,
        login_status: "expired",
      });
      return shapePublicQrWaitResponse({
        sessionKey,
        status: "expired",
        connected: false,
        expiresAt: initialState.login_expires_at,
        message: "QR code has expired.",
      });
    }

    // Confirmed state: validate credentials before committing atomically via I-9.
    const botToken = statusResp.bot_token?.trim();
    const ilinkBotId = statusResp.ilink_bot_id?.trim();
    if (!botToken || !ilinkBotId) {
      throw new WeixinQrLoginError(
        "Weixin login confirmed response is missing bot_token or ilink_bot_id",
        502,
      );
    }

    const confirmedBaseUrl = statusResp.baseurl
      ? await this.safeResolveRedirectBaseUrl(
          statusResp.baseurl,
          nextRedirectBaseUrl,
        )
      : nextRedirectBaseUrl;

    const confirmRes = await this.backend.putWeixinState({
      login_session_action: "confirm",
      login_session_key: sessionKey,
      login_owner_user_id: userId,
      login_version: initialState.login_version ?? undefined,
      expected_bot_generation:
        initialState.login_expected_bot_generation ?? undefined,
      ilink_bot_id: ilinkBotId,
      bot_token: botToken,
      base_url: confirmedBaseUrl,
      cdn_base_url: this.cdnBaseUrl,
      owner_user_id: userId,
      ilink_user_id: statusResp.ilink_user_id?.trim() || undefined,
      ilink_username: statusResp.ilink_username?.trim() || undefined,
      status: "active",
    });

    if (
      confirmRes.stale_session ||
      (!confirmRes.updated && !confirmRes.idempotent)
    ) {
      if (confirmRes.reason === "admin_required") {
        throw new WeixinQrLoginError(
          "Admin permission is required to confirm Weixin login.",
          403,
        );
      }
      return shapePublicQrWaitResponse({
        sessionKey,
        status: "expired",
        connected: false,
        message:
          "QR login session was superseded or expired before confirmation.",
      });
    }

    const botGeneration =
      confirmRes.bot_generation ?? (initialState.bot_generation ?? 1) + 1;
    if (confirmRes.updated && this.onAccountConfirmed) {
      await this.onAccountConfirmed({
        ilinkBotId,
        botToken,
        baseUrl: confirmedBaseUrl,
        cdnBaseUrl: this.cdnBaseUrl,
        botGeneration,
        getUpdatesBuf: "",
        ownerUserId: userId,
        ilinkUserId: statusResp.ilink_user_id?.trim() || null,
        ilinkUsername: statusResp.ilink_username?.trim() || null,
      });
    }

    const confirmedPayload = confirmRes.confirmed_result;
    return shapePublicQrWaitResponse({
      sessionKey,
      status: "confirmed",
      connected: true,
      ilinkBotId:
        (typeof confirmedPayload?.ilink_bot_id === "string"
          ? confirmedPayload.ilink_bot_id
          : null) ?? ilinkBotId,
      ilinkUserId:
        (typeof confirmedPayload?.ilink_user_id === "string"
          ? confirmedPayload.ilink_user_id
          : null) ??
        statusResp.ilink_user_id?.trim() ??
        null,
      ilinkUsername:
        (typeof confirmedPayload?.ilink_username === "string"
          ? confirmedPayload.ilink_username
          : null) ??
        statusResp.ilink_username?.trim() ??
        null,
      botGeneration,
      expiresAt: initialState.login_expires_at,
    });
  }
}
