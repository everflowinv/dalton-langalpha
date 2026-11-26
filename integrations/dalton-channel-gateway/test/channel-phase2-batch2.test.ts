import { describe, expect, it, vi } from "vitest";
import { AgentRoutesHandler } from "../src/agent-routes.js";
import {
  BackendHttpError,
  type BackendClient,
  type ChartSnapshotItem,
  type ChartSnapshotsStatusResponse,
} from "../src/backend-client.js";
import type {
  ChannelAdapter,
  SendCardOptions,
  SendCardResult,
  SendFileOptions,
  SendFileResult,
  SendTextChunkOptions,
  SendTextChunkResult,
  SendTextWithFileOptions,
} from "../src/channels/adapter.js";
import {
  buildChartLayerDeepLink,
  buildDiscordLinkButtonRow,
  buildHtmlWidgetDeepLink,
  deliverTurnChartsAndWidgets,
  FEISHU_CHART_IMAGE_MAX_BYTES,
  waitForChartSnapshotsReady,
} from "../src/chart-snapshots.js";
import { buildFeishuChartSnapshotCard } from "../src/channels/feishu/format.js";
import { DeliveryService } from "../src/delivery-service.js";
import { ChannelGatewayOrchestrator } from "../src/orchestrator.js";
import type { ChannelPlatform } from "../src/types.js";

const WEB_BASE = "https://dalton.example.com";
const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "run-b2-gw-1";
const PNG_BYTES = Buffer.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d,
]);
const SNAP_KEY = `channel-snapshots/${THREAD_ID}/0123456789abcdef/chart_layer-1.png`;

function makeMockBackend(overrides: Partial<BackendClient> = {}): BackendClient {
  return {
    authorizeAndClaimDelivery: vi.fn(async (req) => ({
      claim_status: "claimed" as const,
      delivery_key: req.delivery_key,
      delivery_attempt: 1,
      platform: req.platform ?? "feishu",
      target_address: req.target ?? "feishu:@me",
      recipient_id: "ou_alice",
      dm_recipient_platform_user_id: "ou_alice",
      is_current: true,
    })),
    updateDeliveryProgress: vi.fn(async () => ({ ok: true })),
    verifyDmDeliveryRecipient: vi.fn(async () => ({
      authorized: true,
      platform: "feishu",
      recipient_id: "ou_alice",
      delivery_attempt: 1,
    })),
    preflightWeixinSend: vi.fn(async () => ({
      authorized: true as const,
      decision: "allow" as const,
      retryable: false as const,
      account_id: "wx-acct-1",
      ilink_bot_id: "wx-bot-1",
      bot_generation: 1,
      bot_token: "bot-tok",
      base_url: "https://ilink.example.com",
      cdn_base_url: "https://cdn.example.com",
      recipient_id: "wx_alice",
      weixin_context_token: "ctx-tok",
    })),
    getChartSnapshotsStatus: vi.fn(async () => ({
      run_id: RUN_ID,
      thread_id: THREAD_ID,
      outbound_blocked: false,
      has_pending: false,
      snapshots: [],
    })),
    downloadChartSnapshot: vi.fn(async () => ({
      content: PNG_BYTES,
      mimeType: "image/png",
      filename: "chart_layer-1.png",
    })),
    getThreadWakeups: vi.fn(async () => []),
    ...overrides,
  } as unknown as BackendClient;
}

function makeMockAdapter(platform: ChannelPlatform) {
  const sentCards: SendCardOptions[] = [];
  const sentTexts: SendTextChunkOptions[] = [];
  const sentFiles: SendFileOptions[] = [];
  const sentTextWithFiles: SendTextWithFileOptions[] = [];
  const adapter: ChannelAdapter & {
    sentCards: SendCardOptions[];
    sentTexts: SendTextChunkOptions[];
    sentFiles: SendFileOptions[];
    sentTextWithFiles: SendTextWithFileOptions[];
  } = {
    platform,
    maxChunkChars: 4000,
    sentCards,
    sentTexts,
    sentFiles,
    sentTextWithFiles,
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
      await opts.verifyRecipientBeforeSend?.();
      sentFiles.push(opts);
      return { messageId: `${platform}_file_${sentFiles.length}`, status: "sent", reason: null };
    }),
    sendTextWithFile: vi.fn(async (opts: SendTextWithFileOptions): Promise<SendFileResult> => {
      await opts.verifyRecipientBeforeSend?.();
      sentTextWithFiles.push(opts);
      return { messageId: `${platform}_twf_${sentTextWithFiles.length}`, status: "sent", reason: null };
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

describe("Channel Phase 2 Batch 2: chart_layer DM snapshots & interactive deep links", () => {
  it("builds deep links for chart_layer, html_widget, monitors, and hides localhost URLs", () => {
    expect(buildChartLayerDeepLink(WEB_BASE, THREAD_ID, "NVDA:1day", "layer-1")).toBe(
      `${WEB_BASE}/chat/t/${THREAD_ID}?chart=NVDA%3A1day&layer=layer-1`,
    );
    expect(buildHtmlWidgetDeepLink(WEB_BASE, THREAD_ID, "widget_1")).toBe(
      `${WEB_BASE}/chat/t/${THREAD_ID}?widget=widget_1`,
    );
    expect(buildHtmlWidgetDeepLink(WEB_BASE, THREAD_ID, "widget_1", "nvda-board")).toBe(
      `${WEB_BASE}/monitors/nvda-board`,
    );
    expect(buildChartLayerDeepLink("http://127.0.0.1:8088", THREAD_ID, "NVDA:1day", "layer-1")).toBeNull();
    expect(buildHtmlWidgetDeepLink("http://localhost:5173", THREAD_ID, "widget_1")).toBeNull();

    const loopbackCard = buildFeishuChartSnapshotCard({
      title: "NVDA",
      webUrl: null,
    });
    expect(JSON.stringify(loopbackCard)).toContain("请在网页端对应会话或工作区查看");
  });

  it("polls pending snapshots up to 20s and sends Feishu Schema 2.0 PNG card once ready", async () => {
    let now = 1_000;
    let calls = 0;
    const backend = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async () => {
        calls += 1;
        if (calls < 3) {
          return {
            run_id: RUN_ID,
            thread_id: THREAD_ID,
            outbound_blocked: false,
            has_pending: true,
            snapshots: [
              {
                status: "pending",
                chart_id: "NVDA:1day",
                layer_id: "layer-1",
                title: "NVDA 支撑位",
              },
            ],
          };
        }
        return {
          run_id: RUN_ID,
          thread_id: THREAD_ID,
          outbound_blocked: false,
          has_pending: false,
          snapshots: [
            {
              status: "ready",
              chart_id: "NVDA:1day",
              layer_id: "layer-1",
              title: "NVDA 支撑位",
              snapshot_key: SNAP_KEY,
              sha256: "a".repeat(64),
              size_bytes: PNG_BYTES.length,
            },
          ],
        };
      }),
    });

    const feishu = makeMockAdapter("feishu");
    const res = await deliverTurnChartsAndWidgets({
      backend,
      adapters: new Map([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: RUN_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-1", title: "NVDA 支撑位" }],
      initialSnapshots: [{ status: "pending", chart_id: "NVDA:1day", layer_id: "layer-1" }],
      waitTimeoutMs: 20_000,
      pollIntervalMs: 1_000,
      nowFn: () => now,
      sleepFn: async (ms) => {
        now += ms;
      },
    });

    expect(calls).toBe(3);
    expect(res.settled).toBe(true);
    expect(res.sentImages).toBe(1);
    expect(res.timedOutPending).toBe(false);
    expect(feishu.sentCards).toHaveLength(1);
    expect(feishu.sentCards[0]?.images).toHaveLength(1);
    expect(JSON.stringify(feishu.sentCards[0]?.card)).toContain("open_url");
    expect(JSON.stringify(feishu.sentCards[0]?.card)).toContain("chart=NVDA%3A1day&layer=layer-1");
  });

  it("falls back to web interactive deep link after 20s timeout when render worker crashes leaving pending", async () => {
    let now = 0;
    const backend = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async () => ({
        run_id: RUN_ID,
        thread_id: THREAD_ID,
        outbound_blocked: false,
        has_pending: true,
        snapshots: [
          {
            status: "pending",
            chart_id: "NVDA:1day",
            layer_id: "layer-crashed",
            title: "NVDA 崩盘测试",
          },
        ],
      })),
    });

    const feishu = makeMockAdapter("feishu");
    const res = await deliverTurnChartsAndWidgets({
      backend,
      adapters: new Map([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: RUN_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-crashed", title: "NVDA 崩盘测试" }],
      initialSnapshots: [{ status: "pending", chart_id: "NVDA:1day", layer_id: "layer-crashed" }],
      waitTimeoutMs: 20_000,
      pollIntervalMs: 5_000,
      nowFn: () => now,
      sleepFn: async (ms) => {
        now += ms;
      },
    });

    expect(now).toBe(20_000);
    expect(res.timedOutPending).toBe(true);
    expect(res.sentImages).toBe(0);
    expect(res.sentLinks).toBe(1);
    expect(backend.downloadChartSnapshot).not.toHaveBeenCalled();
    expect(feishu.sentCards).toHaveLength(1);
    expect(feishu.sentCards[0]?.images).toBeUndefined();
    expect(JSON.stringify(feishu.sentCards[0]?.card)).toContain("layer-crashed");
  });

  it("sends Discord DM multipart PNG + Link Button (style 5) and sends link-only in group channels & ShowWidget", async () => {
    const readySnap: ChartSnapshotItem = {
      status: "ready",
      chart_id: "AAPL:1hour",
      layer_id: "layer-aapl-1",
      title: "AAPL 1H",
      snapshot_key: SNAP_KEY,
      sha256: "b".repeat(64),
    };

    // 1. Discord DM -> multipart PNG + Link Button + ShowWidget link-only
    const discordDm = makeMockAdapter("discord");
    const backendDm = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async (req) => ({
        claim_status: "claimed" as const,
        delivery_key: req.delivery_key,
        delivery_attempt: 1,
        platform: "discord",
        target_address: "discord:@me",
        recipient_id: "100001",
        dm_recipient_platform_user_id: "100001",
        is_current: true,
      })),
    });

    const dmRes = await deliverTurnChartsAndWidgets({
      backend: backendDm,
      adapters: new Map([["discord", discordDm]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: RUN_ID,
      platform: "discord",
      targetAddress: "discord:@me",
      charts: [{ chartId: "AAPL:1hour", layerId: "layer-aapl-1", title: "AAPL 1H" }],
      initialSnapshots: [readySnap],
      widgets: [{ artifactId: "widget_99", title: "Valuation Table" }],
    });

    expect(dmRes.sentImages).toBe(1);
    expect(dmRes.sentLinks).toBe(1);
    expect(discordDm.sentTextWithFiles).toHaveLength(1);
    expect(discordDm.sentTextWithFiles[0]?.components).toEqual(
      buildDiscordLinkButtonRow(
        "打开网页交互版",
        `${WEB_BASE}/chat/t/${THREAD_ID}?chart=AAPL%3A1hour&layer=layer-aapl-1`,
      ),
    );
    expect(discordDm.sentTexts).toHaveLength(1);
    expect(discordDm.sentTexts[0]?.components).toEqual(
      buildDiscordLinkButtonRow(
        "打开网页交互组件",
        `${WEB_BASE}/chat/t/${THREAD_ID}?widget=widget_99`,
      ),
    );

    // 2. Discord Group Channel -> zero snapshot polling/download, link-only with ActionRow Link Button
    const discordGrp = makeMockAdapter("discord");
    const backendGrp = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async (req) => ({
        claim_status: "claimed" as const,
        delivery_key: req.delivery_key,
        delivery_attempt: 1,
        platform: "discord",
        target_address: "discord:111/222",
        recipient_id: "222",
        external_topic_id: "333",
        is_current: true,
      })),
    });

    const grpRes = await deliverTurnChartsAndWidgets({
      backend: backendGrp,
      adapters: new Map([["discord", discordGrp]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-group-1",
      platform: "discord",
      targetAddress: "discord:111/222",
      charts: [{ chartId: "AAPL:1hour", layerId: "layer-aapl-1", title: "AAPL 1H" }],
      initialSnapshots: [readySnap],
    });

    expect(grpRes.sentImages).toBe(0);
    expect(grpRes.sentLinks).toBe(1);
    expect(backendGrp.getChartSnapshotsStatus).not.toHaveBeenCalled();
    expect(backendGrp.downloadChartSnapshot).not.toHaveBeenCalled();
    expect(discordGrp.sentTexts).toHaveLength(1);
    expect(discordGrp.sentTexts[0]?.externalTopicId).toBe("333");
  });

  it("delivers Weixin DM encrypted IMAGE + web interactive deep link text and degrades cleanly on 403/oversize", async () => {
    const readySnap: ChartSnapshotItem = {
      status: "ready",
      chart_id: "NVDA:1day",
      layer_id: "layer-1",
      title: "NVDA",
      snapshot_key: SNAP_KEY,
      sha256: "c".repeat(64),
    };
    const weixin = makeMockAdapter("weixin");
    const backendWx = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async (req) => ({
        claim_status: "claimed" as const,
        delivery_key: req.delivery_key,
        delivery_attempt: 1,
        platform: "weixin",
        target_address: "weixin:@me",
        recipient_id: "wx_alice",
        is_current: true,
      })),
    });

    const res = await deliverTurnChartsAndWidgets({
      backend: backendWx,
      adapters: new Map([["weixin", weixin]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-wx-1",
      platform: "weixin",
      targetAddress: "weixin:@me",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-1", title: "NVDA" }],
      initialSnapshots: [readySnap],
    });

    expect(res.sentImages).toBe(1);
    expect(weixin.sentFiles).toHaveLength(1);
    expect(weixin.sentFiles[0]?.mimeType).toBe("image/png");
    expect(weixin.sentTexts).toHaveLength(1);
    expect(weixin.sentTexts[0]?.text).toContain(
      `网页交互版：${WEB_BASE}/chat/t/${THREAD_ID}?chart=NVDA%3A1day&layer=layer-1`,
    );

    // Download 403 -> degrades to link-only on Feishu
    const feishu = makeMockAdapter("feishu");
    const backend403 = makeMockBackend({
      downloadChartSnapshot: vi.fn(async () => {
        throw new BackendHttpError("denied", 403, { code: "snapshot_recipient_denied" });
      }),
    });
    const degRes = await deliverTurnChartsAndWidgets({
      backend: backend403,
      adapters: new Map([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-403-deg",
      platform: "feishu",
      targetAddress: "feishu:@me",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-1", title: "NVDA" }],
      initialSnapshots: [readySnap],
    });
    expect(degRes.sentImages).toBe(0);
    expect(degRes.sentLinks).toBe(1);
    expect(feishu.sentCards[0]?.images).toBeUndefined();

    // Oversize >10MB check constant is honored
    expect(FEISHU_CHART_IMAGE_MAX_BYTES).toBe(10 * 1024 * 1024);

    // Outbound-blocked status immediately stops polling without waiting
    const blockedBackend = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async () => ({
        run_id: "run-blk",
        thread_id: THREAD_ID,
        outbound_blocked: true,
        has_pending: false,
        snapshots: [],
      })),
    });
    const waitedBlk = await waitForChartSnapshotsReady({
      backend: blockedBackend,
      runId: "run-blk",
      threadId: THREAD_ID,
      expectedLayerIds: ["layer-1"],
      isDm: true,
    });
    expect(waitedBlk.outboundBlocked).toBe(true);
    expect(waitedBlk.snapshotsByLayerId.size).toBe(0);
  });

  it("degrades to link-only on Feishu and Discord when dm_recipient_platform_user_id is empty and real recipient verification rejects", async () => {
    const readySnap: ChartSnapshotItem = {
      status: "ready",
      chart_id: "NVDA:1day",
      layer_id: "layer-unpinned-1",
      title: "NVDA 支撑位",
      snapshot_key: SNAP_KEY,
      sha256: "d".repeat(64),
    };

    for (const platform of ["feishu", "discord"] as const) {
      const adapter = makeMockAdapter(platform);
      const pinnedByKey = new Map<string, string | null>();
      const backend = makeMockBackend({
        authorizeAndClaimDelivery: vi.fn(async (req) => {
          // Simulate empty dm_recipient_platform_user_id on the delivery row
          pinnedByKey.set(req.delivery_key, null);
          return {
            claim_status: "claimed" as const,
            delivery_key: req.delivery_key,
            delivery_attempt: 1,
            platform,
            target_address: `${platform}:@me`,
            recipient_id: platform === "feishu" ? "ou_alice" : "100001",
            dm_recipient_platform_user_id: null,
            is_current: true,
          };
        }),
        verifyDmDeliveryRecipient: vi.fn(async (deliveryKey) => {
          const pinned = pinnedByKey.get(deliveryKey);
          if (!pinned) {
            throw new BackendHttpError(
              "Delivery has no verified original DM platform recipient.",
              403,
              { code: "deliverable_recipient_denied" },
            );
          }
          return {
            authorized: true,
            platform,
            recipient_id: pinned,
            delivery_attempt: 1,
          };
        }),
      });

      const res = await deliverTurnChartsAndWidgets({
        backend,
        adapters: new Map([[platform, adapter]]),
        webBaseUrl: WEB_BASE,
        userId: "user-alice",
        threadId: THREAD_ID,
        runId: `run-unpinned-${platform}`,
        platform,
        targetAddress: `${platform}:@me`,
        charts: [{ chartId: "NVDA:1day", layerId: "layer-unpinned-1", title: "NVDA 支撑位" }],
        initialSnapshots: [readySnap],
      });

      expect(res.settled).toBe(true);
      expect(res.sentImages).toBe(0);
      expect(res.sentLinks).toBe(1);
      if (platform === "feishu") {
        expect(adapter.sentCards).toHaveLength(1);
        expect(adapter.sentCards[0]?.images).toBeUndefined();
        expect(JSON.stringify(adapter.sentCards[0]?.card)).toContain("layer-unpinned-1");
      } else {
        expect(adapter.sentTextWithFiles).toHaveLength(0);
        expect(adapter.sentTexts).toHaveLength(1);
        expect(adapter.sentTexts[0]?.text).toContain("layer-unpinned-1");
      }
    }
  });

  it("uses fixed copy without model-generated titles for outbound-blocked turns, group chats, and widgets", async () => {
    const secretChartTitle = "SECRET_COLLEAGUE_ALPHA_12345";
    const secretWidgetTitle = "SECRET_WIDGET_NOTES_99999";

    // 1. Outbound-blocked DM on Feishu + widget
    const feishuRestricted = makeMockAdapter("feishu");
    const backendBlocked = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async () => ({
        run_id: "run-restricted-1",
        thread_id: THREAD_ID,
        outbound_blocked: true,
        has_pending: false,
        snapshots: [],
      })),
    });
    const resBlocked = await deliverTurnChartsAndWidgets({
      backend: backendBlocked,
      adapters: new Map([["feishu", feishuRestricted]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-restricted-1",
      platform: "feishu",
      targetAddress: "feishu:@me",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-r1", title: secretChartTitle }],
      widgets: [{ artifactId: "widget_r1", title: secretWidgetTitle }],
    });
    expect(resBlocked.sentImages).toBe(0);
    expect(resBlocked.sentLinks).toBe(2);
    const cardsJson = JSON.stringify(feishuRestricted.sentCards);
    expect(cardsJson).not.toContain(secretChartTitle);
    expect(cardsJson).not.toContain(secretWidgetTitle);
    expect(cardsJson).toContain("看盘图表已生成，请登录后在网页端查看交互视图。");
    expect(cardsJson).toContain("交互组件已生成，请登录后在网页端查看交互视图。");
    expect(cardsJson).toContain("layer-r1");
    expect(cardsJson).toContain("widget_r1");

    // 2. Group chat on Discord + widget
    const discordGrp = makeMockAdapter("discord");
    const backendGrp = makeMockBackend({
      authorizeAndClaimDelivery: vi.fn(async (req) => ({
        claim_status: "claimed" as const,
        delivery_key: req.delivery_key,
        delivery_attempt: 1,
        platform: "discord",
        target_address: "discord:111/222",
        recipient_id: "222",
        is_current: true,
      })),
    });
    const resGrp = await deliverTurnChartsAndWidgets({
      backend: backendGrp,
      adapters: new Map([["discord", discordGrp]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-grp-fixed-1",
      platform: "discord",
      targetAddress: "discord:111/222",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-g1", title: secretChartTitle }],
      widgets: [{ artifactId: "widget_g1", title: secretWidgetTitle }],
    });
    expect(resGrp.sentImages).toBe(0);
    expect(resGrp.sentLinks).toBe(2);
    const allDiscordText = discordGrp.sentTexts.map((t) => t.text).join("\n");
    expect(allDiscordText).not.toContain(secretChartTitle);
    expect(allDiscordText).not.toContain(secretWidgetTitle);
    expect(allDiscordText).toContain("请登录后在网页端查看");
  });

  it("aborts a hanging final status request at the shared wait deadline", async () => {
    let observedAborted = false;
    let callCount = 0;
    const startedAt = Date.now();
    const backend = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async (req) => {
        callCount += 1;
        if (callCount === 1) {
          return {
            run_id: RUN_ID,
            thread_id: THREAD_ID,
            outbound_blocked: false,
            has_pending: true,
            snapshots: [{ status: "pending", chart_id: "NVDA:1day", layer_id: "layer-hang" }],
          };
        }
        // Final request hangs indefinitely until aborted by deadline signal
        return await new Promise<ChartSnapshotsStatusResponse>((_resolve, reject) => {
          req.signal?.addEventListener("abort", () => {
            observedAborted = true;
            reject(new Error("aborted"));
          });
        });
      }),
    });

    const res = await waitForChartSnapshotsReady({
      backend,
      runId: RUN_ID,
      threadId: THREAD_ID,
      platform: "feishu",
      expectedLayerIds: ["layer-hang"],
      isDm: true,
      timeoutMs: 80,
      pollIntervalMs: 25,
    });
    const elapsedMs = Date.now() - startedAt;

    expect(callCount).toBeGreaterThanOrEqual(2);
    expect(observedAborted).toBe(true);
    expect(res.timedOut).toBe(true);
    expect(elapsedMs).toBeLessThan(1_000);
  });

  it("recovers group chat tool_call_result chart_layers on disconnect replay and DM command_report snapshots across processes", async () => {
    // 1. Group chat disconnect replay: no `artifact` event, no `chart_snapshots`, only `tool_call_result.artifact.chart_layers`
    const discordGrp = makeMockAdapter("discord");
    const grpBackend = makeMockBackend({
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "user_message",
            data: JSON.stringify({
              turn_index: 1,
              run_id: "run-grp-replay-1",
              run_completed_at: "2026-11-25T10:00:00Z",
              metadata: {},
            }),
          };
          yield {
            event: "tool_call_result",
            data: JSON.stringify({
              turn_index: 1,
              response_id: "run-grp-replay-1",
              tool_call_id: "call-exec-1",
              artifact: {
                type: "execute_code",
                chart_layers: [
                  {
                    workspace_id: "ws-1",
                    chart_id: "NVDA:1day",
                    symbol: "NVDA",
                    timeframe: "1day",
                    layer_id: "layer-grp-replay-1",
                    title: "Secret Group Chart Title",
                  },
                ],
              },
            }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "群聊图表分析完成。",
              turn_index: 1,
              response_id: "run-grp-replay-1",
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              latest_run_id: "run-grp-replay-1",
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: "run-grp-replay-1",
        sent_to_current_count: 1,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
      authorizeAndClaimDelivery: vi.fn(async (req) => ({
        claim_status: "claimed" as const,
        delivery_key: req.delivery_key,
        delivery_attempt: 1,
        platform: "discord",
        target_address: "discord:111/222",
        recipient_id: "222",
        is_current: true,
      })),
    });
    const grpAdapters = new Map<ChannelPlatform, ChannelAdapter>([["discord", discordGrp]]);
    const grpDelivery = new DeliveryService({
      backend: grpBackend,
      adapters: grpAdapters,
      webBaseUrl: WEB_BASE,
    });
    const grpRoutes = new AgentRoutesHandler({
      backend: grpBackend,
      adapters: grpAdapters,
      deliveryService: grpDelivery,
      webBaseUrl: WEB_BASE,
    });
    const grpOrch = new ChannelGatewayOrchestrator({
      backend: grpBackend,
      adapters: grpAdapters,
      deliveryService: grpDelivery,
      agentRoutes: grpRoutes,
      webBaseUrl: WEB_BASE,
    });

    const grpOk = await grpOrch.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-grp-replay-1",
      turnIndex: 1,
      platform: "discord",
      targetAddress: "discord:111/222",
      scopeKey: "chat:111/222",
      generation: 1,
    });
    expect(grpOk).toBe(true);
    expect(discordGrp.sentTexts).toHaveLength(1);
    expect(discordGrp.sentTexts[0]?.text).toContain("layer-grp-replay-1");
    expect(discordGrp.sentTexts[0]?.text).not.toContain("Secret Group Chart Title");

    // 2. Cross-process DM command report-back replay: `user_message.metadata.report_source.chart_layers`
    //    + `replay_done.chart_snapshots_by_run[reportRunId]`
    const feishuDm = makeMockAdapter("feishu");
    const dmBackend = makeMockBackend({
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "user_message",
            data: JSON.stringify({
              turn_index: 2,
              query_type: "system",
              run_id: "run-dm-report-2",
              report_run_id: "run-dm-report-2",
              run_completed_at: "2026-11-25T10:02:00Z",
              metadata: {
                report_source: {
                  kind: "command_report",
                  command_id: "bg-123456abcdef",
                  chart_layers: [
                    {
                      workspace_id: "ws-1",
                      chart_id: "NVDA:1day",
                      symbol: "NVDA",
                      timeframe: "1day",
                      layer_id: "layer-1",
                      title: "NVDA 后台回报图",
                    },
                  ],
                },
              },
            }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "后台任务已完成。",
              turn_index: 2,
              response_id: "run-dm-report-2",
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              latest_run_id: "run-dm-report-2",
              chart_snapshots_by_run: {
                "run-dm-report-2": [
                  {
                    status: "ready",
                    chart_id: "NVDA:1day",
                    layer_id: "layer-1",
                    title: "NVDA 后台回报图",
                    command_id: "bg-123456abcdef",
                    snapshot_key: SNAP_KEY,
                    sha256: "e".repeat(64),
                  },
                ],
              },
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: "run-grp-replay-1",
        sent_to_current_count: 1,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
    });
    const dmAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuDm]]);
    const dmDelivery = new DeliveryService({
      backend: dmBackend,
      adapters: dmAdapters,
      webBaseUrl: WEB_BASE,
    });
    const dmRoutes = new AgentRoutesHandler({
      backend: dmBackend,
      adapters: dmAdapters,
      deliveryService: dmDelivery,
      webBaseUrl: WEB_BASE,
    });
    const dmOrch = new ChannelGatewayOrchestrator({
      backend: dmBackend,
      adapters: dmAdapters,
      deliveryService: dmDelivery,
      agentRoutes: dmRoutes,
      webBaseUrl: WEB_BASE,
    });

    const dmOk = await dmOrch.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-dm-report-2",
      turnIndex: 2,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      isBackgroundRun: true,
    });
    expect(dmOk).toBe(true);
    expect(feishuDm.sentCards).toHaveLength(1);
    expect(feishuDm.sentCards[0]?.images).toHaveLength(1);
  });

  it("delivers DM chart snapshots and widget deep links during automation finish", async () => {
    const feishu = makeMockAdapter("feishu");
    const autoRunId = "run-auto-1";
    const execId = "exec-auto-1";
    const backend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_execId, req) => ({
        status: 200,
        data: {
          execution_id: execId,
          status: req.mark_finished ? ("finished" as const) : ("finishing" as const),
          previous_status: req.mark_finished ? ("finishing" as const) : ("registered" as const),
          user_id: "user-alice",
          workspace_id: "ws-1",
          thread_id: THREAD_ID,
          run_id: autoRunId,
          chart_snapshots: [
            {
              status: "ready",
              chart_id: "NVDA:1day",
              layer_id: "layer-1",
              title: "Automation NVDA",
              snapshot_key: SNAP_KEY,
              sha256: "f".repeat(64),
            },
          ],
          outbound_blocked: false,
          automation_name: "Daily Brief",
          finish_status: "completed",
          final_text: "每日自动化总结已完成。",
          targets: [
            {
              entry: "feishu:@me",
              address: "feishu:@me",
              name: "Feishu DM",
              ok: true,
              message: null,
              agent_sent: false,
            },
          ],
        },
      })),
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "user_message",
            data: JSON.stringify({ turn_index: 1, run_id: autoRunId }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "html_widget",
              artifact_id: "widget_auto_1",
              response_id: autoRunId,
              payload: { title: "Auto Widget" },
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({ thread_id: THREAD_ID }),
          };
        })(),
      })),
    });

    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
    const deliveryService = new DeliveryService({ backend, adapters, webBaseUrl: WEB_BASE });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });

    const resp = await agentRoutes.handleFinishAutomationRun("user-alice", execId, {
      status: "completed",
      thread_id: THREAD_ID,
      final_text: "每日自动化总结已完成。",
    });
    expect(resp.status).toBe(200);
    // 1 summary text + 2 cards (1 chart PNG card + 1 widget deep link card)
    expect(feishu.sentTexts.length + feishu.sentCards.length).toBeGreaterThanOrEqual(2);
    expect(feishu.sentCards.some((c) => (c.images?.length ?? 0) === 1)).toBe(true);
    expect(
      feishu.sentCards.some((c) => JSON.stringify(c.card).includes("widget_auto_1")),
    ).toBe(true);
  });

  it("delivers group chat chart deep link on normal background command report completion with real metadata and run_end.chart_layers", async () => {
    const discordGrp = makeMockAdapter("discord");
    const bgRunId = "run-bg-grp-live-1";
    const backend = makeMockBackend({
      streamThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          // Real live SSE stream for command report-back: top-level report_source on `metadata`,
          // assistant text chunk, and `run_end` carrying `chart_layers` (no `user_message`, no `chart_snapshots`).
          yield {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: bgRunId,
              turn_index: 2,
              report_source: {
                kind: "command_report",
                command_id: "bg-grp-cmd-1",
                chart_layers: [
                  {
                    workspace_id: "ws-1",
                    chart_id: "NVDA:1day",
                    symbol: "NVDA",
                    timeframe: "1day",
                    layer_id: "layer-bg-grp-1",
                    title: "Secret Group Chart Title",
                  },
                ],
              },
            }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "后台命令执行完成，结果已汇总。",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: bgRunId,
              outcome: "completed",
              outbound_blocked: false,
              chart_layers: [
                {
                  workspace_id: "ws-1",
                  chart_id: "NVDA:1day",
                  symbol: "NVDA",
                  timeframe: "1day",
                  layer_id: "layer-bg-grp-1",
                  title: "Secret Group Chart Title",
                },
                {
                  workspace_id: "ws-1",
                  chart_id: "AAPL:1day",
                  symbol: "AAPL",
                  timeframe: "1day",
                  layer_id: "layer-bg-grp-2",
                  title: "Second Command Report Layer",
                },
              ],
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: bgRunId,
        sent_to_current_count: 1,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
      authorizeAndClaimDelivery: vi.fn(async (req) => ({
        claim_status: "claimed" as const,
        delivery_key: req.delivery_key,
        delivery_attempt: 1,
        platform: "discord",
        target_address: "discord:111/222",
        recipient_id: "222",
        is_current: true,
      })),
      getThreadStatus: vi.fn(async () => ({
        status: "idle",
        pending_report_back: false,
      })),
    });

    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["discord", discordGrp]]);
    const deliveryService = new DeliveryService({ backend, adapters, webBaseUrl: WEB_BASE });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });
    const orch = new ChannelGatewayOrchestrator({
      backend,
      adapters,
      deliveryService,
      agentRoutes,
      webBaseUrl: WEB_BASE,
    });

    const ok = await (
      orch as unknown as {
        consumeBackgroundRunStream: (p: Record<string, unknown>) => Promise<boolean>;
      }
    ).consumeBackgroundRunStream({
      handle: {
        threadId: THREAD_ID,
        userId: "user-alice",
        platform: "discord",
        targetAddress: "discord:111/222",
        scopeKey: "chat:111/222",
        generation: 1,
        lastSeenTurnIndex: 1,
        abortController: new AbortController(),
        syncingReconnect: false,
        needsReconnectSync: false,
      },
      runId: bgRunId,
      turnIndex: 2,
    });

    expect(ok).toBe(true);
    expect(discordGrp.sentTexts).toHaveLength(2);
    const allText = discordGrp.sentTexts.map((t) => t.text).join("\n");
    expect(allText).toContain("layer-bg-grp-1");
    expect(allText).toContain("layer-bg-grp-2");
    expect(allText).not.toContain("Secret Group Chart Title");
    expect(allText).toContain("请登录后在网页端查看");
  });

  it("uses settlement run_id so delayed automation finish excludes follow-up user turn charts and settles charts/widgets on restart recovery", async () => {
    const feishu = makeMockAdapter("feishu");
    const oldAutoRunId = "run-auto-old-1";
    const followUpRunId = "run-user-followup-2";
    const execId = "exec-auto-delayed-1";
    const seenFinishReqs: Array<Record<string, unknown>> = [];

    const backend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_execId, req) => {
        seenFinishReqs.push(req as unknown as Record<string, unknown>);
        return {
          status: 200,
          data: {
            execution_id: execId,
            status: req.mark_finished ? ("finished" as const) : ("finishing" as const),
            previous_status: req.mark_finished
              ? ("finishing" as const)
              : ("registered" as const),
            user_id: "user-alice",
            workspace_id: "ws-1",
            thread_id: THREAD_ID,
            run_id: req.run_id ?? oldAutoRunId,
            chart_snapshots: [],
            outbound_blocked: false,
            automation_name: "Delayed Brief",
            finish_status: "completed",
            final_text: "旧自动化结果。",
            targets: [
              {
                entry: "feishu:@me",
                address: "feishu:@me",
                name: "Feishu DM",
                ok: true,
                message: null,
                agent_sent: false,
              },
            ],
          },
        };
      }),
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          // Turn 1: old automation run (no charts/widgets)
          yield {
            event: "user_message",
            data: JSON.stringify({ turn_index: 1, run_id: oldAutoRunId }),
          };
          // Turn 2: user follow-up in the same thread during delayed settlement (has chart + widget!)
          yield {
            event: "user_message",
            data: JSON.stringify({ turn_index: 2, run_id: followUpRunId }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "chart_layer",
              artifact_id: "layer-followup-leak",
              response_id: followUpRunId,
              payload: {
                chart_id: "TSLA:1day",
                layer_id: "layer-followup-leak",
                title: "User Follow-up Chart",
              },
            }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "html_widget",
              artifact_id: "widget_followup_leak",
              response_id: followUpRunId,
              payload: { title: "User Follow-up Widget" },
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              outbound_blocked_by_run: {
                [oldAutoRunId]: false,
                [followUpRunId]: false,
              },
            }),
          };
        })(),
      })),
    });

    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
    const deliveryService = new DeliveryService({ backend, adapters, webBaseUrl: WEB_BASE });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });

    const resp = await agentRoutes.handleFinishAutomationRun("user-alice", execId, {
      status: "completed",
      thread_id: THREAD_ID,
      run_id: oldAutoRunId,
      final_text: "旧自动化结果。",
    });
    expect(resp.status).toBe(200);
    expect(seenFinishReqs[0]?.run_id).toBe(oldAutoRunId);
    // Follow-up user turn's chart & widget MUST NOT be sent with old automation execution!
    expect(feishu.sentCards).toHaveLength(0);

    // Part 2: Gateway restart recovery of a `finishing` automation run with persisted `run_id`
    const feishuRec = makeMockAdapter("feishu");
    const recRunId = "run-auto-recover-1";
    const recExecId = "exec-auto-recover-1";
    const recBackend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_id, req) => ({
        status: 200,
        data: {
          execution_id: recExecId,
          status: "finished" as const,
          previous_status: "finishing" as const,
          user_id: "user-alice",
          workspace_id: "ws-1",
          thread_id: THREAD_ID,
          run_id: recRunId,
          automation_name: "Recover Brief",
          finish_status: "completed",
          final_text: "恢复完成。",
          targets: (req.targets ?? []) as never[],
        },
      })),
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "user_message",
            data: JSON.stringify({ turn_index: 1, run_id: recRunId }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "html_widget",
              artifact_id: "widget_rec_1",
              response_id: recRunId,
              payload: { title: "Recovered Widget" },
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              outbound_blocked_by_run: { [recRunId]: false },
            }),
          };
        })(),
      })),
    });
    const recAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuRec]]);
    const recDelivery = new DeliveryService({
      backend: recBackend,
      adapters: recAdapters,
      webBaseUrl: WEB_BASE,
    });
    const recAgentRoutes = new AgentRoutesHandler({
      backend: recBackend,
      adapters: recAdapters,
      deliveryService: recDelivery,
      webBaseUrl: WEB_BASE,
    });
    const recOrch = new ChannelGatewayOrchestrator({
      backend: recBackend,
      adapters: recAdapters,
      deliveryService: recDelivery,
      agentRoutes: recAgentRoutes,
      webBaseUrl: WEB_BASE,
    });

    await (
      recOrch as unknown as {
        recoverSingleAutomationRun: (row: Record<string, unknown>) => Promise<void>;
      }
    ).recoverSingleAutomationRun({
      execution_id: recExecId,
      user_id: "user-alice",
      workspace_id: "ws-1",
      thread_id: THREAD_ID,
      run_id: recRunId,
      chart_snapshots: [
        {
          status: "ready",
          chart_id: "NVDA:1day",
          layer_id: "layer-1",
          title: "Recovered NVDA",
          snapshot_key: SNAP_KEY,
          sha256: "e".repeat(64),
        },
      ],
      outbound_blocked: false,
      automation_name: "Recover Brief",
      entries: ["feishu:@me"],
      status: "finishing",
      finish_status: "completed",
      final_text: "恢复完成。",
      targets: [
        {
          entry: "feishu:@me",
          address: "feishu:@me",
          name: "Feishu DM",
          ok: true,
          message: null,
          agent_sent: false,
        },
      ],
    });

    expect(feishuRec.sentCards.some((c) => (c.images?.length ?? 0) === 1)).toBe(true);
    expect(
      feishuRec.sentCards.some((c) => JSON.stringify(c.card).includes("widget_rec_1")),
    ).toBe(true);
  });

  it("uses fixed copy when status check times out or fails and propagates outbound_blocked from replay_done", async () => {
    const secretTitle = "CONFIDENTIAL_MODEL_TITLE_XYZ";

    // 1. DM status check fails/times out without any 200 response -> MUST use fixed copy without model title
    const feishuFail = makeMockAdapter("feishu");
    const backendTimeout = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async () => {
        throw new Error("ETIMEDOUT");
      }),
    });
    const resUnconfirmed = await deliverTurnChartsAndWidgets({
      backend: backendTimeout,
      adapters: new Map([["feishu", feishuFail]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-status-timeout-1",
      platform: "feishu",
      targetAddress: "feishu:@me",
      charts: [{ chartId: "NVDA:1day", layerId: "layer-to-1", title: secretTitle }],
      waitTimeoutMs: 30,
      pollIntervalMs: 10,
      sleepFn: async () => undefined,
    });
    expect(resUnconfirmed.sentImages).toBe(0);
    expect(resUnconfirmed.sentLinks).toBe(1);
    const failCardJson = JSON.stringify(feishuFail.sentCards);
    expect(failCardJson).not.toContain(secretTitle);
    expect(failCardJson).toContain("看盘图表已生成，请登录后在网页端查看交互视图。");

    // 2. replay_done.outbound_blocked_by_run[runId] === true -> skips status poll and uses fixed copy
    const feishuReplayBlk = makeMockAdapter("feishu");
    const statusSpy = vi.fn();
    const blkRunId = "run-replay-blocked-1";
    const blkBackend = makeMockBackend({
      getChartSnapshotsStatus: statusSpy,
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "user_message",
            data: JSON.stringify({
              turn_index: 1,
              run_id: blkRunId,
              run_completed_at: "2026-11-25T10:00:00Z",
            }),
          };
          yield {
            event: "tool_call_result",
            data: JSON.stringify({
              turn_index: 1,
              response_id: blkRunId,
              artifact: {
                chart_layers: [
                  {
                    chart_id: "NVDA:1day",
                    layer_id: "layer-blk-1",
                    title: secretTitle,
                  },
                ],
              },
            }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "完成。",
              turn_index: 1,
              response_id: blkRunId,
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              latest_run_id: blkRunId,
              outbound_blocked_by_run: { [blkRunId]: true },
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: blkRunId,
        sent_to_current_count: 1,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
    });
    const blkAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuReplayBlk]]);
    const blkDelivery = new DeliveryService({
      backend: blkBackend,
      adapters: blkAdapters,
      webBaseUrl: WEB_BASE,
    });
    const blkRoutes = new AgentRoutesHandler({
      backend: blkBackend,
      adapters: blkAdapters,
      deliveryService: blkDelivery,
      webBaseUrl: WEB_BASE,
    });
    const blkOrch = new ChannelGatewayOrchestrator({
      backend: blkBackend,
      adapters: blkAdapters,
      deliveryService: blkDelivery,
      agentRoutes: blkRoutes,
      webBaseUrl: WEB_BASE,
    });

    const okBlk = await blkOrch.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: blkRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
    });
    expect(okBlk).toBe(true);
    expect(statusSpy).not.toHaveBeenCalled();
    expect(feishuReplayBlk.sentCards).toHaveLength(1);
    expect(feishuReplayBlk.sentCards[0]?.images).toBeUndefined();
    expect(JSON.stringify(feishuReplayBlk.sentCards)).not.toContain(secretTitle);
  });

  it("collects and merges charts, widgets, and run_end.chart_snapshots when attaching to an in-flight stream after disconnect replay", async () => {
    const feishu = makeMockAdapter("feishu");
    const liveRunId = "run-reconnect-live-1";
    const backend = makeMockBackend({
      replayThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "user_message",
            data: JSON.stringify({ turn_index: 1 }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "前半段分析，",
              turn_index: 1,
              response_id: liveRunId,
            }),
          };
          yield {
            event: "snapshot",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              active_runs: [
                {
                  run_id: liveRunId,
                  lane: "main",
                  cursor: { last_event_id: 0 },
                },
              ],
            }),
          };
          yield {
            event: "replay_done",
            data: JSON.stringify({ thread_id: THREAD_ID }),
          };
        })(),
      })),
      streamThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "chart_layer",
              artifact_id: "layer-1",
              payload: {
                chart_id: "NVDA:1day",
                layer_id: "layer-1",
                title: "续接流图表",
              },
            }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "html_widget",
              artifact_id: "widget_attached_1",
              payload: { title: "续接流组件" },
            }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "后半段结论。",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: liveRunId,
              outcome: "completed",
              outbound_blocked: false,
              chart_snapshots: [
                {
                  status: "ready",
                  chart_id: "NVDA:1day",
                  layer_id: "layer-1",
                  title: "续接流图表",
                  snapshot_key: SNAP_KEY,
                  sha256: "a".repeat(64),
                },
              ],
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: liveRunId,
        sent_to_current_count: 1,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
    });

    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
    const deliveryService = new DeliveryService({ backend, adapters, webBaseUrl: WEB_BASE });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });
    const orch = new ChannelGatewayOrchestrator({
      backend,
      adapters,
      deliveryService,
      agentRoutes,
      webBaseUrl: WEB_BASE,
    });

    const ok = await orch.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: liveRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
    });

    expect(ok).toBe(true);
    // Chart PNG card + widget deep-link card must both be delivered from the attached live stream!
    expect(feishu.sentCards).toHaveLength(2);
    expect(feishu.sentCards[0]?.images).toHaveLength(1);
    expect(JSON.stringify(feishu.sentCards[1]?.card)).toContain("widget_attached_1");
  });

  it("preserves two charts sharing the same layer_id and matches snapshots by (chart_id, layer_id) while passing delivery_key and delivery_attempt on download", async () => {
    const feishu = makeMockAdapter("feishu");
    const downloadCalls: Array<Record<string, unknown>> = [];
    const backend = makeMockBackend({
      getChartSnapshotsStatus: vi.fn(async () => ({
        run_id: "run-same-layer-1",
        thread_id: THREAD_ID,
        outbound_blocked: false,
        has_pending: false,
        snapshots: [
          {
            status: "ready",
            chart_id: "NVDA:1day",
            layer_id: "support",
            title: "NVDA Support",
            snapshot_key: SNAP_KEY,
            sha256: "b".repeat(64),
          },
        ],
      })),
      downloadChartSnapshot: vi.fn(async (req) => {
        downloadCalls.push(req as unknown as Record<string, unknown>);
        return {
          content: PNG_BYTES,
          mimeType: "image/png",
          filename: "chart_support.png",
        };
      }),
    });

    const res = await deliverTurnChartsAndWidgets({
      backend,
      adapters: new Map([["feishu", feishu]]),
      webBaseUrl: WEB_BASE,
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: "run-same-layer-1",
      platform: "feishu",
      targetAddress: "feishu:@me",
      outboundBlocked: false,
      charts: [
        { chartId: "NVDA:1day", layerId: "support", title: "NVDA Support" },
        { chartId: "AAPL:1day", layerId: "support", title: "AAPL Support" },
      ],
    });

    // Both charts MUST be preserved (neither overwritten), only NVDA gets PNG, AAPL gets link-only
    expect(res.settled).toBe(true);
    expect(res.sentImages).toBe(1);
    expect(res.sentLinks).toBe(1);
    expect(feishu.sentCards).toHaveLength(2);
    expect(feishu.sentCards[0]?.images).toHaveLength(1);
    expect(JSON.stringify(feishu.sentCards[0]?.card)).toContain("NVDA%3A1day");
    expect(feishu.sentCards[1]?.images).toBeUndefined();
    expect(JSON.stringify(feishu.sentCards[1]?.card)).toContain("AAPL%3A1day");
    // Download MUST carry deliveryKey + deliveryAttempt for @me pinning
    expect(downloadCalls).toHaveLength(1);
    expect(downloadCalls[0]?.deliveryKey).toBe("charts:run-same-layer-1");
    expect(downloadCalls[0]?.deliveryAttempt).toBe(1);
  });

  it("paginates /messages/replay via before_turn during delayed automation finish and restart recovery when the target run is older than the first 10 turns", async () => {
    const oldAutoRunId = "run-auto-page2-turn1";
    const execId = "exec-auto-page2-1";
    const seenReplayCalls: Array<unknown> = [];

    const buildPaginatedReplay = () =>
      vi.fn(async (_uid: string, _tid: string, opts?: unknown) => {
        seenReplayCalls.push(opts);
        const beforeTurn =
          typeof opts === "number"
            ? opts
            : opts && typeof opts === "object" && "beforeTurn" in opts
              ? (opts as { beforeTurn?: number }).beforeTurn
              : undefined;

        if (beforeTurn === undefined) {
          // Page 1: recent 10 turns (turns 3..12), none of which is oldAutoRunId,
          // even though replay_done already carries thread-wide outbound_blocked_by_run.
          return {
            status: 200,
            isSse: true,
            events: (async function* () {
              yield {
                event: "history_page",
                data: JSON.stringify({ first_turn_index: 3, has_more: true }),
              };
              for (let turn = 3; turn <= 12; turn += 1) {
                yield {
                  event: "user_message",
                  data: JSON.stringify({ turn_index: turn, run_id: `run-followup-${turn}` }),
                };
              }
              yield {
                event: "replay_done",
                data: JSON.stringify({
                  thread_id: THREAD_ID,
                  outbound_blocked_by_run: { [oldAutoRunId]: false },
                }),
              };
            })(),
          };
        }

        expect(beforeTurn).toBe(3);
        // Page 2: turns 1..2 containing the target automation run's replay-only chart + html_widget
        return {
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "history_page",
              data: JSON.stringify({ first_turn_index: 1, has_more: false }),
            };
            yield {
              event: "user_message",
              data: JSON.stringify({ turn_index: 1, run_id: oldAutoRunId }),
            };
            yield {
              event: "tool_call_result",
              data: JSON.stringify({
                turn_index: 1,
                response_id: oldAutoRunId,
                artifact: {
                  chart_layers: [
                    {
                      chart_id: "NVDA:1day",
                      layer_id: "layer-page2-replay-only",
                      title: "Page 2 Replay Chart",
                    },
                  ],
                },
              }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "html_widget",
                artifact_id: "widget_page2_auto_1",
                response_id: oldAutoRunId,
                payload: { title: "Page 2 Auto Widget" },
              }),
            };
            yield {
              event: "replay_done",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                outbound_blocked_by_run: { [oldAutoRunId]: false },
              }),
            };
          })(),
        };
      });

    // 1. Delayed finish via handleFinishAutomationRun
    const feishuDelay = makeMockAdapter("feishu");
    const delayBackend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_id, req) => ({
        status: 200,
        data: {
          execution_id: execId,
          status: req.mark_finished ? ("finished" as const) : ("finishing" as const),
          previous_status: req.mark_finished ? ("finishing" as const) : ("registered" as const),
          user_id: "user-alice",
          workspace_id: "ws-1",
          thread_id: THREAD_ID,
          run_id: oldAutoRunId,
          chart_snapshots: [],
          outbound_blocked: false,
          automation_name: "Old Page2 Brief",
          finish_status: "completed",
          final_text: "延迟结算结果。",
          targets: [
            {
              entry: "feishu:@me",
              address: "feishu:@me",
              name: "Feishu DM",
              ok: true,
              message: null,
              agent_sent: false,
            },
          ],
        },
      })),
      replayThreadMessages: buildPaginatedReplay(),
    });
    const delayAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuDelay]]);
    const delayDelivery = new DeliveryService({
      backend: delayBackend,
      adapters: delayAdapters,
      webBaseUrl: WEB_BASE,
    });
    const delayRoutes = new AgentRoutesHandler({
      backend: delayBackend,
      adapters: delayAdapters,
      deliveryService: delayDelivery,
      webBaseUrl: WEB_BASE,
    });

    const resp = await delayRoutes.handleFinishAutomationRun("user-alice", execId, {
      status: "completed",
      thread_id: THREAD_ID,
      run_id: oldAutoRunId,
      final_text: "延迟结算结果。",
    });
    expect(resp.status).toBe(200);
    expect(seenReplayCalls).toHaveLength(2);
    expect(seenReplayCalls[1]).toMatchObject({ beforeTurn: 3 });
    const delayCardsJson = JSON.stringify(feishuDelay.sentCards);
    expect(delayCardsJson).toContain("layer-page2-replay-only");
    expect(delayCardsJson).toContain("widget_page2_auto_1");

    // 2. Restart recovery via recoverSingleAutomationRun
    seenReplayCalls.length = 0;
    const feishuRec = makeMockAdapter("feishu");
    const recBackend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_id, req) => ({
        status: 200,
        data: {
          execution_id: "exec-auto-page2-rec",
          status: "finished" as const,
          previous_status: "finishing" as const,
          user_id: "user-alice",
          workspace_id: "ws-1",
          thread_id: THREAD_ID,
          run_id: oldAutoRunId,
          automation_name: "Old Page2 Recover",
          finish_status: "completed",
          final_text: "重启恢复结果。",
          targets: (req.targets ?? []) as never[],
        },
      })),
      replayThreadMessages: buildPaginatedReplay(),
    });
    const recAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuRec]]);
    const recDelivery = new DeliveryService({
      backend: recBackend,
      adapters: recAdapters,
      webBaseUrl: WEB_BASE,
    });
    const recRoutes = new AgentRoutesHandler({
      backend: recBackend,
      adapters: recAdapters,
      deliveryService: recDelivery,
      webBaseUrl: WEB_BASE,
    });
    const recOrch = new ChannelGatewayOrchestrator({
      backend: recBackend,
      adapters: recAdapters,
      deliveryService: recDelivery,
      agentRoutes: recRoutes,
      webBaseUrl: WEB_BASE,
    });

    await (
      recOrch as unknown as {
        recoverSingleAutomationRun: (row: Record<string, unknown>) => Promise<void>;
      }
    ).recoverSingleAutomationRun({
      execution_id: "exec-auto-page2-rec",
      user_id: "user-alice",
      workspace_id: "ws-1",
      thread_id: THREAD_ID,
      run_id: oldAutoRunId,
      chart_snapshots: [],
      outbound_blocked: false,
      automation_name: "Old Page2 Recover",
      entries: ["feishu:@me"],
      status: "finishing",
      finish_status: "completed",
      final_text: "重启恢复结果。",
      targets: [
        {
          entry: "feishu:@me",
          address: "feishu:@me",
          name: "Feishu DM",
          ok: true,
          message: null,
          agent_sent: false,
        },
      ],
    });
    expect(seenReplayCalls).toHaveLength(2);
    expect(seenReplayCalls[1]).toMatchObject({ beforeTurn: 3 });
    const recCardsJson = JSON.stringify(feishuRec.sentCards);
    expect(recCardsJson).toContain("layer-page2-replay-only");
    expect(recCardsJson).toContain("widget_page2_auto_1");
  });

  it("re-reads full replay after run_end when in-flight replay only returns user_message with real cursor.last_event_id and preserves both pre-cursor and post-cursor charts and widgets", async () => {
    const feishu = makeMockAdapter("feishu");
    const liveRunId = "run-cursor-split-1";
    let replayCallCount = 0;

    const backend = makeMockBackend({
      replayThreadMessages: vi.fn(async () => {
        replayCallCount += 1;
        if (replayCallCount === 1) {
          // Pass 1 (in-flight stub): only `user_message` + real `snapshot` with `cursor.last_event_id: 10`.
          // Pre-cursor widget + un-snapshotted chart_layer were emitted at seq < 10 and are absent here.
          return {
            status: 200,
            isSse: true,
            events: (async function* () {
              yield {
                event: "user_message",
                data: JSON.stringify({ turn_index: 1 }),
              };
              yield {
                event: "snapshot",
                data: JSON.stringify({
                  active_runs: [
                    {
                      lane: "main",
                      run_id: liveRunId,
                      cursor: { last_event_id: 10 },
                    },
                  ],
                  revalidations: 0,
                }),
              };
              yield {
                event: "replay_done",
                data: JSON.stringify({ thread_id: THREAD_ID }),
              };
            })(),
          };
        }

        // Pass 2 (post-run_end full replay): includes BOTH pre-cursor and post-cursor artifacts + final text!
        return {
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "user_message",
              data: JSON.stringify({
                turn_index: 1,
                run_id: liveRunId,
                run_completed_at: "2026-11-26T00:10:00Z",
              }),
            };
            // Pre-cursor artifacts (emitted before cursor 10):
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "html_widget",
                artifact_id: "widget_pre_cursor_1",
                response_id: liveRunId,
                payload: { title: "游标前组件" },
              }),
            };
            yield {
              event: "tool_call_result",
              data: JSON.stringify({
                turn_index: 1,
                response_id: liveRunId,
                artifact: {
                  chart_layers: [
                    {
                      chart_id: "AAPL:1day",
                      layer_id: "layer-pre-cursor-nosnap",
                      title: "游标前未快照图表",
                    },
                  ],
                },
              }),
            };
            // Post-cursor tool call, artifacts, and final text:
            yield {
              event: "tool_calls",
              data: JSON.stringify({
                agent: "main",
                turn_index: 1,
                response_id: liveRunId,
              }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "chart_layer",
                artifact_id: "layer-1",
                response_id: liveRunId,
                payload: {
                  chart_id: "NVDA:1day",
                  layer_id: "layer-1",
                  title: "游标后图表",
                },
              }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "html_widget",
                artifact_id: "widget_post_cursor_1",
                response_id: liveRunId,
                payload: { title: "游标后组件" },
              }),
            };
            yield {
              event: "message_chunk",
              data: JSON.stringify({
                agent: "main",
                role: "assistant",
                content: "游标后最终完整正文。",
                turn_index: 1,
                response_id: liveRunId,
              }),
            };
            yield {
              event: "replay_done",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                latest_run_id: liveRunId,
                outbound_blocked_by_run: { [liveRunId]: false },
                chart_snapshots_by_run: {
                  [liveRunId]: [
                    {
                      status: "ready",
                      chart_id: "NVDA:1day",
                      layer_id: "layer-1",
                      title: "游标后图表",
                      snapshot_key: SNAP_KEY,
                      sha256: "a".repeat(64),
                    },
                  ],
                },
              }),
            };
          })(),
        };
      }),
      streamThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          // Resumed live stream after cursor 10 has post-cursor tool_calls + post-cursor chart/widget + full final text!
          yield {
            event: "tool_calls",
            data: JSON.stringify({ agent: "main" }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "chart_layer",
              artifact_id: "layer-1",
              payload: {
                chart_id: "NVDA:1day",
                layer_id: "layer-1",
                title: "游标后图表",
              },
            }),
          };
          yield {
            event: "artifact",
            data: JSON.stringify({
              artifact_type: "html_widget",
              artifact_id: "widget_post_cursor_1",
              payload: { title: "游标后组件" },
            }),
          };
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "游标后最终完整正文。",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: liveRunId,
              outcome: "completed",
              outbound_blocked: false,
              chart_snapshots: [
                {
                  status: "ready",
                  chart_id: "NVDA:1day",
                  layer_id: "layer-1",
                  title: "游标后图表",
                  snapshot_key: SNAP_KEY,
                  sha256: "a".repeat(64),
                },
              ],
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: liveRunId,
        sent_to_current_count: 1,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
    });

    const adapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]);
    const deliveryService = new DeliveryService({ backend, adapters, webBaseUrl: WEB_BASE });
    const agentRoutes = new AgentRoutesHandler({
      backend,
      adapters,
      deliveryService,
      webBaseUrl: WEB_BASE,
    });
    const orch = new ChannelGatewayOrchestrator({
      backend,
      adapters,
      deliveryService,
      agentRoutes,
      webBaseUrl: WEB_BASE,
    });

    const ok = await orch.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: liveRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
    });

    expect(ok).toBe(true);
    expect(backend.streamThreadMessages).toHaveBeenCalledWith("user-alice", THREAD_ID, {
      runId: liveRunId,
      lastEventId: "10",
    });
    // Must re-read /messages/replay after run_end instead of settling directly on tail stream
    expect(replayCallCount).toBe(2);
    // Both pre-cursor (layer-pre-cursor-nosnap, widget_pre_cursor_1) and post-cursor (layer-1 PNG, widget_post_cursor_1) must be sent!
    expect(feishu.sentCards).toHaveLength(4);
    const cardsJson = JSON.stringify(feishu.sentCards);
    expect(cardsJson).toContain("layer-pre-cursor-nosnap");
    expect(cardsJson).toContain("layer-1");
    expect(cardsJson).toContain("widget_pre_cursor_1");
    expect(cardsJson).toContain("widget_post_cursor_1");
    expect(feishu.sentCards.some((c) => (c.images?.length ?? 0) === 1)).toBe(true);
  });

  it("uses fixed copy when /internal/snapshots/status omits outbound_blocked or returns null", async () => {
    const secretTitle = "CONFIDENTIAL_UNKNOWN_STATUS_TITLE";

    for (const statusBody of [
      // Missing outbound_blocked field entirely
      {
        run_id: "run-missing-flag",
        thread_id: THREAD_ID,
        has_pending: false,
        snapshots: [],
      },
      // Explicit null (unknown) outbound_blocked
      {
        run_id: "run-null-flag",
        thread_id: THREAD_ID,
        outbound_blocked: null,
        has_pending: false,
        snapshots: [],
      },
    ]) {
      const feishu = makeMockAdapter("feishu");
      const backend = makeMockBackend({
        getChartSnapshotsStatus: vi.fn(
          async () => statusBody as ChartSnapshotsStatusResponse,
        ),
      });

      const waited = await waitForChartSnapshotsReady({
        backend,
        runId: statusBody.run_id,
        threadId: THREAD_ID,
        platform: "feishu",
        expectedChartKeys: ["NVDA:1day\0layer-unk-1"],
        isDm: true,
      });
      expect(waited.outboundBlocked).toBe(false);
      expect(waited.statusConfirmed).toBe(false);

      const res = await deliverTurnChartsAndWidgets({
        backend,
        adapters: new Map([["feishu", feishu]]),
        webBaseUrl: WEB_BASE,
        userId: "user-alice",
        threadId: THREAD_ID,
        runId: statusBody.run_id,
        platform: "feishu",
        targetAddress: "feishu:@me",
        charts: [{ chartId: "NVDA:1day", layerId: "layer-unk-1", title: secretTitle }],
      });
      expect(res.settled).toBe(true);
      expect(res.sentImages).toBe(0);
      expect(res.sentLinks).toBe(1);
      const cardsJson = JSON.stringify(feishu.sentCards);
      expect(cardsJson).not.toContain(secretTitle);
      expect(cardsJson).toContain("看盘图表已生成，请登录后在网页端查看交互视图。");
    }
  });

  it("re-reads full replay after run_end even when elapsed time exceeds busyThreadMaxWaitMs, while unfinished runs still stop waiting on timeout", async () => {
    const WAIT_CAP_MS = 30 * 60 * 1000;

    // 1. Long-running turn (>30m): non-zero cursor live attach receives `run_end` after elapsed > busyThreadMaxWaitMs.
    //    Gateway MUST still do one immediate full `/messages/replay` read and deliver text, chart, and widget.
    let nowMs = 1_000;
    let replayCallsDone = 0;
    const longRunId = "run-overtime-run-end-1";
    const feishuDone = makeMockAdapter("feishu");
    const backendDone = makeMockBackend({
      replayThreadMessages: vi.fn(async () => {
        replayCallsDone += 1;
        if (replayCallsDone === 1) {
          return {
            status: 200,
            isSse: true,
            events: (async function* () {
              yield {
                event: "user_message",
                data: JSON.stringify({ turn_index: 1 }),
              };
              yield {
                event: "snapshot",
                data: JSON.stringify({
                  active_runs: [
                    {
                      lane: "main",
                      run_id: longRunId,
                      cursor: { last_event_id: 10 },
                    },
                  ],
                  revalidations: 0,
                }),
              };
              yield {
                event: "replay_done",
                data: JSON.stringify({ thread_id: THREAD_ID }),
              };
            })(),
          };
        }

        return {
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "user_message",
              data: JSON.stringify({
                turn_index: 1,
                run_id: longRunId,
                run_completed_at: "2026-11-26T00:40:00Z",
              }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "html_widget",
                artifact_id: "widget_overtime_1",
                response_id: longRunId,
                payload: { title: "长回合组件" },
              }),
            };
            yield {
              event: "artifact",
              data: JSON.stringify({
                artifact_type: "chart_layer",
                artifact_id: "layer-1",
                response_id: longRunId,
                payload: {
                  chart_id: "NVDA:1day",
                  layer_id: "layer-1",
                  title: "长回合图表",
                },
              }),
            };
            yield {
              event: "message_chunk",
              data: JSON.stringify({
                agent: "main",
                role: "assistant",
                content: "超过30分钟后的完整分析结论。",
                turn_index: 1,
                response_id: longRunId,
              }),
            };
            yield {
              event: "replay_done",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                latest_run_id: longRunId,
                outbound_blocked_by_run: { [longRunId]: false },
                chart_snapshots_by_run: {
                  [longRunId]: [
                    {
                      status: "ready",
                      chart_id: "NVDA:1day",
                      layer_id: "layer-1",
                      title: "长回合图表",
                      snapshot_key: SNAP_KEY,
                      sha256: "a".repeat(64),
                    },
                  ],
                },
              }),
            };
          })(),
        };
      }),
      streamThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          // Simulate the run taking 35 minutes before `run_end` arrives
          nowMs += 35 * 60 * 1000;
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "尾段。",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: longRunId,
              outcome: "completed",
              outbound_blocked: false,
            }),
          };
        })(),
      })),
      updateInboundProgress: vi.fn(async () => ({
        ok: true,
        run_id: longRunId,
        sent_to_current_count: 0,
        has_in_flight_current_text: false,
        has_unknown_current_text: false,
        has_in_flight_or_unknown_current_text: false,
      })),
    });

    const adaptersDone = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuDone]]);
    const deliveryDone = new DeliveryService({
      backend: backendDone,
      adapters: adaptersDone,
      webBaseUrl: WEB_BASE,
    });
    const agentRoutesDone = new AgentRoutesHandler({
      backend: backendDone,
      adapters: adaptersDone,
      deliveryService: deliveryDone,
      webBaseUrl: WEB_BASE,
    });
    const orchDone = new ChannelGatewayOrchestrator({
      backend: backendDone,
      adapters: adaptersDone,
      deliveryService: deliveryDone,
      agentRoutes: agentRoutesDone,
      webBaseUrl: WEB_BASE,
      busyThreadMaxWaitMs: WAIT_CAP_MS,
      nowFn: () => nowMs,
      backoffDelayFn: async () => undefined,
    });

    const settledDone = await orchDone.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: longRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
    });

    expect(settledDone).toBe(true);
    expect(replayCallsDone).toBe(2);
    expect(feishuDone.sentTexts.map((t) => t.text).join("\n")).toContain(
      "超过30分钟后的完整分析结论。",
    );
    expect(feishuDone.sentCards).toHaveLength(2);
    const doneCardsJson = JSON.stringify(feishuDone.sentCards);
    expect(doneCardsJson).toContain("layer-1");
    expect(doneCardsJson).toContain("widget_overtime_1");
    expect(feishuDone.sentCards.some((c) => (c.images?.length ?? 0) === 1)).toBe(true);

    // 2. Unfinished turn (no `run_end` before live stream drops) exceeding `busyThreadMaxWaitMs`
    //    MUST still stop waiting and return false without extra replay passes.
    let nowUnfinished = 1_000;
    let replayCallsUnfinished = 0;
    const unfinishedRunId = "run-overtime-unfinished-1";
    const feishuUnfinished = makeMockAdapter("feishu");
    const backendUnfinished = makeMockBackend({
      replayThreadMessages: vi.fn(async () => {
        replayCallsUnfinished += 1;
        return {
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "user_message",
              data: JSON.stringify({ turn_index: 1 }),
            };
            yield {
              event: "snapshot",
              data: JSON.stringify({
                active_runs: [
                  {
                    lane: "main",
                    run_id: unfinishedRunId,
                    cursor: { last_event_id: 10 },
                  },
                ],
                revalidations: 0,
              }),
            };
            yield {
              event: "replay_done",
              data: JSON.stringify({ thread_id: THREAD_ID }),
            };
          })(),
        };
      }),
      streamThreadMessages: vi.fn(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          // Exceed wait cap, then EOF without `run_end`
          nowUnfinished += 35 * 60 * 1000;
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              role: "assistant",
              content: "尚未结束的片段。",
            }),
          };
        })(),
      })),
    });

    const adaptersUnfinished = new Map<ChannelPlatform, ChannelAdapter>([
      ["feishu", feishuUnfinished],
    ]);
    const deliveryUnfinished = new DeliveryService({
      backend: backendUnfinished,
      adapters: adaptersUnfinished,
      webBaseUrl: WEB_BASE,
    });
    const agentRoutesUnfinished = new AgentRoutesHandler({
      backend: backendUnfinished,
      adapters: adaptersUnfinished,
      deliveryService: deliveryUnfinished,
      webBaseUrl: WEB_BASE,
    });
    const orchUnfinished = new ChannelGatewayOrchestrator({
      backend: backendUnfinished,
      adapters: adaptersUnfinished,
      deliveryService: deliveryUnfinished,
      agentRoutes: agentRoutesUnfinished,
      webBaseUrl: WEB_BASE,
      busyThreadMaxWaitMs: WAIT_CAP_MS,
      nowFn: () => nowUnfinished,
      backoffDelayFn: async () => undefined,
    });

    const settledUnfinished = await orchUnfinished.deliverReplayFallback({
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: unfinishedRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
    });

    expect(settledUnfinished).toBe(false);
    expect(replayCallsUnfinished).toBe(1);
    expect(feishuUnfinished.sentTexts).toHaveLength(0);
    expect(feishuUnfinished.sentCards).toHaveLength(0);
  });

  it("BW2: reads target run directly by run_id beyond 500 turns and preserves pending finishing state when target run cannot be read", async () => {
    const deepRunId = "run-auto-turn-1-of-600";
    const execId = "exec-bw2-deep";

    // 1) When replayThreadRun is available, target run at turn 1 (beyond 50 pages / 500 turns)
    //    is read directly in 1 call without paginating replayThreadMessages, and charts are delivered.
    const feishuDirect = makeMockAdapter("feishu");
    const markFinishedCallsDirect: boolean[] = [];
    const replayThreadMessagesSpy = vi.fn(async () => {
      throw new Error("Should not paginate replayThreadMessages when replayThreadRun is available");
    });
    const replayThreadRunSpy = vi.fn(async (_uid: string, _tid: string, rid: string) => ({
      status: 200,
      isSse: true as const,
      events: (async function* () {
        yield {
          event: "user_message",
          data: JSON.stringify({ turn_index: 1, run_id: rid }),
        };
        yield {
          event: "artifact",
          data: JSON.stringify({
            artifact_type: "chart_layer",
            artifact_id: "layer-turn1-deep",
            response_id: rid,
            payload: {
              chart_id: "NVDA:1day",
              layer_id: "layer-turn1-deep",
              title: "Deep Historical Chart",
            },
          }),
        };
        yield {
          event: "replay_done",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: rid,
            turn_index: 1,
            outbound_blocked_by_run: { [rid]: false },
          }),
        };
      })(),
    }));

    const directBackend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_id, req) => {
        markFinishedCallsDirect.push(Boolean(req.mark_finished));
        return {
          status: 200,
          data: {
            execution_id: execId,
            status: req.mark_finished ? ("finished" as const) : ("finishing" as const),
            previous_status: req.mark_finished ? ("finishing" as const) : ("registered" as const),
            user_id: "user-alice",
            workspace_id: "ws-1",
            thread_id: THREAD_ID,
            run_id: deepRunId,
            chart_snapshots: [],
            outbound_blocked: false,
            automation_name: "Deep Turn Brief",
            finish_status: "completed",
            final_text: "深度历史回合摘要。",
            targets: [
              {
                entry: "feishu:@me",
                address: "feishu:@me",
                name: "Feishu DM",
                ok: true,
                message: null,
                agent_sent: false,
              },
            ],
          },
        };
      }),
      replayThreadMessages: replayThreadMessagesSpy,
      replayThreadRun: replayThreadRunSpy,
    });
    const directAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuDirect]]);
    const directDelivery = new DeliveryService({
      backend: directBackend,
      adapters: directAdapters,
      webBaseUrl: WEB_BASE,
    });
    const directRoutes = new AgentRoutesHandler({
      backend: directBackend,
      adapters: directAdapters,
      deliveryService: directDelivery,
      webBaseUrl: WEB_BASE,
    });

    const directResp = await directRoutes.handleFinishAutomationRun("user-alice", execId, {
      status: "completed",
      thread_id: THREAD_ID,
      run_id: deepRunId,
      final_text: "深度历史回合摘要。",
    });
    expect(directResp.status).toBe(200);
    expect(replayThreadRunSpy).toHaveBeenCalledTimes(1);
    expect(replayThreadRunSpy).toHaveBeenCalledWith("user-alice", THREAD_ID, deepRunId);
    expect(replayThreadMessagesSpy).not.toHaveBeenCalled();
    expect(JSON.stringify(feishuDirect.sentCards)).toContain("layer-turn1-deep");
    expect(markFinishedCallsDirect).toEqual([false, true]);

    // 2) When the target run cannot be read (e.g. 404 / unreachable or beyond fallback page cap),
    //    executeAutomationRunFinish MUST keep pending recovery state (MUST NOT call mark_finished: true).
    const feishuMissing = makeMockAdapter("feishu");
    const markFinishedCallsMissing: boolean[] = [];
    const missingBackend = makeMockBackend({
      finishAutomationRun: vi.fn(async (_id, req) => {
        markFinishedCallsMissing.push(Boolean(req.mark_finished));
        return {
          status: 200,
          data: {
            execution_id: "exec-bw2-missing",
            status: "finishing" as const,
            previous_status: "finishing" as const,
            user_id: "user-alice",
            workspace_id: "ws-1",
            thread_id: THREAD_ID,
            run_id: deepRunId,
            chart_snapshots: [],
            outbound_blocked: false,
            automation_name: "Missing Run Brief",
            finish_status: "completed",
            final_text: "等待目标回合可读取。",
            targets: [
              {
                entry: "feishu:@me",
                address: "feishu:@me",
                name: "Feishu DM",
                ok: true,
                message: null,
                agent_sent: false,
              },
            ],
          },
        };
      }),
      replayThreadRun: vi.fn(async () => ({
        status: 404,
        isSse: false as const,
        errorBody: { detail: "Run not found in thread" },
      })),
    });
    const missingAdapters = new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishuMissing]]);
    const missingDelivery = new DeliveryService({
      backend: missingBackend,
      adapters: missingAdapters,
      webBaseUrl: WEB_BASE,
    });
    const missingRoutes = new AgentRoutesHandler({
      backend: missingBackend,
      adapters: missingAdapters,
      deliveryService: missingDelivery,
      webBaseUrl: WEB_BASE,
    });

    await missingRoutes.executeAutomationRunFinish({
      executionId: "exec-bw2-missing",
      userId: "user-alice",
      threadId: THREAD_ID,
      runId: deepRunId,
      initialSnapshots: [],
      outboundBlocked: false,
      automationName: "Missing Run Brief",
      finishStatus: "completed",
      finalText: "等待目标回合可读取。",
      targets: [
        {
          entry: "feishu:@me",
          address: "feishu:@me",
          name: "Feishu DM",
          ok: true,
          message: null,
          agent_sent: false,
        },
      ],
    });
    expect(markFinishedCallsMissing).toEqual([]);
  });
});
