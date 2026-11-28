import { describe, expect, it, vi } from "vitest";
import type { I6AuthorizeAndClaimResponse } from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { loadConfig } from "../src/config.js";
import type { DataTaskAuthorizePlanResponse } from "../src/data-task-notification-types.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";
import {
  createMockBackend,
  fetchGatewayServer,
  TEST_SERVICE_TOKEN,
} from "./helpers.js";

const RECEIPT_ID = "aaaaaaaa-1111-4222-8333-444444444444";
const CLAIM_TOKEN = "bbbbbbbb-1111-4222-8333-444444444444";
const FEISHU_TARGET = "feishu:user:ou_alice";
const FAILURE_CARD = {
  schema: "2.0",
  header: { title: { tag: "plain_text", content: "定时任务执行失败提醒" }, template: "red" },
  body: { elements: [{ tag: "markdown", content: "**原因**：订阅模型配额已用尽" }] },
};

function makeApp(
  backend: ReturnType<typeof createMockBackend>,
  adapters: Map<ChannelPlatform, ChannelAdapter>,
) {
  const config = loadConfig({
    GATEWAY_HOST: "127.0.0.1",
    GATEWAY_PORT: "0",
    DALTON_GATEWAY_MODE: "outgoing",
    INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
    DALTON_BACKEND_URL: "http://127.0.0.1:8000",
  });
  return createGatewayApp({ config, backend, adapters });
}

function failureNoticePlan(): DataTaskAuthorizePlanResponse {
  return {
    authorized: true,
    status: "authorized",
    run_id: RECEIPT_ID,
    owner_user_id: "user-1",
    platform: "feishu",
    target_kind: "dm",
    requested_target: FEISHU_TARGET,
    canonical_target_address: FEISHU_TARGET,
    recipient_id: "ou_alice",
    items: [
      {
        item_index: 0,
        delivery_key: `failure_notice:${RECEIPT_ID}:item:0`,
        kind: "feishu_card",
        card: FAILURE_CARD,
        images: [],
      },
    ],
  };
}

describe("failure notice gateway route & automation finish 503", () => {
  it("POST /internal/failure-notices rejects invalid payload and extra fields", async () => {
    const backend = createMockBackend();
    const feishu = new FakeChannelAdapter("feishu");
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const resMissing = await fetchGatewayServer(
      app,
      "http://127.0.0.1/internal/failure-notices",
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ target_receipt_id: RECEIPT_ID }),
      },
    );
    expect(resMissing.status).toBe(400);

    const resExtra = await fetchGatewayServer(
      app,
      "http://127.0.0.1/internal/failure-notices",
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          target_receipt_id: RECEIPT_ID,
          claim_token: CLAIM_TOKEN,
          card: FAILURE_CARD,
        }),
      },
    );
    expect(resExtra.status).toBe(400);
    expect(backend.authorizeFailureNoticePlan).not.toHaveBeenCalled();
  });

  it("POST /internal/failure-notices forwards claim_token to plan and I-6 authorize-and-claim", async () => {
    const backend = createMockBackend();
    vi.spyOn(backend, "authorizeFailureNoticePlan").mockResolvedValueOnce(failureNoticePlan());
    const claimResp: I6AuthorizeAndClaimResponse = {
      claim_status: "claimed",
      platform: "feishu",
      recipient_id: "ou_alice",
      target_address: FEISHU_TARGET,
      is_current: false,
      home_workspace_id: "ws-home",
      weixin_context_token: null,
    };
    vi.spyOn(backend, "authorizeAndClaimDelivery").mockResolvedValueOnce(claimResp);

    const feishu = new FakeChannelAdapter("feishu");
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const res = await fetchGatewayServer(
      app,
      "http://127.0.0.1/internal/failure-notices",
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          target_receipt_id: RECEIPT_ID,
          claim_token: CLAIM_TOKEN,
        }),
      },
    );
    expect(res.status).toBe(200);
    const data = (await res.json()) as { ok: boolean; status: string; items_sent: number };
    expect(data.ok).toBe(true);
    expect(data.status).toBe("sent");
    expect(data.items_sent).toBe(1);

    expect(backend.authorizeFailureNoticePlan).toHaveBeenCalledWith({
      target_receipt_id: RECEIPT_ID,
      claim_token: CLAIM_TOKEN,
    });
    expect(backend.authorizeAndClaimDelivery).toHaveBeenCalledWith(
      expect.objectContaining({
        delivery_key: `failure_notice:${RECEIPT_ID}:item:0`,
        kind: "failure_notice",
        claim_token: CLAIM_TOKEN,
      }),
      undefined,
    );
    expect(feishu.sentCards.length).toBe(1);
  });

  it("POST /internal/failure-notices aborts without sending when I-6 denies expired claim_token", async () => {
    const backend = createMockBackend();
    vi.spyOn(backend, "authorizeFailureNoticePlan").mockResolvedValueOnce(failureNoticePlan());
    vi.spyOn(backend, "authorizeAndClaimDelivery").mockResolvedValueOnce({
      claim_status: "denied",
      status: "failed",
      code: "claim_expired_or_superseded",
      message: "Failure notice claim lease expired or was superseded.",
    });

    const feishu = new FakeChannelAdapter("feishu");
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const res = await fetchGatewayServer(
      app,
      "http://127.0.0.1/internal/failure-notices",
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          target_receipt_id: RECEIPT_ID,
          claim_token: CLAIM_TOKEN,
        }),
      },
    );
    expect(res.status).toBe(200);
    const data = (await res.json()) as { ok: boolean; status: string; code: string };
    expect(data.ok).toBe(false);
    expect(data.status).toBe("failed");
    expect(data.code).toBe("claim_expired_or_superseded");
    expect(feishu.sentCards.length).toBe(0);
  });

  it("executeAutomationRunFinishDetailed returns 503 and leaves run unsealed on transient noticeRes failure when failure_notice_enabled is true", async () => {
    const backend = createMockBackend();
    const execId = "cccccccc-1111-4222-8333-444444444444";
    const finishSpy = vi.spyOn(backend, "finishAutomationRun").mockResolvedValue({
      status: 200,
      data: {
        execution_id: execId,
        user_id: "user-1",
        workspace_id: null,
        thread_id: "thread-1",
        automation_name: "Daily Brief",
        status: "finishing",
        previous_status: "registered",
        finish_status: "failed",
        final_text: null,
        failure_notice_enabled: true,
        targets: [
          {
            entry: "feishu:@me",
            address: "feishu:@me",
            name: "Alice DM",
            ok: true,
            message: null,
          },
        ],
      },
    });

    const feishu = new FakeChannelAdapter("feishu");
    // Disconnected adapter makes executeDelivery return { status: "failed", code: "unavailable" }
    vi.spyOn(feishu, "isConnected").mockReturnValue(false);
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const res = await fetchGatewayServer(
      app,
      `http://127.0.0.1/agent/automation-runs/${execId}/finish`,
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": "user-1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          status: "failed",
          thread_id: "thread-1",
          final_text: null,
        }),
      },
    );
    expect(res.status).toBe(503);
    const data = (await res.json()) as { retryable?: boolean };
    expect(data.retryable).toBe(true);
    // Must only have called mark_finished: false (never sealed mark_finished: true)
    expect(finishSpy).toHaveBeenCalledTimes(1);
    expect(finishSpy.mock.calls[0]?.[1]?.mark_finished).toBe(false);
  });

  it("executeAutomationRunFinishDetailed seals run and returns 200 on transient noticeRes failure when failure_notice_enabled is false (default)", async () => {
    const backend = createMockBackend();
    const execId = "cceeeeee-1111-4222-8333-444444444444";
    const finishSpy = vi
      .spyOn(backend, "finishAutomationRun")
      .mockResolvedValueOnce({
        status: 200,
        data: {
          execution_id: execId,
          user_id: "user-1",
          workspace_id: null,
          thread_id: "thread-1",
          automation_name: "Daily Brief",
          status: "finishing",
          previous_status: "registered",
          finish_status: "failed",
          final_text: null,
          failure_notice_enabled: false,
          targets: [
            {
              entry: "feishu:@me",
              address: "feishu:@me",
              name: "Alice DM",
              ok: true,
              message: null,
            },
          ],
        },
      })
      .mockResolvedValueOnce({
        status: 200,
        data: {
          execution_id: execId,
          user_id: "user-1",
          workspace_id: null,
          thread_id: "thread-1",
          automation_name: "Daily Brief",
          status: "finished",
          previous_status: "finishing",
          finish_status: "failed",
          final_text: null,
          targets: [],
        },
      });

    const feishu = new FakeChannelAdapter("feishu");
    vi.spyOn(feishu, "isConnected").mockReturnValue(false);
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const res = await fetchGatewayServer(
      app,
      `http://127.0.0.1/agent/automation-runs/${execId}/finish`,
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": "user-1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          status: "failed",
          thread_id: "thread-1",
          final_text: null,
        }),
      },
    );
    expect(res.status).toBe(200);
    expect(finishSpy).toHaveBeenCalledTimes(2);
    expect(finishSpy.mock.calls[1]?.[1]?.mark_finished).toBe(true);
  });

  it("executeAutomationRunFinishDetailed seals run and returns 200 when final_attempt is true despite transient noticeRes failure", async () => {
    const backend = createMockBackend();
    const execId = "ccffffff-1111-4222-8333-444444444444";
    const finishSpy = vi
      .spyOn(backend, "finishAutomationRun")
      .mockResolvedValueOnce({
        status: 200,
        data: {
          execution_id: execId,
          user_id: "user-1",
          workspace_id: null,
          thread_id: "thread-1",
          automation_name: "Daily Brief",
          status: "finishing",
          previous_status: "registered",
          finish_status: "failed",
          final_text: null,
          failure_notice_enabled: true,
          targets: [
            {
              entry: "feishu:@me",
              address: "feishu:@me",
              name: "Alice DM",
              ok: true,
              message: null,
            },
          ],
        },
      })
      .mockResolvedValueOnce({
        status: 200,
        data: {
          execution_id: execId,
          user_id: "user-1",
          workspace_id: null,
          thread_id: "thread-1",
          automation_name: "Daily Brief",
          status: "finished",
          previous_status: "finishing",
          finish_status: "failed",
          final_text: null,
          targets: [],
        },
      });

    const feishu = new FakeChannelAdapter("feishu");
    vi.spyOn(feishu, "isConnected").mockReturnValue(false);
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const res = await fetchGatewayServer(
      app,
      `http://127.0.0.1/agent/automation-runs/${execId}/finish`,
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": "user-1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          status: "failed",
          thread_id: "thread-1",
          final_text: null,
          final_attempt: true,
        }),
      },
    );
    expect(res.status).toBe(200);
    expect(finishSpy).toHaveBeenCalledTimes(2);
    expect(finishSpy.mock.calls[1]?.[1]?.mark_finished).toBe(true);
  });

  it("startup recovery executeAutomationRunFinish seals run on unknown/in_flight notice and retries boundedly on unavailable", async () => {
    const backend = createMockBackend();
    const execUnknown = "eeeeeeee-1111-4222-8333-444444444444";
    const finishSpy = vi.spyOn(backend, "finishAutomationRun").mockResolvedValue({
      status: 200,
      data: {
        execution_id: execUnknown,
        user_id: "user-1",
        workspace_id: null,
        thread_id: "thread-1",
        automation_name: "Daily Brief",
        status: "finished",
        previous_status: "finishing",
        finish_status: "failed",
        final_text: null,
        targets: [],
      },
    });
    vi.spyOn(backend, "authorizeAndClaimDelivery").mockResolvedValueOnce({
      claim_status: "in_flight_or_unknown",
      status: "unknown",
      code: "in_flight",
      message: "Earlier attempt still unknown.",
      platform: "feishu",
      target_address: "feishu:@me",
      is_current: false,
    });

    const feishu = new FakeChannelAdapter("feishu");
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    await app.agentRoutes.executeAutomationRunFinish({
      executionId: execUnknown,
      userId: "user-1",
      threadId: "thread-1",
      automationName: "Daily Brief",
      finishStatus: "failed",
      finalText: null,
      failureNoticeEnabled: true,
      recoveryRetryDelayMs: 0,
      targets: [{ entry: "feishu:@me", address: "feishu:@me", name: "Alice DM", ok: true, message: null }],
    });
    expect(finishSpy).toHaveBeenCalledTimes(1);
    expect(finishSpy.mock.calls[0]?.[1]?.mark_finished).toBe(true);

    // On genuinely retryable unavailable error during startup recovery, retries once then seals
    const execUnavailable = "ffffffff-1111-4222-8333-444444444444";
    finishSpy.mockClear();
    vi.spyOn(feishu, "isConnected").mockReturnValue(false);
    await app.agentRoutes.executeAutomationRunFinish({
      executionId: execUnavailable,
      userId: "user-1",
      threadId: "thread-1",
      automationName: "Daily Brief",
      finishStatus: "failed",
      finalText: null,
      failureNoticeEnabled: true,
      recoveryRetryDelayMs: 0,
      targets: [{ entry: "feishu:@me", address: "feishu:@me", name: "Alice DM", ok: true, message: null }],
    });
    expect(finishSpy).toHaveBeenCalledTimes(1);
    expect(finishSpy.mock.calls[0]?.[1]?.mark_finished).toBe(true);
  });

  it("executeAutomationRunFinishDetailed seals run and returns 200 on permanent noticeRes denial", async () => {
    const backend = createMockBackend();
    const execId = "dddddddd-1111-4222-8333-444444444444";
    const finishSpy = vi
      .spyOn(backend, "finishAutomationRun")
      .mockResolvedValueOnce({
        status: 200,
        data: {
          execution_id: execId,
          user_id: "user-1",
          workspace_id: null,
          thread_id: "thread-1",
          automation_name: "Daily Brief",
          status: "finishing",
          previous_status: "registered",
          finish_status: "failed",
          final_text: null,
          targets: [
            {
              entry: "feishu:@me",
              address: "feishu:@me",
              name: "Alice DM",
              ok: true,
              message: null,
            },
          ],
        },
      })
      .mockResolvedValueOnce({
        status: 200,
        data: {
          execution_id: execId,
          user_id: "user-1",
          workspace_id: null,
          thread_id: "thread-1",
          automation_name: "Daily Brief",
          status: "finished",
          previous_status: "finishing",
          finish_status: "failed",
          final_text: null,
          targets: [],
        },
      });
    vi.spyOn(backend, "authorizeAndClaimDelivery").mockResolvedValueOnce({
      claim_status: "denied",
      status: "failed",
      code: "not_allowed",
      message: "Target not permitted",
    });

    const feishu = new FakeChannelAdapter("feishu");
    const app = makeApp(backend, new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]));

    const res = await fetchGatewayServer(
      app,
      `http://127.0.0.1/agent/automation-runs/${execId}/finish`,
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": "user-1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          status: "failed",
          thread_id: "thread-1",
          final_text: null,
        }),
      },
    );
    expect(res.status).toBe(200);
    expect(finishSpy).toHaveBeenCalledTimes(2);
    expect(finishSpy.mock.calls[1]?.[1]?.mark_finished).toBe(true);
  });
});
