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
import type { GatewayConfig } from "./config.js";
import { DataTaskNotificationService } from "./data-task-notifications.js";
import { DeliveryService } from "./delivery-service.js";
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

export function createGatewayApp(options: {
  config: GatewayConfig;
  backend: BackendClient;
  adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  backoffDelayFn?: (ms: number) => Promise<void>;
  busyThreadMaxWaitMs?: number;
  nowFn?: () => number;
}): GatewayApp {
  const { config, backend, adapters } = options;
  if (config.host !== "127.0.0.1") {
    throw new Error(
      `Gateway server must bind only to 127.0.0.1 (got "${config.host}")`,
    );
  }

  const deliveryService = new DeliveryService({ backend, adapters });
  const dataTaskNotifications = new DataTaskNotificationService({
    backend,
    adapters,
    mode: config.mode,
  });
  const agentRoutes = new AgentRoutesHandler({
    backend,
    adapters,
    deliveryService,
    webBaseUrl: config.webBaseUrl,
  });
  const orchestrator = new ChannelGatewayOrchestrator({
    backend,
    adapters,
    deliveryService,
    agentRoutes,
    webBaseUrl: config.webBaseUrl,
    weixinCacheDir: config.weixinCacheDir,
    backoffDelayFn: options.backoffDelayFn,
    busyThreadMaxWaitMs: options.busyThreadMaxWaitMs,
    nowFn: options.nowFn,
  });

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

      if (method === "GET" && pathname === "/health") {
        sendJson(res, 200, { ok: true });
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

      // Internal Weixin QR Login Proxy Routes (§1.5 #4)
      if (method === "POST" && pathname === "/internal/weixin/qrcode/start") {
        const body = await readJsonBody<{ user_id?: string }>(req);
        const uid = body.user_id ?? headerUserId ?? "";
        const wxAdapter = adapters.get("weixin");
        if (!wxAdapter?.startQrLogin) {
          sendJson(res, 503, { detail: "Weixin QR login is not available" });
          return;
        }
        const out = await wxAdapter.startQrLogin({ userId: uid });
        sendJson(res, 200, out);
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
        const out = await wxAdapter.waitQrLogin({
          userId: uid,
          sessionKey,
        });
        if (out.status === "confirmed" && out.bot_token && out.ilink_bot_id) {
          await backend.putWeixinState({
            ilink_bot_id: out.ilink_bot_id,
            owner_user_id: uid,
            bot_token: out.bot_token,
            base_url: out.base_url ?? null,
            status: "active",
            ilink_user_id: out.ilink_user_id ?? null,
            ilink_username: out.ilink_username ?? null,
          });
        }
        sendJson(res, 200, out);
        return;
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
        if (out.status === "confirmed" && out.bot_token && out.ilink_bot_id) {
          await backend.putWeixinState({
            ilink_bot_id: out.ilink_bot_id,
            owner_user_id: uid,
            bot_token: out.bot_token,
            base_url: out.base_url ?? null,
            status: "active",
            ilink_user_id: out.ilink_user_id ?? null,
            ilink_username: out.ilink_username ?? null,
          });
        }
        sendJson(res, 200, out);
        return;
      }

      sendJson(res, 404, { detail: "Not found" });
    } catch (err) {
      sendJson(res, 500, {
        detail: err instanceof Error ? err.message : "Internal gateway error",
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
    async start(): Promise<{ host: string; port: number }> {
      if (config.mode !== "outgoing") {
        await orchestrator.syncWeixinStateCache();
        for (const adapter of adapters.values()) {
          if (adapter.isConfigured()) {
            await adapter.start();
          }
        }
      }
      await new Promise<void>((resolveListen, rejectListen) => {
        server.once("error", rejectListen);
        server.listen(config.port, "127.0.0.1", () => {
          server.removeListener("error", rejectListen);
          resolveListen();
        });
      });
      if (config.mode !== "outgoing") {
        // Kick off startup recovery in background without blocking HTTP listen or adapters
        orchestrator.startStartupRecoveryInBackground();
      }
      const addr = server.address() as AddressInfo;
      return { host: addr.address, port: addr.port };
    },
    async stop(): Promise<void> {
      if (config.mode !== "outgoing") {
        await orchestrator.stopAllWatches();
        for (const adapter of adapters.values()) {
          await adapter.stop();
        }
      }
      if (server.listening) {
        await new Promise<void>((resolveClose) => {
          server.close(() => resolveClose());
        });
      }
    },
  };
}
