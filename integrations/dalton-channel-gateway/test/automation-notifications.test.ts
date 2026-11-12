import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type * as Lark from "@larksuiteoapi/node-sdk";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { I6AuthorizeAndClaimResponse } from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { FeishuChannelAdapter } from "../src/channels/feishu/index.js";
import { loadConfig } from "../src/config.js";
import type { DataTaskAuthorizePlanResponse } from "../src/data-task-notification-types.js";
import { handleOpsAlert } from "../src/ops-alerts.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";
import { createMockBackend, TEST_SERVICE_TOKEN } from "./helpers.js";

const DELIVERY_ID = "12345678-1234-4234-8234-1234567890ab";
const FEISHU_TARGET = "feishu:user:ou_analyst_a";
const DISCORD_TARGET = "discord:user:123456789012345678";
const CARD = {
  schema: "2.0",
  header: { title: { tag: "plain_text", content: "完成" }, template: "blue" },
  body: { elements: [{ tag: "markdown", content: "已完成" }] },
};

const originalOpsFile = process.env.DALTON_OPS_ALERTS_FILE;

afterEach(() => {
  if (originalOpsFile === undefined) {
    delete process.env.DALTON_OPS_ALERTS_FILE;
  } else {
    process.env.DALTON_OPS_ALERTS_FILE = originalOpsFile;
  }
});

function feishuPlan(): DataTaskAuthorizePlanResponse {
  return {
    authorized: true,
    status: "authorized",
    run_id: DELIVERY_ID,
    owner_user_id: "user-1",
    platform: "feishu",
    target_kind: "dm",
    requested_target: FEISHU_TARGET,
    canonical_target_address: FEISHU_TARGET,
    recipient_id: "ou_analyst_a",
    items: [
      {
        item_index: 0,
        delivery_key: `automation_notify:${DELIVERY_ID}:item:0`,
        kind: "feishu_card",
        card: CARD,
        images: [],
      },
    ],
  };
}

function discordPlan(): DataTaskAuthorizePlanResponse {
  return {
    authorized: true,
    status: "authorized",
    run_id: DELIVERY_ID,
    owner_user_id: "user-1",
    platform: "discord",
    target_kind: "dm",
    requested_target: DISCORD_TARGET,
    canonical_target_address: "discord:@me",
    recipient_id: "123456789012345678",
    items: [
      {
        item_index: 0,
        delivery_key: `automation_notify:${DELIVERY_ID}:item:0`,
        kind: "text",
        text: "Discord 正文",
      },
    ],
  };
}

function claimed(
  platform: string,
  address: string,
  recipient: string,
): I6AuthorizeAndClaimResponse {
  return {
    claim_status: "claimed",
    platform,
    recipient_id: recipient,
    target_address: address,
    is_current: false,
    home_workspace_id: "ws-home",
    weixin_context_token: null,
  };
}

function appFor(
  backend: ReturnType<typeof createMockBackend>,
  adapters: Map<ChannelPlatform, ChannelAdapter>,
  extraEnv: Record<string, string> = {},
) {
  const config = loadConfig({
    GATEWAY_HOST: "127.0.0.1",
    GATEWAY_PORT: "0",
    DALTON_GATEWAY_MODE: "outgoing",
    INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
    DALTON_BACKEND_URL: "http://127.0.0.1:8000",
    ...extraEnv,
  });
  return createGatewayApp({ config, backend, adapters });
}

describe("automation notification gateway route", () => {
  it("rejects a missing delivery_id and extra fields before calling the backend", async () => {
    const backend = createMockBackend();
    const feishu = new FakeChannelAdapter("feishu");
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
    );
    const { host, port } = await app.start();
    const url = `http://${host}:${port}/internal/automation-notifications`;
    try {
      for (const body of [{}, { delivery_id: DELIVERY_ID, card: CARD }, { delivery_id: "exec-1" }]) {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify(body),
        });
        expect(res.status).toBe(400);
      }
      expect(backend.authorizeAutomationNotificationPlan).not.toHaveBeenCalled();
      expect(backend.authorizeDataTaskNotificationPlan).not.toHaveBeenCalled();
      expect(feishu.sentCards).toHaveLength(0);
    } finally {
      await app.stop();
    }
  });

  it("sends the plan card on Feishu and the plan text on Discord", async () => {
    const backend = createMockBackend();
    vi.mocked(backend.authorizeAutomationNotificationPlan)
      .mockResolvedValueOnce(feishuPlan())
      .mockResolvedValueOnce(discordPlan());
    vi.mocked(backend.authorizeAndClaimDelivery)
      .mockResolvedValueOnce(claimed("feishu", FEISHU_TARGET, "ou_analyst_a"))
      .mockResolvedValueOnce(claimed("discord", "discord:@me", "123456789012345678"));
    const feishu = new FakeChannelAdapter("feishu");
    const discord = new FakeChannelAdapter("discord");
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([
        ["feishu", feishu],
        ["discord", discord],
      ]),
    );
    const { host, port } = await app.start();
    const url = `http://${host}:${port}/internal/automation-notifications`;
    try {
      const feishuRes = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ delivery_id: DELIVERY_ID }),
      });
      expect(feishuRes.status).toBe(200);
      const feishuBody = (await feishuRes.json()) as { status: string };
      expect(feishuBody.status).toBe("sent");
      expect(feishu.sentCards).toHaveLength(1);
      expect(feishu.sentCards[0]?.card).toEqual(CARD);
      expect(feishu.sentTexts).toHaveLength(0);
      expect(backend.downloadDataTaskRunArtifact).not.toHaveBeenCalled();

      const discordRes = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ delivery_id: DELIVERY_ID }),
      });
      expect(discordRes.status).toBe(200);
      expect(discord.sentTexts.map((item) => item.text)).toEqual(["Discord 正文"]);
      expect(discord.sentCards).toHaveLength(0);
      const claimKinds = vi.mocked(backend.authorizeAndClaimDelivery).mock.calls.map(
        (call) => call[0].kind,
      );
      expect(claimKinds).toEqual(["automation_notify", "automation_notify"]);
      expect(backend.authorizeDataTaskNotificationPlan).not.toHaveBeenCalled();
    } finally {
      await app.stop();
    }
  });

  it("fails a Feishu card when the adapter has no sendCard and does not send text", async () => {
    const backend = createMockBackend();
    vi.mocked(backend.authorizeAutomationNotificationPlan).mockResolvedValueOnce(
      feishuPlan(),
    );
    const baseFake = new FakeChannelAdapter("feishu");
    const bare: ChannelAdapter = {
      platform: "feishu",
      maxChunkChars: 1000,
      isConfigured: () => true,
      isConnected: () => true,
      isReadyForOutbound: () => true,
      start: async () => undefined,
      stop: async () => undefined,
      onInboundEvent: () => undefined,
      splitTextChunks: (text: string) => [text],
      sendTextChunk: async () => {
        throw new Error("text send");
      },
      sendFile: async () => {
        throw new Error("file");
      },
      downloadAttachment: async () => {
        throw new Error("download");
      },
      listAvailableChats: () => baseFake.listAvailableChats(),
    };
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", bare]]),
    );
    const { host, port } = await app.start();
    try {
      const res = await fetch(
        `http://${host}:${port}/internal/automation-notifications`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ delivery_id: DELIVERY_ID }),
        },
      );
      const body = (await res.json()) as { status: string; code: string };
      expect(body.status).toBe("failed");
      expect(body.code).toBe("unsupported_item_kind");
      expect(backend.authorizeAndClaimDelivery).not.toHaveBeenCalled();
    } finally {
      await app.stop();
    }
  });

  it("does not call the adapter again when the item is sent, in flight, or unknown", async () => {
    const backend = createMockBackend();
    vi.mocked(backend.authorizeAutomationNotificationPlan).mockResolvedValue(feishuPlan());
    vi.mocked(backend.authorizeAndClaimDelivery)
      .mockResolvedValueOnce({
        claim_status: "already_completed",
        status: "sent",
        platform: "feishu",
        target_address: FEISHU_TARGET,
        is_current: false,
      })
      .mockResolvedValueOnce({
        claim_status: "in_flight_or_unknown",
        status: "unknown",
        code: "in_flight",
        platform: "feishu",
        target_address: FEISHU_TARGET,
        is_current: false,
        message: "in flight",
      })
      .mockResolvedValueOnce({
        claim_status: "in_flight_or_unknown",
        status: "unknown",
        code: "unknown",
        platform: "feishu",
        target_address: FEISHU_TARGET,
        is_current: false,
        message: "unknown",
      });
    const feishu = new FakeChannelAdapter("feishu");
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
    );
    const { host, port } = await app.start();
    const url = `http://${host}:${port}/internal/automation-notifications`;
    try {
      for (const expected of ["sent", "unknown", "unknown"]) {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Service-Token": TEST_SERVICE_TOKEN,
          },
          body: JSON.stringify({ delivery_id: DELIVERY_ID }),
        });
        const body = (await res.json()) as { status: string };
        expect(body.status).toBe(expected);
      }
      expect(feishu.sentCards).toHaveLength(0);
      expect(feishu.sentTexts).toHaveLength(0);
    } finally {
      await app.stop();
    }
  });
});

describe("loopback ops alerts", () => {
  it("sends the configured sentence to the configured targets and does not call the backend", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-"));
    const configPath = join(dir, "ops-alerts.json");
    const sentence = "Dalton 后端不可用 | 请看本机";
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a", "feishu:chat:oc_ops_room"],
        messages: {
          backend_down: sentence,
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );
    delete process.env.DALTON_OPS_ALERTS_FILE;
    const sent: Array<{ targetAddress: string; text: string }> = [];
    const feishu = Object.assign(new FakeChannelAdapter("feishu"), {
      sendPlainText: async (options: { targetAddress: string; text: string }) => {
        sent.push(options);
        return { messageId: `om_${sent.length}` };
      },
    });
    const backend = createMockBackend();
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      { DALTON_OPS_ALERTS_FILE: configPath },
    );
    const { host, port } = await app.start();
    try {
      const res = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ code: "backend_down" }),
      });
      expect(res.status).toBe(200);
      const body = (await res.json()) as { ok: boolean; code: string };
      expect(body.ok).toBe(true);
      expect(body.code).toBe("backend_down");
      expect(sent).toEqual([
        { targetAddress: "feishu:user:ou_ops_a", text: sentence },
        { targetAddress: "feishu:chat:oc_ops_room", text: sentence },
      ]);
      expect(backend.authorizeAutomationNotificationPlan).not.toHaveBeenCalled();
      expect(backend.authorizeDataTaskNotificationPlan).not.toHaveBeenCalled();
      expect(feishu.sentTexts).toHaveLength(0);
      expect(feishu.sentCards).toHaveLength(0);
    } finally {
      await app.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("sends an ops alert when the file path is only in the gateway env file", async () => {
    delete process.env.DALTON_OPS_ALERTS_FILE;
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-env-"));
    const configPath = join(dir, "ops-alerts.json");
    const envPath = join(dir, "channel-gateway.env");
    const sentence = "Dalton 后端不可用 | 只在环境文件";
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_env"],
        messages: {
          backend_down: sentence,
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );
    writeFileSync(envPath, `DALTON_OPS_ALERTS_FILE="${configPath}"\n`, { mode: 0o600 });
    chmodSync(envPath, 0o600);
    const sent: Array<{ targetAddress: string; text: string }> = [];
    const feishu = Object.assign(new FakeChannelAdapter("feishu"), {
      sendPlainText: async (options: { targetAddress: string; text: string }) => {
        sent.push(options);
        return { messageId: "om_env_file" };
      },
    });
    const backend = createMockBackend();
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      { DALTON_CHANNEL_GATEWAY_ENV_FILE: envPath },
    );
    const { host, port } = await app.start();
    try {
      const res = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ code: "backend_down" }),
      });
      expect(res.status).toBe(200);
      expect(sent).toEqual([{ targetAddress: "feishu:user:ou_ops_env", text: sentence }]);
      expect(backend.authorizeAutomationNotificationPlan).not.toHaveBeenCalled();
    } finally {
      await app.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("returns 503 for a missing or invalid config, 400 for a bad body, and 403 off loopback", async () => {
    const sender = vi.fn(async () => ({ messageId: "om_should_not_send" }));
    delete process.env.DALTON_OPS_ALERTS_FILE;
    const missing = await handleOpsAlert({
      body: { code: "backend_down" },
      remoteAddress: "127.0.0.1",
      configPath: undefined,
      sendPlainText: sender,
    });
    expect(missing.status).toBe(503);
    expect(sender).not.toHaveBeenCalled();

    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-bad-"));
    const configPath = join(dir, "bad.json");
    writeFileSync(configPath, JSON.stringify({ targets: ["discord:channel:12345"] }));
    const invalid = await handleOpsAlert({
      body: { code: "backend_recovered" },
      remoteAddress: "::1",
      configPath,
      sendPlainText: sender,
    });
    expect(invalid.status).toBe(503);

    const extra = await handleOpsAlert({
      body: { code: "backend_down", text: "caller text" },
      remoteAddress: "::ffff:127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(extra.status).toBe(400);

    const remote = await handleOpsAlert({
      body: { code: "backend_down" },
      remoteAddress: "10.1.2.3",
      configPath,
      sendPlainText: sender,
    });
    expect(remote.status).toBe(403);
    expect(sender).not.toHaveBeenCalled();
    rmSync(dir, { recursive: true, force: true });
  });

  it("sends ops text through the plain post API when the sentence contains a pipe", async () => {
    const creates: Array<{ msg_type?: string; content?: string }> = [];
    const client = {
      im: {
        message: {
          create: async (req: { data: { msg_type?: string; content?: string } }) => {
            creates.push(req.data);
            return { code: 0, data: { message_id: "om_plain_1" } };
          },
        },
      },
    } as unknown as Lark.Client;
    const adapter = new FeishuChannelAdapter({
      appId: "cli_ops_test",
      appSecret: "sec_ops_test",
      outgoingOnly: true,
      client,
    });
    const text = "后端不可用 | 请检查";
    const sent = await adapter.sendPlainText({
      targetAddress: FEISHU_TARGET,
      text,
    });
    expect(sent.messageId).toBe("om_plain_1");
    expect(creates).toHaveLength(1);
    expect(creates[0]?.msg_type).toBe("post");
    expect(creates[0]?.content).toContain(text);
  });
});
