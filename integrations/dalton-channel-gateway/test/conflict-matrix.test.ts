import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DispatchLivenessRunInfo,
  I1RegisterAndResolveResponse,
  I7SteersReconcileResponse,
  ThreadStatusInfo,
} from "../src/backend-client.js";
import {
  makeInboundRequestKey,
  makeReturnedSteerRequestKey,
  makeRetryRequestKey,
  makeTurnAfterSteerRequestKey,
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
  WORKSPACE_ID,
} from "./helpers.js";

describe("Conflict Matrix (Types A, B, C, D), Steer Reconciliation & SSE Stream Lifecycle (§1.3.1, §1.3.2, Codex r2 #6-#11)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("Type B `running`: keeps the SAME request_key when switching to `steer_only: true`, then resubmits with `:returned:{run_id}` on `pending_resubmit`", async () => {
    const inboundKey = makeInboundRequestKey("feishu", "m_steer_1");
    const returnedKey = makeReturnedSteerRequestKey(
      "feishu",
      "m_steer_1",
      RUN_ID,
    );

    const http409Running = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_running",
    );
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const reconcilePendingResubmit = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_pending_resubmit",
    );

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Running,
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
          runId: "run_after_steer",
          turnIndex: 2,
          content: "Processed returned steer!",
        }),
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
      messageId: "m_steer_1",
      text: "Also include NVDA",
    });

    expect(h.backend.appendCalls).toHaveLength(3);
    expect(h.backend.appendCalls[0]?.payload.request_key).toBe(inboundKey);
    expect(h.backend.appendCalls[0]?.payload.platform).toBe("feishu");
    expect(h.backend.appendCalls[0]?.payload.steer_only).toBeUndefined();
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(inboundKey);
    expect(h.backend.appendCalls[1]?.payload.steer_only).toBe(true);
    expect(h.backend.appendCalls[2]?.payload.request_key).toBe(returnedKey);
    expect(h.backend.appendCalls[2]?.payload.steer_only).toBeUndefined();
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Processed returned steer!",
    );
  });

  it("Type B `not_running` on `steer_only: true`: mints `:turn:1` key (`makeTurnAfterSteerRequestKey`), persists to I-7, and dispatches new turn", async () => {
    const inboundKey = makeInboundRequestKey("feishu", "m_not_running_1");
    const turnAfterSteerKey = makeTurnAfterSteerRequestKey(
      "feishu",
      "m_not_running_1",
      1,
    );
    const http409Running = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_running",
    );
    const http409NotRunning = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_not_running",
    );

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Running,
      })
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409NotRunning,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_new_turn",
          turnIndex: 2,
          content: "Started new turn after steer race!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_not_running_1",
      text: "Follow up right as run ended",
    });

    expect(h.backend.appendCalls).toHaveLength(3);
    expect(h.backend.appendCalls[0]?.payload.request_key).toBe(inboundKey);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(inboundKey);
    expect(h.backend.appendCalls[1]?.payload.steer_only).toBe(true);
    expect(h.backend.appendCalls[2]?.payload.request_key).toBe(
      turnAfterSteerKey,
    );
    expect(h.backend.appendCalls[2]?.payload.steer_only).toBeUndefined();
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Started new turn after steer race!",
    );
  });

  it("Type B `stopping` / `compacting` / `workspace_moving`: polls liveness in a loop until transitional state clears and retries keeping the SAME request_key (Codex r2 #8)", async () => {
    const inboundKey = makeInboundRequestKey("feishu", "m_backoff_stopping");
    const http409Stopping = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_stopping",
    );
    const statusStopping = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_stopping",
    );
    const statusIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    vi.mocked(h.backend.getThreadStatus)
      .mockResolvedValueOnce(statusStopping)
      .mockResolvedValueOnce(statusIdle);

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Stopping,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_after_stopping",
          turnIndex: 3,
          content: "Succeeded after stopping backoff!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_backoff_stopping",
      text: "Retry after stopping",
    });

    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[0]?.payload.request_key).toBe(inboundKey);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(inboundKey);
    expect(vi.mocked(h.backend.getThreadStatus).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Succeeded after stopping backoff!",
    );
  });

  it("Type A `duplicate_request` handles A1 (`in_progress` -> `/messages/stream?run_id`), A3 (`cancelled` + `retryable: true` -> `/retry`), and SSE `retry` / `stream_gap` / `run_end` (Codex r2 #9/#10)", async () => {
    const http409DupInProgress = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_duplicate_in_progress",
    );
    const http409DupCancelled = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_duplicate_cancelled_retryable",
    );
    const sseRetryEvent = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_retry_recoverable",
    );
    const sseStreamGap = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_stream_gap",
    );

    // 1. A1: `run_status: "in_progress"` -> attaches to `GET /messages/stream?run_id={run_id}`
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: http409DupInProgress,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_dup_a1",
      text: "Duplicate live turn",
    });

    expect(h.backend.streamThreadMessages).toHaveBeenCalledWith(
      USER_ID,
      THREAD_ID,
      { runId: RUN_ID, lastEventId: undefined },
    );

    // 2. A3: `run_status: "cancelled", retryable: true` -> mints `:retry:{failed_run_id}` and calls `POST /threads/{id}/retry` with `workspace_id`
    const failedRunId = "ffffffff-1111-2222-3333-444444444444";
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: http409DupCancelled,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_dup_a3",
      text: "Retry cancelled turn",
    });

    const expectedRetryKey = makeRetryRequestKey(
      "feishu",
      "m_dup_a3",
      failedRunId,
    );
    expect(h.backend.retryCalls).toHaveLength(1);
    expect(h.backend.retryCalls[0]?.payload).toMatchObject({
      workspace_id: WORKSPACE_ID,
      run_id: failedRunId,
      request_key: expectedRetryKey,
      platform: "feishu",
    });
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe("Retried failed turn!");

    // 3. SSE `event: retry` is treated as informational (not terminal error), and `event: stream_gap` triggers `/messages/replay` (Codex r2 #9/#10)
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        {
          event: "metadata",
          data: { thread_id: THREAD_ID, run_id: RUN_ID, turn_index: 1 },
        },
        sseRetryEvent,
        sseStreamGap,
      ]),
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_gap_and_retry",
      text: "Stream gap after retry",
    });

    expect(h.backend.replayThreadMessages).toHaveBeenCalled();
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Replayed assistant output.",
    );
  });

  it("Type C (`writer_capacity` vs `capacity_limit`) and Type D (`external_id_conflict` via I-4)", async () => {
    const capKey = makeInboundRequestKey("feishu", "m_writer_cap");
    const http409ExtConflict = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_external_id_conflict",
    );
    const newThreadUnresolved = loadFixtureKey<I1RegisterAndResolveResponse>(
      "i1_register_and_resolve.json",
      "new_thread_unresolved",
    );
    const http503WriterCapacity = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_503_writer_capacity",
    );

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 503,
        isSse: false,
        errorBody: http503WriterCapacity,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: RUN_ID,
          turnIndex: 1,
          content: "Recovered from writer_capacity!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_writer_cap",
      text: "Writer capacity test",
    });

    expect(h.backend.appendCalls[0]?.payload.request_key).toBe(capKey);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(capKey);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Recovered from writer_capacity!",
    );

    // Type D: `POST /api/v1/threads/messages` returns `409 { detail: "external_thread_id already bound to another thread" }`
    const extKey = makeInboundRequestKey("feishu", "m_ext_conflict");
    vi.mocked(h.backend.registerAndResolveInbound).mockResolvedValueOnce({
      ...newThreadUnresolved,
      request_key: extKey,
    });

    vi.mocked(h.backend.createThreadMessages).mockResolvedValueOnce({
      status: 409,
      isSse: false,
      errorBody: http409ExtConflict,
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_ext_conflict",
      text: "First message in raced thread",
    });

    expect(h.backend.getChannelState).toHaveBeenCalledWith({
      user_id: USER_ID,
      platform: "feishu",
      external_thread_id: "feishu:dm:ou_alice:gen:2",
    });
    expect(h.backend.appendCalls.at(-1)?.threadId).toBe(THREAD_ID);
    expect(h.backend.appendCalls.at(-1)?.payload.request_key).toBe(extKey);
  });

  it("Bare SSE EOF without `run_end` does NOT deliver partial text or mark `completed`, falls back to `/messages/replay`, and keeps `fallback_pending: true` when replay is unavailable (Codex r3 #1, Grok r3 #5)", async () => {
    const partialSseStream = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("conflict_and_sse_events.json", "sse_stream_partial_eof");

    // 1. Bare EOF after partial `message_chunk` (`sse_stream_partial_eof`) -> does NOT deliver partial text; falls back to `/messages/replay`
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(partialSseStream),
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_bare_eof_replay_ok",
      text: "Disconnect mid-stream",
    });

    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(1);
    // Delivered the full replayed text ("Replayed assistant output."), never the partial stream text ("TRUNCATED_PARTIAL_TEXT_DO_NOT_SEND")
    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toEqual([
      "Replayed assistant output.",
    ]);

    // 2. Bare EOF when `/messages/replay` also fails (e.g. HTTP 500) -> keeps row in recovery set (`fallback_pending: true`), never marks `completed`
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(partialSseStream),
    });
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 500,
      isSse: false,
      errorBody: { detail: "Replay temporarily down" },
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_bare_eof_unsettled",
      text: "Disconnect and replay down",
    });

    const unsettledUpdates = h.backend.progressUpdates.filter(
      (u) => u.message_id === "m_bare_eof_unsettled",
    );
    expect(unsettledUpdates.some((u) => u.status === "completed")).toBe(false);
    expect(unsettledUpdates.at(-1)?.fallback_pending).toBe(true);
    expect(
      h.feishuAdapter.sentTexts.some((t) =>
        t.text.includes("TRUNCATED_PARTIAL_TEXT_DO_NOT_SEND"),
      ),
    ).toBe(false);
  });

  it("handles `event: retry` with `auto_retry: true, recovery: \"resend\"` by re-sending `/messages`, and preserves `failedRunId` on `/retry` after `writer_capacity` (Codex r3 #2, #3)", async () => {
    const sseRetryResend = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_retry_resend",
    );
    const sseRetryRecoverable = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_retry_recoverable",
    );

    // 1. `recovery: "resend"` -> re-sends original `/messages` request (not `/retry`)
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
          runId: "run_resent_ok",
          turnIndex: 1,
          content: "Succeeded after resend!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_retry_resend",
      text: "Message needing resend",
    });

    expect(h.backend.retryCalls).toHaveLength(0);
    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[1]?.payload.messages).toEqual([
      { role: "user", content: "Message needing resend" },
    ]);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Succeeded after resend!",
    );

    // 2. `recovery: "retry"` -> calls `POST /threads/{id}/retry` with `run_id: failedRunId`, and if `/retry` hits `writer_capacity` (no `run_id`), preserves `run_id: failedRunId` on next `/retry`
    const failedRunId = "dddddddd-1111-2222-3333-444444444444";
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        {
          event: "metadata",
          data: { thread_id: THREAD_ID, run_id: failedRunId, turn_index: 2 },
        },
        {
          event: sseRetryRecoverable.event,
          data: {
            ...(sseRetryRecoverable.data as Record<string, unknown>),
            run_id: failedRunId,
          },
        },
      ]),
    });

    const http503WriterCapacity = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_503_writer_capacity",
    );
    vi.mocked(h.backend.retryThreadTurn)
      .mockResolvedValueOnce({
        status: 503,
        isSse: false,
        errorBody: http503WriterCapacity,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_retried_after_cap",
          turnIndex: 2,
          content: "Retried with preserved failedRunId!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_retry_preserve_run_id",
      text: "Trigger retry + writer_capacity",
    });

    expect(h.backend.retryCalls).toHaveLength(2);
    expect(h.backend.retryCalls[0]?.payload.run_id).toBe(failedRunId);
    expect(h.backend.retryCalls[1]?.payload.run_id).toBe(failedRunId);
    expect(h.backend.retryCalls[0]?.payload.request_key).toBe(
      h.backend.retryCalls[1]?.payload.request_key,
    );
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Retried with preserved failedRunId!",
    );
  });

  it("polls long `compacting` / `stopping` (including `running` status during compaction) across > 3 cycles without dropping the message (Codex r3 #5, Grok r3 #4)", async () => {
    const inboundKey = makeInboundRequestKey("feishu", "m_long_compacting");
    const http409Compacting = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_compacting",
    );
    const statusCompacting = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_compacting",
    );
    const statusRunning = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_running",
    );
    const statusStopping = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_stopping",
    );
    const statusIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    vi.mocked(h.backend.getThreadStatus)
      .mockResolvedValueOnce(statusCompacting)
      .mockResolvedValueOnce(statusRunning)
      .mockResolvedValueOnce(statusRunning)
      .mockResolvedValueOnce(statusCompacting)
      .mockResolvedValueOnce(statusStopping)
      .mockResolvedValueOnce(statusIdle);

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Compacting,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeCompletedTurnSseEvents({
          threadId: THREAD_ID,
          runId: "run_after_long_compact",
          turnIndex: 4,
          content: "Dispatched after 6 compaction polls!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_long_compacting",
      text: "Wait out long compaction",
    });

    expect(
      vi.mocked(h.backend.getThreadStatus).mock.calls.length,
    ).toBeGreaterThanOrEqual(6);
    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[0]?.payload.request_key).toBe(inboundKey);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(inboundKey);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Dispatched after 6 compaction polls!",
    );
  });

  it("continues polling `reconcileSteer` when `reconcile_status === \"still_pending\"` across > 3 polls until the host run settles into `pending_resubmit` (Codex r4 #4)", async () => {
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const reconcileStillPendingRunning =
      loadFixtureKey<I7SteersReconcileResponse>(
        "i7_progress_and_reconcile.json",
        "reconcile_still_pending_running",
      );
    const reconcilePendingResubmit = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_pending_resubmit",
    );
    const returnedKey = makeReturnedSteerRequestKey(
      "feishu",
      "m_long_still_pending",
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
          runId: "run_after_long_still_pending",
          turnIndex: 2,
          content: "Resubmitted after 6 still_pending polls!",
        }),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([]),
    });
    const livenessRunning = loadFixtureKey<{
      runs: DispatchLivenessRunInfo[];
    }>("status_liveness_wakeups_watch.json", "dispatches_liveness_running");
    vi.mocked(h.backend.getDispatchesLiveness).mockResolvedValue({
      [RUN_ID]: livenessRunning.runs[0]!,
    });
    vi.mocked(h.backend.reconcileSteer)
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockResolvedValueOnce({
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
      messageId: "m_long_still_pending",
      text: "Follow-up during long host run",
    });

    expect(
      vi.mocked(h.backend.reconcileSteer).mock.calls.length,
    ).toBeGreaterThanOrEqual(6);
    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(returnedKey);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Resubmitted after 6 still_pending polls!",
    );
  });

  it("enforces the 30-minute busy-wait ceiling using real wall-clock elapsed time (`nowFn` + persisted `busy_wait_started_at_ms`), not just accumulated backoff sleep (Codex r4 #7)", async () => {
    await h.cleanup();

    let fakeNowMs = 1_762_600_000_000;
    h = createTestHarness({
      nowFn: () => fakeNowMs,
      backoffDelayFn: async (ms: number) => {
        // Backoff sleep is only 1s/2s/4s, while each status poll / network roundtrip takes 12 minutes of real wall-clock time!
        fakeNowMs += ms;
      },
    });

    const http409Stopping = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_stopping",
    );

    vi.mocked(h.backend.appendThreadMessages).mockResolvedValue({
      status: 409,
      isSse: false,
      errorBody: http409Stopping,
    });

    const statusStopping = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_stopping",
    );
    let statusPolls = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(async () => {
      statusPolls++;
      // Each status query takes 12 minutes of wall-clock time (`12 * 60 * 1000 = 720_000 ms`).
      // Even though accumulated sleep after 3 polls is only 1s + 2s + 4s = 7s (<< 30m),
      // real elapsed wall-clock time reaches 36 minutes (> 30m) on the 3rd poll and must stop!
      fakeNowMs += 12 * 60 * 1000;
      return statusStopping;
    });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_wall_clock_timeout",
      text: "Message hitting slow stopping polls",
    });

    // Stopped after 3 status polls (36 minutes real elapsed time), NOT 450 polls of 4s sleep!
    expect(statusPolls).toBe(3);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_wall_clock_timeout" &&
          typeof u.raw_payload_patch?.busy_wait_started_at_ms === "number",
      ),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_wall_clock_timeout" &&
          u.status === "failed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "currently busy (stopping)",
    );
  });

  it("survives transient 5xx/network errors from `reconcileSteer` during `still_pending` polling and continues polling until the host run settles into `pending_resubmit` (Codex r5 #4)", async () => {
    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const reconcileStillPendingRunning =
      loadFixtureKey<I7SteersReconcileResponse>(
        "i7_progress_and_reconcile.json",
        "reconcile_still_pending_running",
      );
    const reconcilePendingResubmit = loadFixtureKey<I7SteersReconcileResponse>(
      "i7_progress_and_reconcile.json",
      "reconcile_pending_resubmit",
    );
    const returnedKey = makeReturnedSteerRequestKey(
      "feishu",
      "m_reconcile_transient_err",
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
          runId: "run_after_transient_reconcile_err",
          turnIndex: 2,
          content: "Resubmitted after transient reconcile errors!",
        }),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([]),
    });
    const livenessRunning = loadFixtureKey<{
      runs: DispatchLivenessRunInfo[];
    }>("status_liveness_wakeups_watch.json", "dispatches_liveness_running");
    vi.mocked(h.backend.getDispatchesLiveness).mockResolvedValue({
      [RUN_ID]: livenessRunning.runs[0]!,
    });
    vi.mocked(h.backend.reconcileSteer)
      .mockRejectedValueOnce(new Error("HTTP 503 Service Unavailable"))
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockRejectedValueOnce(new Error("ECONNRESET during reconcile"))
      .mockResolvedValueOnce(reconcileStillPendingRunning)
      .mockResolvedValueOnce({
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
      messageId: "m_reconcile_transient_err",
      text: "Follow-up surviving transient reconcile errors",
    });

    expect(
      vi.mocked(h.backend.reconcileSteer).mock.calls.length,
    ).toBeGreaterThanOrEqual(5);
    expect(h.backend.appendCalls).toHaveLength(2);
    expect(h.backend.appendCalls[1]?.payload.request_key).toBe(returnedKey);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Resubmitted after transient reconcile errors!",
    );
  });

  it("separates long-busy status polling count from post-dispatch retry count and resets `retryAttempt` to 0 on first dispatch after thread becomes idle so `writer_capacity` and `turn_conflict` retain their full retry budget (Codex r5 #6, Grok r5 #2)", async () => {
    const inboundKey = makeInboundRequestKey("feishu", "m_busy_then_conflicts");
    const http409Compacting = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_compacting",
    );
    const http409TurnConflict = loadFixtureKey<Record<string, unknown>>(
      "conflict_and_sse_events.json",
      "http_409_admission_turn_conflict",
    );
    const sseErrorWriterCap = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_error_writer_capacity",
    );

    const statusCompacting = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_compacting",
    );
    const statusRunning = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_running",
    );
    const statusStopping = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_stopping",
    );
    const statusIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    // 1st attempt hits a short `turn_conflict` (`retryAttempt: 0 -> 1`).
    // 2nd attempt hits `compacting` and polls `getThreadStatus` 6 times (`busyPollStep === 6 >= 3`) until `idle`.
    // When thread becomes `idle`, the first re-dispatch resets `retryAttempt` to `0`, so it can still survive
    // a `turn_conflict` (`0 -> 1`) AND two `writer_capacity` errors (`1 -> 2 -> 3`) before succeeding!
    vi.mocked(h.backend.getThreadStatus)
      .mockResolvedValueOnce(statusCompacting)
      .mockResolvedValueOnce(statusRunning)
      .mockResolvedValueOnce(statusRunning)
      .mockResolvedValueOnce(statusCompacting)
      .mockResolvedValueOnce(statusStopping)
      .mockResolvedValueOnce(statusIdle);

    vi.mocked(h.backend.appendThreadMessages)
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409TurnConflict,
      })
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409Compacting,
      })
      .mockResolvedValueOnce({
        status: 409,
        isSse: false,
        errorBody: http409TurnConflict,
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([sseErrorWriterCap]),
      })
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
          runId: "run_after_busy_and_3_retries",
          turnIndex: 5,
          content: "Succeeded after reset retryAttempt = 0!",
        }),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_busy_then_conflicts",
      text: "Message hitting long compaction then 3 post-idle conflicts",
    });

    expect(
      vi.mocked(h.backend.getThreadStatus).mock.calls.length,
    ).toBeGreaterThanOrEqual(6);
    expect(h.backend.appendCalls).toHaveLength(6);
    for (const call of h.backend.appendCalls) {
      expect(call.payload.request_key).toBe(inboundKey);
    }
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Succeeded after reset retryAttempt = 0!",
    );
  });

  it("keeps inbound row in `steered` recovery state on `still_pending` timeout (without marking `failed` or asking user to resend) and sends a one-time queued notice (Grok r5 #4)", async () => {
    await h.cleanup();
    h = createTestHarness({ busyThreadMaxWaitMs: 3_000 });

    const steeringAccepted = loadFixtureKey<{ event: string; data: unknown }>(
      "conflict_and_sse_events.json",
      "sse_steering_accepted",
    );
    const reconcileStillPendingRunning =
      loadFixtureKey<I7SteersReconcileResponse>(
        "i7_progress_and_reconcile.json",
        "reconcile_still_pending_running",
      );

    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([steeringAccepted]),
    });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([]),
    });
    const livenessRunning = loadFixtureKey<{
      runs: DispatchLivenessRunInfo[];
    }>("status_liveness_wakeups_watch.json", "dispatches_liveness_running");
    vi.mocked(h.backend.getDispatchesLiveness).mockResolvedValue({
      [RUN_ID]: livenessRunning.runs[0]!,
    });
    vi.mocked(h.backend.reconcileSteer).mockResolvedValue(
      reconcileStillPendingRunning,
    );

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatAddress: "feishu:@me",
      chatId: "ou_alice",
      threadScopeKey: "dm:ou_alice",
      messageId: "m_steer_timeout_keep_steered",
      text: "Supplementary instruction during 30m host run",
    });

    const updatesForSteer = h.backend.progressUpdates.filter(
      (u) => u.message_id === "m_steer_timeout_keep_steered",
    );
    // Must NEVER mark `status: "failed"` on `still_pending` timeout; must keep `status: "steered"` in recovery set!
    expect(updatesForSteer.some((u) => u.status === "failed")).toBe(false);
    expect(updatesForSteer.at(-1)?.status).toBe("steered");

    // Sent one-time queued notice (`notice:feishu:m_steer_timeout_keep_steered:steer_queued`) and did NOT ask user to resend
    expect(h.backend.deliveryClaims.map((c) => c.delivery_key)).toContain(
      "notice:feishu:m_steer_timeout_keep_steered:steer_queued",
    );
    const noticeText = h.feishuAdapter.sentTexts.at(-1)?.text ?? "";
    expect(noticeText).toContain("这条补充已排队，宿主回合结束后会处理");
    expect(noticeText).not.toContain("again");
  });
});
