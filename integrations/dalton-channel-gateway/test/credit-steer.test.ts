import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I1RegisterAndResolveResponse,
  I7RecoverResponse,
  I7SteersReconcileResponse,
} from "../src/backend-client.js";
import {
  makeCreditSupplementSteerKey,
  makeInboundRequestKey,
  makeReturnedSteerRequestKey,
} from "../src/uuid.js";
import {
  createTestHarness,
  loadFixtureKey,
  makeCompletedTurnSseEvents,
  makeSseEventsFromFixture,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
  WORKSPACE_ID,
} from "./helpers.js";

describe("`credit_pause` Resume & Supplementary Steer State Machine (Codex r2 #5, Grok r2 #6)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("executes 3-step `credit_pause` supplementary steer: ① HITL resume via `/messages` -> ② `steer_only: true` on new `run_id` -> ③ reconcile & resubmit if unconsumed", async () => {
    const inboundKey = makeInboundRequestKey("feishu", "m_credit_supp");
    const newResumedRunId = "bbbbbbbb-1111-2222-3333-444444444444";
    const expectedSteerKey = makeCreditSupplementSteerKey(
      "feishu",
      "m_credit_supp",
      newResumedRunId,
    );
    const expectedReturnedKey = makeReturnedSteerRequestKey(
      "feishu",
      "m_credit_supp",
      newResumedRunId,
    );

    const baseI1 = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "default_resolved",
    );
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const reconcilePendingResubmit = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_pending_resubmit",
    );
    const approveShape = loadFixtureKey<
      Record<string, { decisions: Array<{ type: string; message: string }> }>
    >("hitl_interrupts_and_responses.json", "hitl_response_approve");
    const expectedCreditHitl = {
      int_credit_1: {
        decisions: [
          {
            type: approveShape.int_ask_1!.decisions[0]!.type,
            message: "继续，顺便看下 Q4 毛利率",
          },
        ],
      },
    };

    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...baseI1,
      request_key: inboundKey,
      run_id: RUN_ID,
      turn_index: 1,
      pending_interrupt: {
        interrupt_id: "int_credit_1",
        kind: "credit_pause",
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        turn_index: 1,
      },
    });

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: newResumedRunId,
          turnIndex: 1,
          content: "Resumed main turn output.",
        }),
      })
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
          runId: "run_supp_turn_2",
          turnIndex: 2,
          content: "Supplementary Q4 margin analysis complete.",
        }),
      });

    vi.mocked(h.backend.reconcileSteer).mockResolvedValueOnce({
      ...reconcilePendingResubmit,
      resubmitted_request_key: expectedReturnedKey,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_credit_supp",
      text: "继续，顺便看下 Q4 毛利率",
    });

    expect(h.backend.retryCalls).toHaveLength(0);
    expect(h.backend.appendCalls).toHaveLength(3);

    // Step ①: HITL resume via `/messages` (`messages: []` + `hitl_response`)
    expect(h.backend.appendCalls[0]?.payload).toMatchObject({
      messages: [],
      request_key: inboundKey,
      platform: "feishu",
      steer_only: false,
      hitl_response: expectedCreditHitl,
    });

    // Step ②: Supplementary steer sent to `newResumedRunId` with `steer_only: true`
    expect(h.backend.appendCalls[1]?.payload).toMatchObject({
      messages: [{ role: "user", content: "顺便看下 Q4 毛利率" }],
      request_key: expectedSteerKey,
      platform: "feishu",
      steer_only: true,
    });

    // Step ③: Reconciled `pending_resubmit` -> dispatched as new turn with `expectedReturnedKey`
    expect(h.backend.appendCalls[2]?.payload).toMatchObject({
      messages: [{ role: "user", content: "顺便看下 Q4 毛利率" }],
      request_key: expectedReturnedKey,
      platform: "feishu",
    });

    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.raw_payload_patch?.credit_steer_request_key === expectedSteerKey &&
          u.raw_payload_patch?.credit_steer_state === "pending",
      ),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) => u.raw_payload_patch?.credit_turn_text_settled === true,
      ),
    ).toBe(true);

    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toEqual([
      "Resumed main turn output.",
      "Supplementary Q4 margin analysis complete.",
    ]);
  });

  it("crash recovery handles both `credit_turn_text_settled: true` and `credit_resume_dispatched: true` without prematurely completing the inbound row (Codex r2 #5, Grok r2 #6)", async () => {
    const newResumedRunId = "bbbbbbbb-1111-2222-3333-444444444444";
    const steerKey = makeCreditSupplementSteerKey(
      "feishu",
      "m_rec_credit_settled",
      newResumedRunId,
    );
    const returnedKey = makeReturnedSteerRequestKey(
      "feishu",
      "m_rec_credit_settled",
      newResumedRunId,
    );
    const reconcilePendingResubmit = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_pending_resubmit",
    );

    const recoverPayload: I7RecoverResponse = {
      mode: "startup",
      inbound_events: [
        {
          platform: "feishu",
          message_id: "m_rec_credit_settled",
          dalton_user_id: USER_ID,
          chat_type: "dm",
          chat_address: "feishu:@me",
          thread_scope_key: "dm:ou_alice",
          request_key: makeInboundRequestKey("feishu", "m_rec_credit_settled"),
          retry_request_key: null,
          resubmitted_request_key: null,
          steered_into_run_id: newResumedRunId,
          thread_id: THREAD_ID,
          run_id: newResumedRunId,
          turn_index: 1,
          workspace_id: WORKSPACE_ID,
          external_thread_id: "ext_1",
          status: "dispatched",
          fallback_pending: true,
          sent_to_current_count: 1,
          uploaded_attachments: [],
          raw_payload: {
            platform_user_id: "ou_alice",
            text: "继续，补充分析现金流",
            credit_steer_queued: true,
            credit_steer_request_key: steerKey,
            credit_steer_run_id: newResumedRunId,
            credit_steer_state: "pending",
            credit_turn_text_settled: true,
            credit_supplement_text: "补充分析现金流",
          },
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    };

    vi.mocked(h.backend.recoverState).mockResolvedValueOnce(recoverPayload);

    vi.mocked(h.backend.reconcileSteer).mockResolvedValueOnce({
      ...reconcilePendingResubmit,
      resubmitted_request_key: returnedKey,
    });

    await h.app.orchestrator.runStartupRecovery();

    // Did not call replayThreadMessages for the already-settled main turn; directly reconciled & resubmitted the supplementary steer
    expect(h.backend.replayThreadMessages).not.toHaveBeenCalled();
    expect(h.backend.appendCalls).toHaveLength(1);
    expect(h.backend.appendCalls[0]?.payload).toMatchObject({
      messages: [{ role: "user", content: "补充分析现金流" }],
      request_key: returnedKey,
    });

    // When `credit_resume_dispatched: true` (turn text not yet settled) and both `/messages/stream` and `/messages/replay` fail,
    // recovery must NOT set `credit_turn_text_settled: true` or mark the inbound row `completed` (Codex r3 #9)
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 500,
      isSse: false,
      errorBody: { detail: "Stream unavailable" },
    });
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 500,
      isSse: false,
      errorBody: { detail: "Replay unavailable" },
    });

    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: [
        {
          platform: "feishu",
          message_id: "m_rec_credit_unsettled_fail",
          dalton_user_id: USER_ID,
          chat_type: "dm",
          chat_address: "feishu:@me",
          thread_scope_key: "dm:ou_alice",
          request_key: makeInboundRequestKey(
            "feishu",
            "m_rec_credit_unsettled_fail",
          ),
          retry_request_key: null,
          resubmitted_request_key: null,
          steered_into_run_id: null,
          thread_id: THREAD_ID,
          run_id: newResumedRunId,
          turn_index: 1,
          workspace_id: WORKSPACE_ID,
          external_thread_id: "ext_1",
          status: "dispatched",
          fallback_pending: true,
          sent_to_current_count: 0,
          uploaded_attachments: [],
          raw_payload: {
            platform_user_id: "ou_alice",
            text: "继续，补充分析估值",
            credit_steer_queued: true,
            credit_resume_dispatched: true,
            credit_turn_text_settled: false,
            credit_supplement_text: "补充分析估值",
          },
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });

    await h.app.orchestrator.runStartupRecovery();

    const unsettledUpdates = h.backend.progressUpdates.filter(
      (u) => u.message_id === "m_rec_credit_unsettled_fail",
    );
    expect(
      unsettledUpdates.some(
        (u) => u.raw_payload_patch?.credit_turn_text_settled === true,
      ),
    ).toBe(false);
    expect(unsettledUpdates.some((u) => u.status === "completed")).toBe(false);
  });
});
