import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I1RegisterAndResolveResponse,
  I7SteersReconcileResponse,
  ThreadStatusInfo,
} from "../src/backend-client.js";
import {
  makeInboundRequestKey,
  makeReturnedSteerRequestKey,
  makeRetryRequestKey,
} from "../src/uuid.js";
import {
  createTestHarness,
  loadFixtureKey,
  makeCompletedTurnSseEvents,
  makeSseEvents,
  makeSseEventsFromFixture,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

describe("HITL Interrupt Parsing (`action_requests`), Answering via `/messages` & Host Interrupt Steer (§1.3.1, §1.3.2, Codex r2 #2/#3/#4/#12, Grok r2 #3/#7)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("parses flat `AskUserQuestion` `action_requests` (`question`, `options: string[]`) and answers or skips via POST /threads/{id}/messages (Codex r2 #2/#3, Grok r2 #3)", async () => {
    const askUserSse = loadFixtureKey<{ event: string; data: unknown }>(
      "hitl_interrupts_and_responses.json",
      "ask_user_question_sse",
    );
    const expectedAnsweredHitl = loadFixtureKey<Record<string, unknown>>(
      "hitl_interrupts_and_responses.json",
      "hitl_response_approve",
    );
    const expectedSkippedHitl = loadFixtureKey<Record<string, unknown>>(
      "hitl_interrupts_and_responses.json",
      "hitl_response_skip_reject",
    );
    const baseI1 = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "default_resolved",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );

    // 1. Emit `event: interrupt` with real flat `AskUserQuestion` `action_requests` from `ask_user.py:56-66`
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        {
          event: "metadata",
          data: {
            thread_id: THREAD_ID,
            run_id: RUN_ID,
            turn_index: 1,
          },
        },
        askUserSse,
      ]),
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_trigger_ask",
      text: "Analyze AAPL valuation",
    });

    const savedInterrupt = h.backend.progressUpdates.find(
      (u) => u.session_interrupt_update?.pending_interrupt != null,
    )?.session_interrupt_update?.pending_interrupt;

    expect(savedInterrupt).toMatchObject({
      interrupt_id: "int_ask_1",
      kind: "ask_user_question",
      category: "ask_user_question",
      thread_id: THREAD_ID,
      run_id: RUN_ID,
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("1. 1 Year");
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain("2. 5 Years");

    // 2. User replies with option number "2" -> resolved to "5 Years" and sent via `POST /threads/{id}/messages` (`messages: []` + `hitl_response`)
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: makeInboundRequestKey("feishu", "m_answer_ask"),
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: savedInterrupt ?? null,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_answer_ask",
      text: "2",
    });

    expect(h.backend.retryCalls).toHaveLength(0);
    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[1]?.payload).toMatchObject({
      messages: [],
      platform: "feishu",
      steer_only: false,
      hitl_response: expectedAnsweredHitl,
    });

    // 3. User replies `"skip"` -> sends `{ type: "reject", message: "" }` (`ask_user.py:78-80`, Codex r2 #3)
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: makeInboundRequestKey("feishu", "m_skip_ask"),
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: savedInterrupt ?? null,
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_skip_ask",
      text: "skip",
    });

    expect(h.backend.appendCalls[2]?.payload).toMatchObject({
      messages: [],
      platform: "feishu",
      steer_only: false,
      hitl_response: expectedSkippedHitl,
    });
  });

  it("clears stale `pending_interrupt` when GET /status shows the interrupt was already resolved on Web, and preserves `pending_interrupt` across `writer_capacity` SSE error (Codex r2 #4, Grok r2 #7)", async () => {
    const baseI1 = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "default_resolved",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );
    const statusIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const sseErrorWriterCap = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_error_writer_capacity",
    );

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: makeInboundRequestKey("feishu", "m_stale_int"),
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: {
        interrupt_id: "int_already_done",
        kind: "ask_user_question",
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        turn_index: 1,
      },
    });

    // Web UI already answered the interrupt, so live status is `"idle"`
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusIdle,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_stale_int",
      text: "New unrelated question",
    });

    // Cleared pending_interrupt via I-7 and sent normal user message
    expect(
      h.backend.progressUpdates.some(
        (u) => u.session_interrupt_update?.pending_interrupt === null,
      ),
    ).toBe(true);
    expect(h.backend.appendCalls[0]?.payload.messages).toEqual([
      { role: "user", content: "New unrelated question" },
    ]);
    expect(h.backend.appendCalls[0]?.payload.hitl_response).toBeUndefined();

    // Now test `writer_capacity` SSE error on HITL resume: must NOT clear `pending_interrupt` before stream acceptance!
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: makeInboundRequestKey("feishu", "m_hitl_writer_cap"),
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: {
        interrupt_id: "int_ask_cap",
        kind: "ask_user_question",
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        turn_index: 1,
      },
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });
    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([sseErrorWriterCap]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: RUN_ID,
          turnIndex: 1,
          content: "Resumed after writer_capacity retry!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_hitl_writer_cap",
      text: "My answer",
    });

    // Both initial attempt and retry after `writer_capacity` sent `hitl_response`
    expect(
      h.backend.appendCalls.at(-1)?.payload.hitl_response?.int_ask_cap,
    ).toBeDefined();

    // Bare EOF after only keepalive comment (when replay is also unavailable) must NOT clear `pending_interrupt` (Codex r3 #1)
    const progressCountBeforeBareEof = h.backend.progressUpdates.length;
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: makeInboundRequestKey("feishu", "m_hitl_bare_eof"),
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: {
        interrupt_id: "int_ask_eof",
        kind: "ask_user_question",
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        turn_index: 1,
      },
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([{ event: ":comment", data: "keepalive" }]),
    });
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 500,
      isSse: false,
      errorBody: { detail: "Replay unavailable" },
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_hitl_bare_eof",
      text: "Answer lost on bare EOF",
    });

    const updatesDuringBareEof = h.backend.progressUpdates.slice(
      progressCountBeforeBareEof,
    );
    expect(
      updatesDuringBareEof.some(
        (u) => u.session_interrupt_update?.pending_interrupt === null,
      ),
    ).toBe(false);
  });

  it("handles `web_only_approval` by directing user to `/chat/t/{thread_id}` without approving from chat (Codex r2 #12)", async () => {
    const baseI1 = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "default_resolved",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: makeInboundRequestKey("feishu", "m_web_only"),
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: {
        interrupt_id: "int_plan_1",
        kind: "web_only_approval",
        action_type: "SubmitOrder",
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        turn_index: 1,
      },
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_web_only",
      text: "approve",
    });

    expect(h.backend.appendCalls).toHaveLength(0);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      `https://app.ginlix.ai/chat/t/${THREAD_ID}`,
    );
  });

  it("does NOT resubmit when `I-7 steer/reconcile` returns `still_pending` (e.g. `taken` on `interrupted` host run), and ONLY resubmits with `:returned:{run_id}` on `pending_resubmit` (Codex r3 #4, Grok r3 #1)", async () => {
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const creditPauseSse = loadFixtureKey<{ event: string; data: unknown }>(
      "hitl_interrupts_and_responses.json",
      "credit_pause_sse",
    );
    const reconcileStillPending = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_still_pending_interrupted",
    );
    const reconcilePendingResubmit = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_pending_resubmit",
    );

    // 1. When host turn enters `interrupted` with `outcome: "taken"`, `reconcileSteer` returns `still_pending` -> gateway must NOT mint `:returned:{run_id}` or resubmit
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([steeringAccepted]),
    });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([creditPauseSse]),
    });
    vi.mocked(h.backend.reconcileSteer).mockResolvedValueOnce(
      reconcileStillPending,
    );

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_steer_taken_still_pending",
      text: "Steer already taken before interrupt",
    });

    // Only the initial dispatch happened; no `:returned:{run_id}` resubmit was issued
    expect(h.backend.appendCalls).toHaveLength(1);

    // 2. When host turn enters `interrupted` with unconsumed `outcome: "pending"`, `reconcileSteer` returns `pending_resubmit` -> gateway resubmits with `:returned:{run_id}`
    const returnedKey = makeReturnedSteerRequestKey(
      "feishu",
      "m_steer_interrupted",
      RUN_ID,
    );
    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([steeringAccepted]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_resubmitted_after_int",
          turnIndex: 2,
          content: "Resubmitted after host interrupted!",
        }),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([creditPauseSse]),
    });
    vi.mocked(h.backend.reconcileSteer).mockResolvedValueOnce({
      ...reconcilePendingResubmit,
      resubmitted_request_key: returnedKey,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_steer_interrupted",
      text: "Steer unconsumed when host pauses",
    });

    expect(h.backend.appendCalls).toHaveLength(3);
    expect(h.backend.appendCalls[2]?.payload.request_key).toBe(returnedKey);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Resubmitted after host interrupted!",
    );
  });

  it("mints a new `:retry:{failed_run_id}` key and re-sends `POST /threads/{id}/messages` with `hitl_response` when HITL resume gets `event: retry` (`recovery: \"resend\"`) after run creation or hits `409 duplicate_request` (`run_status: \"error\"`) / `resend_required` (Codex r4 #2)", async () => {
    const baseI1 = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "default_resolved",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );
    const sseRetryResend = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_retry_resend",
    );
    const http409DupErrorRetryable = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_duplicate_error_retryable",
    );
    const http409ResendRequired = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_resend_required",
    );
    const expectedAnsweredHitl = loadFixtureKey<Record<string, unknown>>(
      "hitl_interrupts_and_responses.json",
      "hitl_response_approve",
    );

    const pendingAskInterrupt = {
      interrupt_id: "int_ask_1",
      kind: "ask_user_question" as const,
      category: "ask_user_question" as const,
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      turn_index: 1,
      questions: [
        {
          id: "q_horizon",
          question: "Which valuation horizon should we use?",
          options: [{ label: "1 Year" }, { label: "5 Years" }],
        },
      ],
    };

    // 1. HITL resume stream creates run `RUN_ID` (`metadata` + `retry` with `recovery: "resend"` and `run_id: RUN_ID`):
    // must NOT reuse the original `request_key` (which would hit `409 duplicate_request`); must mint `:retry:{RUN_ID}`,
    // persist `retry_request_key` via I-7, and re-send `/messages` with `hitl_response`!
    const origKey1 = makeInboundRequestKey("feishu", "m_hitl_resend_run");
    const expectedNewKey1 = makeRetryRequestKey(
      "feishu",
      "m_hitl_resend_run",
      RUN_ID,
    );

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: origKey1,
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: pendingAskInterrupt,
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });
    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([
          {
            event: "metadata",
            data: { thread_id: THREAD_ID, run_id: RUN_ID, turn_index: 1 },
          },
          sseRetryResend,
        ]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_hitl_resent_ok",
          turnIndex: 1,
          content: "HITL answer executed with new key!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_hitl_resend_run",
      text: "2",
    });

    expect(h.backend.retryCalls).toHaveLength(0);
    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[0]?.payload.request_key).toBe(origKey1);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(expectedNewKey1);
    expect(h.backend.appendCalls[1]?.payload.hitl_response).toEqual(
      expectedAnsweredHitl,
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_hitl_resend_run" &&
          u.retry_request_key === expectedNewKey1,
      ),
    ).toBe(true);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "HITL answer executed with new key!",
    );

    // 2. HITL resume hits `409 duplicate_request` (`run_status: "error", retryable: true`):
    // gateway first calls `POST /threads/{id}/retry`; when the answer had not entered the graph (`pending_input.py:300-301`),
    // `/retry` returns `409 resend_required` (`http_409_resend_required`), and gateway then re-sends `POST /threads/{id}/messages`
    // with `:retry:{failed_run_id}` and `hitl_response`!
    const origKey2 = makeInboundRequestKey("feishu", "m_hitl_dup_error");
    const expectedNewKey2 = makeRetryRequestKey(
      "feishu",
      "m_hitl_dup_error",
      RUN_ID,
    );

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: origKey2,
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: pendingAskInterrupt,
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });
    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409DupErrorRetryable,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_hitl_after_409",
          turnIndex: 1,
          content: "HITL answer recovered after 409 duplicate error!",
        }),
      });
    vi.mocked(h.backend.retryThreadTurn).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: http409ResendRequired,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_hitl_dup_error",
      text: "2",
    });

    expect(h.backend.retryCalls).toHaveLength(1);
    expect(h.backend.retryCalls[0]?.payload.request_key).toBe(expectedNewKey2);
    expect(h.backend.appendCalls.at(-1)?.payload.request_key).toBe(
      expectedNewKey2,
    );
    expect(h.backend.appendCalls.at(-1)?.payload.hitl_response).toEqual(
      expectedAnsweredHitl,
    );
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "HITL answer recovered after 409 duplicate error!",
    );
  });

  it("calls `POST /threads/{id}/retry` first on `409 duplicate_request` (`run_status: \"error\", retryable: true`) for multimodal turns so in-graph turns resume from checkpoint without duplicate `/messages` submission, and only falls back to `/messages` on `409 resend_required` (Codex r5 #2)", async () => {
    const baseI1 = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "default_resolved",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );
    const http409DupErrorRetryable = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_duplicate_error_retryable",
    );
    const http409ResendRequired = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_resend_required",
    );

    h.feishuAdapter.setAttachment("att_chart_1", {
      content: Buffer.from([137, 80, 78, 71]),
      filename: "chart.png",
      mimeType: "image/png",
    });

    // 1. Multimodal turn that ALREADY entered the graph and failed mid-execution (`409 duplicate_request, run_status: "error", retryable: true`):
    // calls `POST /threads/{id}/retry` (`messaging.py:1755-1775`), which succeeds and resumes from checkpoint WITHOUT re-submitting `POST /threads/{id}/messages`!
    const origKeyInGraph = makeInboundRequestKey(
      "feishu",
      "m_multimodal_in_graph",
    );
    const expectedRetryKeyInGraph = makeRetryRequestKey(
      "feishu",
      "m_multimodal_in_graph",
      RUN_ID,
    );

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: origKeyInGraph,
      run_id: RUN_ID,
      turn_index: 1,
    });
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: http409DupErrorRetryable,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_multimodal_in_graph",
      text: "Analyze this chart",
      attachments: [
        {
          id: "att_chart_1",
          filename: "chart.png",
          mimeType: "image/png",
        },
      ],
    });

    expect(h.backend.appendCalls).toHaveLength(1);
    expect(h.backend.retryCalls).toHaveLength(1);
    expect(h.backend.retryCalls[0]?.payload).toMatchObject({
      run_id: RUN_ID,
      request_key: expectedRetryKeyInGraph,
      platform: "feishu",
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe("Retried failed turn!");

    // 2. Multimodal turn that failed BEFORE input reached the graph: `/retry` returns `409 resend_required` (`pending_input.py:300-301`),
    // and gateway then re-sends `POST /threads/{id}/messages` with `:retry:{failed_run_id}` and the preserved multimodal content!
    const origKeyPreGraph = makeInboundRequestKey(
      "feishu",
      "m_multimodal_pre_graph",
    );
    const expectedRetryKeyPreGraph = makeRetryRequestKey(
      "feishu",
      "m_multimodal_pre_graph",
      RUN_ID,
    );

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: origKeyPreGraph,
      run_id: RUN_ID,
      turn_index: 1,
    });
    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409DupErrorRetryable,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_multimodal_resent",
          turnIndex: 1,
          content: "Multimodal turn re-sent after 409 resend_required!",
        }),
      });
    vi.mocked(h.backend.retryThreadTurn).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: http409ResendRequired,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_multimodal_pre_graph",
      text: "Analyze this chart pre-graph",
      attachments: [
        {
          id: "att_chart_1",
          filename: "chart.png",
          mimeType: "image/png",
        },
      ],
    });

    expect(h.backend.retryCalls).toHaveLength(2);
    expect(h.backend.appendCalls).toHaveLength(3);
    expect(h.backend.appendCalls[2]?.payload.request_key).toBe(
      expectedRetryKeyPreGraph,
    );
    expect(Array.isArray(h.backend.appendCalls[2]?.payload.messages[0]?.content)).toBe(
      true,
    );
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Multimodal turn re-sent after 409 resend_required!",
    );
  });
});
