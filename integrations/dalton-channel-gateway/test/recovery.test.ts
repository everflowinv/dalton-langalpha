import { existsSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I7RecoverResponse,
  I9WeixinStateGetResponse,
  ThreadStatusInfo,
} from "../src/backend-client.js";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
import { makeInboundRequestKey } from "../src/uuid.js";
import {
  createTestHarness,
  fetchGatewayServer,
  loadFixtureKey,
  makeSseEventsFromFixture,
  RUN_ID,
  TEST_SERVICE_TOKEN,
  THREAD_ID,
  type TestHarness,
  USER_ID,
} from "./helpers.js";

describe("Startup Recovery & Non-Blocking Server Start (§5.2, Codex r2 #17/#18, Grok r2 #2)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("`app.start()` binds HTTP server and starts adapters immediately without blocking on long-running startup recovery streams", async () => {
    let resolveSlowRecovery!: () => void;
    const slowRecoveryGate = new Promise<void>((r) => {
      resolveSlowRecovery = r;
    });

    vi.mocked(h.backend.recoverState).mockImplementationOnce(async () => {
      await slowRecoveryGate;
      return {
        mode: "startup",
        inbound_events: [],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      };
    });

    const { host, port } = await h.app.start();
    expect(host).toBe("127.0.0.1");
    expect(h.feishuAdapter.isConnected()).toBe(true);

    const healthRes = await fetchGatewayServer(
      h.app,
      `http://${host}:${port}/health`,
      {
        headers: { "X-Service-Token": TEST_SERVICE_TOKEN },
      },
    );
    expect(healthRes.status).toBe(200);

    const qrWaitRes = await fetchGatewayServer(
      h.app,
      `http://${host}:${port}/internal/weixin/qrcode/wait?session_key=sess_123`,
      {
        method: "GET",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": USER_ID,
        },
      },
    );
    expect(qrWaitRes.status).toBe(200);

    const qrVerifyRes = await fetchGatewayServer(
      h.app,
      `http://${host}:${port}/internal/weixin/qrcode/verify-code`,
      {
        method: "POST",
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": USER_ID,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          session_key: "sess_123",
          verify_code: "654321",
        }),
      },
    );
    expect(qrVerifyRes.status).toBe(200);

    resolveSlowRecovery();
    await h.app.orchestrator.waitForStartupRecovery();
  });

  it("executes full Startup Recovery (§5.2) using real `recover_startup` fixture (without top-level `platform_user_id`/`generation` on inbound rows)", async () => {
    const wxConfigured = loadFixtureKey<I9WeixinStateGetResponse>(
      "i9_weixin_state.json",
      "get_configured",
    );
    const recoverStartup = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_startup_populated",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );
    const recoverRunRec1Page = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "recover_run_rec_1_page");
    const recoverBgRec1Page = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "recover_bg_rec_1_page");

    vi.mocked(h.backend.getWeixinState).mockResolvedValueOnce(wxConfigured);
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce(recoverStartup);
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(recoverRunRec1Page),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(recoverBgRec1Page),
      });

    const missingRunKey = makeInboundRequestKey(
      "feishu",
      "m_rec_dispatched_no_run",
    );
    const hitlReceivedKey = makeInboundRequestKey(
      "feishu",
      "m_rec_received_hitl",
    );

    // Real `recover` rows do not carry `pending_interrupt`; `recoverSingleInboundRow` queries idempotent `I-1` for `pending_interrupt`
    const defaultI1 = await h.backend.registerAndResolveInbound({
      platform: "feishu",
      platform_user_id: "ou_alice",
      chat_type: "dm",
      chat_address: "feishu:@me",
      thread_scope_key: "dm:ou_alice",
      message_id: "m_rec_received_hitl",
      request_key: hitlReceivedKey,
    });
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...defaultI1,
      existing_status: "pending",
      request_key: hitlReceivedKey,
      pending_interrupt: {
        interrupt_id: "int_rec_q1",
        kind: "ask_user_question",
        thread_id: statusInterrupted.thread_id!,
        run_id: RUN_ID,
        turn_index: 1,
      },
    });

    // When `m_rec_received_hitl` checks `getThreadStatus`, return `"interrupted"` so it answers the HITL interrupt
    vi.mocked(h.backend.getThreadStatus).mockImplementation(async () => ({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    }));

    await h.app.orchestrator.runStartupRecovery();

    // Verify Weixin state is loaded into adapter memory from I-9 and never written as plaintext state.json
    const wxDir = join(h.tempDir, "weixin");
    const wxFile = join(wxDir, "state.json");
    expect(existsSync(wxFile)).toBe(false);
    expect(h.weixinAdapter.persistedWeixinState).toMatchObject({
      configured: true,
      bot_token: "decrypted_secret_bot_token",
      get_updates_buf: "cursor_xyz",
    });

    // Verify `dispatched` row without `run_id` re-dispatched with `missingRunKey`
    expect(
      h.backend.appendCalls.some(
        (c) => c.payload.request_key === missingRunKey,
      ),
    ).toBe(true);

    // Verify `received` row with `pending_interrupt` answered HITL via `/messages` (`messages: []` + `hitl_response`)
    expect(
      h.backend.appendCalls.some(
        (c) =>
          c.payload.request_key === hitlReceivedKey &&
          c.payload.hitl_response?.int_rec_q1?.decisions?.[0]?.message ===
            "My answer to question",
      ),
    ).toBe(true);

    // Verify fallback deliveries + automation failure notice were delivered
    const claimKeys = h.backend.deliveryClaims.map((c) => c.delivery_key);
    expect(claimKeys).toContain("fallback:run_rec_1");
    expect(claimKeys).toContain("fallback:bg_rec_1");
    expect(claimKeys).toContain(
      "automation:33333333-4444-5555-6666-777777777777:feishu:notice",
    );
  });

  it("retries a real not_found ended notice on the same dispatched key, then does not send it again", async () => {
    const recoverStartup = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_startup_populated",
    );
    const messageId = "m_rec_not_found_notice";
    const noticeKey = `notice:feishu:${messageId}:rec_not_found`;
    const endedText = 'The interrupted run ended with status "not_found".';
    const dispatchedRow = {
      ...structuredClone(recoverStartup.inbound_events![0]!),
      platform: "feishu" as const,
      message_id: messageId,
      dalton_user_id: USER_ID,
      chat_type: "dm" as const,
      chat_address: "feishu:@me",
      thread_scope_key: "dm:ou_alice",
      event_kind: "message",
      status: "dispatched" as "dispatched" | "failed",
      thread_id: THREAD_ID,
      run_id: "run_not_found_notice_1",
      turn_index: 1,
      fallback_pending: true,
      command_reply_pending: false,
      raw_payload: {
        platform_user_id: "ou_alice",
        text: "Where did the run go",
      },
    };
    const recoveryOf = (
      row: typeof dispatchedRow,
    ): I7RecoverResponse => ({
      mode: "startup",
      inbound_events: [row],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });

    vi.mocked(h.backend.getDispatchesLiveness).mockResolvedValue({});
    vi.mocked(h.backend.getThreadStatus).mockResolvedValue({
      status: "not_found",
    });
    vi.mocked(h.backend.recoverState)
      .mockResolvedValueOnce(recoveryOf(dispatchedRow))
      .mockResolvedValueOnce(recoveryOf(dispatchedRow))
      .mockResolvedValueOnce(
        recoveryOf({
          ...dispatchedRow,
          status: "failed",
          fallback_pending: false,
        }),
      );

    let failEndedNotice = true;
    h.feishuAdapter.setSendTextBehavior((options) => {
      if (failEndedNotice && options.text === endedText) {
        throw new ChannelDeliveryError("ended notice failed", {
          outcome: "failed",
          code: "unavailable",
        });
      }
      return { messageId: `om_ended_${options.chunkIndex}` };
    });

    const replayCallsBefore = vi.mocked(h.backend.replayThreadMessages).mock
      .calls.length;
    const appendCallsBefore = h.backend.appendCalls.length;
    const noticeClaims = () =>
      h.backend.deliveryClaims.filter((claim) => claim.delivery_key === noticeKey);
    const inboundWrites = () =>
      h.backend.progressUpdates.filter((update) => update.message_id === messageId);
    const lastNoticeStatus = () =>
      [...h.backend.deliveryUpdates]
        .reverse()
        .find(
          (update) =>
            update.delivery_key === noticeKey && update.status !== undefined,
        )?.status;

    await h.app.orchestrator.runStartupRecovery();

    expect(h.feishuAdapter.sentTexts.map((item) => item.text)).not.toContain(
      endedText,
    );
    expect(noticeClaims()).toHaveLength(1);
    expect(noticeClaims()[0]).toMatchObject({
      delivery_key: noticeKey,
      kind: "notice",
      activity_origin: "none",
      source_scope_key: "dm:ou_alice",
    });
    expect(lastNoticeStatus()).toBe("failed");
    expect(inboundWrites()).toEqual([]);
    expect(vi.mocked(h.backend.replayThreadMessages).mock.calls).toHaveLength(
      replayCallsBefore,
    );
    expect(h.backend.appendCalls).toHaveLength(appendCallsBefore);

    failEndedNotice = false;
    await h.app.orchestrator.runStartupRecovery();

    expect(
      h.feishuAdapter.sentTexts.filter((item) => item.text === endedText),
    ).toHaveLength(1);
    expect(noticeClaims()).toHaveLength(2);
    expect(noticeClaims()[1]).toMatchObject({
      delivery_key: noticeKey,
      kind: "notice",
      activity_origin: "none",
      source_scope_key: "dm:ou_alice",
    });
    expect(lastNoticeStatus()).toBe("sent");
    expect(inboundWrites()).toEqual([
      expect.objectContaining({
        platform: "feishu",
        message_id: messageId,
        status: "failed",
        fallback_pending: false,
      }),
    ]);

    await h.app.orchestrator.runStartupRecovery();

    expect(
      h.feishuAdapter.sentTexts.filter((item) => item.text === endedText),
    ).toHaveLength(1);
    expect(noticeClaims()).toHaveLength(2);
    expect(lastNoticeStatus()).toBe("sent");
    expect(inboundWrites()).toHaveLength(1);
    expect(vi.mocked(h.backend.replayThreadMessages).mock.calls).toHaveLength(
      replayCallsBefore,
    );
    expect(h.backend.appendCalls).toHaveLength(appendCallsBefore);
  });
});
