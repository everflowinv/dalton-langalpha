import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { AddressInfo } from "node:net";
import { AgentRoutesHandler } from "./agent-routes.js";
import { extractUserIdHeader, verifyServiceToken } from "./auth.js";
import type { BackendClient } from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import {
  resolveSafeQrError,
  WeixinQrLoginError,
} from "./channels/weixin/index.js";
import type { GatewayConfig } from "./config.js";
import { AutomationNotificationService } from "./automation-notifications.js";
import { DataTaskNotificationService } from "./data-task-notifications.js";
import { handleOpsAlert } from "./ops-alerts.js";
import { DeliveryService } from "./delivery-service.js";
import {
  GatewayLeaseManager,
  isLeaseSafetyMarginPauseError,
  isStaleGatewayEpochError,
  StaleGatewayEpochError,
} from "./gateway-lease.js";
import { ChannelGatewayOrchestrator } from "./orchestrator.js";
import type {
  AgentAutomationOutputPutRequest,
  AgentAutomationRunFinishRequest,
  AgentAutomationRunStartRequest,
  AgentCheckTargetRequest,
  AgentSendRequest,
  ChannelPlatform,
} from "./types.js";

export interface GatewayApp {
  readonly config: GatewayConfig;
  readonly server: Server;
  readonly backend: BackendClient;
  readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  readonly deliveryService: DeliveryService;
  readonly dataTaskNotifications: DataTaskNotificationService;
  readonly agentRoutes: AgentRoutesHandler;
  readonly orchestrator: ChannelGatewayOrchestrator;
  readonly leaseManager?: GatewayLeaseManager;
  start(): Promise<{ host: string; port: number }>;
  stop(): Promise<void>;
}

async function readJsonBody<T>(req: IncomingMessage): Promise<T> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) {
    return {} as T;
  }
  return JSON.parse(raw) as T;
}

function sendJson(
  res: ServerResponse,
  statusCode: number,
  payload: unknown,
): void {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body, "utf8"),
  });
  res.end(body);
}

function sanitizePublicQrUrl(rawUrl: unknown): string | undefined {
  if (typeof rawUrl !== "string" || !rawUrl.trim()) {
    return undefined;
  }
  const trimmed = rawUrl.trim();
  if (trimmed.toLowerCase().startsWith("data:image/")) {
    return trimmed;
  }
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
      return undefined;
    }
    for (const key of [...parsed.searchParams.keys()]) {
      const lower = key.toLowerCase();
      if (
        lower.includes("token") ||
        lower.includes("secret") ||
        lower.includes("key") ||
        lower.includes("auth") ||
        lower.includes("pass")
      ) {
        parsed.searchParams.delete(key);
      }
    }
    return parsed.toString();
  } catch {
    return undefined;
  }
}

export function sanitizePublicWeixinQrResponse(
  payload: unknown,
): Record<string, unknown> {
  if (!payload || typeof payload !== "object") {
    return { status: "error", message: "Invalid QR response" };
  }
  const src = payload as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  if (typeof src.session_key === "string" && src.session_key.trim()) {
    out.session_key = src.session_key.trim();
  }
  const safeQrUrl = sanitizePublicQrUrl(src.qrcode_url);
  if (safeQrUrl) {
    out.qrcode_url = safeQrUrl;
  }
  const safeQrContent = sanitizePublicQrUrl(src.qrcode_img_content);
  if (safeQrContent) {
    out.qrcode_img_content = safeQrContent;
  }
  if (typeof src.status === "string" && src.status.trim()) {
    out.status = src.status.trim();
  }
  if (typeof src.connected === "boolean") {
    out.connected = src.connected;
  }
  if (typeof src.already_connected === "boolean") {
    out.already_connected = src.already_connected;
  }
  if (typeof src.account_id === "string" && src.account_id.trim()) {
    out.account_id = src.account_id.trim();
  }
  if (typeof src.account_status === "string" && src.account_status.trim()) {
    out.account_status = src.account_status.trim();
  }
  if (
    typeof src.accounts_revision === "number" &&
    Number.isFinite(src.accounts_revision)
  ) {
    out.accounts_revision = Math.trunc(src.accounts_revision);
  }
  if (
    typeof src.login_version === "number" &&
    Number.isFinite(src.login_version)
  ) {
    out.login_version = Math.trunc(src.login_version);
  }
  if (typeof src.pending_verification === "boolean") {
    out.pending_verification = src.pending_verification;
  }
  if (typeof src.revoked === "boolean") {
    out.revoked = src.revoked;
  }
  if (typeof src.code === "string" && src.code.trim()) {
    out.code = src.code.trim();
  }
  if (typeof src.need_rescan === "boolean") {
    out.need_rescan = src.need_rescan;
  }
  if (typeof src.expires_at === "string" && src.expires_at.trim()) {
    out.expires_at = src.expires_at.trim();
  }
  if (typeof src.ilink_bot_id === "string" && src.ilink_bot_id.trim()) {
    out.ilink_bot_id = src.ilink_bot_id.trim();
  }
  if (typeof src.ilink_user_id === "string" && src.ilink_user_id.trim()) {
    out.ilink_user_id = src.ilink_user_id.trim();
  }
  if (typeof src.ilink_username === "string" && src.ilink_username.trim()) {
    out.ilink_username = src.ilink_username.trim();
  }
  if (
    typeof src.bot_generation === "number" &&
    Number.isFinite(src.bot_generation)
  ) {
    out.bot_generation = Math.trunc(src.bot_generation);
  }
  if (typeof src.verify_code_accepted === "boolean") {
    out.verify_code_accepted = src.verify_code_accepted;
  }
  if (typeof src.message === "string" && src.message.trim()) {
    out.message = src.message.trim();
  }
  if (typeof src.error_code === "string" && src.error_code.trim()) {
    out.error_code = src.error_code.trim();
  }
  return out;
}

export function createGatewayApp(options: {
  config: GatewayConfig;
  backend: BackendClient;
  adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  backoffDelayFn?: (ms: number) => Promise<void>;
  busyThreadMaxWaitMs?: number;
  nowFn?: () => number;
  leaseManager?: GatewayLeaseManager;
  enableLease?: boolean;
  leaseTtlSeconds?: number;
  leaseHeartbeatIntervalMs?: number;
  leaseAcquireRetryMs?: number;
  retryDelayFn?: (ms: number, signal?: AbortSignal) => Promise<boolean>;
  preflightRetryMaxElapsedMs?: number;
  claim503MaxAttempts?: number;
  onLeaseLost?: (reason: string, error?: unknown) => void;
}): GatewayApp {
  const { config, backend, adapters } = options;
  if (config.host !== "127.0.0.1") {
    throw new Error(
      `Gateway server must bind only to 127.0.0.1 (got "${config.host}")`,
    );
  }

  for (const adapter of adapters.values()) {
    adapter.setBackendClient?.(backend);
  }

  const onWeixinAccountRotated = (info: {
    accountId?: string | null;
    reason: string;
  }): void => {
    // R7 #1: Rotation hints without an authoritative snapshot generation only trigger
    // accounts sync; actual attachment cancellation is driven exclusively by snapshot application.
    void adapters.get("weixin")?.triggerAccountsSync?.(undefined, info.reason);
  };

  const deliveryService = new DeliveryService({
    backend,
    adapters,
    webBaseUrl: config.webBaseUrl,
    onWeixinAccountRotated,
    retryDelayFn: options.retryDelayFn,
    preflightRetryMaxElapsedMs: options.preflightRetryMaxElapsedMs,
    claim503MaxAttempts: options.claim503MaxAttempts,
    nowFn: options.nowFn,
  });
  const dataTaskNotifications = new DataTaskNotificationService({
    backend,
    adapters,
    webBaseUrl: config.webBaseUrl,
    mode: config.mode,
    onWeixinAccountRotated,
    retryDelayFn: options.retryDelayFn,
    preflightRetryMaxElapsedMs: options.preflightRetryMaxElapsedMs,
    claim503MaxAttempts: options.claim503MaxAttempts,
    nowFn: options.nowFn,
  });
  const automationNotifications = new AutomationNotificationService(
    backend,
    dataTaskNotifications,
  );
  const feishuAdapter = adapters.get("feishu");
  const sendOpsPlainText =
    typeof feishuAdapter?.sendPlainText === "function"
      ? (targetAddress: string, text: string) =>
          feishuAdapter.sendPlainText!({ targetAddress, text })
      : undefined;
  const agentRoutes = new AgentRoutesHandler({
    backend,
    adapters,
    deliveryService,
    dataTaskNotifications,
    webBaseUrl: config.webBaseUrl,
  });
  const orchestrator = new ChannelGatewayOrchestrator({
    backend,
    adapters,
    deliveryService,
    agentRoutes,
    webBaseUrl: config.webBaseUrl,
    statusFeedback: config.statusFeedback,
    backoffDelayFn: options.backoffDelayFn,
    busyThreadMaxWaitMs: options.busyThreadMaxWaitMs,
    nowFn: options.nowFn,
  });

  const shouldUseLease =
    config.mode !== "outgoing" && options.enableLease !== false;
  let leaseReady = !shouldUseLease;
  let stopInProgress = false;
  const handleFatalLeaseLost = (reason: string, error?: unknown): void => {
    // Decision #1 & R5 #3: synchronously abort ALL in-flight sends, uploads, QR, inbound
    // attachment uploads, in-delivery retry waits, and long-polling loops BEFORE waiting on
    // any async teardown, and prevent any post-lease-loss I-7 settlement.
    leaseReady = false;
    deliveryService.abortOnLeaseLost();
    dataTaskNotifications.abortOnLeaseLost();
    backend.setGatewayEpoch?.(null);
    orchestrator.abortAllActiveInboundUploads();
    for (const adapter of adapters.values()) {
      adapter.abortAllInFlight?.();
    }
    if (!stopInProgress) {
      stopInProgress = true;
      void (async () => {
        try {
          if (server.listening) {
            (server as Server & { closeAllConnections?: () => void }).closeAllConnections?.();
          }
          await Promise.race([
            (async () => {
              await orchestrator.stopAllWatches();
              for (const adapter of adapters.values()) {
                await adapter.stop();
              }
              if (server.listening) {
                await new Promise<void>((resolveClose) => {
                  server.close(() => resolveClose());
                });
              }
            })(),
            new Promise<void>((resolveTimeout) => {
              const timer = setTimeout(resolveTimeout, 1_500);
              timer.unref?.();
            }),
          ]);
        } finally {
          options.onLeaseLost?.(reason, error);
        }
      })();
    }
  };

  const activeFeishuAppId = adapters.get("feishu")?.getActiveAppId?.();
  const leaseManager = shouldUseLease
    ? (options.leaseManager ??
      new GatewayLeaseManager({
        backend,
        ttlSeconds: options.leaseTtlSeconds,
        heartbeatIntervalMs: options.leaseHeartbeatIntervalMs,
        acquireRetryMs: options.leaseAcquireRetryMs,
        ...(activeFeishuAppId ? { feishuAppId: activeFeishuAppId } : {}),
        onEpochAcquired: (epoch) => {
          backend.setGatewayEpoch?.(epoch);
        },
        onLeaseLost: handleFatalLeaseLost,
        onLeaseRecovered: () => {
          adapters.get("weixin")?.ensurePollableMonitorsRunning?.();
        },
      }))
    : undefined;

  if (leaseManager) {
    backend.setOnStaleGatewayEpoch?.((err) => {
      leaseManager.notifyLeaseLost("backend_409_stale_epoch", err);
    });
    adapters.get("weixin")?.setLeaseManager?.(leaseManager);
  }

  const server = createServer(async (req, res) => {
    try {
      const method = (req.method ?? "GET").toUpperCase();
      const parsedUrl = new URL(req.url ?? "/", `http://${config.host}:${config.port}`);
      const pathname = parsedUrl.pathname;

      // Authenticate all requests with constant-time X-Service-Token check (§1.1)
      if (!verifyServiceToken(req.headers, config.serviceToken)) {
        sendJson(res, 401, { detail: "Invalid or missing X-Service-Token" });
        return;
      }

      // R7 #6 & R8 #6: `ready` is true only when shutdown has not begun, write routes are open,
      // and any backend-attached Weixin adapter has applied a real initial snapshot (or confirmed
      // HTTP 404 meaning Weixin routes are unmounted in this deployment).
      const isGatewayReady = (): boolean => {
        if (stopInProgress) {
          return false;
        }
        if (shouldUseLease && !leaseReady) {
          return false;
        }
        const wxAdapter = adapters.get("weixin");
        if (
          wxAdapter?.hasAppliedStartupSnapshot &&
          !wxAdapter.hasAppliedStartupSnapshot()
        ) {
          return false;
        }
        return true;
      };

      if (method === "GET" && pathname === "/health") {
        sendJson(res, 200, { ok: true, ready: isGatewayReady() });
        return;
      }

      if (method === "POST" && pathname === "/internal/ops-alerts") {
        let body: unknown;
        try {
          body = await readJsonBody<unknown>(req);
        } catch {
          sendJson(res, 400, {
            ok: false,
            code: "invalid_json",
            message: "Malformed JSON request body.",
          });
          return;
        }
        const result = await handleOpsAlert({
          body,
          remoteAddress: req.socket.remoteAddress,
          configPath: config.opsAlertsFile,
          sendPlainText: sendOpsPlainText,
        });
        sendJson(res, result.status, result.data);
        return;
      }

      // Round 3 Decision #11: While acquiring deployment lease / completing startup recovery,
      // allow read-only `GET /agent/*` inspection routes (plus `/health` & `/internal/ops-alerts`)
      // to respond normally; only block write & iLink routes with 503 `gateway_starting`.
      const isReadOnlyStartupRoute =
        method === "GET" &&
        (pathname === "/agent/targets" ||
          pathname === "/agent/settings" ||
          pathname === "/agent/settings/available" ||
          pathname === "/agent/automation-targets");
      if (shouldUseLease && !leaseReady && !isReadOnlyStartupRoute) {
        sendJson(res, 503, {
          ok: false,
          code: "gateway_starting",
          detail: "Gateway is acquiring deployment lease; retry shortly.",
        });
        return;
      }

      if (method === "POST" && pathname === "/internal/data-task-notifications") {
        let body: unknown;
        try {
          body = await readJsonBody<unknown>(req);
        } catch {
          sendJson(res, 400, {
            ok: false,
            status: "failed",
            code: "invalid_json",
            message: "Malformed JSON request body.",
            run_id: "",
            target: "",
            platform: null,
            items_total: 0,
            items_sent: 0,
            items: [],
          });
          return;
        }
        const result =
          await dataTaskNotifications.handleDataTaskNotification(body);
        sendJson(res, result.status, result.data);
        return;
      }

      if (method === "POST" && pathname === "/internal/automation-notifications") {
        let body: unknown;
        try {
          body = await readJsonBody<unknown>(req);
        } catch {
          sendJson(res, 400, {
            ok: false,
            status: "failed",
            code: "invalid_json",
            message: "Malformed JSON request body.",
            run_id: "",
            target: "",
            platform: null,
            items_total: 0,
            items_sent: 0,
            items: [],
          });
          return;
        }
        const result = await automationNotifications.handle(body);
        sendJson(res, result.status, result.data);
        return;
      }

      if (method === "POST" && pathname === "/internal/failure-notices") {
        let body: unknown;
        try {
          body = await readJsonBody<unknown>(req);
        } catch {
          sendJson(res, 400, {
            ok: false,
            status: "failed",
            code: "invalid_json",
            message: "Malformed JSON request body.",
            run_id: "",
            target: "",
            platform: null,
            items_total: 0,
            items_sent: 0,
            items: [],
          });
          return;
        }
        const result = await agentRoutes.handleFailureNotice(body);
        sendJson(res, result.status, result.data);
        return;
      }

      const headerUserId = extractUserIdHeader(req.headers);

      // 1. POST /agent/send (§1.2.1)
      if (method === "POST" && pathname === "/agent/send") {
        const body = await readJsonBody<AgentSendRequest>(req);
        const result = await agentRoutes.handleAgentSend(headerUserId ?? "", body);
        sendJson(res, result.status, result.data);
        return;
      }

      // 2. GET /agent/targets (§1.2.2)
      if (method === "GET" && pathname === "/agent/targets") {
        const result = await agentRoutes.handleGetTargets(headerUserId ?? "", {
          thread_id: parsedUrl.searchParams.get("thread_id"),
          run_id: parsedUrl.searchParams.get("run_id"),
          turn_platform: parsedUrl.searchParams.get("turn_platform"),
        });
        sendJson(res, result.status, result.data);
        return;
      }

      // 3. GET /agent/settings (§1.2.3)
      if (method === "GET" && pathname === "/agent/settings") {
        const result = await agentRoutes.handleGetSettings(headerUserId ?? "");
        sendJson(res, result.status, result.data);
        return;
      }

      // 4. PUT /agent/settings (§1.2.3)
      if (method === "PUT" && pathname === "/agent/settings") {
        const body = await readJsonBody<{
          version: string;
          settings: Record<string, unknown>;
        }>(req);
        const result = await agentRoutes.handlePutSettings(headerUserId ?? "", body);
        sendJson(res, result.status, result.data);
        return;
      }

      // 5. GET /agent/settings/available (§1.2.4)
      if (method === "GET" && pathname === "/agent/settings/available") {
        const result = await agentRoutes.handleGetSettingsAvailable(
          headerUserId ?? "",
        );
        sendJson(res, result.status, result.data);
        return;
      }

      // 6. POST /agent/check-target (§1.2.5)
      if (method === "POST" && pathname === "/agent/check-target") {
        const body = await readJsonBody<AgentCheckTargetRequest>(req);
        const result = await agentRoutes.handleCheckTarget(
          headerUserId ?? "",
          body,
        );
        sendJson(res, result.status, result.data);
        return;
      }

      // 7. GET /agent/automation-targets (§1.2.6)
      if (method === "GET" && pathname === "/agent/automation-targets") {
        const workspaceId = parsedUrl.searchParams.get("workspace_id");
        const result = await agentRoutes.handleGetAutomationTargets(
          headerUserId ?? "",
          workspaceId,
        );
        sendJson(res, result.status, result.data);
        return;
      }

      // 8. PUT /agent/automation-output (§1.2.6)
      if (method === "PUT" && pathname === "/agent/automation-output") {
        const body = await readJsonBody<AgentAutomationOutputPutRequest>(req);
        const result = await agentRoutes.handlePutAutomationOutput(
          headerUserId ?? "",
          body,
        );
        sendJson(res, result.status, result.data);
        return;
      }

      // 9a. POST /agent/automation-runs (§1.2.7)
      if (method === "POST" && pathname === "/agent/automation-runs") {
        const body = await readJsonBody<AgentAutomationRunStartRequest>(req);
        const result = await agentRoutes.handleStartAutomationRun(
          headerUserId ?? "",
          body,
        );
        sendJson(res, result.status, result.data);
        return;
      }

      // 9b. POST /agent/automation-runs/{execution_id}/finish (§1.2.7)
      const finishMatch =
        /^\/agent\/automation-runs\/([^/]+)\/finish$/.exec(pathname);
      if (method === "POST" && finishMatch) {
        const executionId = decodeURIComponent(finishMatch[1]!);
        const body = await readJsonBody<AgentAutomationRunFinishRequest>(req);
        const result = await agentRoutes.handleFinishAutomationRun(
          headerUserId ?? "",
          executionId,
          body,
        );
        sendJson(res, result.status, result.data);
        return;
      }

      // Internal Weixin Multi-Account Sync Nudge (§3.1)
      if (method === "POST" && pathname === "/internal/weixin/accounts/sync") {
        const body = await readJsonBody<{
          accounts_revision?: number;
          reason?: string;
        }>(req);
        const wxAdapter = adapters.get("weixin");
        if (!wxAdapter?.triggerAccountsSync) {
          sendJson(res, 200, {
            ok: true,
            accepted_revision: body.accounts_revision ?? 0,
          });
          return;
        }
        const syncRes = await wxAdapter.triggerAccountsSync(
          typeof body.accounts_revision === "number"
            ? body.accounts_revision
            : undefined,
          typeof body.reason === "string" ? body.reason : "backend_nudge",
        );
        sendJson(res, 200, syncRes);
        return;
      }

      // Internal Weixin QR Login Proxy Routes (§1.5 #4, §3.1)
      if (method === "POST" && pathname === "/internal/weixin/qrcode/start") {
        const body = await readJsonBody<{
          user_id?: string;
          session_key?: string;
          login_version?: number;
          expected_bot_generation?: number;
        }>(req);
        const uid = body.user_id ?? headerUserId ?? "";
        const wxAdapter = adapters.get("weixin");
        if (!wxAdapter?.startQrLogin) {
          sendJson(res, 503, { detail: "Weixin QR login is not available" });
          return;
        }
        const out = await wxAdapter.startQrLogin({
          userId: uid,
          ...(typeof body.session_key === "string" && body.session_key.trim()
            ? { sessionKey: body.session_key.trim() }
            : {}),
          ...(typeof body.login_version === "number"
            ? { loginVersion: body.login_version }
            : {}),
          ...(typeof body.expected_bot_generation === "number"
            ? { expectedBotGeneration: body.expected_bot_generation }
            : {}),
        });
        sendJson(res, 200, sanitizePublicWeixinQrResponse(out));
        return;
      }

      if (
        (method === "GET" || method === "POST") &&
        pathname === "/internal/weixin/qrcode/wait"
      ) {
        const body =
          method === "POST"
            ? await readJsonBody<{
                user_id?: string;
                session_key?: string;
              }>(req)
            : {};
        const uid =
          parsedUrl.searchParams.get("user_id") ??
          body.user_id ??
          headerUserId ??
          "";
        const sessionKey =
          parsedUrl.searchParams.get("session_key") ?? body.session_key ?? "";
        const wxAdapter = adapters.get("weixin");
        if (!wxAdapter?.waitQrLogin) {
          sendJson(res, 503, { detail: "Weixin QR login is not available" });
          return;
        }
        const waitAbort = new AbortController();
        const onClientClose = () => {
          if (!res.writableEnded) {
            waitAbort.abort(new Error("client_disconnected"));
          }
        };
        req.once("aborted", onClientClose);
        res.once("close", onClientClose);
        try {
          const out = await wxAdapter.waitQrLogin({
            userId: uid,
            sessionKey,
            signal: waitAbort.signal,
          });
          if (waitAbort.signal.aborted || res.writableEnded || res.destroyed) {
            return;
          }
          sendJson(res, 200, sanitizePublicWeixinQrResponse(out));
          return;
        } catch (err) {
          if (waitAbort.signal.aborted && (res.writableEnded || res.destroyed)) {
            return;
          }
          throw err;
        } finally {
          req.off("aborted", onClientClose);
          res.off("close", onClientClose);
        }
      }

      if (
        method === "POST" &&
        pathname === "/internal/weixin/qrcode/verify-code"
      ) {
        const body = await readJsonBody<{
          user_id?: string;
          session_key: string;
          verify_code?: string;
          code?: string;
        }>(req);
        const uid = body.user_id ?? headerUserId ?? "";
        const wxAdapter = adapters.get("weixin");
        if (!wxAdapter?.submitVerifyCode) {
          sendJson(res, 503, { detail: "Weixin QR verify-code is not available" });
          return;
        }
        const out = await wxAdapter.submitVerifyCode({
          userId: uid,
          sessionKey: body.session_key,
          code: body.verify_code ?? body.code ?? "",
        });
        sendJson(res, 200, sanitizePublicWeixinQrResponse(out));
        return;
      }

      sendJson(res, 404, { detail: "Not found" });
    } catch (err) {
      if (isLeaseSafetyMarginPauseError(err)) {
        sendJson(res, 503, {
          code: "gateway_lease_paused",
          detail: "Gateway lease is renewing; please retry shortly.",
        });
        return;
      }
      if (isStaleGatewayEpochError(err)) {
        sendJson(res, 409, {
          code: "stale_gateway_epoch",
          detail: "Gateway lease epoch is stale.",
        });
        return;
      }
      if (err instanceof WeixinQrLoginError) {
        // Round 3 Decision #10 (§4.3): whitelist error code and return fixed safe message;
        // never forward arbitrary backend or iLink free-text `message`.
        const safe = resolveSafeQrError(err.code, "qr_login_error");
        sendJson(res, err.statusCode, {
          code: safe.code,
          detail: safe.message,
        });
        return;
      }
      // Decision #15 (§4.3): never echo arbitrary internal/iLink error text on HTTP 500 exit.
      sendJson(res, 500, {
        code: "internal_error",
        detail: "Internal gateway error",
      });
    }
  });

  return {
    config,
    server,
    backend,
    adapters,
    deliveryService,
    dataTaskNotifications,
    agentRoutes,
    orchestrator,
    leaseManager,
    async start(): Promise<{ host: string; port: number }> {
      let startupRecoverySnapshot = null;
      if (config.mode !== "outgoing") {
        // D1 / §3.5 strict startup ordering:
        // Block I-6 claims while HTTP /health binds, lease is acquired, mode=startup recovery
        // snapshot completes, and initial managed account snapshots are applied (R4 #5).
        deliveryService.setStartupBlocked(true);
        dataTaskNotifications.setStartupBlocked(true);
      }
      await new Promise<void>((resolveListen, rejectListen) => {
        server.once("error", rejectListen);
        server.listen(config.port, "127.0.0.1", () => {
          server.removeListener("error", rejectListen);
          resolveListen();
        });
      });
      if (config.mode !== "outgoing") {
        try {
          if (leaseManager) {
            const epoch = await leaseManager.acquireUntilGranted();
            backend.setGatewayEpoch?.(epoch);
          }
          // Complete mode=startup recovery snapshot BEFORE starting any long-polling or I-6 claims.
          startupRecoverySnapshot =
            await orchestrator.fetchStartupRecoverySnapshot();
          // R4 #5 & R5 #4 (Codex #6): Apply initial managed account snapshots BEFORE lifting write
          // restrictions so no outbound request can run before the first snapshot is applied.
          // If snapshot fails or lease is lost during startup, keep write routes blocked.
          for (const adapter of adapters.values()) {
            if (adapter.isConfigured() || adapter.platform === "weixin") {
              await adapter.prepareStartupSnapshot?.();
            }
          }
          if (
            stopInProgress ||
            (leaseManager && (leaseManager.isLost || leaseManager.isLeaseExpired()))
          ) {
            throw new StaleGatewayEpochError(
              "Gateway lease lost before startup completed",
              { status: 409, epoch: leaseManager?.epoch ?? null },
            );
          }
          leaseReady = true;
          deliveryService.setStartupBlocked(false);
          dataTaskNotifications.setStartupBlocked(false);
          for (const adapter of adapters.values()) {
            if (adapter.isConfigured() || adapter.platform === "weixin") {
              await adapter.start();
            }
          }
        } catch (err) {
          leaseReady = false;
          deliveryService.setStartupBlocked(true);
          dataTaskNotifications.setStartupBlocked(true);
          if (server.listening) {
            await new Promise<void>((resolveClose) => {
              server.close(() => resolveClose());
            });
          }
          throw err;
        }
        if (startupRecoverySnapshot) {
          // Dispatch long-running recovery streams/tasks in background after HTTP listen & adapters are ready.
          orchestrator.dispatchStartupRecoverySnapshot(startupRecoverySnapshot);
        }
      }
      const addr = server.address() as AddressInfo;
      return { host: addr.address, port: addr.port };
    },
    async stop(): Promise<void> {
      stopInProgress = true;
      leaseReady = false;
      agentRoutes.dispose();
      deliveryService.setStartupBlocked(true);
      dataTaskNotifications.setStartupBlocked(true);
      if (config.mode !== "outgoing") {
        orchestrator.abortAllActiveInboundUploads();
        await orchestrator.stopAllWatches();
        for (const adapter of adapters.values()) {
          await adapter.stop();
        }
        await leaseManager?.stopAndRelease();
        backend.setGatewayEpoch?.(null);
      }
      if (server.listening) {
        await new Promise<void>((resolveClose) => {
          server.close(() => resolveClose());
        });
      }
    },
  };
}
