import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I7RecoverResponse,
  I9WeixinStateGetResponse,
  RecoverInboundEventRow,
  StoredBindingCommandResult,
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

    // Verify Weixin cache directory & state.json permissions and content
    const wxDir = join(h.tempDir, "weixin");
    const wxFile = join(wxDir, "state.json");
    expect(statSync(wxDir).mode & 0o777).toBe(0o700);
    expect(statSync(wxFile).mode & 0o777).toBe(0o600);
    const cachedWx = JSON.parse(readFileSync(wxFile, "utf8"));
    expect(cachedWx.bot_token).toBe("decrypted_secret_bot_token");
    expect(cachedWx.get_updates_buf).toBe("cursor_xyz");

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

  it("delivers stored bind and unbind confirmations without repeating the binding", async () => {
    const secret = "DLT-SECRET-ZZZZZZ";
    const linked = "Successfully linked your feishu account to Dalton.";
    const unlinked = "Unlinked your feishu account from Dalton.";
    const invalid =
      "Bind code is invalid or expired. Please generate a new code in Web Settings → Channels.";
    const revoked =
      "For security, `/bind` cannot be used in a group chat. This code has been revoked; please generate a new code and send `/bind <CODE>` in a direct message.";
    const mismatch =
      "This bind code was generated for discord, not feishu.";
    const row = (
      messageId: string,
      action: "bind" | "unbind",
      result: StoredBindingCommandResult,
      overrides: Partial<RecoverInboundEventRow> = {},
    ): RecoverInboundEventRow => ({
      platform: "feishu",
      message_id: messageId,
      platform_user_id: "ou_bind_ok",
      dalton_user_id: result.user_id ?? null,
      chat_type: "dm",
      chat_address: "feishu:@me",
      thread_scope_key: "dm:ou_bind_ok",
      request_key: `rk_${messageId}`,
      retry_request_key: null,
      resubmitted_request_key: null,
      steered_into_run_id: null,
      thread_id: null,
      run_id: null,
      turn_index: null,
      workspace_id: null,
      external_thread_id: null,
      event_kind: "command",
      command_request: { action, is_group: overrides.chat_type === "group" },
      command_result: result,
      command_effect_status: "done",
      command_reply_pending: true,
      status: result.ok ? "completed" : "failed",
      fallback_pending: false,
      sent_to_current_count: 0,
      uploaded_attachments: [],
      raw_payload: {
        platform_user_id: "ou_bind_ok",
        text: `/bind ${secret}`,
        code: secret,
      },
      ...overrides,
    });
    const rows = [
      row("om_rec_bind_ok", "bind", { ok: true, user_id: USER_ID, reason: null }),
      row("om_rec_unbind_ok", "unbind", { ok: true, user_id: USER_ID }, {
        dalton_user_id: null,
      }),
      row("om_rec_bind_invalid", "bind", {
        ok: false,
        reason: "invalid_or_expired",
        user_id: null,
      }, {
        platform_user_id: "ou_bind_invalid",
        dalton_user_id: null,
        raw_payload: {
          platform_user_id: "ou_bind_invalid",
          text: `/bind ${secret}`,
          code: secret,
        },
      }),
      row("om_rec_bind_group", "bind", {
        ok: false,
        reason: "revoked_in_group",
        user_id: null,
      }, {
        platform_user_id: "ou_bind_group",
        dalton_user_id: null,
        chat_type: "group",
        chat_address: "feishu:oc_group_1",
        thread_scope_key: "chat:oc_group_1",
        raw_payload: {
          platform_user_id: "ou_bind_group",
          chat_id: "oc_group_1",
          text: `/bind ${secret}`,
          code: secret,
        },
      }),
      row("om_rec_bind_mismatch", "bind", {
        ok: false,
        reason: "platform_mismatch",
        expected_platform: "discord",
        user_id: null,
      }, {
        platform_user_id: "ou_bind_mismatch",
        dalton_user_id: null,
        raw_payload: {
          platform_user_id: "ou_bind_mismatch",
          text: `/bind ${secret}`,
          code: secret,
        },
      }),
      row("om_rec_bind_done", "bind", { ok: true, user_id: USER_ID }, {
        command_reply_pending: false,
        platform_user_id: "ou_bind_done",
      }),
    ];
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: rows,
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });
    const watchAbort = new AbortController();
    h.app.orchestrator.activeWatches.set("watch-bind-recovery", {
      threadId: "watch-bind-recovery",
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_bind_ok",
      generation: 2,
      lastSeenTurnIndex: 0,
      abortController: watchAbort,
      promise: Promise.resolve(),
    });
    const stateCalls = vi.mocked(h.backend.getChannelState).mock.calls.length;
    const sessionCalls = vi.mocked(h.backend.sessionCommand).mock.calls.length;
    const bindCalls = vi.mocked(h.backend.consumeBindCode).mock.calls.length;
    const appendCalls = h.backend.appendCalls.length;
    const createCalls = h.backend.createCalls.length;

    await h.app.orchestrator.runStartupRecovery();

    const sent = h.feishuAdapter.sentTexts;
    const textOf = (needle: string) => sent.filter((item) => item.text === needle);
    expect(textOf(linked)).toHaveLength(1);
    expect(textOf(unlinked)).toHaveLength(1);
    expect(textOf(invalid)).toHaveLength(1);
    expect(textOf(revoked)).toHaveLength(1);
    expect(textOf(mismatch)).toHaveLength(1);
    expect(sent).toHaveLength(5);
    expect(sent.some((item) => item.text.includes(secret))).toBe(false);
    expect(sent.some((item) => item.text.includes("Available workspaces"))).toBe(false);
    expect(sent.some((item) => item.text.includes("Current workspace"))).toBe(false);
    expect(sent.some((item) => item.recipientPlatformUserId === "recovered_user")).toBe(false);

    expect(textOf(linked)[0]?.targetAddress).toBe("feishu:@me");
    expect(textOf(unlinked)[0]?.targetAddress).toBe("feishu:@me");
    expect(textOf(invalid)[0]).toMatchObject({
      targetAddress: "feishu:@me",
      recipientPlatformUserId: "ou_bind_invalid",
    });
    expect(textOf(revoked)[0]).toMatchObject({
      targetAddress: "feishu:oc_group_1",
      recipientPlatformUserId: "ou_bind_group",
    });
    expect(textOf(mismatch)[0]).toMatchObject({
      targetAddress: "feishu:@me",
      recipientPlatformUserId: "ou_bind_mismatch",
    });

    const claimsFor = (messageId: string) =>
      h.backend.deliveryClaims.filter((claim) => claim.inbound_message_id === messageId);
    expect(claimsFor("om_rec_bind_ok")).toEqual([
      expect.objectContaining({
        delivery_key: "notice:feishu:om_rec_bind_ok:command",
        user_id: USER_ID,
        kind: "notice",
        activity_origin: "none",
      }),
    ]);
    expect(claimsFor("om_rec_unbind_ok")).toEqual([
      expect.objectContaining({
        delivery_key: "notice:feishu:om_rec_unbind_ok:command",
        user_id: USER_ID,
        kind: "notice",
        activity_origin: "none",
      }),
    ]);
    expect(claimsFor("om_rec_bind_invalid")).toEqual([]);
    expect(claimsFor("om_rec_bind_group")).toEqual([]);
    expect(claimsFor("om_rec_bind_mismatch")).toEqual([]);
    expect(claimsFor("om_rec_bind_done")).toEqual([]);

    const progressFor = (messageId: string) =>
      h.backend.progressUpdates.filter((update) => update.message_id === messageId);
    expect(progressFor("om_rec_bind_ok")).toEqual([
      expect.objectContaining({
        status: "completed",
        command_reply_pending: false,
        fallback_pending: false,
      }),
    ]);
    expect(progressFor("om_rec_unbind_ok")).toEqual([
      expect.objectContaining({
        status: "completed",
        command_reply_pending: false,
      }),
    ]);
    for (const messageId of [
      "om_rec_bind_invalid",
      "om_rec_bind_group",
      "om_rec_bind_mismatch",
    ]) {
      expect(progressFor(messageId)).toEqual([
        expect.objectContaining({
          status: "failed",
          command_reply_pending: false,
          fallback_pending: false,
        }),
      ]);
    }
    expect(progressFor("om_rec_bind_done")).toEqual([]);

    expect(vi.mocked(h.backend.getChannelState).mock.calls).toHaveLength(stateCalls);
    expect(vi.mocked(h.backend.sessionCommand).mock.calls).toHaveLength(sessionCalls);
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls).toHaveLength(bindCalls);
    expect(h.backend.appendCalls).toHaveLength(appendCalls);
    expect(h.backend.createCalls).toHaveLength(createCalls);
    expect(h.app.orchestrator.activeWatches.has("watch-bind-recovery")).toBe(true);
    expect(watchAbort.signal.aborted).toBe(false);
  });

  it("retries a failed bind confirmation once, then ignores the already confirmed row", async () => {
    const messageId = "om_rec_bind_retry";
    const linked = "Successfully linked your feishu account to Dalton.";
    const pendingRow: RecoverInboundEventRow = {
      platform: "feishu",
      message_id: messageId,
      platform_user_id: "ou_bind_retry",
      dalton_user_id: USER_ID,
      chat_type: "dm",
      chat_address: "feishu:@me",
      thread_scope_key: "dm:ou_bind_retry",
      request_key: "rk_bind_retry",
      retry_request_key: null,
      resubmitted_request_key: null,
      steered_into_run_id: null,
      thread_id: null,
      run_id: null,
      turn_index: null,
      workspace_id: null,
      external_thread_id: null,
      event_kind: "command",
      command_request: { action: "bind", is_group: false },
      command_result: { ok: true, user_id: USER_ID },
      command_effect_status: "done",
      command_reply_pending: true,
      status: "completed",
      fallback_pending: false,
      sent_to_current_count: 0,
      uploaded_attachments: [],
      raw_payload: { platform_user_id: "ou_bind_retry", text: "" },
    };
    const recoveryOf = (replyPending: boolean): I7RecoverResponse => ({
      mode: "startup",
      inbound_events: [{ ...pendingRow, command_reply_pending: replyPending }],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });
    vi.mocked(h.backend.recoverState)
      .mockResolvedValueOnce(recoveryOf(true))
      .mockResolvedValueOnce(recoveryOf(true))
      .mockResolvedValueOnce(recoveryOf(false));
    let failConfirmation = true;
    h.feishuAdapter.setSendTextBehavior(() => {
      if (failConfirmation) {
        failConfirmation = false;
        throw new ChannelDeliveryError("bind confirmation failed", {
          outcome: "failed",
          code: "unavailable",
        });
      }
      return { messageId: "om_confirm_1" };
    });
    const sessionCalls = vi.mocked(h.backend.sessionCommand).mock.calls.length;
    const bindCalls = vi.mocked(h.backend.consumeBindCode).mock.calls.length;
    const claims = () =>
      h.backend.deliveryClaims.filter((claim) => claim.inbound_message_id === messageId);
    const progress = () =>
      h.backend.progressUpdates.filter((update) => update.message_id === messageId);

    await h.app.orchestrator.runStartupRecovery();

    expect(h.feishuAdapter.sentTexts.map((item) => item.text)).not.toContain(linked);
    expect(claims()).toHaveLength(1);
    expect(claims()[0]).toMatchObject({
      delivery_key: `notice:feishu:${messageId}:command`,
      user_id: USER_ID,
      activity_origin: "none",
    });
    expect(progress()).toEqual([]);
    expect(vi.mocked(h.backend.sessionCommand).mock.calls).toHaveLength(sessionCalls);
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls).toHaveLength(bindCalls);

    await h.app.orchestrator.runStartupRecovery();

    expect(h.feishuAdapter.sentTexts.filter((item) => item.text === linked)).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts.some((item) => item.text.includes("Available workspaces"))).toBe(false);
    expect(claims()).toHaveLength(2);
    expect(claims()[1]).toMatchObject({ activity_origin: "none" });
    expect(progress()).toEqual([
      expect.objectContaining({
        status: "completed",
        command_reply_pending: false,
        fallback_pending: false,
      }),
    ]);

    await h.app.orchestrator.runStartupRecovery();

    expect(h.feishuAdapter.sentTexts.filter((item) => item.text === linked)).toHaveLength(1);
    expect(claims()).toHaveLength(2);
    expect(progress()).toHaveLength(1);
    expect(vi.mocked(h.backend.sessionCommand).mock.calls).toHaveLength(sessionCalls);
    expect(vi.mocked(h.backend.consumeBindCode).mock.calls).toHaveLength(bindCalls);
  });
});
