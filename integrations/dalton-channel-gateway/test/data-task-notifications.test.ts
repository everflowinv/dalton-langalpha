import { createHash } from "node:crypto";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { describe, expect, it, vi } from "vitest";
import { HttpBackendClient } from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/index.js";
import { loadConfig } from "../src/config.js";
import type { DataTaskAuthorizePlanResponse } from "../src/data-task-notification-types.js";
import { buildConfiguredAdapters } from "../src/main.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";
import {
  createMockBackend,
  fetchGatewayServer,
  TEST_SERVICE_TOKEN,
} from "./helpers.js";

const SYNTH_RUN_ID = "12345678-1234-4234-8234-1234567890ab";
const SYNTH_USER_ID = "pm_synthetic_owner";

function makeSyntheticPng(label: string): Buffer {
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    Buffer.from(`IHDR-${label}-SYNTHETIC`, "utf8"),
  ]);
}

function sha256Hex(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
}

function createFakeOutboundLarkClient(handlers?: {
  onImageCreate?: (buf: Buffer, idx: number) => Promise<{
    code?: number;
    msg?: string;
    data?: { image_key?: string };
  }>;
  onMessageCreate?: (call: {
    receive_id_type: string;
    receive_id: string;
    msg_type: string;
    content: string;
  }) => Promise<{ code?: number; msg?: string; data?: { message_id?: string } }>;
}): {
  client: Lark.Client;
  uploadedImages: Buffer[];
  sentMessages: Array<{
    receive_id_type: string;
    receive_id: string;
    msg_type: string;
    content: string;
  }>;
} {
  const uploadedImages: Buffer[] = [];
  const sentMessages: Array<{
    receive_id_type: string;
    receive_id: string;
    msg_type: string;
    content: string;
  }> = [];

  const client = {
    im: {
      image: {
        create: async (req: { data: { image: Buffer } }) => {
          const idx = uploadedImages.length;
          uploadedImages.push(Buffer.from(req.data.image));
          if (handlers?.onImageCreate) {
            return await handlers.onImageCreate(req.data.image, idx);
          }
          return { code: 0, data: { image_key: `img_v3_synth_${idx}` } };
        },
      },
      message: {
        create: async (req: {
          params: { receive_id_type: string };
          data: { receive_id: string; msg_type: string; content: string };
        }) => {
          const call = {
            receive_id_type: req.params.receive_id_type,
            receive_id: req.data.receive_id,
            msg_type: req.data.msg_type,
            content: req.data.content,
          };
          sentMessages.push(call);
          if (handlers?.onMessageCreate) {
            return await handlers.onMessageCreate(call);
          }
          return {
            code: 0,
            data: { message_id: `om_synth_${sentMessages.length}` },
          };
        },
      },
    },
    contact: {
      v3: {
        user: { get: async () => ({ code: 0, data: {} }) },
      },
    },
  } as unknown as Lark.Client;

  return { client, uploadedImages, sentMessages };
}

describe("Dalton data-task report delivery via channel gateway", () => {
  it("refuses unauthorized tokens, forbidden caller fields, URLs, and backend-denied plans without platform calls", async () => {
    const backend = createMockBackend();
    const feishuAdapter = new FakeChannelAdapter("feishu");
    const discordAdapter = new FakeChannelAdapter("discord");
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_BACKEND_URL: "http://127.0.0.1:8000",
    });
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["feishu", feishuAdapter],
        ["discord", discordAdapter],
      ]),
    });

    try {
      // 1. Missing or invalid X-Service-Token -> 401
      const unauth = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Service-Token": "wrong" },
          body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
        },
      );
      expect(unauth.status).toBe(401);

      // 2. Arbitrary caller content/path/url/user_id/succeeded fields -> 400
      for (const extra of [
        { card: { schema: "2.0" } },
        { files: ["/etc/passwd"] },
        { url: "https://evil.example/a.png" },
        { user_id: "override_owner" },
        { succeeded: true },
      ]) {
        const res = await fetchGatewayServer(
          app,
          "http://127.0.0.1:8962/internal/data-task-notifications",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Service-Token": TEST_SERVICE_TOKEN,
            },
            body: JSON.stringify({
              run_id: SYNTH_RUN_ID,
              target: "feishu:@me",
              ...extra,
            }),
          },
        );
        expect(res.status).toBe(400);
      }

      // 3. URL supplied as target -> 400
      const urlTarget = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({
            run_id: SYNTH_RUN_ID,
            target: "https://evil.example/webhook",
          }),
        },
      );
      expect(urlTarget.status).toBe(400);
      expect(backend.authorizeDataTaskNotificationPlan).not.toHaveBeenCalled();

      // 4. Preview run / disabled task / removed route denied by backend -> 0 platform calls
      for (const deny of [
        { status: "failed" as const, code: "preview_run_forbidden" },
        { status: "skipped" as const, code: "task_disabled" },
        { status: "skipped" as const, code: "target_not_routed" },
        { status: "failed" as const, code: "not_linked" },
      ]) {
        vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce({
          authorized: false,
          status: deny.status,
          code: deny.code,
          message: `Denied: ${deny.code}`,
        });
        const res = await fetchGatewayServer(
          app,
          "http://127.0.0.1:8962/internal/data-task-notifications",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Service-Token": TEST_SERVICE_TOKEN,
            },
            body: JSON.stringify({
              run_id: SYNTH_RUN_ID,
              target: "feishu:chat:oc_synth_grp",
            }),
          },
        );
        expect(res.status).toBe(200);
        const data = (await res.json()) as { status: string; code: string };
        expect(data.status).toBe(deny.status);
        expect(data.code).toBe(deny.code);
      }

      expect(feishuAdapter.sentCards).toHaveLength(0);
      expect(feishuAdapter.sentTexts).toHaveLength(0);
      expect(discordAdapter.sentTexts).toHaveLength(0);
    } finally {
      await app.stop();
    }
  });

  it("preserves Feishu Card Schema 2.0 structure (T13/T27), nested collapsible_panel images, and rejects unresolved placeholders", async () => {
    const img0 = makeSyntheticPng("CN-MAIN");
    const img1 = makeSyntheticPng("PANEL-SUB");
    const { client, uploadedImages, sentMessages } = createFakeOutboundLarkClient();

    const feishuAdapter = new FeishuChannelAdapter({
      appId: "cli_synth_dalton_app",
      appSecret: "sec_synth_dalton_secret",
      outgoingOnly: true,
      client,
    });

    const originalCard = {
      schema: "2.0",
      config: { wide_screen_mode: true, update_multi: true },
      header: {
        title: { tag: "plain_text", content: "亚洲四地杠杆/拥挤度监控 — 合成测试" },
        subtitle: { tag: "plain_text", content: "截面窗口" },
        template: "indigo",
        text_tag_list: [
          {
            tag: "text_tag",
            text: { tag: "plain_text", content: "实时监控" },
            color: "wathet",
          },
        ],
      },
      body: {
        direction: "vertical",
        vertical_spacing: "8px",
        elements: [
          {
            tag: "markdown",
            content: "**🇨🇳 中国A股**\n\n| 指标 | 值 |\n|---|---|\n| 融资 | 9.1% |",
          },
          { tag: "__img__", path: "artifact:card0-img0" },
          { tag: "hr" },
          {
            tag: "collapsible_panel",
            expanded: false,
            header: { title: { tag: "markdown", content: "**折叠附图**" } },
            border: { color: "grey", corner_radius: "6px" },
            elements: [
              { tag: "markdown", content: "面板内文字" },
              { tag: "__img__", path: "artifact:card0-img1" },
            ],
          },
          { tag: "hr" },
          {
            tag: "markdown",
            text_size: "notation",
            content: "<font color='grey'>**口径说明**</font>",
          },
        ],
      },
    };

    // Unresolved __img__ placeholder is rejected BEFORE uploading or sending any card
    await expect(
      feishuAdapter.sendCard({
        targetAddress: "feishu:oc_synth_group",
        card: structuredClone(originalCard),
        images: [
          {
            placeholder: "artifact:card0-img0",
            content: img0,
            mimeType: "image/png",
            filename: "cn_latest.png",
          },
        ],
      }),
    ).rejects.toThrow(/Unresolved __img__ placeholder/);
    expect(uploadedImages).toHaveLength(0);
    expect(sentMessages).toHaveLength(0);

    // Full end-to-end delivery through DataTaskNotificationService
    const backend = createMockBackend();
    const deliveryKey = `data_task:${SYNTH_RUN_ID}:feishu:oc_synth_group:item:0`;
    vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce({
      authorized: true,
      status: "authorized",
      run_id: SYNTH_RUN_ID,
      job_key: "asia-leverage-crossview-daily",
      owner_user_id: SYNTH_USER_ID,
      owner_platform_user_id: "ou_synth_owner",
      platform: "feishu",
      target_kind: "dm",
      requested_target: "feishu:@me",
      canonical_target_address: "feishu:@me",
      recipient_id: "ou_synth_owner",
      items: [
        {
          item_index: 0,
          delivery_key: deliveryKey,
          kind: "feishu_card",
          card: structuredClone(originalCard),
          images: [
            {
              artifact_id: "card0-img0",
              placeholder: "artifact:card0-img0",
              filename: "cn_latest.png",
              mime_type: "image/png",
              size_bytes: img0.byteLength,
              sha256: sha256Hex(img0),
            },
            {
              artifact_id: "card0-img1",
              placeholder: "artifact:card0-img1",
              filename: "kr_latest.png",
              mime_type: "image/png",
              size_bytes: img1.byteLength,
              sha256: sha256Hex(img1),
            },
          ],
        },
      ],
    });
    vi.mocked(backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
      claim_status: "claimed",
      platform: "feishu",
      target_address: "feishu:@me",
      recipient_id: "ou_synth_owner",
      is_current: false,
    });
    vi.mocked(backend.downloadDataTaskRunArtifact).mockImplementation(
      async (_runId, artifactId) => {
        const buf = artifactId === "card0-img0" ? img0 : img1;
        return {
          content: buf,
          mimeType: "image/png",
          filename: `${artifactId}.png`,
          sha256Header: sha256Hex(buf),
        };
      },
    );

    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      DALTON_GATEWAY_MODE: "outgoing",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
    });
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["feishu", feishuAdapter],
      ]),
    });

    try {
      const resp = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
        },
      );
      expect(resp.status).toBe(200);
      const body = (await resp.json()) as { status: string; items_sent: number };
      expect(body.status).toBe("sent");
      expect(body.items_sent).toBe(1);

      expect(uploadedImages).toHaveLength(2);
      expect(uploadedImages[0]?.equals(img0)).toBe(true);
      expect(uploadedImages[1]?.equals(img1)).toBe(true);
      expect(sentMessages).toHaveLength(1);
      expect(sentMessages[0]?.receive_id_type).toBe("open_id");
      expect(sentMessages[0]?.receive_id).toBe("ou_synth_owner");
      expect(sentMessages[0]?.msg_type).toBe("interactive");

      const sentCard = JSON.parse(sentMessages[0]!.content) as Record<string, unknown>;
      const expectedSentCard = structuredClone(originalCard) as Record<string, unknown>;
      const elems = (expectedSentCard.body as { elements: unknown[] }).elements;
      elems[1] = {
        tag: "img",
        img_key: "img_v3_synth_0",
        alt: { tag: "plain_text", content: "" },
        mode: "fit_horizontal",
        preview: true,
      };
      const panelElems = (elems[3] as { elements: unknown[] }).elements;
      panelElems[1] = {
        tag: "img",
        img_key: "img_v3_synth_1",
        alt: { tag: "plain_text", content: "" },
        mode: "fit_horizontal",
        preview: true,
      };
      expect(sentCard).toEqual(expectedSentCard);
    } finally {
      await app.stop();
    }
  });

  it("delivers T26 two ordered Discord text+chart items, verifies SHA256, and returns unavailable without claiming when Discord adapter is absent", async () => {
    const chart0 = makeSyntheticPng("T26-TOP20");
    const chart1 = makeSyntheticPng("T26-BOT20");

    const t26Plan: DataTaskAuthorizePlanResponse = {
      authorized: true,
      status: "authorized",
      run_id: SYNTH_RUN_ID,
      job_key: "us-topbottom-dm-v23",
      owner_user_id: SYNTH_USER_ID,
      owner_platform_user_id: "100000000000000001",
      platform: "discord",
      target_kind: "dm",
      requested_target: "discord:user:100000000000000001",
      canonical_target_address: "discord:@me",
      recipient_id: "100000000000000001",
      items: [
        {
          item_index: 0,
          delivery_key: `data_task:${SYNTH_RUN_ID}:discord:100000000000000001:item:0`,
          kind: "text_and_file",
          text: "Synthetic US Top 20 DeMark Summary",
          artifact: {
            artifact_id: "item0-media0",
            filename: "0.png",
            mime_type: "image/png",
            size_bytes: chart0.byteLength,
            sha256: sha256Hex(chart0),
          },
        },
        {
          item_index: 1,
          delivery_key: `data_task:${SYNTH_RUN_ID}:discord:100000000000000001:item:1`,
          kind: "text_and_file",
          text: "Synthetic US Bottom 20 DeMark Summary",
          artifact: {
            artifact_id: "item1-media0",
            filename: "1.png",
            mime_type: "image/png",
            size_bytes: chart1.byteLength,
            sha256: sha256Hex(chart1),
          },
        },
      ],
    };

    const backend = createMockBackend();
    vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValue(
      structuredClone(t26Plan),
    );
    vi.mocked(backend.downloadDataTaskRunArtifact).mockImplementation(
      async (_runId, artifactId) => {
        const buf = artifactId === "item0-media0" ? chart0 : chart1;
        return {
          content: buf,
          mimeType: "image/png",
          filename: artifactId === "item0-media0" ? "0.png" : "1.png",
          sha256Header: sha256Hex(buf),
        };
      },
    );

    // 1. Missing Discord adapter (`lane-discord` not yet merged) -> returns `unavailable` BEFORE claiming any item!
    const appNoDiscord = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        DALTON_GATEWAY_MODE: "outgoing",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      }),
      backend,
      adapters: new Map(),
    });

    try {
      const resUnavail = await fetchGatewayServer(
        appNoDiscord,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({
            run_id: SYNTH_RUN_ID,
            target: "discord:user:100000000000000001",
          }),
        },
      );
      const unavailBody = (await resUnavail.json()) as {
        status: string;
        code: string;
      };
      expect(unavailBody.status).toBe("failed");
      expect(unavailBody.code).toBe("unavailable");
      expect(backend.deliveryClaims).toHaveLength(0);
    } finally {
      await appNoDiscord.stop();
    }

    // 2. With FakeChannelAdapter("discord"): delivers both ordered text+chart items and verifies SHA-256
    const discordAdapter = new FakeChannelAdapter("discord", 2000);
    discordAdapter.connected = false;
    discordAdapter.outboundReady = true;

    const appWithDiscord = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        DALTON_GATEWAY_MODE: "outgoing",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      }),
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["discord", discordAdapter],
      ]),
    });

    try {
      const resOk = await fetchGatewayServer(
        appWithDiscord,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({
            run_id: SYNTH_RUN_ID,
            target: "discord:user:100000000000000001",
          }),
        },
      );
      const okBody = (await resOk.json()) as {
        ok: boolean;
        status: string;
        items_sent: number;
      };
      expect(okBody.ok).toBe(true);
      expect(okBody.status).toBe("sent");
      expect(okBody.items_sent).toBe(2);

      expect(discordAdapter.sentTexts.map((t) => t.text)).toEqual([
        "Synthetic US Top 20 DeMark Summary",
        "Synthetic US Bottom 20 DeMark Summary",
      ]);
      expect(discordAdapter.sentFiles).toHaveLength(2);
      expect(sha256Hex(discordAdapter.sentFiles[0]!.content)).toBe(sha256Hex(chart0));
      expect(sha256Hex(discordAdapter.sentFiles[1]!.content)).toBe(sha256Hex(chart1));
    } finally {
      await appWithDiscord.stop();
    }
  });

  it("retries pre-send upload failure from confirmed progress, blocks resend on post-send timeout, and rejects tampered artifacts before send", async () => {
    const img0 = makeSyntheticPng("MULTI-0");
    const img1 = makeSyntheticPng("MULTI-1");

    const ledger = new Map<
      string,
      { status: "in_flight" | "sent" | "partial" | "failed" | "unknown" }
    >();

    let failSecondImageUploadOnce = true;
    let timeoutCardSend = false;

    const { client, sentMessages } = createFakeOutboundLarkClient({
      onImageCreate: async (buf) => {
        if (failSecondImageUploadOnce && buf.equals(img1)) {
          failSecondImageUploadOnce = false;
          return { code: 234001, msg: "transient image upload error" };
        }
        return {
          code: 0,
          data: { image_key: `img_key_${sha256Hex(buf).slice(0, 8)}` },
        };
      },
      onMessageCreate: async () => {
        if (timeoutCardSend) {
          const timeoutErr = Object.assign(new Error("socket timeout"), {
            code: "ETIMEDOUT",
          });
          throw timeoutErr;
        }
        return { code: 0, data: { message_id: `om_${sentMessages.length}` } };
      },
    });

    const feishuAdapter = new FeishuChannelAdapter({
      appId: "cli_synth",
      appSecret: "sec_synth",
      outgoingOnly: true,
      client,
    });

    const backend = createMockBackend();
    const makeTwoCardPlan = (
      runId: string,
      corruptDigest = false,
    ): DataTaskAuthorizePlanResponse => ({
      authorized: true,
      status: "authorized",
      run_id: runId,
      job_key: "sentiment-dashboard-digest",
      owner_user_id: SYNTH_USER_ID,
      owner_platform_user_id: "ou_synth_pm",
      platform: "feishu",
      target_kind: "dm",
      requested_target: "feishu:@me",
      canonical_target_address: "feishu:@me",
      recipient_id: "ou_synth_pm",
      items: [0, 1].map((idx) => {
        const buf = idx === 0 ? img0 : img1;
        return {
          item_index: idx,
          delivery_key: `data_task:${runId}:feishu:ou_synth_pm:item:${idx}`,
          kind: "feishu_card" as const,
          card: {
            schema: "2.0",
            body: {
              elements: [
                { tag: "markdown", content: `Card ${idx}` },
                { tag: "__img__", path: `artifact:card${idx}-img0` },
              ],
            },
          },
          images: [
            {
              artifact_id: `card${idx}-img0`,
              placeholder: `artifact:card${idx}-img0`,
              filename: `${idx}.png`,
              mime_type: "image/png",
              size_bytes: buf.byteLength,
              sha256: corruptDigest ? "0".repeat(64) : sha256Hex(buf),
            },
          ],
        };
      }),
    });

    vi.mocked(backend.authorizeAndClaimDelivery).mockImplementation(async (req) => {
      const existing = ledger.get(req.delivery_key);
      if (existing) {
        if (existing.status === "sent" || existing.status === "partial") {
          return {
            claim_status: "already_completed",
            status: existing.status,
            platform: "feishu",
            target_address: "feishu:@me",
          };
        }
        if (existing.status === "in_flight" || existing.status === "unknown") {
          return {
            claim_status: "in_flight_or_unknown",
            status: "unknown",
            code: "in_flight",
            platform: "feishu",
            target_address: "feishu:@me",
          };
        }
      }
      ledger.set(req.delivery_key, { status: "in_flight" });
      return {
        claim_status: "claimed",
        platform: "feishu",
        target_address: "feishu:@me",
        recipient_id: "ou_synth_pm",
      };
    });
    vi.mocked(backend.updateDeliveryProgress).mockImplementation(async (req) => {
      if (req.status) {
        ledger.set(req.delivery_key, { status: req.status });
      }
      return {
        ok: true,
        delivery_key: req.delivery_key,
        status: req.status ?? "in_flight",
      };
    });
    vi.mocked(backend.downloadDataTaskRunArtifact).mockImplementation(
      async (_runId, artId) => {
        const buf = artId === "card0-img0" ? img0 : img1;
        return {
          content: buf,
          mimeType: "image/png",
          filename: `${artId}.png`,
          sha256Header: sha256Hex(buf),
        };
      },
    );

    const app = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        DALTON_GATEWAY_MODE: "outgoing",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      }),
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["feishu", feishuAdapter],
      ]),
    });

    try {
      // 1. Attempt 1: Card 0 succeeds (`sent`), Card 1 fails image upload (`failed`)
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(SYNTH_RUN_ID),
      );
      const r1 = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
        },
      );
      const b1 = (await r1.json()) as { status: string; items_sent: number };
      expect(b1.status).toBe("failed");
      expect(b1.items_sent).toBe(1);
      expect(sentMessages).toHaveLength(1);

      // 2. Attempt 2 (retry): Card 0 is NOT repeated; ONLY Card 1 is uploaded and sent!
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(SYNTH_RUN_ID),
      );
      const r2 = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
        },
      );
      const b2 = (await r2.json()) as { status: string; items_sent: number };
      expect(b2.status).toBe("sent");
      expect(b2.items_sent).toBe(2);
      expect(sentMessages).toHaveLength(2);

      // 3. Post-send platform timeout (`ETIMEDOUT`) marks item `unknown`; subsequent retry sends NOTHING
      const timeoutRunId = "22222222-2222-4222-8222-222222222222";
      timeoutCardSend = true;
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(timeoutRunId),
      );
      const rTimeout = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ run_id: timeoutRunId, target: "feishu:@me" }),
        },
      );
      const bTimeout = (await rTimeout.json()) as { status: string };
      expect(bTimeout.status).toBe("unknown");
      const msgCountAfterTimeout = sentMessages.length;

      timeoutCardSend = false;
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(timeoutRunId),
      );
      const rRetryAfterTimeout = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ run_id: timeoutRunId, target: "feishu:@me" }),
        },
      );
      const bRetryAfterTimeout = (await rRetryAfterTimeout.json()) as {
        status: string;
        code: string;
      };
      expect(bRetryAfterTimeout.status).toBe("unknown");
      expect(bRetryAfterTimeout.code).toBe("in_flight");
      expect(sentMessages).toHaveLength(msgCountAfterTimeout);

      // 4. Digest mismatch on downloaded artifact fails BEFORE any platform call
      const corruptRunId = "33333333-3333-4333-8333-333333333333";
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(corruptRunId, true),
      );
      const rCorrupt = await fetchGatewayServer(
        app,
        "http://127.0.0.1:8962/internal/data-task-notifications",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ run_id: corruptRunId, target: "feishu:@me" }),
        },
      );
      const bCorrupt = (await rCorrupt.json()) as {
        status: string;
        code: string;
      };
      expect(bCorrupt.status).toBe("failed");
      expect(bCorrupt.code).toBe("artifact_invalid");
      expect(sentMessages).toHaveLength(msgCountAfterTimeout);
    } finally {
      await app.stop();
    }
  });

  it("starts outgoing-only mode without WS listeners, Weixin sync, or startup recovery while preserving default incoming mode", async () => {
    const wsStartSpy = vi.fn();
    const outgoingCfg = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      DALTON_GATEWAY_MODE: "outgoing",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_FEISHU_APP_ID: "cli_synth_outgoing",
      DALTON_FEISHU_APP_SECRET: "sec_synth_outgoing",
      DALTON_WEIXIN_ENABLED: "1",
    });
    expect(outgoingCfg.mode).toBe("outgoing");
    expect(outgoingCfg.channels.feishu.enabled).toBe(true);
    expect(outgoingCfg.channels.weixin.enabled).toBe(false);

    const { client } = createFakeOutboundLarkClient();
    const outgoingAdapters = buildConfiguredAdapters(outgoingCfg, {
      feishu: {
        client,
        wsClientFactory: () => ({
          start: wsStartSpy,
        }),
      },
    });
    const outgoingBackend = createMockBackend();
    const outgoingApp = createGatewayApp({
      config: outgoingCfg,
      backend: outgoingBackend,
      adapters: outgoingAdapters,
    });

    try {
      await outgoingApp.start();
    } catch (err) {
      // Sandbox environments that forbid TCP bind still validate pre-listen hooks; ignore EPERM on listen
      if ((err as { code?: string })?.code !== "EPERM") {
        throw err;
      }
    } finally {
      expect(wsStartSpy).not.toHaveBeenCalled();
      expect(outgoingBackend.getWeixinState).not.toHaveBeenCalled();
      expect(outgoingBackend.recoverState).not.toHaveBeenCalled();
      await outgoingApp.stop();
    }

    // Default incoming mode retains Weixin sync, adapter.start(), and startup recovery
    const incomingCfg = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
    });
    expect(incomingCfg.mode).toBe("incoming");
    const incomingBackend = createMockBackend();
    const fakeFeishu = new FakeChannelAdapter("feishu");
    const incomingApp = createGatewayApp({
      config: incomingCfg,
      backend: incomingBackend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["feishu", fakeFeishu],
      ]),
    });
    try {
      await incomingApp.start();
    } catch (err) {
      if ((err as { code?: string })?.code !== "EPERM") {
        throw err;
      }
    } finally {
      expect(incomingBackend.getWeixinState).toHaveBeenCalledTimes(1);
      expect(fakeFeishu.started).toBe(true);
      await incomingApp.stop();
    }

    // Verify HttpBackendClient wire paths against an in-process Node HTTP server when loopback bind is permitted
    const fakeBackendServer = createServer((req, res) => {
      if (req.headers["x-service-token"] !== TEST_SERVICE_TOKEN) {
        res.writeHead(401).end();
        return;
      }
      if (
        req.method === "POST" &&
        req.url === "/api/v1/dalton/channels/internal/data-task-notifications/plan"
      ) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            authorized: false,
            status: "skipped",
            code: "task_disabled",
          }),
        );
        return;
      }
      if (
        req.method === "GET" &&
        req.url ===
          `/api/v1/dalton/channels/internal/data-task-notifications/runs/${SYNTH_RUN_ID}/artifacts/card0-img0`
      ) {
        const png = makeSyntheticPng("HTTP-CLIENT");
        res.writeHead(200, {
          "Content-Type": "image/png",
          "Content-Length": String(png.byteLength),
          "X-Artifact-Filename": "0.png",
          "X-Artifact-Sha256": sha256Hex(png),
        });
        res.end(png);
        return;
      }
      res.writeHead(404).end();
    });

    let boundPort: number | null = null;
    try {
      await new Promise<void>((resolveListen, rejectListen) => {
        fakeBackendServer.once("error", rejectListen);
        fakeBackendServer.listen(0, "127.0.0.1", () => resolveListen());
      });
      boundPort = (fakeBackendServer.address() as AddressInfo).port;
    } catch (err) {
      if ((err as { code?: string })?.code !== "EPERM") {
        throw err;
      }
    }

    if (boundPort !== null) {
      try {
        const httpBackend = new HttpBackendClient({
          baseUrl: `http://127.0.0.1:${boundPort}`,
          serviceToken: TEST_SERVICE_TOKEN,
        });
        const plan = await httpBackend.authorizeDataTaskNotificationPlan({
          run_id: SYNTH_RUN_ID,
          target: "feishu:@me",
        });
        expect(plan.authorized).toBe(false);
        expect(plan.code).toBe("task_disabled");

        const art = await httpBackend.downloadDataTaskRunArtifact(
          SYNTH_RUN_ID,
          "card0-img0",
        );
        expect(art.mimeType).toBe("image/png");
        expect(art.filename).toBe("0.png");
        expect(art.sha256Header).toBe(sha256Hex(art.content));
      } finally {
        await new Promise<void>((r) => fakeBackendServer.close(() => r()));
      }
    }
  });
});
