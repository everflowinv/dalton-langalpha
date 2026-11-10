import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { performance } from "node:perf_hooks";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { describe, expect, it, vi } from "vitest";
import { HttpBackendClient } from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/index.js";
import { loadConfig } from "../src/config.js";
import type {
  DataTaskAuthorizePlanResponse,
  DataTaskNotificationResponse,
} from "../src/data-task-notification-types.js";
import {
  buildConfiguredAdapters,
  prepareGatewayRuntime,
} from "../src/main.js";
import { createGatewayApp } from "../src/server.js";
import { createSubsystemLogger, type LoggerSink } from "../src/shared/logger.js";
import type { ChannelPlatform } from "../src/types.js";
import {
  loadFeishuCredentialsFromVault,
  VaultBootstrapError,
  type VaultBootstrapErrorCode,
} from "../src/vault-credentials.js";
import { createMockBackend, TEST_SERVICE_TOKEN } from "./helpers.js";

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

async function listenEphemeralLoopback(server: Server): Promise<number> {
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", rejectListen);
      resolveListen();
    });
  });
  return (server.address() as AddressInfo).port;
}

async function closeHttpServer(server: Server): Promise<void> {
  if (!server.listening) {
    return;
  }
  await new Promise<void>((resolveClose) => {
    server.close(() => resolveClose());
  });
}

function createFakeOutboundLarkClient(handlers?: {
  onImageCreate?: (
    buf: Buffer,
    idx: number,
  ) => Promise<{
    code?: number;
    msg?: string;
    data?: { image_key?: string };
  }>;
  onMessageCreate?: (call: {
    receive_id_type: string;
    receive_id: string;
    msg_type: string;
    content: string;
  }) => Promise<{
    code?: number;
    msg?: string;
    data?: { message_id?: string };
  }>;
  onChatList?: (call: {
    params: { page_size?: number; page_token?: string };
  }) => Promise<{
    code?: number;
    msg?: string;
    data?: {
      items?: Array<{ chat_id?: string; name?: string }>;
      has_more?: boolean;
      page_token?: string;
    };
  }>;
  onChatGet?: (call: {
    path: { chat_id: string };
  }) => Promise<{
    code?: number;
    msg?: string;
    data?: { name?: string; chat_mode?: string; chat_type?: string };
  }>;
  onChatMembersGet?: () => Promise<unknown>;
}): {
  client: Lark.Client;
  uploadedImages: Buffer[];
  sentMessages: Array<{
    receive_id_type: string;
    receive_id: string;
    msg_type: string;
    content: string;
  }>;
  chatListCalls: Array<{ page_size?: number; page_token?: string }>;
  chatGetCalls: string[];
} {
  const uploadedImages: Buffer[] = [];
  const sentMessages: Array<{
    receive_id_type: string;
    receive_id: string;
    msg_type: string;
    content: string;
  }> = [];
  const chatListCalls: Array<{ page_size?: number; page_token?: string }> = [];
  const chatGetCalls: string[] = [];

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
      chat: {
        list: async (req: {
          params: { page_size?: number; page_token?: string };
        }) => {
          chatListCalls.push({ ...req.params });
          if (handlers?.onChatList) {
            return await handlers.onChatList(req);
          }
          return { code: 0, data: { items: [], has_more: false } };
        },
        get: async (req: { path: { chat_id: string } }) => {
          chatGetCalls.push(req.path.chat_id);
          if (handlers?.onChatGet) {
            return await handlers.onChatGet(req);
          }
          return {
            code: 0,
            data: {
              name: `Synth Group ${req.path.chat_id}`,
              chat_mode: "group",
              chat_type: "group",
            },
          };
        },
      },
      chatMembers: {
        get: async () => {
          if (handlers?.onChatMembersGet) {
            return await handlers.onChatMembersGet();
          }
          throw new Error(
            "Personal chatMembers.get check must not be invoked on administrator group report delivery",
          );
        },
      },
    },
    contact: {
      v3: {
        user: { get: async () => ({ code: 0, data: {} }) },
      },
    },
  } as unknown as Lark.Client;

  return {
    client,
    uploadedImages,
    sentMessages,
    chatListCalls,
    chatGetCalls,
  };
}

describe("Dalton data-task report delivery via channel gateway", () => {
  it("refuses unauthorized tokens, forbidden caller fields, URLs, and backend-denied plans without platform calls", async () => {
    const backend = createMockBackend();
    const feishuAdapter = new FakeChannelAdapter("feishu");
    const discordAdapter = new FakeChannelAdapter("discord");
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      DALTON_GATEWAY_MODE: "outgoing",
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

    const { host, port } = await app.start();
    const notifyUrl = `http://${host}:${port}/internal/data-task-notifications`;

    try {
      // 1. Missing or invalid X-Service-Token -> 401
      const unauth = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": "wrong",
        },
        body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
      });
      expect(unauth.status).toBe(401);

      // 2. Arbitrary caller content/path/url/user_id/succeeded fields -> 400
      for (const extra of [
        { card: { schema: "2.0" } },
        { files: ["/etc/passwd"] },
        { url: "https://evil.example/a.png" },
        { user_id: "override_owner" },
        { succeeded: true },
      ]) {
        const res = await fetch(notifyUrl, {
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
        });
        expect(res.status).toBe(400);
      }

      // 3. URL supplied as target -> 400
      const urlTarget = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({
          run_id: SYNTH_RUN_ID,
          target: "https://evil.example/webhook",
        }),
      });
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
        const res = await fetch(notifyUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({
            run_id: SYNTH_RUN_ID,
            target: "feishu:chat:oc_synth_grp",
          }),
        });
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
    const { client, uploadedImages, sentMessages } =
      createFakeOutboundLarkClient();

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
        title: {
          tag: "plain_text",
          content: "亚洲四地杠杆/拥挤度监控 — 合成测试",
        },
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
            content:
              "**🇨🇳 中国A股**\n\n| 指标 | 值 |\n|---|---|\n| 融资 | 9.1% |",
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

    // Full end-to-end delivery through DataTaskNotificationService over real HTTP
    const backend = createMockBackend();
    const deliveryKey = `data_task:${SYNTH_RUN_ID}:feishu:ou_synth_owner:item:0`;
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

    const { host, port } = await app.start();
    try {
      const resp = await fetch(
        `http://${host}:${port}/internal/data-task-notifications`,
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
      const body = (await resp.json()) as DataTaskNotificationResponse;
      expect(body.ok).toBe(true);
      expect(body.status).toBe("sent");
      expect(body.items_sent).toBe(1);

      expect(uploadedImages).toHaveLength(2);
      expect(uploadedImages[0]?.equals(img0)).toBe(true);
      expect(uploadedImages[1]?.equals(img1)).toBe(true);
      expect(sentMessages).toHaveLength(1);
      expect(sentMessages[0]?.receive_id_type).toBe("open_id");
      expect(sentMessages[0]?.receive_id).toBe("ou_synth_owner");
      expect(sentMessages[0]?.msg_type).toBe("interactive");

      const sentCard = JSON.parse(sentMessages[0]!.content) as Record<
        string,
        unknown
      >;
      const expectedSentCard = structuredClone(originalCard) as Record<
        string,
        unknown
      >;
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

    // 1. Missing Discord adapter -> returns `unavailable` BEFORE claiming any item
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

    const listenNoDiscord = await appNoDiscord.start();
    try {
      const resUnavail = await fetch(
        `http://${listenNoDiscord.host}:${listenNoDiscord.port}/internal/data-task-notifications`,
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
      expect(resUnavail.status).toBe(200);
      const unavailBody =
        (await resUnavail.json()) as DataTaskNotificationResponse;
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

    const listenWithDiscord = await appWithDiscord.start();
    try {
      const resOk = await fetch(
        `http://${listenWithDiscord.host}:${listenWithDiscord.port}/internal/data-task-notifications`,
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
      expect(resOk.status).toBe(200);
      const okBody = (await resOk.json()) as DataTaskNotificationResponse;
      expect(okBody.ok).toBe(true);
      expect(okBody.status).toBe("sent");
      expect(okBody.items_sent).toBe(2);

      expect(discordAdapter.sentTexts.map((t) => t.text)).toEqual([
        "Synthetic US Top 20 DeMark Summary",
        "Synthetic US Bottom 20 DeMark Summary",
      ]);
      expect(discordAdapter.sentFiles).toHaveLength(2);
      expect(sha256Hex(discordAdapter.sentFiles[0]!.content)).toBe(
        sha256Hex(chart0),
      );
      expect(sha256Hex(discordAdapter.sentFiles[1]!.content)).toBe(
        sha256Hex(chart1),
      );
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

    vi.mocked(backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
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
      },
    );
    vi.mocked(backend.updateDeliveryProgress).mockImplementation(
      async (req) => {
        if (req.status) {
          ledger.set(req.delivery_key, { status: req.status });
        }
        return {
          ok: true,
          delivery_key: req.delivery_key,
          status: req.status ?? "in_flight",
          sent_to_current_count: 0,
        };
      },
    );
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

    const { host, port } = await app.start();
    const notifyUrl = `http://${host}:${port}/internal/data-task-notifications`;

    try {
      // 1. Attempt 1: Card 0 succeeds (`sent`), Card 1 fails image upload (`failed`)
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(SYNTH_RUN_ID),
      );
      const r1 = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
      });
      const b1 = (await r1.json()) as DataTaskNotificationResponse;
      expect(b1.status).toBe("failed");
      expect(b1.items_sent).toBe(1);
      expect(sentMessages).toHaveLength(1);

      // 2. Attempt 2 (retry): Card 0 is NOT repeated; ONLY Card 1 is uploaded and sent
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(SYNTH_RUN_ID),
      );
      const r2 = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ run_id: SYNTH_RUN_ID, target: "feishu:@me" }),
      });
      const b2 = (await r2.json()) as DataTaskNotificationResponse;
      expect(b2.status).toBe("sent");
      expect(b2.items_sent).toBe(2);
      expect(sentMessages).toHaveLength(2);

      // 3. Post-send platform timeout (`ETIMEDOUT`) marks item `unknown`; subsequent retry sends NOTHING
      const timeoutRunId = "22222222-2222-4222-8222-222222222222";
      timeoutCardSend = true;
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(timeoutRunId),
      );
      const rTimeout = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ run_id: timeoutRunId, target: "feishu:@me" }),
      });
      const bTimeout = (await rTimeout.json()) as DataTaskNotificationResponse;
      expect(bTimeout.status).toBe("unknown");
      const msgCountAfterTimeout = sentMessages.length;

      timeoutCardSend = false;
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(timeoutRunId),
      );
      const rRetryAfterTimeout = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ run_id: timeoutRunId, target: "feishu:@me" }),
      });
      const bRetryAfterTimeout =
        (await rRetryAfterTimeout.json()) as DataTaskNotificationResponse;
      expect(bRetryAfterTimeout.status).toBe("unknown");
      expect(bRetryAfterTimeout.code).toBe("in_flight");
      expect(sentMessages).toHaveLength(msgCountAfterTimeout);

      // 4. Digest mismatch on downloaded artifact fails BEFORE any platform call
      const corruptRunId = "33333333-3333-4333-8333-333333333333";
      vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValueOnce(
        makeTwoCardPlan(corruptRunId, true),
      );
      const rCorrupt = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ run_id: corruptRunId, target: "feishu:@me" }),
      });
      const bCorrupt = (await rCorrupt.json()) as DataTaskNotificationResponse;
      expect(bCorrupt.status).toBe("failed");
      expect(bCorrupt.code).toBe("artifact_invalid");
      expect(sentMessages).toHaveLength(msgCountAfterTimeout);
    } finally {
      await app.stop();
    }
  });

  it("serializes concurrent same-key HTTP notification requests and returns full HTTP responses without duplicating sends", async () => {
    const backend = createMockBackend();
    const feishuAdapter = new FakeChannelAdapter("feishu");
    feishuAdapter.connected = false;
    feishuAdapter.outboundReady = true;

    let activeSends = 0;
    let maxConcurrentSends = 0;
    let completedKeys = new Set<string>();

    feishuAdapter.setSendCardBehavior(async () => {
      activeSends += 1;
      maxConcurrentSends = Math.max(maxConcurrentSends, activeSends);
      await new Promise((r) => setTimeout(r, 35));
      activeSends -= 1;
      return { messageId: "om_concurrent_1" };
    });

    const dKey = `data_task:${SYNTH_RUN_ID}:feishu:ou_synth:item:0`;
    vi.mocked(backend.authorizeDataTaskNotificationPlan).mockResolvedValue({
      authorized: true,
      status: "authorized",
      run_id: SYNTH_RUN_ID,
      job_key: "softbank-nav-monitor",
      owner_user_id: SYNTH_USER_ID,
      owner_platform_user_id: "ou_synth",
      platform: "feishu",
      target_kind: "dm",
      requested_target: "feishu:@me",
      canonical_target_address: "feishu:@me",
      recipient_id: "ou_synth",
      items: [
        {
          item_index: 0,
          delivery_key: dKey,
          kind: "feishu_card",
          card: { schema: "2.0", body: { elements: [{ tag: "hr" }] } },
          images: [],
        },
      ],
    });
    vi.mocked(backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (completedKeys.has(req.delivery_key)) {
          return {
            claim_status: "already_completed",
            status: "sent",
            platform: "feishu",
            target_address: "feishu:@me",
          };
        }
        return {
          claim_status: "claimed",
          platform: "feishu",
          target_address: "feishu:@me",
          recipient_id: "ou_synth",
        };
      },
    );
    vi.mocked(backend.updateDeliveryProgress).mockImplementation(
      async (req) => {
        if (req.status === "sent") {
          completedKeys.add(req.delivery_key);
        }
        return {
          ok: true,
          delivery_key: req.delivery_key,
          status: req.status ?? "in_flight",
          sent_to_current_count: 0,
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

    const { host, port } = await app.start();
    try {
      const postOnce = async (): Promise<{
        status: number;
        body: DataTaskNotificationResponse;
      }> => {
        const res = await fetch(
          `http://${host}:${port}/internal/data-task-notifications`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Service-Token": TEST_SERVICE_TOKEN,
            },
            body: JSON.stringify({
              run_id: SYNTH_RUN_ID,
              target: "feishu:@me",
            }),
          },
        );
        return {
          status: res.status,
          body: (await res.json()) as DataTaskNotificationResponse,
        };
      };

      const [first, second] = await Promise.all([postOnce(), postOnce()]);
      expect(maxConcurrentSends).toBe(1);
      expect(feishuAdapter.sentCards).toHaveLength(1);
      expect(first.status).toBe(200);
      expect(second.status).toBe(200);
      expect(first.body.ok).toBe(true);
      expect(second.body.ok).toBe(true);
      expect(first.body.status).toBe("sent");
      expect(second.body.status).toBe("sent");
      const dupFlags = [
        first.body.items[0]?.duplicate,
        second.body.items[0]?.duplicate,
      ].sort();
      expect(dupFlags).toEqual([false, true]);
    } finally {
      await app.stop();
    }
  });

  it("verifies live group target before claiming or sending, omits personal platformUserId, and rejects missing checkTargetLive, incomplete pagination, or non-member bot", async () => {
    const img0 = makeSyntheticPng("GROUP-CARD-IMG");
    const groupAddress = "feishu:oc_synth_joined_group";
    const groupDeliveryKey = `data_task:${SYNTH_RUN_ID}:feishu:oc_synth_joined_group:item:0`;

    // Plan returned for administrator group delivery omits `owner_platform_user_id` completely
    const makeGroupPlan = (): DataTaskAuthorizePlanResponse => ({
      authorized: true,
      status: "authorized",
      run_id: SYNTH_RUN_ID,
      job_key: "softbank-nav-monitor",
      owner_user_id: SYNTH_USER_ID,
      platform: "feishu",
      target_kind: "group",
      requested_target: "feishu:chat:oc_synth_joined_group",
      canonical_target_address: groupAddress,
      recipient_id: "oc_synth_joined_group",
      items: [
        {
          item_index: 0,
          delivery_key: groupDeliveryKey,
          kind: "feishu_card",
          card: {
            schema: "2.0",
            body: {
              elements: [
                { tag: "markdown", content: "Group Report" },
                { tag: "__img__", path: "artifact:card0-img0" },
              ],
            },
          },
          images: [
            {
              artifact_id: "card0-img0",
              placeholder: "artifact:card0-img0",
              filename: "c0.png",
              mime_type: "image/png",
              size_bytes: img0.byteLength,
              sha256: sha256Hex(img0),
            },
          ],
        },
      ],
    });

    // 1. Adapter lacking `checkTargetLive` -> returns `unavailable` BEFORE claiming or sending
    const backendNoLiveCheck = createMockBackend();
    vi.mocked(
      backendNoLiveCheck.authorizeDataTaskNotificationPlan,
    ).mockResolvedValueOnce(makeGroupPlan());
    const baseFake = new FakeChannelAdapter("feishu");
    const adapterWithoutLiveCheck: ChannelAdapter = {
      platform: "feishu",
      maxChunkChars: 2000,
      isConfigured: () => true,
      isConnected: () => true,
      isReadyForOutbound: () => true,
      start: async () => undefined,
      stop: async () => undefined,
      onInboundEvent: () => undefined,
      splitTextChunks: (t) => baseFake.splitTextChunks(t),
      sendTextChunk: (o) => baseFake.sendTextChunk(o),
      sendFile: (o) => baseFake.sendFile(o),
      sendCard: (o) => baseFake.sendCard(o),
      downloadAttachment: (d, e) => baseFake.downloadAttachment(d, e),
      listAvailableChats: () => baseFake.listAvailableChats(),
    };
    const appNoLiveCheck = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        DALTON_GATEWAY_MODE: "outgoing",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      }),
      backend: backendNoLiveCheck,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["feishu", adapterWithoutLiveCheck],
      ]),
    });
    const listenNoLive = await appNoLiveCheck.start();
    try {
      const rNoLive = await fetch(
        `http://${listenNoLive.host}:${listenNoLive.port}/internal/data-task-notifications`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({
            run_id: SYNTH_RUN_ID,
            target: "feishu:chat:oc_synth_joined_group",
          }),
        },
      );
      const bNoLive = (await rNoLive.json()) as DataTaskNotificationResponse;
      expect(bNoLive.status).toBe("failed");
      expect(bNoLive.code).toBe("unavailable");
      expect(backendNoLiveCheck.deliveryClaims).toHaveLength(0);
      expect(baseFake.sentCards).toHaveLength(0);
    } finally {
      await appNoLiveCheck.stop();
    }

    // 2. Incomplete `im.chat.list` pagination -> rejected as `unavailable` BEFORE claiming or sending
    // 3. Complete `im.chat.list` without target group -> rejected as `not_allowed` BEFORE claiming or sending
    // 4. Complete paginated `im.chat.list` containing target group + `im.chat.get` -> delivers Card 2.0 to `chat_id` without personal `chatMembers.get`
    let listMode: "incomplete" | "not_in_group" | "joined_paginated" =
      "incomplete";

    const { client, uploadedImages, sentMessages, chatListCalls, chatGetCalls } =
      createFakeOutboundLarkClient({
        onChatList: async (call) => {
          if (listMode === "incomplete") {
            return {
              code: 0,
              data: {
                items: [{ chat_id: "oc_synth_other_1", name: "Other 1" }],
                has_more: true,
                // Missing page_token -> incomplete enumeration!
              },
            };
          }
          if (listMode === "not_in_group") {
            return {
              code: 0,
              data: {
                items: [{ chat_id: "oc_synth_other_1", name: "Other 1" }],
                has_more: false,
              },
            };
          }
          if (!call.params.page_token) {
            return {
              code: 0,
              data: {
                items: [{ chat_id: "oc_synth_page_1", name: "Page 1" }],
                has_more: true,
                page_token: "tok_p2",
              },
            };
          }
          return {
            code: 0,
            data: {
              items: [
                { chat_id: "oc_synth_joined_group", name: "Synth Joined Group" },
              ],
              has_more: false,
            },
          };
        },
      });

    const feishuAdapter = new FeishuChannelAdapter({
      appId: "cli_synth_grp",
      appSecret: "sec_synth_grp",
      outgoingOnly: true,
      client,
    });
    const backend = createMockBackend();
    vi.mocked(backend.authorizeDataTaskNotificationPlan).mockImplementation(
      async () => makeGroupPlan(),
    );
    vi.mocked(backend.authorizeAndClaimDelivery).mockResolvedValue({
      claim_status: "claimed",
      platform: "feishu",
      target_address: groupAddress,
      recipient_id: "oc_synth_joined_group",
      is_current: false,
    });
    vi.mocked(backend.downloadDataTaskRunArtifact).mockResolvedValue({
      content: img0,
      mimeType: "image/png",
      filename: "c0.png",
      sha256Header: sha256Hex(img0),
    });

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

    const { host, port } = await app.start();
    const notifyUrl = `http://${host}:${port}/internal/data-task-notifications`;

    try {
      // Case 2: incomplete pagination -> `unavailable`, zero claims, zero sends
      const rIncomplete = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({
          run_id: SYNTH_RUN_ID,
          target: "feishu:chat:oc_synth_joined_group",
        }),
      });
      const bIncomplete =
        (await rIncomplete.json()) as DataTaskNotificationResponse;
      expect(bIncomplete.status).toBe("failed");
      expect(bIncomplete.code).toBe("unavailable");
      expect(backend.deliveryClaims).toHaveLength(0);
      expect(uploadedImages).toHaveLength(0);
      expect(sentMessages).toHaveLength(0);

      // Case 3: bot not in group -> `not_allowed`, zero claims, zero sends
      listMode = "not_in_group";
      const rNotMember = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({
          run_id: SYNTH_RUN_ID,
          target: "feishu:chat:oc_synth_joined_group",
        }),
      });
      const bNotMember =
        (await rNotMember.json()) as DataTaskNotificationResponse;
      expect(bNotMember.status).toBe("failed");
      expect(bNotMember.code).toBe("not_allowed");
      expect(backend.deliveryClaims).toHaveLength(0);
      expect(uploadedImages).toHaveLength(0);
      expect(sentMessages).toHaveLength(0);

      // Case 4: bot present on page 2 + chat.get ok -> claims & delivers Card 2.0 to `chat_id`
      listMode = "joined_paginated";
      chatListCalls.length = 0;
      const rJoined = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({
          run_id: SYNTH_RUN_ID,
          target: "feishu:chat:oc_synth_joined_group",
        }),
      });
      const bJoined = (await rJoined.json()) as DataTaskNotificationResponse;
      expect(bJoined.ok).toBe(true);
      expect(bJoined.status).toBe("sent");
      expect(bJoined.items_sent).toBe(1);
      expect(chatListCalls).toHaveLength(2);
      expect(chatGetCalls).toEqual(["oc_synth_joined_group"]);
      expect(backend.deliveryClaims).toHaveLength(1);
      expect(sentMessages).toHaveLength(1);
      expect(sentMessages[0]?.receive_id_type).toBe("chat_id");
      expect(sentMessages[0]?.receive_id).toBe("oc_synth_joined_group");
    } finally {
      await app.stop();
    }

    // 5. Confirm group check & group sends never forward any personal `platformUserId`
    const inspectBackend = createMockBackend();
    const inspectDiscord = new FakeChannelAdapter("discord", 2000);
    const observedCheckCalls: Array<{
      userId: string;
      platformUserId?: string | null;
      address: string;
      purpose: string;
    }> = [];
    inspectDiscord.setCheckTargetBehavior((opts) => {
      observedCheckCalls.push({ ...opts });
      return { ok: true, address: opts.address, message: null };
    });
    vi.mocked(
      inspectBackend.authorizeDataTaskNotificationPlan,
    ).mockResolvedValueOnce({
      authorized: true,
      status: "authorized",
      run_id: SYNTH_RUN_ID,
      job_key: "quantum-ew-monitor",
      owner_user_id: SYNTH_USER_ID,
      // Even if a future caller ever populated this, group delivery must omit personal platformUserId
      owner_platform_user_id: undefined,
      platform: "discord",
      target_kind: "group",
      requested_target: "discord:channel:999999999999999999",
      canonical_target_address: "discord:999999999999999999",
      recipient_id: "999999999999999999",
      items: [
        {
          item_index: 0,
          delivery_key: `data_task:${SYNTH_RUN_ID}:discord:999999999999999999:item:0`,
          kind: "text",
          text: "Synthetic group alert",
        },
      ],
    });
    vi.mocked(inspectBackend.authorizeAndClaimDelivery).mockResolvedValueOnce({
      claim_status: "claimed",
      platform: "discord",
      target_address: "discord:999999999999999999",
      recipient_id: "999999999999999999",
    });

    const inspectApp = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        DALTON_GATEWAY_MODE: "outgoing",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      }),
      backend: inspectBackend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([
        ["discord", inspectDiscord],
      ]),
    });
    const listenInspect = await inspectApp.start();
    try {
      const rInsp = await fetch(
        `http://${listenInspect.host}:${listenInspect.port}/internal/data-task-notifications`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({
            run_id: SYNTH_RUN_ID,
            target: "discord:channel:999999999999999999",
          }),
        },
      );
      const bInsp = (await rInsp.json()) as DataTaskNotificationResponse;
      expect(bInsp.status).toBe("sent");
      expect(observedCheckCalls).toEqual([
        {
          userId: SYNTH_USER_ID,
          address: "discord:999999999999999999",
          purpose: "data_task_notification",
        },
      ]);
      expect("platformUserId" in observedCheckCalls[0]!).toBe(false);
      expect(inspectDiscord.sentTexts[0]?.recipientPlatformUserId).toBeUndefined();
    } finally {
      await inspectApp.stop();
    }
  });

  it("starts outgoing-only mode without WS listeners, Weixin sync, or startup recovery", async () => {
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

    await outgoingApp.start();
    try {
      expect(wsStartSpy).not.toHaveBeenCalled();
      expect(outgoingBackend.getWeixinState).toHaveBeenCalledTimes(0);
      expect(outgoingBackend.recoverState).toHaveBeenCalledTimes(0);
    } finally {
      await outgoingApp.stop();
    }
  });

  it("preserves default incoming startup mode with adapter start, startup recovery, and two Weixin sync passes", async () => {
    const tempDir = mkdtempSync(join(tmpdir(), "dalton-dtn-incoming-"));
    const incomingCfg = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_WEIXIN_CACHE_DIR: join(tempDir, "weixin"),
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

    await incomingApp.start();
    try {
      await incomingApp.orchestrator.waitForStartupRecovery();
      // 1 sync before listen (`server.ts`) + 1 sync inside `runStartupRecovery` (`orchestrator.ts`)
      expect(incomingBackend.getWeixinState).toHaveBeenCalledTimes(2);
      expect(incomingBackend.recoverState).toHaveBeenCalledTimes(1);
      expect(fakeFeishu.started).toBe(true);
    } finally {
      await incomingApp.stop();
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it("executes HttpBackendClient data-task plan and artifact wire calls over real local HTTP", async () => {
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

    const boundPort = await listenEphemeralLoopback(fakeBackendServer);
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
      await closeHttpServer(fakeBackendServer);
    }
  });

  it("loads dedicated Feishu credentials from Vault before readiness, rejects auth/missing/redirect/timeout/malformed failures without env fallback, reloads on restart, and never logs secret values", async () => {
    const SYNTH_APP_ID_V1 = "cli_synth_vault_app_v1";
    const SYNTH_SECRET_V1 = "sec_synth_vault_never_logged_v1";
    const SYNTH_APP_ID_V2 = "cli_synth_vault_app_v2";
    const SYNTH_SECRET_V2 = "sec_synth_vault_never_logged_v2";
    const BROKEN_SECRET_FRAGMENT = "sec_synth_broken_json_fragment_999";

    const logLines: string[] = [];
    const sink: LoggerSink = {
      debug: (line) => logLines.push(line),
      info: (line) => logLines.push(line),
      warn: (line) => logLines.push(line),
      error: (line) => logLines.push(line),
    };
    const logger = createSubsystemLogger("test-vault-startup", {
      sink,
      enableDebug: true,
    });

    type VaultTestServerBehavior =
      | "valid_v1"
      | "valid_v2"
      | "missing_secret"
      | "empty_200"
      | "auth_401"
      | "redirect_302"
      | "timeout"
      | "malformed_json";

    let serverBehavior: VaultTestServerBehavior = "valid_v1";
    const pendingTimers = new Set<NodeJS.Timeout>();

    const vaultHttpServer = createServer((req, res) => {
      if (
        req.method !== "POST" ||
        req.url !==
          "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials"
      ) {
        res.writeHead(404).end();
        return;
      }
      if (
        serverBehavior === "auth_401" ||
        req.headers["x-service-token"] !== TEST_SERVICE_TOKEN
      ) {
        res.writeHead(401, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.end(
          JSON.stringify({
            detail: `unauthorized ${SYNTH_SECRET_V1}`,
          }),
        );
        return;
      }
      if (serverBehavior === "redirect_302") {
        res.writeHead(302, {
          Location: `http://127.0.0.1:9/evil?leak=${SYNTH_SECRET_V1}`,
        });
        res.end();
        return;
      }
      if (serverBehavior === "timeout") {
        // Hold connection past client timeout
        const t = setTimeout(() => {
          pendingTimers.delete(t);
          if (!res.writableEnded) {
            res.writeHead(504).end();
          }
        }, 300);
        pendingTimers.add(t);
        return;
      }
      if (serverBehavior === "malformed_json") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(`{"credentials":{"DALTON_FEISHU_APP_SECRET":"${BROKEN_SECRET_FRAGMENT}`);
        return;
      }
      if (serverBehavior === "missing_secret") {
        res.writeHead(503, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.end(
          JSON.stringify({
            code: "vault_credentials_missing",
            missing: ["DALTON_FEISHU_APP_SECRET", SYNTH_SECRET_V1],
          }),
        );
        return;
      }
      if (serverBehavior === "empty_200") {
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.end(
          JSON.stringify({
            credentials: {
              DALTON_FEISHU_APP_ID: SYNTH_APP_ID_V1,
              DALTON_FEISHU_APP_SECRET: "   ",
            },
          }),
        );
        return;
      }

      const appId =
        serverBehavior === "valid_v2" ? SYNTH_APP_ID_V2 : SYNTH_APP_ID_V1;
      const appSecret =
        serverBehavior === "valid_v2" ? SYNTH_SECRET_V2 : SYNTH_SECRET_V1;
      res.writeHead(200, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      });
      res.end(
        JSON.stringify({
          credentials: {
            DALTON_FEISHU_APP_ID: appId,
            DALTON_FEISHU_APP_SECRET: appSecret,
          },
        }),
      );
    });

    const backendPort = await listenEphemeralLoopback(vaultHttpServer);
    const backendUrl = `http://127.0.0.1:${backendPort}`;
    const envDir = mkdtempSync(join(tmpdir(), "dalton-gw-envfile-"));

    try {
      // 1. Secure `0600` env file provides `INTERNAL_SERVICE_TOKEN`; Vault provides Feishu credentials BEFORE readiness
      const secureEnvFile = join(envDir, "channel-gateway.env");
      writeFileSync(
        secureEnvFile,
        `# Owner-only service token\nINTERNAL_SERVICE_TOKEN=${TEST_SERVICE_TOKEN}\n`,
        { mode: 0o600 },
      );
      chmodSync(secureEnvFile, 0o600);

      const instantiatedCreds: Array<{ appId: string; appSecret: string }> = [];
      const { client: fakeLarkV1 } = createFakeOutboundLarkClient();
      const runtimeV1 = await prepareGatewayRuntime({
        env: {
          GATEWAY_HOST: "127.0.0.1",
          GATEWAY_PORT: "0",
          DALTON_GATEWAY_MODE: "outgoing",
          DALTON_BACKEND_URL: backendUrl,
          DALTON_CHANNEL_GATEWAY_ENV_FILE: secureEnvFile,
          DALTON_FEISHU_ENABLED: "true",
          DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
          // Even if stale env vars exist, `"vault"` mode strictly ignores them
          DALTON_FEISHU_APP_ID: "cli_stale_env_ignored",
          DALTON_FEISHU_APP_SECRET: "sec_stale_env_ignored",
        },
        logger,
        adapterOverrides: {
          feishu: {
            clientFactory: (creds) => {
              instantiatedCreds.push({
                appId: creds.appId,
                appSecret: creds.appSecret,
              });
              return fakeLarkV1;
            },
          },
        },
      });

      expect(runtimeV1.config.feishuCredentialSource).toBe("vault");
      expect(runtimeV1.config.channels.feishu.appId).toBe("");
      expect(runtimeV1.config.channels.feishu.appSecret).toBe("");
      const adapterV1 = runtimeV1.adapters.get("feishu") as FeishuChannelAdapter;
      expect(adapterV1).toBeDefined();
      expect(adapterV1.isConfigured()).toBe(true);
      expect(adapterV1.isReadyForOutbound()).toBe(true);
      await adapterV1.sendTextChunk({
        targetAddress: "feishu:oc_synth_probe",
        text: "probe",
        chunkIndex: 0,
        totalChunks: 1,
      });
      expect(instantiatedCreds).toEqual([
        { appId: SYNTH_APP_ID_V1, appSecret: SYNTH_SECRET_V1 },
      ]);

      const listenV1 = await runtimeV1.app.start();
      const healthRes = await fetch(
        `http://${listenV1.host}:${listenV1.port}/health`,
        { headers: { "X-Service-Token": TEST_SERVICE_TOKEN } },
      );
      expect(healthRes.status).toBe(200);
      await runtimeV1.app.stop();

      // 2. Credential rotation in Vault + controlled service restart loads v2 credentials before readiness
      serverBehavior = "valid_v2";
      const loadedV2 = await loadFeishuCredentialsFromVault({
        backendUrl,
        serviceToken: TEST_SERVICE_TOKEN,
        logger,
      });
      expect(loadedV2).toEqual({
        appId: SYNTH_APP_ID_V2,
        appSecret: SYNTH_SECRET_V2,
      });

      const { client: fakeLarkV2 } = createFakeOutboundLarkClient();
      const runtimeV2 = await prepareGatewayRuntime({
        env: {
          GATEWAY_HOST: "127.0.0.1",
          GATEWAY_PORT: "0",
          DALTON_GATEWAY_MODE: "outgoing",
          DALTON_BACKEND_URL: backendUrl,
          DALTON_CHANNEL_GATEWAY_ENV_FILE: secureEnvFile,
          DALTON_FEISHU_ENABLED: "true",
          DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
        },
        logger,
        adapterOverrides: {
          feishu: {
            clientFactory: (creds) => {
              instantiatedCreds.push({
                appId: creds.appId,
                appSecret: creds.appSecret,
              });
              return fakeLarkV2;
            },
          },
        },
      });
      const adapterV2 = runtimeV2.adapters.get("feishu") as FeishuChannelAdapter;
      expect(adapterV2?.isConfigured()).toBe(true);
      await adapterV2.sendTextChunk({
        targetAddress: "feishu:oc_synth_probe",
        text: "probe-v2",
        chunkIndex: 0,
        totalChunks: 1,
      });
      expect(instantiatedCreds).toEqual([
        { appId: SYNTH_APP_ID_V1, appSecret: SYNTH_SECRET_V1 },
        { appId: SYNTH_APP_ID_V2, appSecret: SYNTH_SECRET_V2 },
      ]);
      await runtimeV2.app.stop();

      // 3. Error cases: missing values, empty 200, 401 auth failure, 302 redirect, timeout, malformed JSON -> fail closed with zero env fallback!
      const failureCases: Array<{
        behavior: VaultTestServerBehavior;
        expectedCode: VaultBootstrapErrorCode;
      }> = [
        { behavior: "missing_secret", expectedCode: "vault_credentials_missing" },
        { behavior: "empty_200", expectedCode: "vault_credentials_missing" },
        { behavior: "auth_401", expectedCode: "vault_auth_failed" },
        { behavior: "redirect_302", expectedCode: "vault_redirect_rejected" },
        { behavior: "timeout", expectedCode: "vault_timeout" },
        { behavior: "malformed_json", expectedCode: "vault_malformed_response" },
      ];

      for (const tc of failureCases) {
        serverBehavior = tc.behavior;
        let caught: unknown;
        try {
          await prepareGatewayRuntime({
            env: {
              GATEWAY_HOST: "127.0.0.1",
              GATEWAY_PORT: "0",
              DALTON_GATEWAY_MODE: "outgoing",
              DALTON_BACKEND_URL: backendUrl,
              INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
              DALTON_FEISHU_ENABLED: "true",
              DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
              // Must NEVER fall back to environment credentials when Vault fails!
              DALTON_FEISHU_APP_ID: "cli_fallback_forbidden",
              DALTON_FEISHU_APP_SECRET: "sec_fallback_forbidden",
            },
            vaultTimeoutMs: 60,
            logger,
          });
        } catch (err) {
          caught = err;
        }
        expect(caught).toBeInstanceOf(VaultBootstrapError);
        expect((caught as VaultBootstrapError).code).toBe(tc.expectedCode);
        const errText = (caught as Error).message;
        expect(errText).not.toContain(SYNTH_SECRET_V1);
        expect(errText).not.toContain(SYNTH_SECRET_V2);
        expect(errText).not.toContain(BROKEN_SECRET_FRAGMENT);
      }

      // 4. Env-file security checks: world-readable (`0644`), symlink, and plaintext Feishu credentials rejected
      const insecureFile = join(envDir, "insecure.env");
      writeFileSync(
        insecureFile,
        `INTERNAL_SERVICE_TOKEN=${TEST_SERVICE_TOKEN}\n`,
        { mode: 0o644 },
      );
      chmodSync(insecureFile, 0o644);
      expect(() =>
        loadConfig({
          DALTON_CHANNEL_GATEWAY_ENV_FILE: insecureFile,
          DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
        }),
      ).toThrow(/owner-only permissions/);

      const symlinkFile = join(envDir, "symlink.env");
      symlinkSync(secureEnvFile, symlinkFile);
      expect(() =>
        loadConfig({
          DALTON_CHANNEL_GATEWAY_ENV_FILE: symlinkFile,
          DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
        }),
      ).toThrow(/symlinks forbidden/);

      const plaintextFeishuFile = join(envDir, "plaintext-feishu.env");
      writeFileSync(
        plaintextFeishuFile,
        `INTERNAL_SERVICE_TOKEN=${TEST_SERVICE_TOKEN}\nDALTON_FEISHU_APP_SECRET=${SYNTH_SECRET_V1}\n`,
        { mode: 0o600 },
      );
      chmodSync(plaintextFeishuFile, 0o600);
      let forbiddenCaught: unknown;
      try {
        loadConfig({
          DALTON_CHANNEL_GATEWAY_ENV_FILE: plaintextFeishuFile,
          DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
        });
      } catch (err) {
        forbiddenCaught = err;
      }
      expect(forbiddenCaught).toBeInstanceOf(VaultBootstrapError);
      expect((forbiddenCaught as VaultBootstrapError).code).toBe(
        "plaintext_feishu_credentials_forbidden",
      );
      expect((forbiddenCaught as Error).message).not.toContain(SYNTH_SECRET_V1);

      // 5. Verify zero synthetic secret values ever appeared in any log line across all success & error paths
      const combinedLogs = logLines.join("\n");
      for (const secretCandidate of [
        SYNTH_APP_ID_V1,
        SYNTH_SECRET_V1,
        SYNTH_APP_ID_V2,
        SYNTH_SECRET_V2,
        BROKEN_SECRET_FRAGMENT,
        TEST_SERVICE_TOKEN,
      ]) {
        expect(combinedLogs).not.toContain(secretCandidate);
      }
    } finally {
      for (const t of pendingTimers) {
        clearTimeout(t);
      }
      pendingTimers.clear();
      vaultHttpServer.closeAllConnections();
      await closeHttpServer(vaultHttpServer);
      rmSync(envDir, { recursive: true, force: true });
    }
  });

  it("enforces one Vault deadline across immediate 200 headers + delayed or stalled body, succeeds on within-deadline chunked body, and redacts mid-body stream errors", async () => {
    const SYNTH_STREAM_APP_ID = "cli_synth_stream_deadline_app";
    const SYNTH_STREAM_SECRET = "sec_synth_stream_deadline_secret_900";
    const STALLED_BODY_SECRET = "sec_synth_stalled_body_fragment_901";
    const DESTROYED_BODY_SECRET = "sec_synth_destroyed_body_fragment_902";
    const OVERSIZED_BODY_SECRET = "sec_synth_oversized_body_fragment_903";

    const logLines: string[] = [];
    const sink: LoggerSink = {
      debug: (line) => logLines.push(line),
      info: (line) => logLines.push(line),
      warn: (line) => logLines.push(line),
      error: (line) => logLines.push(line),
    };
    const logger = createSubsystemLogger("test-vault-body-deadline", {
      sink,
      enableDebug: true,
    });

    type BodyDeadlineScenario =
      | "headers_flushed_delayed_body"
      | "partial_body_stalled_indefinitely"
      | "within_deadline_chunked_ok"
      | "mid_body_socket_destroy"
      | "oversized_streaming_body";

    let scenario: BodyDeadlineScenario = "headers_flushed_delayed_body";
    const pendingTimers = new Set<NodeJS.Timeout>();

    const scheduleTimer = (fn: () => void, ms: number): void => {
      const handle = setTimeout(() => {
        pendingTimers.delete(handle);
        fn();
      }, ms);
      pendingTimers.add(handle);
    };

    const bodyServer = createServer((req, res) => {
      if (
        req.method !== "POST" ||
        req.url !==
          "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials"
      ) {
        res.writeHead(404).end();
        return;
      }
      if (req.headers["x-service-token"] !== TEST_SERVICE_TOKEN) {
        res.writeHead(401, { "Cache-Control": "no-store" }).end();
        return;
      }

      if (scenario === "headers_flushed_delayed_body") {
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.flushHeaders();
        scheduleTimer(() => {
          if (!res.writableEnded && !res.destroyed) {
            res.end(
              JSON.stringify({
                credentials: {
                  DALTON_FEISHU_APP_ID: SYNTH_STREAM_APP_ID,
                  DALTON_FEISHU_APP_SECRET: SYNTH_STREAM_SECRET,
                },
              }),
            );
          }
        }, 260);
        return;
      }

      if (scenario === "partial_body_stalled_indefinitely") {
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.flushHeaders();
        res.write(
          `{"credentials":{"DALTON_FEISHU_APP_SECRET":"${STALLED_BODY_SECRET}`,
        );
        // Intentionally never end `res`: client deadline must abort and clean up stream.
        return;
      }

      if (scenario === "within_deadline_chunked_ok") {
        const fullJson = JSON.stringify({
          credentials: {
            DALTON_FEISHU_APP_ID: SYNTH_STREAM_APP_ID,
            DALTON_FEISHU_APP_SECRET: SYNTH_STREAM_SECRET,
          },
        });
        const midpoint = Math.floor(fullJson.length / 2);
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.flushHeaders();
        scheduleTimer(() => {
          if (!res.writableEnded && !res.destroyed) {
            res.write(fullJson.slice(0, midpoint));
          }
        }, 10);
        scheduleTimer(() => {
          if (!res.writableEnded && !res.destroyed) {
            res.end(fullJson.slice(midpoint));
          }
        }, 25);
        return;
      }

      if (scenario === "mid_body_socket_destroy") {
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        });
        res.flushHeaders();
        res.write(
          `{"credentials":{"DALTON_FEISHU_APP_SECRET":"${DESTROYED_BODY_SECRET}`,
        );
        scheduleTimer(() => {
          req.socket.destroy();
        }, 10);
        return;
      }

      // `oversized_streaming_body`: exceed 16 KiB without Content-Length header
      res.writeHead(200, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      });
      res.flushHeaders();
      const pad = "x".repeat(18 * 1024);
      res.end(
        `{"credentials":{"DALTON_FEISHU_APP_ID":"${SYNTH_STREAM_APP_ID}","DALTON_FEISHU_APP_SECRET":"${OVERSIZED_BODY_SECRET}","pad":"${pad}"}}`,
      );
    });

    const port = await listenEphemeralLoopback(bodyServer);
    const backendUrl = `http://127.0.0.1:${port}`;

    const withHardTestGuard = async <T>(
      promise: Promise<T>,
      guardMs = 1500,
    ): Promise<T> => {
      let guardTimer: NodeJS.Timeout | undefined;
      try {
        return await Promise.race([
          promise,
          new Promise<never>((_resolve, reject) => {
            guardTimer = setTimeout(
              () => reject(new Error(`Hard test guard (${guardMs}ms) exceeded`)),
              guardMs,
            );
          }),
        ]);
      } finally {
        if (guardTimer !== undefined) {
          clearTimeout(guardTimer);
        }
      }
    };

    try {
      // 1. Immediate 200 headers + body delayed past deadline -> `vault_timeout`, zero credentials returned
      scenario = "headers_flushed_delayed_body";
      const t0 = performance.now();
      let delayedErr: unknown;
      try {
        await withHardTestGuard(
          prepareGatewayRuntime({
            env: {
              GATEWAY_HOST: "127.0.0.1",
              GATEWAY_PORT: "0",
              DALTON_GATEWAY_MODE: "outgoing",
              DALTON_BACKEND_URL: backendUrl,
              INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
              DALTON_FEISHU_ENABLED: "true",
              DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
              DALTON_FEISHU_APP_ID: "cli_env_fallback_forbidden",
              DALTON_FEISHU_APP_SECRET: "sec_env_fallback_forbidden",
            },
            vaultTimeoutMs: 35,
            logger,
          }),
        );
      } catch (err) {
        delayedErr = err;
      }
      const delayedElapsedMs = performance.now() - t0;
      expect(delayedErr).toBeInstanceOf(VaultBootstrapError);
      expect((delayedErr as VaultBootstrapError).code).toBe("vault_timeout");
      expect((delayedErr as Error).cause).toBeUndefined();
      expect(delayedElapsedMs).toBeLessThan(220);

      // 2. Partial body begins streaming then stalls indefinitely -> `vault_timeout` without leaking partial chunk
      scenario = "partial_body_stalled_indefinitely";
      let stalledErr: unknown;
      try {
        await withHardTestGuard(
          prepareGatewayRuntime({
            env: {
              GATEWAY_HOST: "127.0.0.1",
              GATEWAY_PORT: "0",
              DALTON_GATEWAY_MODE: "outgoing",
              DALTON_BACKEND_URL: backendUrl,
              INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
              DALTON_FEISHU_ENABLED: "true",
              DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
            },
            vaultTimeoutMs: 40,
            logger,
          }),
        );
      } catch (err) {
        stalledErr = err;
      }
      expect(stalledErr).toBeInstanceOf(VaultBootstrapError);
      expect((stalledErr as VaultBootstrapError).code).toBe("vault_timeout");
      expect((stalledErr as Error).cause).toBeUndefined();
      expect((stalledErr as Error).message).not.toContain(STALLED_BODY_SECRET);

      // 3. Within-deadline chunked body -> succeeds cleanly and passes credentials in memory only
      scenario = "within_deadline_chunked_ok";
      const observedCreds: Array<{ appId: string; appSecret: string }> = [];
      const { client: fakeLark } = createFakeOutboundLarkClient();
      const okRuntime = await withHardTestGuard(
        prepareGatewayRuntime({
          env: {
            GATEWAY_HOST: "127.0.0.1",
            GATEWAY_PORT: "0",
            DALTON_GATEWAY_MODE: "outgoing",
            DALTON_BACKEND_URL: backendUrl,
            INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
            DALTON_FEISHU_ENABLED: "true",
            DALTON_FEISHU_CREDENTIAL_SOURCE: "vault",
          },
          vaultTimeoutMs: 500,
          logger,
          adapterOverrides: {
            feishu: {
              clientFactory: (creds) => {
                observedCreds.push({
                  appId: creds.appId,
                  appSecret: creds.appSecret,
                });
                return fakeLark;
              },
            },
          },
        }),
      );
      const okAdapter = okRuntime.adapters.get("feishu") as FeishuChannelAdapter;
      await okAdapter.sendTextChunk({
        targetAddress: "feishu:oc_synth_stream_ok",
        text: "stream-ok",
        chunkIndex: 0,
        totalChunks: 1,
      });
      expect(observedCreds).toEqual([
        { appId: SYNTH_STREAM_APP_ID, appSecret: SYNTH_STREAM_SECRET },
      ]);
      await okRuntime.app.stop();

      // 4. Failure during response-body consumption (mid-stream socket reset) -> safe `vault_unavailable`, no cause/secret leak
      scenario = "mid_body_socket_destroy";
      let destroyErr: unknown;
      try {
        await withHardTestGuard(
          loadFeishuCredentialsFromVault({
            backendUrl,
            serviceToken: TEST_SERVICE_TOKEN,
            timeoutMs: 500,
            logger,
          }),
        );
      } catch (err) {
        destroyErr = err;
      }
      expect(destroyErr).toBeInstanceOf(VaultBootstrapError);
      expect((destroyErr as VaultBootstrapError).code).toBe("vault_unavailable");
      expect((destroyErr as Error).cause).toBeUndefined();
      expect((destroyErr as Error).message).not.toContain(DESTROYED_BODY_SECRET);
      expect((destroyErr as Error).message).not.toContain(TEST_SERVICE_TOKEN);

      // 5. Oversized streaming body (> 16 KiB) -> rejected as `vault_malformed_response` without leaking secret
      scenario = "oversized_streaming_body";
      let oversizedErr: unknown;
      try {
        await withHardTestGuard(
          loadFeishuCredentialsFromVault({
            backendUrl,
            serviceToken: TEST_SERVICE_TOKEN,
            timeoutMs: 500,
            logger,
          }),
        );
      } catch (err) {
        oversizedErr = err;
      }
      expect(oversizedErr).toBeInstanceOf(VaultBootstrapError);
      expect((oversizedErr as VaultBootstrapError).code).toBe(
        "vault_malformed_response",
      );
      expect((oversizedErr as Error).cause).toBeUndefined();
      expect((oversizedErr as Error).message).not.toContain(
        OVERSIZED_BODY_SECRET,
      );

      // 6. Verify zero credentials or service tokens ever appeared in captured logs
      const allLogs = logLines.join("\n");
      for (const secretCandidate of [
        SYNTH_STREAM_APP_ID,
        SYNTH_STREAM_SECRET,
        STALLED_BODY_SECRET,
        DESTROYED_BODY_SECRET,
        OVERSIZED_BODY_SECRET,
        TEST_SERVICE_TOKEN,
      ]) {
        expect(allLogs).not.toContain(secretCandidate);
      }
    } finally {
      for (const t of pendingTimers) {
        clearTimeout(t);
      }
      pendingTimers.clear();
      bodyServer.closeAllConnections();
      await closeHttpServer(bodyServer);
    }
  });
});
