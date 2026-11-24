import type * as Lark from "@larksuiteoapi/node-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError } from "../src/channels/adapter.js";
import {
  DISCORD_FAILED_EMOJI,
  DISCORD_PROCESSING_EMOJI,
  DISCORD_TYPING_INTERVAL_MS,
  DISCORD_TYPING_MAX_DURATION_MS,
  DiscordChannelAdapter,
  RequestClient,
} from "../src/channels/discord/index.js";
import {
  createFeishuHttpInstance,
  createFeishuSdkLogger,
  FEISHU_FAILED_EMOJI_TYPE,
  FEISHU_PROCESSING_EMOJI_FALLBACK_TYPE,
  FEISHU_PROCESSING_EMOJI_TYPE,
  FeishuChannelAdapter,
} from "../src/channels/feishu/index.js";
import {
  TypingStatus,
  WEIXIN_TYPING_INTERVAL_MS,
  WEIXIN_TYPING_MAX_DURATION_MS,
  WeixinChannelAdapter,
} from "../src/channels/weixin/index.js";
import { loadConfig } from "../src/config.js";
import type { SseEvent } from "../src/backend-client.js";
import type { SubsystemLogger } from "../src/shared/logger.js";
import type { InboundChannelEvent } from "../src/types.js";
import {
  createMockBackend,
  createTestHarness,
  loadFixtureKey,
  makeSseEvents,
  makeSseEventsFromFixture,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

function defer<T = void>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flushAsync(rounds = 32): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
  }
}

function makeFeishuDmEvent(
  messageId: string,
  text = "Analyze NVDA earnings",
): InboundChannelEvent {
  return {
    platform: "feishu",
    platformUserId: "ou_alice",
    chatType: "dm",
    chatAddress: "feishu:@me",
    chatId: "oc_dm_1",
    threadScopeKey: "dm:ou_alice",
    messageId,
    text,
  };
}

function makeFeishuGroupEvent(
  messageId: string,
  isGroupTrigger: boolean,
  text = "Check TSLA",
): InboundChannelEvent {
  return {
    platform: "feishu",
    platformUserId: "ou_alice",
    chatType: "group",
    chatAddress: "feishu:oc_group_1",
    chatId: "oc_group_1",
    threadScopeKey: "chat:oc_group_1",
    messageId,
    text,
    isGroupTrigger,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: status === 204 ? {} : { "content-type": "application/json" },
  });
}

describe("Status feedback config switches", () => {
  it("defaults global and all per-platform switches to true", () => {
    const cfg = loadConfig({});
    expect(cfg.statusFeedback).toEqual({
      enabled: true,
      platforms: {
        feishu: true,
        discord: true,
        weixin: true,
      },
    });
  });

  it("parses global and per-platform disable switches", () => {
    const globalOff = loadConfig({
      DALTON_STATUS_FEEDBACK_ENABLED: "false",
    });
    expect(globalOff.statusFeedback.enabled).toBe(false);
    expect(globalOff.statusFeedback.platforms).toEqual({
      feishu: true,
      discord: true,
      weixin: true,
    });

    const perPlatform = loadConfig({
      DALTON_STATUS_FEEDBACK_ENABLED: "true",
      DALTON_FEISHU_STATUS_FEEDBACK_ENABLED: "0",
      DALTON_DISCORD_STATUS_FEEDBACK_ENABLED: "no",
      DALTON_WEIXIN_STATUS_FEEDBACK_ENABLED: "off",
    });
    expect(perPlatform.statusFeedback).toEqual({
      enabled: true,
      platforms: {
        feishu: false,
        discord: false,
        weixin: false,
      },
    });
  });
});

describe("Orchestrator processing status feedback lifecycle (FakeChannelAdapter)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("does not add feedback for untriggered group messages, unlinked/denied users, or slash commands", async () => {
    // 1. Group message without mention trigger
    await h.feishuAdapter.emitInbound(makeFeishuGroupEvent("m_grp_skip", false));
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(0);

    // 2. Unlinked user (I-1 returns linked: false)
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      existing_status: null,
      linked: false,
      allowed: false,
      deny_reason: "not_linked",
      dalton_user_id: null,
      request_key: "rk_unlinked",
      retry_request_key: null,
      resubmitted_request_key: null,
      steered_into_run_id: null,
      thread_id: null,
      run_id: null,
      turn_index: null,
      workspace_id: null,
      external_thread_id: null,
      generation: 1,
      fallback_pending: false,
      sent_to_current_count: 0,
      uploaded_attachments: [],
      raw_payload: {},
      pending_interrupt: null,
    });
    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_unlinked"));
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(0);

    // 3. Linked but not allowed (I-1 returns allowed: false)
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      existing_status: null,
      linked: true,
      allowed: false,
      deny_reason: "workspace_write_denied",
      dalton_user_id: USER_ID,
      request_key: "rk_denied",
      retry_request_key: null,
      resubmitted_request_key: null,
      steered_into_run_id: null,
      thread_id: null,
      run_id: null,
      turn_index: null,
      workspace_id: null,
      external_thread_id: null,
      generation: 1,
      fallback_pending: false,
      sent_to_current_count: 0,
      uploaded_attachments: [],
      raw_payload: {},
      pending_interrupt: null,
    });
    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_denied"));
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(0);

    // 4. Slash command
    await h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_slash", "/workspace"),
    );
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(0);
  });

  it("adds feedback after passing trigger gate + I-1 linked/allowed and clears on first reply", async () => {
    let feedbackActiveDuringSend = false;
    h.feishuAdapter.setSendTextBehavior((opts) => {
      const rec = h.feishuAdapter.feedbackRecords.find(
        (r) => r.messageId === "m_grp_ok",
      );
      if (opts.chunkIndex === 0) {
        feedbackActiveDuringSend = Boolean(rec && !rec.settled);
      }
      return { messageId: "feishu_reply_1", resolvedAddress: opts.targetAddress };
    });

    await h.feishuAdapter.emitInbound(makeFeishuGroupEvent("m_grp_ok", true));

    expect(feedbackActiveDuringSend).toBe(true);
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_grp_ok",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
  });

  it("clears feedback immediately when agent delivers first reply via /agent/send mid-turn", async () => {
    const finishTurnGate = defer<void>();
    let settledBeforeRunEnd = false;

    vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(async () => ({
      status: 200,
      isSse: true,
      events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
        yield {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            turn_index: 1,
          }),
        };
        // Simulate mid-turn `/agent/send` before `run_end`
        await h.app.agentRoutes.handleAgentSend(USER_ID, {
          thread_id: THREAD_ID,
          run_id: RUN_ID,
          tool_call_id: "tc_first_reply",
          turn_platform: "feishu",
          text: "First reply from agent!",
        });
        settledBeforeRunEnd =
          h.feishuAdapter.feedbackRecords[0]?.settled === true &&
          h.feishuAdapter.feedbackRecords[0]?.outcome === "done";
        await finishTurnGate.promise;
        yield {
          event: "run_end",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            turn_index: 1,
            outcome: "completed",
          }),
        };
      })(),
    }));

    const turnPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_agent_send_1"),
    );
    await flushAsync(20);
    finishTurnGate.resolve();
    await turnPromise;

    expect(settledBeforeRunEnd).toBe(true);
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_agent_send_1",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
  });

  it("clears the processing marker (`failed`) after an SSE error whether or not the error notice is delivered", async () => {
    // Case A: SSE stream error + error notice succeeds -> outcome is "failed" (clean up only, no CrossMark/⚠️)
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([
        {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            turn_index: 1,
          }),
        },
        {
          event: "error",
          data: JSON.stringify({ message: "Model provider error" }),
        },
      ]),
    });

    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_err_notice_ok"));
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "Model provider error",
    );
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_err_notice_ok",
      settled: true,
      outcome: "failed",
      settleCount: 1,
    });

    // Case B: SSE stream error + error notice delivery fails -> outcome is "failed": a failed or
    // cancelled turn only clears the processing marker (no CrossMark/⚠️), since another reply
    // can still be settling at that moment.
    h.feishuAdapter.setSendTextBehavior(() => {
      throw new ChannelDeliveryError("Platform rejected message", {
        outcome: "failed",
        code: "unavailable",
      });
    });
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([
        {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: "run_total_fail",
            turn_index: 2,
          }),
        },
        {
          event: "error",
          data: JSON.stringify({ message: "Fatal turn failure" }),
        },
      ]),
    });

    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_err_total_fail"));
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(2);
    expect(h.feishuAdapter.feedbackRecords[1]).toMatchObject({
      messageId: "m_err_total_fail",
      settled: true,
      outcome: "failed",
      settleCount: 1,
    });
  });

  it("clears feedback immediately when an interrupt prompt is delivered", async () => {
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([
        {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            turn_index: 1,
          }),
        },
        {
          event: "interrupt",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            interrupt_id: "int_q1",
            kind: "ask_user_question",
            questions: [
              {
                id: "q1",
                question: "Which time horizon?",
                options: [{ label: "1Y" }, { label: "3Y" }],
              },
            ],
          }),
        },
      ]),
    });

    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_interrupt_1"));

    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "Which time horizon?",
    );
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_interrupt_1",
      settled: true,
      outcome: "interrupted",
      settleCount: 1,
    });
  });

  it("adds feedback to steered follow-up messages and settles both original and steered handles when the host turn ends", async () => {
    const releaseHostTurn = defer<void>();
    const hostRunStarted = defer<void>();
    const http409Running = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_running",
    );
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );

    vi.mocked(h.backend.appendThreadMessages)
      // 1. Host turn (m_host) stays open until releaseHostTurn resolves
      .mockImplementationOnce(async () => ({
        status: 200,
        isSse: true,
        events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
          yield {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: RUN_ID,
              turn_index: 1,
            }),
          };
          hostRunStarted.resolve();
          await releaseHostTurn.promise;
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              content_type: "text",
              content: "Combined answer for host + follow-up!",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: RUN_ID,
              turn_index: 1,
              outcome: "completed",
            }),
          };
        })(),
      }))
      // 2. Steer follow-up (m_steer) gets 409 running
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Running,
      })
      // 3. Steer follow-up (m_steer) succeeds with steer_only: true -> steering_accepted
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([steeringAccepted]),
      });

    // Keep the steered message's stream attachment open until the host turn finishes
    vi.mocked(h.backend.streamThreadMessages).mockImplementationOnce(
      async () => ({
        status: 200,
        isSse: true,
        events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
          await releaseHostTurn.promise;
        })(),
      }),
    );

    const hostPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_host", "Analyze AAPL"),
    );
    await hostRunStarted.promise;

    const steerPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_steer", "Also compare with MSFT"),
    );
    await flushAsync(20);

    // Both messages have active, unsettled feedback while the host run is in progress
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(2);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_host",
      settled: false,
    });
    expect(h.feishuAdapter.feedbackRecords[1]).toMatchObject({
      messageId: "m_steer",
      settled: false,
    });

    releaseHostTurn.resolve();
    await Promise.all([hostPromise, steerPromise]);
    await flushAsync(20);

    // Both original and steered messages are settled together when the host turn completes
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_host",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
    expect(h.feishuAdapter.feedbackRecords[1]).toMatchObject({
      messageId: "m_steer",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
  });

  it("clears main turn feedback immediately even when ActiveWatchHandle remains open for background work", async () => {
    // Keep watch open by reporting an armed wakeup on the thread
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([
      {
        wakeup_id: "wk_1",
        state: "armed",
      },
    ]);

    await h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_bg_handoff", "Run deep research in background"),
    );

    expect(h.app.orchestrator.activeWatches.has(THREAD_ID)).toBe(true);
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_bg_handoff",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
  });

  it("does not block or disrupt reply delivery when status feedback API throws, rejects, hangs, or fails on settle", async () => {
    // 1. Synchronous throw in beginProcessingFeedback
    h.feishuAdapter.setBeginFeedbackBehavior(() => {
      throw new Error("sync status api boom");
    });
    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_sync_throw"));
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Assistant reply on existing thread.",
    );

    // 2. Async rejection in beginProcessingFeedback
    h.feishuAdapter.setBeginFeedbackBehavior(async () => {
      throw new Error("async status api boom");
    });
    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_async_reject"));
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Assistant reply on existing thread.",
    );

    // 3. Never-resolving beginProcessingFeedback does NOT block reply delivery
    const neverResolves = defer<null>();
    h.feishuAdapter.setBeginFeedbackBehavior(() => neverResolves.promise);
    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_hang_begin"));
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Assistant reply on existing thread.",
    );

    // 4. settle() throws/rejects does NOT disrupt reply delivery
    h.feishuAdapter.setBeginFeedbackBehavior(() => ({
      settle: async () => {
        throw new Error("settle failed");
      },
    }));
    await h.feishuAdapter.emitInbound(makeFeishuDmEvent("m_settle_throw"));
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Assistant reply on existing thread.",
    );
  });

  it("skips status feedback when global or per-platform switch is disabled", async () => {
    const globalOffHarness = createTestHarness({
      env: { DALTON_STATUS_FEEDBACK_ENABLED: "false" },
    });
    try {
      await globalOffHarness.feishuAdapter.emitInbound(
        makeFeishuDmEvent("m_global_off"),
      );
      expect(globalOffHarness.feishuAdapter.feedbackRecords).toHaveLength(0);
      expect(globalOffHarness.feishuAdapter.sentTexts).toHaveLength(1);
    } finally {
      await globalOffHarness.cleanup();
    }

    const feishuOffHarness = createTestHarness({
      env: {
        DALTON_STATUS_FEEDBACK_ENABLED: "true",
        DALTON_FEISHU_STATUS_FEEDBACK_ENABLED: "false",
      },
    });
    try {
      await feishuOffHarness.feishuAdapter.emitInbound(
        makeFeishuDmEvent("m_feishu_off"),
      );
      expect(feishuOffHarness.feishuAdapter.feedbackRecords).toHaveLength(0);

      await feishuOffHarness.discordAdapter.emitInbound({
        platform: "discord",
        platformUserId: "dc_alice",
        chatType: "dm",
        chatAddress: "discord:@me",
        chatId: "chan_dm_1",
        threadScopeKey: "dm:dc_alice",
        messageId: "m_discord_on",
        text: "Hello from Discord",
      });
      expect(feishuOffHarness.discordAdapter.feedbackRecords).toHaveLength(1);
      expect(feishuOffHarness.discordAdapter.feedbackRecords[0]).toMatchObject({
        messageId: "m_discord_on",
        settled: true,
        outcome: "done",
      });
    } finally {
      await feishuOffHarness.cleanup();
    }
  });

  it("reuses the active feedback handle across in-process recovery after a network reset and settles with done", async () => {
    // 1st attempt hits a mid-request network reset; launchInboundTurn recovers in-process via recoverDetachedInboundTurn
    vi.mocked(h.backend.appendThreadMessages)
      .mockRejectedValueOnce(new Error("ECONNRESET: socket hang up"))
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([
          {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: "run_recovered_1",
              turn_index: 1,
            }),
          },
          {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              content_type: "text",
              content: "Recovered reply after network reset!",
            }),
          },
          {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: "run_recovered_1",
              turn_index: 1,
              outcome: "completed",
            }),
          },
        ]),
      });

    await h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_net_reset", "Recover me"),
    );
    await flushAsync(40);

    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Recovered reply after network reset!",
    );
    // Same handle reused (only 1 begin call, never stamped delivery_failed)
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_net_reset",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
  });

  it("does not stamp delivery_failed when SSE cuts off while delivery is still in_flight/unknown or when error notice status is unknown", async () => {
    // Case A: SSE stream ends cleanly without run_end (bare EOF) while an /agent/send delivery is still in_flight/unknown
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([
        {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: "run_eof_inflight",
            turn_index: 1,
          }),
        },
        // Stream closes abruptly before run_end
      ]),
    });
    vi.mocked(h.backend.updateInboundProgress).mockResolvedValueOnce({
      ok: true,
      run_id: "run_eof_inflight",
      has_in_flight_current_text: true,
      has_unknown_current_text: false,
      has_in_flight_or_unknown_current_text: true,
      sent_to_current_count: 0,
    });

    await h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_sse_eof_inflight", "Wait for in-flight send"),
    );
    await flushAsync(40);

    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_sse_eof_inflight",
      settled: true,
      outcome: "failed",
      settleCount: 1,
    });

    // Case B: SSE error event where error notice transport returns uncertain outcome ("unknown") -> NOT delivery_failed
    h.feishuAdapter.setSendTextBehavior(() => {
      throw new ChannelDeliveryError("Timeout waiting for platform ack", {
        outcome: "unknown",
        code: "unavailable",
      });
    });
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([
        {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: "run_err_unknown",
            turn_index: 2,
          }),
        },
        {
          event: "error",
          data: JSON.stringify({ message: "Upstream model error" }),
        },
      ]),
    });

    await h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_err_unknown_notice", "Uncertain error notice"),
    );
    await flushAsync(40);

    expect(h.feishuAdapter.feedbackRecords).toHaveLength(2);
    expect(h.feishuAdapter.feedbackRecords[1]).toMatchObject({
      messageId: "m_err_unknown_notice",
      settled: true,
      outcome: "failed",
      settleCount: 1,
    });
  });

  it("clears only the primary message on interim /agent/send and keeps steered follow-ups active until host run ends", async () => {
    const releaseHostTurn = defer<void>();
    const hostRunStarted = defer<void>();
    const http409Running = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_running",
    );
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );

    vi.mocked(h.backend.appendThreadMessages)
      .mockImplementationOnce(async () => ({
        status: 200,
        isSse: true,
        events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
          yield {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: RUN_ID,
              turn_index: 1,
            }),
          };
          hostRunStarted.resolve();
          await releaseHostTurn.promise;
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              content_type: "text",
              content: "Final host + steer wrap-up",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: RUN_ID,
              turn_index: 1,
              outcome: "completed",
            }),
          };
        })(),
      }))
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Running,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([steeringAccepted]),
      });

    vi.mocked(h.backend.streamThreadMessages).mockImplementationOnce(
      async () => ({
        status: 200,
        isSse: true,
        events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
          await releaseHostTurn.promise;
        })(),
      }),
    );

    const hostPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_host_interim", "Primary question"),
    );
    await hostRunStarted.promise;

    const steerPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_steer_interim", "Follow-up steer"),
    );
    await flushAsync(20);

    // Trigger an interim `/agent/send` on the host run while both primary & steered handles are active
    await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_interim_1",
      turn_platform: "feishu",
      text: "Interim progress update for primary",
    });

    // Primary message is cleared immediately; steered follow-up remains active!
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_host_interim",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
    expect(h.feishuAdapter.feedbackRecords[1]).toMatchObject({
      messageId: "m_steer_interim",
      settled: false,
    });

    releaseHostTurn.resolve();
    await Promise.all([hostPromise, steerPromise]);
    await flushAsync(40);

    expect(h.feishuAdapter.feedbackRecords[0]?.settleCount).toBe(1);
    expect(h.feishuAdapter.feedbackRecords[1]).toMatchObject({
      messageId: "m_steer_interim",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
  });

  it("re-attaches feedback when an unconsumed steered follow-up is resubmitted as a fresh turn", async () => {
    const releaseHostTurn = defer<void>();
    const hostRunStarted = defer<void>();
    const http409Running = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_running",
    );
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const reconcilePendingResubmit = loadFixtureKey<{
      ok: boolean;
      reconcile_status: "pending_resubmit";
      outcome: "returned";
      run_status: string;
      inbound_status: string;
      resubmitted_request_key: string;
    }>("i7_progress_and_reconcile.json", "reconcile_pending_resubmit");

    let feedbackActiveDuringResubmittedReply = false;
    h.feishuAdapter.setSendTextBehavior((opts) => {
      if (opts.text.includes("Resubmitted follow-up answer")) {
        const activeSteerRec = h.feishuAdapter.feedbackRecords
          .filter((r) => r.messageId === "m_steer_resub")
          .at(-1);
        feedbackActiveDuringResubmittedReply = Boolean(
          activeSteerRec && !activeSteerRec.settled,
        );
      }
      return { messageId: `reply_${Date.now()}`, resolvedAddress: opts.targetAddress };
    });

    vi.mocked(h.backend.reconcileSteer).mockResolvedValueOnce(
      reconcilePendingResubmit,
    );

    vi.mocked(h.backend.appendThreadMessages)
      // 1. Host turn
      .mockImplementationOnce(async () => ({
        status: 200,
        isSse: true,
        events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
          yield {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: RUN_ID,
              turn_index: 1,
            }),
          };
          hostRunStarted.resolve();
          await releaseHostTurn.promise;
          yield {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              content_type: "text",
              content: "Host turn done without taking steer",
            }),
          };
          yield {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: RUN_ID,
              turn_index: 1,
              outcome: "completed",
            }),
          };
        })(),
      }))
      // 2. Steer hits 409 running
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Running,
      })
      // 3. Steer accepted via steer_only
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([steeringAccepted]),
      })
      // 4. Resubmitted as a brand-new turn after host finishes
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([
          {
            event: "metadata",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: "run_resubmitted_2",
              turn_index: 2,
            }),
          },
          {
            event: "message_chunk",
            data: JSON.stringify({
              agent: "main",
              content_type: "text",
              content: "Resubmitted follow-up answer",
            }),
          },
          {
            event: "run_end",
            data: JSON.stringify({
              thread_id: THREAD_ID,
              run_id: "run_resubmitted_2",
              turn_index: 2,
              outcome: "completed",
            }),
          },
        ]),
      });

    vi.mocked(h.backend.streamThreadMessages).mockImplementationOnce(
      async () => ({
        status: 200,
        isSse: true,
        events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
          await releaseHostTurn.promise;
        })(),
      }),
    );

    const hostPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_host_resub", "First question"),
    );
    await hostRunStarted.promise;

    const steerPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_steer_resub", "Unconsumed follow-up"),
    );
    await flushAsync(20);

    releaseHostTurn.resolve();
    await Promise.all([hostPromise, steerPromise]);
    await flushAsync(40);

    expect(feedbackActiveDuringResubmittedReply).toBe(true);
    const steerRecords = h.feishuAdapter.feedbackRecords.filter(
      (r) => r.messageId === "m_steer_resub",
    );
    expect(steerRecords.length).toBeGreaterThanOrEqual(1);
    expect(steerRecords.at(-1)).toMatchObject({
      messageId: "m_steer_resub",
      settled: true,
      outcome: "done",
    });
  });

  it("clears primary feedback immediately upon bindRun when /agent/send arrives before metadata", async () => {
    const finishTurnGate = defer<void>();
    let settledRightAfterMetadata = false;

    vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(async () => ({
      status: 200,
      isSse: true,
      events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
        // `/agent/send` arrives BEFORE `metadata` binds RUN_ID to the inbound message
        await h.app.agentRoutes.handleAgentSend(USER_ID, {
          thread_id: THREAD_ID,
          run_id: "run_early_send",
          tool_call_id: "tc_early_1",
          turn_platform: "feishu",
          text: "Early reply before metadata!",
        });
        yield {
          event: "metadata",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: "run_early_send",
            turn_index: 1,
          }),
        };
        await flushAsync(4);
        settledRightAfterMetadata =
          h.feishuAdapter.feedbackRecords[0]?.settled === true &&
          h.feishuAdapter.feedbackRecords[0]?.outcome === "done";
        await finishTurnGate.promise;
        yield {
          event: "run_end",
          data: JSON.stringify({
            thread_id: THREAD_ID,
            run_id: "run_early_send",
            turn_index: 1,
            outcome: "completed",
          }),
        };
      })(),
    }));

    const turnPromise = h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_early_send"),
    );
    await flushAsync(20);
    finishTurnGate.resolve();
    await turnPromise;

    expect(settledRightAfterMetadata).toBe(true);
    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_early_send",
      settled: true,
      outcome: "done",
      settleCount: 1,
    });
  });

  it("waits for an in-flight /agent/send reply before settling on run_end: cancelled or SSE error (clears the processing marker; never stamps delivery_failed on a cancelled or failed turn)", async () => {
    // Case A: run_end: cancelled while /agent/send body is in-flight & notice fails -> body succeeds -> settles as "done" (no failure emoji)
    {
      const bodySendGate = defer<{ messageId: string; resolvedAddress: string }>();
      const bodySendStarted = defer<void>();

      h.feishuAdapter.setSendTextBehavior((opts) => {
        if (opts.text.includes("In-flight body reply")) {
          bodySendStarted.resolve();
          return bodySendGate.promise;
        }
        // Notice fails definitively
        throw new ChannelDeliveryError("Notice send rejected", {
          outcome: "failed",
          code: "unavailable",
        });
      });

      let agentSendPromise!: Promise<unknown>;
      vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(
        async () => ({
          status: 200,
          isSse: true,
          events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
            yield {
              event: "metadata",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: "run_cancel_inflight",
                turn_index: 1,
              }),
            };
            agentSendPromise = h.app.agentRoutes.handleAgentSend(USER_ID, {
              thread_id: THREAD_ID,
              run_id: "run_cancel_inflight",
              tool_call_id: "tc_cancel_body",
              turn_platform: "feishu",
              text: "In-flight body reply before stop",
            });
            await bodySendStarted.promise;
            yield {
              event: "run_end",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: "run_cancel_inflight",
                turn_index: 1,
                outcome: "cancelled",
              }),
            };
          })(),
        }),
      );

      const turnPromise = h.feishuAdapter.emitInbound(
        makeFeishuDmEvent("m_cancel_inflight", "Run long task then stop"),
      );
      await bodySendStarted.promise;
      await flushAsync(40);

      // While body send is still in-flight, feedback MUST NOT be stamped delivery_failed
      const recMid = h.feishuAdapter.feedbackRecords.find(
        (r) => r.messageId === "m_cancel_inflight",
      );
      expect(recMid).toMatchObject({
        messageId: "m_cancel_inflight",
        settled: false,
      });

      // Body send succeeds -> handle settles cleanly as "done" (no CrossMark/⚠️)
      bodySendGate.resolve({
        messageId: "feishu_body_ok",
        resolvedAddress: "feishu:@me",
      });
      await Promise.all([turnPromise, agentSendPromise]);
      await flushAsync(40);

      expect(recMid).toMatchObject({
        messageId: "m_cancel_inflight",
        settled: true,
        outcome: "done",
        settleCount: 1,
      });
    }

    // Case B: non-conflict SSE error while /agent/send body is in-flight & notice fails -> body succeeds -> "done"
    {
      const bodySendGate = defer<{ messageId: string; resolvedAddress: string }>();
      const bodySendStarted = defer<void>();

      h.feishuAdapter.setSendTextBehavior((opts) => {
        if (opts.text.includes("In-flight body before SSE error")) {
          bodySendStarted.resolve();
          return bodySendGate.promise;
        }
        throw new ChannelDeliveryError("Notice send rejected", {
          outcome: "failed",
          code: "unavailable",
        });
      });

      let agentSendPromise!: Promise<unknown>;
      vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(
        async () => ({
          status: 200,
          isSse: true,
          events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
            yield {
              event: "metadata",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: "run_sse_err_inflight",
                turn_index: 2,
              }),
            };
            agentSendPromise = h.app.agentRoutes.handleAgentSend(USER_ID, {
              thread_id: THREAD_ID,
              run_id: "run_sse_err_inflight",
              tool_call_id: "tc_sse_err_body",
              turn_platform: "feishu",
              text: "In-flight body before SSE error",
            });
            await bodySendStarted.promise;
            yield {
              event: "error",
              data: JSON.stringify({ message: "Model stream error" }),
            };
          })(),
        }),
      );

      const turnPromise = h.feishuAdapter.emitInbound(
        makeFeishuDmEvent("m_sse_err_inflight", "Trigger SSE error mid-send"),
      );
      await bodySendStarted.promise;
      await flushAsync(40);

      const recMid = h.feishuAdapter.feedbackRecords.find(
        (r) => r.messageId === "m_sse_err_inflight",
      );
      expect(recMid?.settled).toBe(false);

      bodySendGate.resolve({
        messageId: "feishu_body_ok_2",
        resolvedAddress: "feishu:@me",
      });
      await Promise.all([turnPromise, agentSendPromise]);
      await flushAsync(40);

      expect(recMid).toMatchObject({
        messageId: "m_sse_err_inflight",
        settled: true,
        outcome: "done",
        settleCount: 1,
      });
    }

    // Case C: run_end: cancelled while /agent/send body is in-flight -> BOTH body and notice fail -> settles as "failed" (cancelled turns only clear the processing marker)
    {
      const bodySendGate = defer<{ messageId: string; resolvedAddress: string }>();
      const bodySendStarted = defer<void>();

      h.feishuAdapter.setSendTextBehavior((opts) => {
        if (opts.text.includes("In-flight body doomed")) {
          bodySendStarted.resolve();
          return bodySendGate.promise;
        }
        throw new ChannelDeliveryError("Notice send rejected", {
          outcome: "failed",
          code: "unavailable",
        });
      });

      let agentSendPromise!: Promise<unknown>;
      vi.mocked(h.backend.appendThreadMessages).mockImplementationOnce(
        async () => ({
          status: 200,
          isSse: true,
          events: (async function* (): AsyncGenerator<SseEvent, void, unknown> {
            yield {
              event: "metadata",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: "run_cancel_both_fail",
                turn_index: 3,
              }),
            };
            agentSendPromise = h.app.agentRoutes.handleAgentSend(USER_ID, {
              thread_id: THREAD_ID,
              run_id: "run_cancel_both_fail",
              tool_call_id: "tc_cancel_fail",
              turn_platform: "feishu",
              text: "In-flight body doomed",
            });
            await bodySendStarted.promise;
            yield {
              event: "run_end",
              data: JSON.stringify({
                thread_id: THREAD_ID,
                run_id: "run_cancel_both_fail",
                turn_index: 3,
                outcome: "cancelled",
              }),
            };
          })(),
        }),
      );

      const turnPromise = h.feishuAdapter.emitInbound(
        makeFeishuDmEvent("m_cancel_both_fail", "Both fail"),
      );
      await bodySendStarted.promise;
      await flushAsync(40);

      const recMid = h.feishuAdapter.feedbackRecords.find(
        (r) => r.messageId === "m_cancel_both_fail",
      );
      expect(recMid?.settled).toBe(false);

      bodySendGate.reject(
        new ChannelDeliveryError("Body send rejected", {
          outcome: "failed",
          code: "unavailable",
        }),
      );
      await Promise.all([turnPromise, agentSendPromise]);
      await flushAsync(40);

      expect(recMid).toMatchObject({
        messageId: "m_cancel_both_fail",
        settled: true,
        outcome: "failed",
        settleCount: 1,
      });
    }
  });

  it("clears processing feedback without delivery_failed when startup recovery throws during executeRegisteredInboundTurn", async () => {
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: [
        {
          platform: "feishu",
          message_id: "m_startup_rec_throw",
          dalton_user_id: USER_ID,
          chat_type: "dm",
          chat_address: "feishu:@me",
          thread_scope_key: "dm:ou_alice",
          request_key: "rk_startup_rec_throw",
          retry_request_key: null,
          resubmitted_request_key: null,
          steered_into_run_id: null,
          thread_id: THREAD_ID,
          run_id: null,
          turn_index: null,
          workspace_id: null,
          external_thread_id: "feishu:dm:ou_alice:gen:1",
          status: "received",
          fallback_pending: false,
          sent_to_current_count: 0,
          uploaded_attachments: [],
          raw_payload: {
            platform_user_id: "ou_alice",
            generation: 1,
            text: "Recover on boot",
          },
          created_at: "2026-11-24T00:00:00+00:00",
          updated_at: "2026-11-24T00:00:00+00:00",
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });

    vi.mocked(h.backend.appendThreadMessages).mockRejectedValueOnce(
      new Error("Startup backend connection refused"),
    );

    await h.app.orchestrator.runStartupRecovery();
    await flushAsync(40);

    const rec = h.feishuAdapter.feedbackRecords.find(
      (r) => r.messageId === "m_startup_rec_throw",
    );
    expect(rec).toMatchObject({
      messageId: "m_startup_rec_throw",
      settled: true,
      outcome: "failed",
      settleCount: 1,
    });
  });

  it("clears processing feedback without delivery_failed when backend returns 409 duplicate_request with no run info", async () => {
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: {
        detail: {
          error_type: "duplicate_request",
        },
      },
    });

    await h.feishuAdapter.emitInbound(
      makeFeishuDmEvent("m_dup_no_run", "Duplicate without run metadata"),
    );
    await flushAsync(40);

    expect(h.feishuAdapter.feedbackRecords).toHaveLength(1);
    expect(h.feishuAdapter.feedbackRecords[0]).toMatchObject({
      messageId: "m_dup_no_run",
      settled: true,
      outcome: "failed",
      settleCount: 1,
    });
  });
});

describe("FeishuChannelAdapter processing status feedback", () => {
  function makeReactionClient(handlers?: {
    onCreate?: (req: {
      path: { message_id: string };
      data: { reaction_type: { emoji_type: string } };
    }) => Promise<{ code?: number; msg?: string; data?: { reaction_id?: string } }>;
    onDelete?: (req: {
      path: { message_id: string; reaction_id: string };
    }) => Promise<{ code?: number; msg?: string }>;
  }) {
    const createCalls: Array<{
      path: { message_id: string };
      data: { reaction_type: { emoji_type: string } };
    }> = [];
    const deleteCalls: Array<{
      path: { message_id: string; reaction_id: string };
    }> = [];
    let seq = 1;
    const client = {
      im: {
        messageReaction: {
          create: async (req: {
            path: { message_id: string };
            data: { reaction_type: { emoji_type: string } };
          }) => {
            createCalls.push(req);
            if (handlers?.onCreate) return handlers.onCreate(req);
            return { code: 0, data: { reaction_id: `rx_${seq++}` } };
          },
          delete: async (req: {
            path: { message_id: string; reaction_id: string };
          }) => {
            deleteCalls.push(req);
            if (handlers?.onDelete) return handlers.onDelete(req);
            return { code: 0 };
          },
        },
      },
    } as unknown as Lark.Client;
    return { client, createCalls, deleteCalls };
  }

  it("adds Typing reaction, deletes it on done, and handles settle before create resolves", async () => {
    const gate = defer<{
      code: number;
      data: { reaction_id: string };
    }>();
    const { client, createCalls, deleteCalls } = makeReactionClient({
      onCreate: () => gate.promise,
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      client,
    });

    const handle = adapter.beginProcessingFeedback({
      event: makeFeishuDmEvent("om_fast_1"),
    });
    expect(handle).not.toBeNull();
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0]!.data.reaction_type.emoji_type).toBe(
      FEISHU_PROCESSING_EMOJI_TYPE,
    );

    // Settle BEFORE create returns (non-blocking fast reply race)
    const settlePromise = handle!.settle("done");
    gate.resolve({ code: 0, data: { reaction_id: "rx_fast_1" } });
    await settlePromise;

    expect(deleteCalls).toHaveLength(1);
    expect(deleteCalls[0]!.path).toEqual({
      message_id: "om_fast_1",
      reaction_id: "rx_fast_1",
    });
  });

  it("falls back to OnIt independently per message when concurrent messages hit 231001", async () => {
    const gate1 = defer<{ code: number; msg: string }>();
    const gate2 = defer<{ code: number; msg: string }>();
    let typingCalls = 0;
    const { client, createCalls, deleteCalls } = makeReactionClient({
      onCreate: async (req) => {
        if (req.data.reaction_type.emoji_type === FEISHU_PROCESSING_EMOJI_TYPE) {
          typingCalls += 1;
          return typingCalls === 1 ? gate1.promise : gate2.promise;
        }
        return { code: 0, data: { reaction_id: `rx_onit_${req.path.message_id}` } };
      },
    });
    const adapter = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      client,
    });

    const h1 = adapter.beginProcessingFeedback({
      event: makeFeishuDmEvent("om_fb_1"),
    });
    const h2 = adapter.beginProcessingFeedback({
      event: makeFeishuDmEvent("om_fb_2"),
    });
    gate1.resolve({ code: 231001, msg: "invalid emoji_type" });
    gate2.resolve({ code: 231001, msg: "invalid emoji_type" });

    await Promise.all([h1!.settle("done"), h2!.settle("done")]);
    expect(createCalls.map((c) => c.data.reaction_type.emoji_type)).toEqual([
      FEISHU_PROCESSING_EMOJI_TYPE,
      FEISHU_PROCESSING_EMOJI_TYPE,
      FEISHU_PROCESSING_EMOJI_FALLBACK_TYPE,
      FEISHU_PROCESSING_EMOJI_FALLBACK_TYPE,
    ]);
    expect(deleteCalls).toHaveLength(2);
  });

  it("removes Typing and adds CrossMark on delivery_failed", async () => {
    const { client, createCalls, deleteCalls } = makeReactionClient();
    const adapter = new FeishuChannelAdapter({
      appId: "cli_test",
      appSecret: "sec_test",
      client,
    });

    const handle = adapter.beginProcessingFeedback({
      event: makeFeishuDmEvent("om_fail_1"),
    });
    await handle!.settle("delivery_failed");

    expect(createCalls.map((c) => c.data.reaction_type.emoji_type)).toEqual([
      FEISHU_PROCESSING_EMOJI_TYPE,
      FEISHU_FAILED_EMOJI_TYPE,
    ]);
    expect(deleteCalls).toHaveLength(1);
  });

  it("silently abandons on withdrawn/deleted errors (230011, 231003, 230110) without adding CrossMark", async () => {
    for (const code of [230011, 231003, 230110]) {
      const { client, createCalls, deleteCalls } = makeReactionClient({
        onCreate: async () => ({ code, msg: "message withdrawn or deleted" }),
      });
      const adapter = new FeishuChannelAdapter({
        appId: "cli_test",
        appSecret: "sec_test",
        client,
      });

      const handle = adapter.beginProcessingFeedback({
        event: makeFeishuDmEvent(`om_withdrawn_${code}`),
      });
      await handle!.settle("delivery_failed");

      expect(createCalls).toHaveLength(1);
      expect(deleteCalls).toHaveLength(0);
    }
  });

  it("applies default HTTP timeout to SDK httpInstance.post (token fetch) while preserving explicit timeouts", async () => {
    const postCalls: Array<{
      url: string;
      data: unknown;
      opts: Record<string, unknown> | undefined;
    }> = [];
    const baseHttp = {
      request: async () => ({}),
      get: async () => ({}),
      post: async (
        url: string,
        data?: unknown,
        opts?: Record<string, unknown>,
      ) => {
        postCalls.push({ url, data, opts });
        return { code: 0, tenant_access_token: "t_123" };
      },
      put: async () => ({}),
      patch: async () => ({}),
      delete: async () => ({}),
      head: async () => ({}),
      options: async () => ({}),
    } as unknown as Lark.HttpInstance;

    const http = createFeishuHttpInstance({
      defaultTimeoutMs: 12_345,
      baseHttpInstance: baseHttp,
    });

    await http.post("/open-apis/auth/v3/tenant_access_token/internal", {
      app_id: "cli_1",
      app_secret: "sec_1",
    });
    await http.post(
      "/open-apis/auth/v3/tenant_access_token/internal",
      {},
      { timeout: 5_000 },
    );

    expect(postCalls).toHaveLength(2);
    expect(postCalls[0]!.opts).toMatchObject({ timeout: 12_345 });
    expect(postCalls[1]!.opts).toMatchObject({ timeout: 5_000 });
  });

  it("downgrades expected Feishu SDK 400 error logs (230011, 231003, 230110, 231001) to debug while keeping unexpected errors at error", () => {
    const debugCalls: unknown[][] = [];
    const errorCalls: unknown[][] = [];
    const mockLogger: SubsystemLogger = {
      subsystem: "test/feishu",
      debug: (...args: unknown[]) => debugCalls.push(args),
      info: () => {},
      warn: () => {},
      error: (...args: unknown[]) => errorCalls.push(args),
      child: () => mockLogger,
    };
    const sdkLogger = createFeishuSdkLogger({
      appId: "cli_secret_id",
      appSecret: "sec_secret_val",
      logger: mockLogger,
    });

    for (const code of [230011, 231003, 230110, 231001]) {
      sdkLogger.error("request failed", {
        response: { status: 400, data: { code, msg: "expected client error" } },
      });
    }
    sdkLogger.error("unexpected server error", {
      response: { status: 500, data: { code: 99991400, msg: "internal error" } },
    });

    expect(debugCalls).toHaveLength(4);
    expect(errorCalls).toHaveLength(1);
  });
});

describe("DiscordChannelAdapter processing status feedback", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function makeDiscordMockRest(
    handler?: (method: string, path: string) => Response | Promise<Response>,
  ) {
    const calls: Array<{ method: string; path: string }> = [];
    const rest = new RequestClient("test-bot-token", {
      queueRequests: false,
      fetchImpl: async (url, init) => {
        const path = new URL(url).pathname.replace(/^\/api\/v10/, "");
        const method = (init.method ?? "GET").toUpperCase();
        calls.push({ method, path });
        if (handler) return handler(method, path);
        return jsonResponse(null, 204);
      },
    });
    return { rest, calls };
  }

  it("adds ⏳ + renews typing every 8s up to 120s in DMs, and removes ⏳ + stops typing on settle", async () => {
    vi.useFakeTimers();
    let nowMs = 1_000_000;
    const { rest, calls } = makeDiscordMockRest();
    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      rest,
      nowFn: () => nowMs,
    });

    const handle = adapter.beginProcessingFeedback({
      event: {
        platform: "discord",
        platformUserId: "u1",
        chatType: "dm",
        chatAddress: "discord:@me",
        chatId: "dm_ch_1",
        threadScopeKey: "dm:u1",
        messageId: "msg_dm_1",
        text: "Hello",
        rawPayload: { discord_message: { id: "msg_dm_1", channel_id: "dm_ch_1" } },
      },
    });

    await flushAsync();
    const encodedWait = encodeURIComponent(DISCORD_PROCESSING_EMOJI);
    expect(calls).toContainEqual({
      method: "PUT",
      path: `/channels/dm_ch_1/messages/msg_dm_1/reactions/${encodedWait}/@me`,
    });
    expect(calls.filter((c) => c.path === "/channels/dm_ch_1/typing")).toHaveLength(1);

    // Advance 24s -> 3 more typing renewals (total 4)
    for (let i = 0; i < 3; i += 1) {
      nowMs += DISCORD_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS);
      await flushAsync();
    }
    expect(calls.filter((c) => c.path === "/channels/dm_ch_1/typing")).toHaveLength(4);

    // Settle stops typing and deletes ⏳
    await handle!.settle("done");
    expect(calls).toContainEqual({
      method: "DELETE",
      path: `/channels/dm_ch_1/messages/msg_dm_1/reactions/${encodedWait}/@me`,
    });

    const typingBeforeAdvance = calls.filter((c) => c.path === "/channels/dm_ch_1/typing").length;
    nowMs += DISCORD_TYPING_INTERVAL_MS * 3;
    vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS * 3);
    await flushAsync();
    expect(calls.filter((c) => c.path === "/channels/dm_ch_1/typing")).toHaveLength(
      typingBeforeAdvance,
    );
  });

  it("caps Discord typing renewal at 120s in threads, and skips typing in public guild channels", async () => {
    vi.useFakeTimers();
    let nowMs = 5_000_000;
    const { rest, calls } = makeDiscordMockRest();
    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      rest,
      nowFn: () => nowMs,
    });

    // 1. Public guild channel -> reaction only, NO typing
    const pubHandle = adapter.beginProcessingFeedback({
      event: {
        platform: "discord",
        platformUserId: "u1",
        chatType: "group",
        chatAddress: "discord:g1/ch_pub",
        chatId: "g1/ch_pub",
        threadScopeKey: "chat:g1/ch_pub",
        messageId: "msg_pub_1",
        text: "Question",
        isGroupTrigger: true,
        rawPayload: {
          discord_message: { id: "msg_pub_1", channel_id: "ch_pub", guild_id: "g1" },
        },
      },
    });
    await flushAsync();
    expect(calls.filter((c) => c.path.endsWith("/typing"))).toHaveLength(0);
    await pubHandle!.settle("delivery_failed");
    expect(calls).toContainEqual({
      method: "PUT",
      path: `/channels/ch_pub/messages/msg_pub_1/reactions/${encodeURIComponent(DISCORD_FAILED_EMOJI)}/@me`,
    });

    // 2. Thread -> reaction + typing capped at 120s
    const threadHandle = adapter.beginProcessingFeedback({
      event: {
        platform: "discord",
        platformUserId: "u1",
        chatType: "group",
        chatAddress: "discord:g1/ch_pub",
        chatId: "g1/ch_pub",
        threadScopeKey: "chat:g1/ch_pub:thread:th_1",
        messageId: "msg_th_1",
        text: "Follow up in thread",
        isGroupTrigger: true,
        rawPayload: {
          discord_message: { id: "msg_th_1", channel_id: "th_1", guild_id: "g1" },
        },
      },
    });
    await flushAsync();

    // Advance 200 seconds in 8s steps
    for (let elapsed = 0; elapsed < 200_000; elapsed += DISCORD_TYPING_INTERVAL_MS) {
      nowMs += DISCORD_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS);
      await flushAsync();
    }
    const threadTypingCount = calls.filter(
      (c) => c.path === "/channels/th_1/typing",
    ).length;
    // t=0, 8, 16, ..., 112 -> 15 pulses (strictly < 120s)
    expect(threadTypingCount).toBe(
      Math.ceil(DISCORD_TYPING_MAX_DURATION_MS / DISCORD_TYPING_INTERVAL_MS),
    );
    await threadHandle!.settle("done");
  });

  it("silently abandons on Discord HTTP 404 (withdrawn message)", async () => {
    const { rest, calls } = makeDiscordMockRest(() =>
      jsonResponse({ message: "Unknown Message", code: 10008 }, 404),
    );
    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      rest,
    });

    const handle = adapter.beginProcessingFeedback({
      event: {
        platform: "discord",
        platformUserId: "u1",
        chatType: "group",
        chatAddress: "discord:g1/ch_pub",
        chatId: "g1/ch_pub",
        threadScopeKey: "chat:g1/ch_pub",
        messageId: "msg_gone",
        text: "Deleted right away",
        isGroupTrigger: true,
        rawPayload: {
          discord_message: { id: "msg_gone", channel_id: "ch_pub", guild_id: "g1" },
        },
      },
    });

    await handle!.settle("delivery_failed");
    // Only the initial PUT was attempted; no DELETE and no ⚠️ fallback
    expect(calls).toHaveLength(1);
    expect(calls[0]!.method).toBe("PUT");
  });

  it("delivers normal text replies immediately even when 4 background status requests are hanging", async () => {
    const statusGates = [
      defer<Response>(),
      defer<Response>(),
      defer<Response>(),
      defer<Response>(),
    ];
    let statusIdx = 0;
    const dispatchedPaths: string[] = [];

    const rest = new RequestClient("test-bot-token", {
      queueRequests: true,
      fetchImpl: async (url) => {
        const path = new URL(url).pathname.replace(/^\/api\/v10/, "");
        dispatchedPaths.push(path);
        if (path.includes("/reactions/") || path.endsWith("/typing")) {
          const gate = statusGates[statusIdx++] ?? defer<Response>();
          return gate.promise;
        }
        return jsonResponse({ id: "reply_msg_1" }, 200);
      },
    });

    const p1 = rest.put(
      `/channels/c1/messages/m1/reactions/${encodeURIComponent(DISCORD_PROCESSING_EMOJI)}/@me`,
      undefined,
      { priority: "background" },
    );
    const p2 = rest.post("/channels/c2/typing", undefined, {
      priority: "background",
    });
    const p3 = rest.put(
      `/channels/c3/messages/m3/reactions/${encodeURIComponent(DISCORD_PROCESSING_EMOJI)}/@me`,
      undefined,
      { priority: "background" },
    );
    const p4 = rest.post("/channels/c4/typing", undefined, {
      priority: "background",
    });

    await flushAsync(10);

    // Send an urgent standard text reply while all 4 background status requests are hanging
    const reply = await rest.post("/channels/c1/messages", {
      content: "Urgent reply text",
    });
    expect(reply).toEqual({ id: "reply_msg_1" });
    expect(dispatchedPaths).toContain("/channels/c1/messages");

    // Cleanly release the hanging background requests
    for (const g of statusGates) {
      g.resolve(jsonResponse(null, 204));
    }
    await Promise.all([p1, p2, p3, p4]);
  });

  it("does not block thread reply channel lookup (GET) or inbound thread resolution while a typing status request is hanging", async () => {
    const typingGate = defer<Response>();
    const typingStarted = defer<void>();
    const observedCalls: Array<{ method: string; path: string }> = [];

    const rest = new RequestClient("test-bot-token", {
      queueRequests: true,
      fetchImpl: async (url, init) => {
        const path = new URL(url).pathname.replace(/^\/api\/v10/, "");
        const method = (init.method ?? "GET").toUpperCase();
        observedCalls.push({ method, path });

        if (method === "POST" && path === "/channels/dm_slow/typing") {
          typingStarted.resolve();
          return typingGate.promise;
        }
        if (method === "GET" && path === "/users/@me") {
          return jsonResponse({ id: "bot_1" }, 200);
        }
        if (method === "GET" && path === "/channels/th_reply") {
          return jsonResponse(
            {
              id: "th_reply",
              type: 11,
              guild_id: "g1",
              parent_id: "ch_parent",
              thread_metadata: { archived: false },
            },
            200,
          );
        }
        if (method === "POST" && path === "/channels/th_reply/messages") {
          return jsonResponse({ id: "dc_thread_reply_1" }, 200);
        }
        if (method === "GET" && path === "/channels/th_inbound") {
          return jsonResponse(
            {
              id: "th_inbound",
              type: 11,
              guild_id: "g1",
              parent_id: "ch_parent",
              thread_metadata: { archived: false },
            },
            200,
          );
        }
        return jsonResponse(null, 204);
      },
    });

    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      rest,
    });

    const inboundReceived: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => {
      inboundReceived.push(ev);
    });

    // Hold the single background status slot with a hanging typing request
    const hangingTyping = rest.post("/channels/dm_slow/typing", undefined, {
      priority: "background",
    });
    await typingStarted.promise;

    // 1. Thread reply must run GET /channels/th_reply + POST /channels/th_reply/messages immediately
    const replyRes = await adapter.sendTextChunk({
      targetAddress: "discord:g1/ch_parent",
      text: "Thread reply while typing hangs",
      chunkIndex: 0,
      totalChunks: 1,
      threadScopeKey: "chat:g1/ch_parent:thread:th_reply",
    });
    expect(replyRes.messageId).toBe("dc_thread_reply_1");
    expect(observedCalls).toContainEqual({
      method: "GET",
      path: "/channels/th_reply",
    });
    expect(observedCalls).toContainEqual({
      method: "POST",
      path: "/channels/th_reply/messages",
    });

    // 2. Inbound message in an uncached thread must run GET /users/@me + GET /channels/th_inbound immediately
    await adapter.handleDispatch("MESSAGE_CREATE", {
      id: "msg_in_thread_1",
      channel_id: "th_inbound",
      guild_id: "g1",
      content: "<@bot_1> Check NVDA",
      author: { id: "user_dc_1", bot: false },
      mentions: [{ id: "bot_1" }],
    });
    expect(observedCalls).toContainEqual({
      method: "GET",
      path: "/users/@me",
    });
    expect(observedCalls).toContainEqual({
      method: "GET",
      path: "/channels/th_inbound",
    });
    expect(inboundReceived).toHaveLength(1);
    expect(inboundReceived[0]).toMatchObject({
      platform: "discord",
      messageId: "msg_in_thread_1",
      threadScopeKey: "chat:g1/ch_parent:thread:th_inbound",
    });

    // Cleanly release the hanging typing status request
    typingGate.resolve(jsonResponse(null, 204));
    await hangingTyping;
  });

  it("cancels queued Discord typing renewals on settle, on 404 withdrawal, and at the 120s ceiling", async () => {
    vi.useFakeTimers();
    let nowMs = 20_000_000;

    // 1. Settle cancels a queued-but-undispatched typing renewal
    {
      const blocker = defer<Response>();
      const typingFetches: string[] = [];
      let blockedOnce = false;
      const rest = new RequestClient("test-bot-token", {
        queueRequests: true,
        fetchImpl: async (url) => {
          const path = new URL(url).pathname.replace(/^\/api\/v10/, "");
          if (path.endsWith("/typing")) {
            typingFetches.push(path);
          }
          if (!blockedOnce) {
            blockedOnce = true;
            return blocker.promise;
          }
          return jsonResponse(null, 204);
        },
      });
      const adapter = new DiscordChannelAdapter({
        botToken: "test-bot-token",
        rest,
        nowFn: () => nowMs,
      });

      const handle = adapter.beginProcessingFeedback({
        event: {
          platform: "discord",
          platformUserId: "u_q1",
          chatType: "dm",
          chatAddress: "discord:@me",
          chatId: "dm_q1",
          threadScopeKey: "dm:u_q1",
          messageId: "msg_q1",
          text: "Hello",
          rawPayload: { discord_message: { id: "msg_q1", channel_id: "dm_q1" } },
        },
      });

      await flushAsync(10);
      // Initial typing holds the single background slot; the 8s renewal is queued behind it
      nowMs += DISCORD_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS);
      await flushAsync(10);

      // Settle aborts typingAbortController BEFORE the initial pulse finishes
      const settlePromise = handle!.settle("done");
      blocker.resolve(jsonResponse(null, 204));
      await settlePromise;
      await flushAsync(20);

      // Only the initial in-flight typing request reached fetchImpl; the queued 8s renewal was cancelled
      expect(typingFetches).toHaveLength(1);
    }

    // 2. HTTP 404 withdrawal cancels a queued typing renewal
    {
      const blocker = defer<Response>();
      const typingFetches: string[] = [];
      let blockedOnce = false;
      const rest = new RequestClient("test-bot-token", {
        queueRequests: true,
        fetchImpl: async (url) => {
          const path = new URL(url).pathname.replace(/^\/api\/v10/, "");
          if (path.endsWith("/typing")) {
            typingFetches.push(path);
          }
          if (!blockedOnce) {
            blockedOnce = true;
            return blocker.promise;
          }
          return jsonResponse(null, 204);
        },
      });
      const adapter = new DiscordChannelAdapter({
        botToken: "test-bot-token",
        rest,
        nowFn: () => nowMs,
      });

      const handle = adapter.beginProcessingFeedback({
        event: {
          platform: "discord",
          platformUserId: "u_q2",
          chatType: "dm",
          chatAddress: "discord:@me",
          chatId: "dm_q2",
          threadScopeKey: "dm:u_q2",
          messageId: "msg_q2",
          text: "Deleted",
          rawPayload: { discord_message: { id: "msg_q2", channel_id: "dm_q2" } },
        },
      });

      await flushAsync(10);
      // Enqueue a renewal while the initial typing pulse is still hanging
      nowMs += DISCORD_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS);
      await flushAsync(10);

      // Initial pulse returns 404 -> must abort the queued renewal before it dispatches
      blocker.resolve(jsonResponse({ message: "Unknown Message", code: 10008 }, 404));
      await flushAsync(20);

      nowMs += DISCORD_TYPING_INTERVAL_MS * 2;
      vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS * 2);
      await flushAsync(20);
      await handle!.settle("done");

      expect(typingFetches).toHaveLength(1);
    }

    // 3. 120s ceiling aborts any queued typing renewal
    {
      const blocker = defer<Response>();
      const typingFetches: string[] = [];
      let blockedOnce = false;
      const rest = new RequestClient("test-bot-token", {
        queueRequests: true,
        fetchImpl: async (url) => {
          const path = new URL(url).pathname.replace(/^\/api\/v10/, "");
          if (path.endsWith("/typing")) {
            typingFetches.push(path);
          }
          if (!blockedOnce) {
            blockedOnce = true;
            return blocker.promise;
          }
          return jsonResponse(null, 204);
        },
      });
      const adapter = new DiscordChannelAdapter({
        botToken: "test-bot-token",
        rest,
        nowFn: () => nowMs,
      });

      const handle = adapter.beginProcessingFeedback({
        event: {
          platform: "discord",
          platformUserId: "u_q3",
          chatType: "dm",
          chatAddress: "discord:@me",
          chatId: "dm_q3",
          threadScopeKey: "dm:u_q3",
          messageId: "msg_q3",
          text: "Slow network",
          rawPayload: { discord_message: { id: "msg_q3", channel_id: "dm_q3" } },
        },
      });

      await flushAsync(10);
      // Enqueue a renewal, then advance past 120s ceiling while the initial pulse still blocks the worker
      nowMs += DISCORD_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(DISCORD_TYPING_INTERVAL_MS);
      await flushAsync(10);

      nowMs += DISCORD_TYPING_MAX_DURATION_MS;
      vi.advanceTimersByTime(DISCORD_TYPING_MAX_DURATION_MS);
      await flushAsync(10);

      blocker.resolve(jsonResponse(null, 204));
      await flushAsync(20);
      await handle!.settle("done");

      expect(typingFetches).toHaveLength(1);
    }
  });

  it("normalizes reaction route keys so RestScheduler route bucket maps do not grow with message count", async () => {
    const rest = new RequestClient("test-bot-token", {
      queueRequests: true,
      fetchImpl: async () =>
        new Response(null, {
          status: 204,
          headers: {
            "x-ratelimit-bucket": "bucket_reactions_ch1",
            "x-ratelimit-remaining": "1",
            "x-ratelimit-reset-after": "0.25",
          },
        }),
    });

    const emojis = [DISCORD_PROCESSING_EMOJI, DISCORD_FAILED_EMOJI];
    for (let i = 1; i <= 8; i += 1) {
      const emoji = encodeURIComponent(emojis[i % emojis.length]!);
      await rest.put(`/channels/ch_norm/messages/msg_${i}/reactions/${emoji}/@me`, undefined, {
        priority: "background",
      });
      await rest.delete(`/channels/ch_norm/messages/msg_${i}/reactions/${emoji}/@me`, {
        priority: "background",
      });
    }

    // All 16 requests across 8 distinct message IDs & 2 emojis normalize to at most 2 route keys (PUT + DELETE)
    expect(rest.scheduler.routeBucketCount).toBeLessThanOrEqual(2);
    expect(rest.scheduler.bucketCount).toBeLessThanOrEqual(2);
  });
});

describe("WeixinChannelAdapter processing status feedback", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fetches & caches typing_ticket via getconfig, renews sendtyping(status=1) every 10s up to 120s, and sends status=2 on settle", async () => {
    vi.useFakeTimers();
    let nowMs = 10_000_000;
    const requests: Array<{ endpoint: string; body: Record<string, unknown> }> = [];
    const adapter = new WeixinChannelAdapter({
      baseUrl: "https://ilinkai.weixin.qq.com",
      botToken: "wx_bot_tok",
      ilinkBotId: "wx_bot_1",
      botGeneration: 1,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      nowFn: () => nowMs,
      fetchImpl: async (input, init) => {
        const endpoint = new URL(String(input)).pathname.replace(/^\/+/, "");
        const body = init?.body
          ? (JSON.parse(String(init.body)) as Record<string, unknown>)
          : {};
        requests.push({ endpoint, body });
        if (endpoint === "ilink/bot/getconfig") {
          return jsonResponse({ ret: 0, typing_ticket: "ticket_u1" });
        }
        return jsonResponse({ ret: 0 });
      },
    });

    const ev: InboundChannelEvent = {
      platform: "weixin",
      platformUserId: "wx_user_1",
      chatType: "dm",
      chatAddress: "weixin:@me",
      chatId: "wx_user_1",
      threadScopeKey: "dm:wx_user_1",
      messageId: "wx_bot_1:101",
      text: "Check macro data",
      weixinContextToken: "ctx_tok_1",
    };

    const h1 = adapter.beginProcessingFeedback({ event: ev });
    await flushAsync(40);

    expect(requests.map((r) => r.endpoint)).toEqual([
      "ilink/bot/getconfig",
      "ilink/bot/sendtyping",
    ]);
    expect(requests[0]!.body).toMatchObject({
      ilink_user_id: "wx_user_1",
      context_token: "ctx_tok_1",
    });
    expect(requests[1]!.body).toMatchObject({
      ilink_user_id: "wx_user_1",
      typing_ticket: "ticket_u1",
      status: TypingStatus.TYPING,
    });

    // Advance 200s in 10s steps -> capped at 120s (12 total start/renew calls: t=0, 10, ..., 110)
    for (let elapsed = 0; elapsed < 200_000; elapsed += WEIXIN_TYPING_INTERVAL_MS) {
      nowMs += WEIXIN_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(WEIXIN_TYPING_INTERVAL_MS);
      await flushAsync(40);
    }
    const startCalls = requests.filter(
      (r) =>
        r.endpoint === "ilink/bot/sendtyping" &&
        r.body.status === TypingStatus.TYPING,
    );
    expect(startCalls).toHaveLength(
      Math.ceil(WEIXIN_TYPING_MAX_DURATION_MS / WEIXIN_TYPING_INTERVAL_MS),
    );

    await h1!.settle("done");
    const cancelCalls = requests.filter(
      (r) =>
        r.endpoint === "ilink/bot/sendtyping" &&
        r.body.status === TypingStatus.CANCEL,
    );
    expect(cancelCalls).toHaveLength(1);
    expect(cancelCalls[0]!.body).toMatchObject({
      ilink_user_id: "wx_user_1",
      typing_ticket: "ticket_u1",
      status: TypingStatus.CANCEL,
    });

    // Second message reuses cached typing_ticket without calling getconfig again
    const h2 = adapter.beginProcessingFeedback({
      event: { ...ev, messageId: "wx_bot_1:102" },
    });
    await flushAsync(40);
    await h2!.settle("done");

    const getConfigCalls = requests.filter(
      (r) => r.endpoint === "ilink/bot/getconfig",
    );
    expect(getConfigCalls).toHaveLength(1);
  });

  it("skips sendtyping cleanly when getconfig fails", async () => {
    const endpoints: string[] = [];
    const adapter = new WeixinChannelAdapter({
      baseUrl: "https://ilinkai.weixin.qq.com",
      botToken: "wx_bot_tok",
      ilinkBotId: "wx_bot_1",
      botGeneration: 1,
      allowedApiDomains: ["ilinkai.weixin.qq.com"],
      checkResolvedIps: false,
      fetchImpl: async (input) => {
        const endpoint = new URL(String(input)).pathname.replace(/^\/+/, "");
        endpoints.push(endpoint);
        return jsonResponse({ ret: -1, errmsg: "config unavailable" });
      },
    });

    const handle = adapter.beginProcessingFeedback({
      event: {
        platform: "weixin",
        platformUserId: "wx_user_2",
        chatType: "dm",
        chatAddress: "weixin:@me",
        chatId: "wx_user_2",
        threadScopeKey: "dm:wx_user_2",
        messageId: "wx_bot_1:201",
        text: "Hello",
        weixinContextToken: "ctx_tok_2",
      },
    });

    await handle!.settle("done");
    expect(endpoints).toEqual(["ilink/bot/getconfig"]);
  });

  it("waits for an in-flight typing renewal before sending status=2, and does not let an older message cancel a newer message's typing", async () => {
    vi.useFakeTimers();
    let nowMs = 30_000_000;

    // Part A: settle() awaits an in-flight status=1 renewal before sending status=2
    {
      const renewalGate = defer<Response>();
      const statusesSent: number[] = [];
      let typingCount = 0;

      const adapter = new WeixinChannelAdapter({
        baseUrl: "https://ilinkai.weixin.qq.com",
        botToken: "wx_bot_tok",
        ilinkBotId: "wx_bot_1",
        botGeneration: 1,
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        nowFn: () => nowMs,
        fetchImpl: async (input, init) => {
          const endpoint = new URL(String(input)).pathname.replace(/^\/+/, "");
          const body = init?.body
            ? (JSON.parse(String(init.body)) as Record<string, unknown>)
            : {};
          if (endpoint === "ilink/bot/getconfig") {
            return jsonResponse({ ret: 0, typing_ticket: "ticket_seq" });
          }
          if (endpoint === "ilink/bot/sendtyping") {
            const st = Number(body.status);
            statusesSent.push(st);
            if (st === TypingStatus.TYPING) {
              typingCount += 1;
              if (typingCount === 2) {
                return renewalGate.promise;
              }
            }
          }
          return jsonResponse({ ret: 0 });
        },
      });

      const handle = adapter.beginProcessingFeedback({
        event: {
          platform: "weixin",
          platformUserId: "wx_user_seq",
          chatType: "dm",
          chatAddress: "weixin:@me",
          chatId: "wx_user_seq",
          threadScopeKey: "dm:wx_user_seq",
          messageId: "wx_bot_1:301",
          text: "First",
          weixinContextToken: "ctx_seq",
        },
      });
      await flushAsync(40);
      expect(statusesSent).toEqual([TypingStatus.TYPING]);

      // Trigger 10s renewal which hangs on renewalGate
      nowMs += WEIXIN_TYPING_INTERVAL_MS;
      vi.advanceTimersByTime(WEIXIN_TYPING_INTERVAL_MS);
      await flushAsync(40);
      expect(statusesSent).toEqual([TypingStatus.TYPING, TypingStatus.TYPING]);

      // Settle while renewal is still in flight -> must NOT send CANCEL yet
      const settlePromise = handle!.settle("done");
      await flushAsync(40);
      expect(statusesSent).toEqual([TypingStatus.TYPING, TypingStatus.TYPING]);

      // Resolve in-flight renewal -> CANCEL is sent AFTER renewal completes
      renewalGate.resolve(jsonResponse({ ret: 0 }));
      await settlePromise;
      expect(statusesSent).toEqual([
        TypingStatus.TYPING,
        TypingStatus.TYPING,
        TypingStatus.CANCEL,
      ]);
    }

    // Part B: older message settling must NOT send status=2 after a newer message started typing for the same user
    {
      const statusesSent: number[] = [];
      const adapter = new WeixinChannelAdapter({
        baseUrl: "https://ilinkai.weixin.qq.com",
        botToken: "wx_bot_tok",
        ilinkBotId: "wx_bot_1",
        botGeneration: 1,
        allowedApiDomains: ["ilinkai.weixin.qq.com"],
        checkResolvedIps: false,
        nowFn: () => nowMs,
        fetchImpl: async (input, init) => {
          const endpoint = new URL(String(input)).pathname.replace(/^\/+/, "");
          const body = init?.body
            ? (JSON.parse(String(init.body)) as Record<string, unknown>)
            : {};
          if (endpoint === "ilink/bot/getconfig") {
            return jsonResponse({ ret: 0, typing_ticket: "ticket_overlap" });
          }
          if (endpoint === "ilink/bot/sendtyping") {
            statusesSent.push(Number(body.status));
          }
          return jsonResponse({ ret: 0 });
        },
      });

      const baseEv: InboundChannelEvent = {
        platform: "weixin",
        platformUserId: "wx_user_overlap",
        chatType: "dm",
        chatAddress: "weixin:@me",
        chatId: "wx_user_overlap",
        threadScopeKey: "dm:wx_user_overlap",
        messageId: "wx_bot_1:401",
        text: "Msg 1",
        weixinContextToken: "ctx_ov_1",
      };

      const h1 = adapter.beginProcessingFeedback({ event: baseEv });
      await flushAsync(40);

      const h2 = adapter.beginProcessingFeedback({
        event: { ...baseEv, messageId: "wx_bot_1:402", text: "Msg 2" },
      });
      await flushAsync(40);

      // Settling older h1 must NOT emit status=2 because h2 owns typing for wx_user_overlap
      await h1!.settle("done");
      expect(statusesSent).toEqual([TypingStatus.TYPING, TypingStatus.TYPING]);

      // Settling newer h2 emits status=2 once
      await h2!.settle("done");
      expect(statusesSent).toEqual([
        TypingStatus.TYPING,
        TypingStatus.TYPING,
        TypingStatus.CANCEL,
      ]);
    }
  });

  it("isolates bot_token, base_url, and typing_ticket cache across multiple Weixin accounts (#499)", async () => {
    const calls: Array<{
      host: string;
      endpoint: string;
      auth: string;
      body: Record<string, unknown>;
    }> = [];
    const backend = createMockBackend();
    vi.mocked(backend.reconcileWeixinAccounts).mockResolvedValue({
      changed: true,
      enabled: true,
      max_concurrent_accounts: 50,
      accounts_revision: 1,
      accounts: [
        {
          account_id: "acc_A",
          user_id: "user_A",
          ilink_bot_id: "bot_A",
          ilink_user_id: "wx_user_A",
          bot_token: "token_A",
          base_url: "https://ilinkai.weixin.qq.com",
          cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
          get_updates_buf: "",
          status: "active",
          bot_generation: 1,
        },
        {
          account_id: "acc_B",
          user_id: "user_B",
          ilink_bot_id: "bot_B",
          ilink_user_id: "wx_user_B",
          bot_token: "token_B",
          base_url: "https://sz.ilinkai.weixin.qq.com",
          cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c",
          get_updates_buf: "",
          status: "active",
          bot_generation: 2,
        },
      ],
    });

    const adapter = new WeixinChannelAdapter({
      backend,
      allowedApiDomains: ["ilinkai.weixin.qq.com", "weixin.qq.com"],
      checkResolvedIps: false,
      staggerDelayMs: 0,
      staggerJitterMs: 0,
      fetchImpl: async (input, init) => {
        const parsed = new URL(String(input));
        const endpoint = parsed.pathname.replace(/^\/+/, "");
        const headers = (init?.headers ?? {}) as Record<string, string>;
        const auth = headers.Authorization ?? headers.authorization ?? "";
        const body = init?.body
          ? (JSON.parse(String(init.body)) as Record<string, unknown>)
          : {};
        if (endpoint === "ilink/bot/getupdates") {
          return jsonResponse({ ret: 0, msgs: [], get_updates_buf: "" });
        }
        calls.push({ host: parsed.host, endpoint, auth, body });
        if (endpoint === "ilink/bot/getconfig") {
          return jsonResponse({
            ret: 0,
            typing_ticket: auth.includes("token_A") ? "ticket_A" : "ticket_B",
          });
        }
        return jsonResponse({ ret: 0 });
      },
    });
    adapter.onInboundEvent(async () => undefined);
    await adapter.start();

    try {
      const hA = adapter.beginProcessingFeedback({
        event: {
          platform: "weixin",
          platformUserId: "wx_user_A",
          chatType: "dm",
          chatAddress: "weixin:@me",
          chatId: "wx_user_A",
          threadScopeKey: "dm:wx_user_A",
          messageId: "bot_A:1",
          text: "Hi from A",
          weixinAccountId: "acc_A",
          weixinBotId: "bot_A",
          weixinBotGeneration: 1,
          weixinContextToken: "ctx_A",
        },
      });
      const hB = adapter.beginProcessingFeedback({
        event: {
          platform: "weixin",
          platformUserId: "wx_user_B",
          chatType: "dm",
          chatAddress: "weixin:@me",
          chatId: "wx_user_B",
          threadScopeKey: "dm:wx_user_B",
          messageId: "bot_B:1",
          text: "Hi from B",
          weixinAccountId: "acc_B",
          weixinBotId: "bot_B",
          weixinBotGeneration: 2,
          weixinContextToken: "ctx_B",
        },
      });
      await flushAsync(40);
      await hA!.settle("done");
      await hB!.settle("done");

      const callsA = calls.filter((c) => c.auth === "Bearer token_A");
      const callsB = calls.filter((c) => c.auth === "Bearer token_B");
      expect(callsA.map((c) => c.endpoint)).toEqual([
        "ilink/bot/getconfig",
        "ilink/bot/sendtyping",
        "ilink/bot/sendtyping",
      ]);
      expect(callsA.every((c) => c.host === "ilinkai.weixin.qq.com")).toBe(true);
      expect(callsA[1]!.body).toMatchObject({
        ilink_user_id: "wx_user_A",
        typing_ticket: "ticket_A",
        status: TypingStatus.TYPING,
      });
      expect(callsA[2]!.body).toMatchObject({
        ilink_user_id: "wx_user_A",
        typing_ticket: "ticket_A",
        status: TypingStatus.CANCEL,
      });

      expect(callsB.map((c) => c.endpoint)).toEqual([
        "ilink/bot/getconfig",
        "ilink/bot/sendtyping",
        "ilink/bot/sendtyping",
      ]);
      expect(callsB.every((c) => c.host === "sz.ilinkai.weixin.qq.com")).toBe(true);
      expect(callsB[1]!.body).toMatchObject({
        ilink_user_id: "wx_user_B",
        typing_ticket: "ticket_B",
        status: TypingStatus.TYPING,
      });
      expect(callsB[2]!.body).toMatchObject({
        ilink_user_id: "wx_user_B",
        typing_ticket: "ticket_B",
        status: TypingStatus.CANCEL,
      });
    } finally {
      await adapter.stop();
    }
  });
});
