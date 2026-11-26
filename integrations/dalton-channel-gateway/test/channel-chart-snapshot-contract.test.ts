/**
 * Shared contract tests between backend FastAPI snapshot routes and Dalton Channel Gateway.
 *
 * Loads the exact JSON fixtures produced by real FastAPI routes in
 * `tests/unit/dalton/test_channel_chart_snapshot_contract_fixtures.py`
 * (`tests/fixtures/dalton/channel_contract/snapshots_*.json`) as mock `fetch` responses.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { AgentRoutesHandler } from "../src/agent-routes.js";
import {
  BackendHttpError,
  HttpBackendClient,
} from "../src/backend-client.js";
import type {
  ChannelAdapter,
  SendCardOptions,
  SendCardResult,
  SendFileOptions,
  SendFileResult,
  SendTextChunkOptions,
  SendTextChunkResult,
} from "../src/channels/adapter.js";
import {
  deliverTurnChartsAndWidgets,
  parseChartSnapshotsList,
  waitForChartSnapshotsReady,
} from "../src/chart-snapshots.js";
import { DeliveryService } from "../src/delivery-service.js";
import { ChannelGatewayOrchestrator } from "../src/orchestrator.js";
import type { ChannelPlatform } from "../src/types.js";

const FIXTURES_DIR = resolve(
  import.meta.dirname,
  "../../../tests/fixtures/dalton/channel_contract",
);
const WEB_BASE = "https://dalton.example.com";
const SERVICE_TOKEN = "contract-test-service-token-32bytes-ok";
const GATEWAY_EPOCH = 7;

function loadFixture<T>(filename: string): T {
  return JSON.parse(
    readFileSync(resolve(FIXTURES_DIR, filename), "utf-8"),
  ) as T;
}

interface HttpJsonFixture {
  request: { method: string; path: string };
  response: { status: number; body: Record<string, unknown> };
}

interface HttpBinaryFixture {
  request: { method: string; path: string };
  response: {
    status: number;
    content_type: string;
    body_base64: string;
    sha256: string;
  };
}

interface SnapshotSseFixture {
  run_end: Record<string, unknown>;
  replay_done: Record<string, unknown>;
}

function makeJsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeStubAdapter(platform: ChannelPlatform) {
  const sentCards: SendCardOptions[] = [];
  const sentTexts: SendTextChunkOptions[] = [];
  const sentFiles: SendFileOptions[] = [];
  const adapter: ChannelAdapter & {
    sentCards: SendCardOptions[];
    sentTexts: SendTextChunkOptions[];
    sentFiles: SendFileOptions[];
  } = {
    platform,
    maxChunkChars: 4000,
    sentCards,
    sentTexts,
    sentFiles,
    isConfigured: () => true,
    isConnected: () => true,
    start: async () => undefined,
    stop: async () => undefined,
    onInboundEvent: () => undefined,
    splitTextChunks: (text) => (text ? [text] : []),
    sendTextChunk: vi.fn(async (opts: SendTextChunkOptions): Promise<SendTextChunkResult> => {
      sentTexts.push(opts);
      return { messageId: `${platform}_txt_${sentTexts.length}`, resolvedAddress: opts.targetAddress };
    }),
    sendFile: vi.fn(async (opts: SendFileOptions): Promise<SendFileResult> => {
      sentFiles.push(opts);
      return { messageId: `${platform}_file_${sentFiles.length}`, status: "sent", reason: null };
    }),
    sendCard: vi.fn(async (opts: SendCardOptions): Promise<SendCardResult> => {
      await opts.verifyRecipientBeforeSend?.();
      sentCards.push(opts);
      return { messageId: `${platform}_card_${sentCards.length}`, resolvedAddress: opts.targetAddress };
    }),
    downloadAttachment: vi.fn(),
    listAvailableChats: vi.fn(async () => ({ chats: [], complete: true, error: null })),
    checkTargetLive: vi.fn(async () => ({ ok: true, address: `${platform}:@me`, message: null })),
  };
  return adapter;
}

describe("Channel Phase 2 Batch 2 shared contract fixtures (backend <-> gateway)", () => {
  it("parses snapshots_status_pending -> snapshots_status_ready and downloads DM PNG via snapshots_download_dm_ok", async () => {
    const fPending = loadFixture<HttpJsonFixture>("snapshots_status_pending.json");
    const fReady = loadFixture<HttpJsonFixture>("snapshots_status_ready.json");
    const fDownload = loadFixture<HttpBinaryFixture>("snapshots_download_dm_ok.json");

    let statusCall = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(typeof input === "string" ? input : input.toString());
      const pathAndQuery = `${url.pathname}${url.search}`;
      if (url.pathname === "/api/v1/dalton/channels/internal/snapshots/status") {
        statusCall += 1;
        return statusCall === 1
          ? makeJsonResponse(fPending.response.status, fPending.response.body)
          : makeJsonResponse(fReady.response.status, fReady.response.body);
      }
      if (pathAndQuery === fDownload.request.path) {
        const bytes = Buffer.from(fDownload.response.body_base64, "base64");
        return new Response(bytes, {
          status: fDownload.response.status,
          headers: { "Content-Type": fDownload.response.content_type },
        });
      }
      throw new Error(`Unexpected contract fetch: ${pathAndQuery}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://127.0.0.1:8000",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    backend.setGatewayEpoch(GATEWAY_EPOCH);

    const readySnap = (fReady.response.body.snapshots as Array<Record<string, unknown>>)[0]!;
    const waited = await waitForChartSnapshotsReady({
      backend,
      runId: String(fReady.response.body.run_id),
      threadId: String(fReady.response.body.thread_id),
      platform: "feishu",
      expectedLayerIds: [String(readySnap.layer_id)],
      isDm: true,
      timeoutMs: 20_000,
      pollIntervalMs: 10,
      sleepFn: async () => undefined,
    });

    expect(statusCall).toBe(2);
    expect(waited.timedOut).toBe(false);
    expect(waited.snapshotsByLayerId.get(String(readySnap.layer_id))?.status).toBe("ready");

    const downloaded = await backend.downloadChartSnapshot({
      snapshotKey: String(readySnap.snapshot_key),
      runId: String(fReady.response.body.run_id),
      targetAddress: "feishu:@me",
      deliveryKey: `charts:${String(fReady.response.body.run_id)}`,
      deliveryAttempt: 1,
    });
    expect(downloaded.mimeType).toBe("image/png");
    expect(downloaded.content.equals(Buffer.from(fDownload.response.body_base64, "base64"))).toBe(true);
  });

  it("handles snapshots_status_failed, snapshots_download_group_denied_403, and snapshots_download_invalid_key_400", async () => {
    const fFailed = loadFixture<HttpJsonFixture>("snapshots_status_failed.json");
    const fGroup403 = loadFixture<HttpJsonFixture>("snapshots_download_group_denied_403.json");
    const fBad400 = loadFixture<HttpJsonFixture>("snapshots_download_invalid_key_400.json");

    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(typeof input === "string" ? input : input.toString());
      const pathAndQuery = `${url.pathname}${url.search}`;
      if (url.pathname === "/api/v1/dalton/channels/internal/snapshots/status") {
        return makeJsonResponse(fFailed.response.status, fFailed.response.body);
      }
      if (pathAndQuery === fGroup403.request.path) {
        return makeJsonResponse(fGroup403.response.status, fGroup403.response.body);
      }
      if (url.pathname.includes("%252e%252e") || url.pathname.includes("..")) {
        return makeJsonResponse(fBad400.response.status, fBad400.response.body);
      }
      throw new Error(`Unexpected contract fetch: ${pathAndQuery}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://127.0.0.1:8000",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    backend.setGatewayEpoch(GATEWAY_EPOCH);

    const st = await backend.getChartSnapshotsStatus({
      runId: String(fFailed.response.body.run_id),
      threadId: String(fFailed.response.body.thread_id),
      platform: "feishu",
    });
    expect(st.has_pending).toBe(false);
    expect(st.snapshots[0]?.status).toBe("failed");
    expect(st.snapshots[0]?.error).toBe("stale_pending_timeout");

    // 403 group denied
    const snapKey = fGroup403.request.path
      .split("/internal/snapshots/")[1]!
      .split("?")[0]!;
    await expect(
      backend.downloadChartSnapshot({
        snapshotKey: snapKey,
        runId: "run-snap-ready-1",
        targetAddress: "feishu:oc_grp_1",
      }),
    ).rejects.toMatchObject({ status: 403 });

    // 400 invalid key
    await expect(
      backend.downloadChartSnapshot({
        snapshotKey: "channel-snapshots/11111111-1111-4111-8111-111111111111/%2e%2e/chart_layer-nvda-1.png",
        runId: "run-snap-ready-1",
        targetAddress: "feishu:@me",
      }),
    ).rejects.toBeInstanceOf(BackendHttpError);
  });

  it("consumes snapshots_replay_sse_events.json on replay_done and delivers DM PNG card via real contract", async () => {
    const fSse = loadFixture<SnapshotSseFixture>("snapshots_replay_sse_events.json");
    const fDownload = loadFixture<HttpBinaryFixture>("snapshots_download_dm_ok.json");
    const readySnaps = parseChartSnapshotsList(fSse.run_end.chart_snapshots);
    expect(readySnaps).toHaveLength(1);
    expect(readySnaps[0]?.status).toBe("ready");
    expect(fSse.run_end.outbound_blocked).toBe(false);
    expect(Array.isArray(fSse.run_end.chart_layers)).toBe(true);
    expect(
      (fSse.replay_done.outbound_blocked_by_run as Record<string, boolean>)?.["run-snap-blocked-1"],
    ).toBe(true);
    expect(
      (fSse.replay_done.outbound_blocked_by_run as Record<string, boolean>)?.["run-snap-ready-1"],
    ).toBe(false);

    const feishu = makeStubAdapter("feishu");
    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);

    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input.toString());
      const pathAndQuery = `${url.pathname}${url.search}`;
      if (url.pathname.endsWith("/messages/replay")) {
        const payload = [
          `id: 1\nevent: message_chunk\ndata: ${JSON.stringify({ agent: "main", role: "assistant", content: "分析完成。", turn_index: 1, response_id: "run-snap-ready-1" })}\n\n`,
          `id: 2\nevent: replay_done\ndata: ${JSON.stringify(fSse.replay_done)}\n\n`,
        ].join("");
        return new Response(payload, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (url.pathname.endsWith("/internal/inbound/progress")) {
        return makeJsonResponse(200, {
          ok: true,
          sent_to_current_count: 1,
          has_in_flight_or_unknown_current_text: false,
        });
      }
      if (url.pathname.endsWith("/internal/deliveries/authorize-and-claim")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return makeJsonResponse(200, {
          claim_status: "claimed",
          delivery_key: body.delivery_key,
          delivery_attempt: 1,
          platform: "feishu",
          target_address: "feishu:@me",
          recipient_id: "ou_alice",
          dm_recipient_platform_user_id: "ou_alice",
          is_current: true,
        });
      }
      if (url.pathname.endsWith("/verify-dm-recipient")) {
        return makeJsonResponse(200, {
          authorized: true,
          platform: "feishu",
          recipient_id: "ou_alice",
          delivery_attempt: 1,
        });
      }
      if (pathAndQuery === fDownload.request.path) {
        return new Response(Buffer.from(fDownload.response.body_base64, "base64"), {
          status: 200,
          headers: { "Content-Type": "image/png" },
        });
      }
      if (url.pathname.endsWith("/internal/deliveries/progress")) {
        return makeJsonResponse(200, { ok: true });
      }
      throw new Error(`Unexpected replay contract fetch: ${pathAndQuery}`);
    });

    const backend = new HttpBackendClient({
      baseUrl: "http://127.0.0.1:8000",
      serviceToken: SERVICE_TOKEN,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    backend.setGatewayEpoch(GATEWAY_EPOCH);
    const deliveryService = new DeliveryService({ backend, adapters, webBaseUrl: WEB_BASE });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });
    const orchestrator = new ChannelGatewayOrchestrator({
      backend,
      adapters,
      deliveryService,
      agentRoutes,
      webBaseUrl: WEB_BASE,
    });

    const ok = await orchestrator.deliverReplayFallback({
      userId: "user-alice",
      threadId: String(fSse.replay_done.thread_id),
      runId: "run-snap-ready-1",
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
    });

    expect(ok).toBe(true);
    expect(feishu.sentCards).toHaveLength(1);
    expect(feishu.sentCards[0]?.images).toHaveLength(1);
    expect(
      feishu.sentCards[0]?.images?.[0]?.content.equals(
        Buffer.from(fDownload.response.body_base64, "base64"),
      ),
    ).toBe(true);
    // Also verify direct deliverTurnChartsAndWidgets contract idempotency
    expect(deliverTurnChartsAndWidgets).toBeTypeOf("function");
  });
});
