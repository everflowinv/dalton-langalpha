import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BackendHttpError,
  type I6AuthorizeAndClaimResponse,
  type I7RecoverResponse,
  type ThreadStatusInfo,
  type ThreadWakeupItem,
} from "../src/backend-client.js";
import {
  createTestHarness,
  loadFixtureKey,
  makeCompletedTurnSseEvents,
  makeSseEvents,
  makeSseEventsFromFixture,
  type TestHarness,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

describe("`/watch` `workflow_started` Shapes, Silent EOF Reconnect & 7-Condition Close Check (§1.3.4, Codex r2 #15/#16, Grok r2 #4)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("handles all shapes of `workflow_started`: (1) live run, (2) settled_Directly, (3) start error, (4) quiet wakeup, and (5) `needs_input` string (Codex r3 #7, Grok r3 #2)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const wakeupsArmed = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_armed",
    ).wakeups;
    const wakeupsHeld = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_held",
    ).wakeups;
    const wakeupsHeldUsage = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_held_usage",
    ).wakeups;

    const settledDirectlyPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "settled_directly_turn_3_page");

    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.watch_snapshot!,
        watchEvents.workflow_started_shape_1_run_id!,
        watchEvents.workflow_started_shape_2_settled_directly!,
        watchEvents.workflow_started_shape_2_error!,
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    // Stream for Shape 1 (`bg_run_99`) with terminal `run_end`
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeCompletedTurnSseEvents({
        threadId: THREAD_ID,
        runId: "bg_run_99",
        turnIndex: 2,
        content: "Shape 1 live background output!",
      }),
    });

    // Replay for Shape 2 (`settled_Directly: true, run_id: null, turn_index: 3`)
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(settledDirectlyPage),
    });

    // When `needs_input: "22222222-3333-4444-5555-666666666666"` (a string from `wake.py:82`) arrives,
    // `getThreadStatus(USER_ID, THREAD_ID, "report_back")` returns `owed_threads` with workspace_name & title
    let getStatusCalls = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(async () => {
      getStatusCalls++;
      // Call #5 is the `needs_input` `owed_threads` lookup
      if (getStatusCalls === 5) {
        return {
          ...baseIdle,
          owed_threads: activeReportBack.owed_threads,
        };
      }
      return baseIdle;
    });

    // Keep watch open during the first 5 evaluations (`watch_snapshot` + first 4 `workflow_started`), then close on 6th (`cleared: true`)
    vi.mocked(h.backend.getThreadWakeups)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsHeld)
      .mockResolvedValueOnce(wakeupsHeldUsage)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 0,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    const claimKeys = h.backend.deliveryClaims.map((c) => c.delivery_key);
    // Shape 1 fallback
    expect(claimKeys).toContain("fallback:bg_run_99");
    // Shape 2 fallback with synthetic key `fallback:{thread_id}:turn:3`
    expect(claimKeys).toContain(`fallback:${THREAD_ID}:turn:3`);
    // Shape 3 error notice
    expect(claimKeys).toContain(`notice:watch_err:${THREAD_ID}:4`);
    // Shape 5 `needs_input` string notice (with pause identity token from first notification, Codex r5 #5)
    expect(claimKeys).toContain(
      `notice:needs_input:${THREAD_ID}:22222222-3333-4444-5555-666666666666:run:1:cp_1`,
    );

    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toEqual([
      "Shape 1 live background output!",
      "Shape 2 directly settled report-back!",
      "Background sandbox failed to boot.",
      "Analyst in Quant Workspace (Analyst Deep Dive) needs your input. Open https://app.ginlix.ai/chat/t/22222222-3333-4444-5555-666666666666",
    ]);
  });

  it("checks all 7 close conditions against real `status_full_idle` / `status_report_back_slice_active` and `pending_report_back: null` (Codex r2 #16, Grok r2 #4)", async () => {
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const wakeupsArmed = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_armed",
    ).wakeups;
    const wakeupsFired = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_fired",
    ).wakeups;

    h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 0,
      lastSeenTurnIndex: 1,
    });

    // 1. Active report-back slice (`pending_report_back: true`, `active_tasks`, `owed_threads`) -> stays open
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...baseIdle,
      ...activeReportBack,
    });
    expect(
      await h.app.orchestrator.evaluateWatchCloseConditions(USER_ID, THREAD_ID),
    ).toBe(false);

    // 2. `pending_report_back: null` (Redis read error in `report_back.py:42-48`) -> stays open (Codex r2 #16)
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...baseIdle,
      pending_report_back: null,
    });
    expect(
      await h.app.orchestrator.evaluateWatchCloseConditions(USER_ID, THREAD_ID),
    ).toBe(false);

    // 3. `pending_steers` non-empty -> stays open (Grok r2 #4)
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...baseIdle,
      pending_steers: [{ steer_id: "s1" }],
    });
    expect(
      await h.app.orchestrator.evaluateWatchCloseConditions(USER_ID, THREAD_ID),
    ).toBe(false);

    // 4. Armed wakeup in `/wakeups` (`state: "armed"`) -> stays open
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValueOnce(wakeupsArmed);
    expect(
      await h.app.orchestrator.evaluateWatchCloseConditions(USER_ID, THREAD_ID),
    ).toBe(false);

    // 5. All conditions idle and only `fired` wakeups -> closes watch and updates `watch_active: false`
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValueOnce(wakeupsFired);
    expect(
      await h.app.orchestrator.evaluateWatchCloseConditions(USER_ID, THREAD_ID),
    ).toBe(true);
  });

  it("reconnects `/watch` with backoff + `watch_reconnect` on 30-min EOF, empty EOF (`wake.py:128`), network error, and non-SSE 502 response (Codex r2 #15, Codex r3 #6, Grok r3 #3)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const wakeupsArmed = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_armed",
    ).wakeups;

    // 1. Stream emits `watch_snapshot` and then ends at 30-min EOF (no `timeout` event) while wakeup is armed -> reconnects
    // 2. Next attempt ends at empty EOF without `watch_snapshot` (Redis wake listener not live in 5s, `wake.py:128`) -> backoff + reconnects
    // 3. Next attempt throws network exception -> backoff + reconnects
    // 4. Next attempt returns non-SSE HTTP 502 -> backoff + reconnects
    // 5. Final attempt receives `watch_snapshot` when conditions are idle -> closes cleanly
    vi.mocked(h.backend.watchThread)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([]),
      })
      .mockRejectedValueOnce(new Error("ECONNRESET"))
      .mockResolvedValueOnce({
        status: 502,
        isSse: false,
        errorBody: { detail: "Bad Gateway" },
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
      });

    // Evaluations 1..5 see an armed wakeup (1: 1st `watch_snapshot`, 2: 1st 30m EOF, 3: 2nd empty EOF, 4: 3rd network error, 5: 4th non-SSE 502);
    // 6th evaluation (on 5th attempt's `watch_snapshot`) sees no wakeups -> closes watch
    vi.mocked(h.backend.getThreadWakeups)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 0,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    expect(h.backend.watchThread).toHaveBeenCalledTimes(5);
    expect(h.backend.recoverState).toHaveBeenCalledTimes(4);
    expect(h.backend.recoverState).toHaveBeenCalledWith({
      mode: "watch_reconnect",
      thread_id: THREAD_ID,
    });
  });

  it("calls `recover?mode=watch_reconnect` BEFORE evaluating close conditions on `/watch` disconnect so background turns settled during the disconnect window are backfilled and delivered before `watch_active: false` (Codex r4 #3)", async () => {
    const recoverSettledTurn2 = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect_settled_turn_2",
    );
    const backfilledReplayPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "backfilled_turn_2_single_page");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([]),
    });
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce(
      recoverSettledTurn2,
    );
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(backfilledReplayPage),
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValue({
      ...baseIdle,
      latest_turn_index: 2,
      turn_index: 2,
    });
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    expect(h.backend.recoverState).toHaveBeenCalledWith({
      mode: "watch_reconnect",
      thread_id: THREAD_ID,
    });
    expect(h.backend.deliveryClaims.map((c) => c.delivery_key)).toContain(
      "fallback:bg_backfill_2",
    );
    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toContain(
      "Backfilled wakeup report from disconnect window.",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) => u.thread_watch?.watch_active === false,
      ),
    ).toBe(true);
  });

  it("delivers a second `needs_input` notice when the same analyst sub-thread pauses a second time by generating a distinct per-pause `delivery_key` (Codex r4 #5)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const subStatusPause2 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_2",
    );
    const wakeupsArmed = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_armed",
    ).wakeups;

    const subThreadId = "22222222-3333-4444-5555-666666666666";

    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    let subThreadStatusCall = 0;
    let mainThreadStatusCall = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          subThreadStatusCall++;
          return subThreadStatusCall === 1 ? subStatusPause1 : subStatusPause2;
        }
        mainThreadStatusCall++;
        if (mainThreadStatusCall <= 2) {
          return {
            ...baseIdle,
            owed_threads: activeReportBack.owed_threads,
          };
        }
        return baseIdle;
      },
    );

    const alreadySentI6 = loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_sent",
    );
    const seenDeliveryKeys = new Set<string>();
    const baseClaimImpl = vi
      .mocked(h.backend.authorizeAndClaimDelivery)
      .getMockImplementation()!;
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (seenDeliveryKeys.has(req.delivery_key)) {
          return structuredClone(alreadySentI6);
        }
        seenDeliveryKeys.add(req.delivery_key);
        return await baseClaimImpl(req);
      },
    );

    vi.mocked(h.backend.getThreadWakeups)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).not.toBe(
      needsInputClaims[1]?.delivery_key,
    );
    expect(h.feishuAdapter.sentTexts).toHaveLength(2);
  });

  it("stops watching immediately and does NOT retry in an infinite loop when a watched thread is deleted (`404` on `/watch`, `/status`, or `/wakeups`, Codex r4 #6)", async () => {
    const http404NotFound = loadFixtureKey<Record<string, unknown>>(
      "status_liveness_wakeups_watch.json",
      "http_404_thread_not_found",
    );

    // 1. `/watch` returns HTTP 404 -> terminates watch immediately without retrying
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 404,
      isSse: false,
      errorBody: http404NotFound,
    });

    const handle1 = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle1.promise;

    expect(h.backend.watchThread).toHaveBeenCalledTimes(1);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.thread_watch?.thread_id === THREAD_ID &&
          u.thread_watch?.watch_active === false,
      ),
    ).toBe(true);

    // 2. `/watch` closes at EOF and `/wakeups` throws `BackendHttpError(404)` -> terminates watch without looping
    const deletedThreadId2 = "99999999-0000-4000-8000-000000000002";
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([]),
    });
    vi.mocked(h.backend.getThreadWakeups).mockRejectedValueOnce(
      new BackendHttpError(
        `GET /api/v1/threads/${deletedThreadId2}/wakeups failed with HTTP 404`,
        404,
        http404NotFound,
      ),
    );

    const handle2 = h.app.orchestrator.ensureThreadWatch({
      threadId: deletedThreadId2,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle2.promise;

    expect(h.backend.watchThread).toHaveBeenCalledTimes(2);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.thread_watch?.thread_id === deletedThreadId2 &&
          u.thread_watch?.watch_active === false,
      ),
    ).toBe(true);
  });

  it("refuses to close `/watch` (does NOT write `watch_active: false`) when `recover?mode=watch_reconnect` throws 503 or backfill replay fails, and retries until backfilled background turns are delivered (Codex r5 #3, Grok r5 #3)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const recoverSettledTurn2 = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect_settled_turn_2",
    );
    const backfilledReplayPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "backfilled_turn_2_single_page");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    // On the 1st disconnect, `recoverState(watch_reconnect)` throws HTTP 503 -> watch MUST NOT close (`watch_active: false` must NOT be written)!
    // On the 2nd attempt, `watch_snapshot` sees `latest_turn_index: 2 > lastSeenTurnIndex (1)`, runs `watch_reconnect`, replays Turn 2, and THEN closes!
    vi.mocked(h.backend.watchThread)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
      });
    vi.mocked(h.backend.recoverState)
      .mockRejectedValueOnce(
        new BackendHttpError(
          "POST /internal/channels/state/recover failed with HTTP 503",
          503,
          { detail: "Service Unavailable" },
        ),
      )
      .mockResolvedValueOnce(recoverSettledTurn2);
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(backfilledReplayPage),
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValue({
      ...baseIdle,
      latest_turn_index: 2,
      turn_index: 2,
    });
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    expect(h.backend.recoverState).toHaveBeenCalledTimes(2);
    expect(h.backend.deliveryClaims.map((c) => c.delivery_key)).toContain(
      "fallback:bg_backfill_2",
    );
    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toContain(
      "Backfilled wakeup report from disconnect window.",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) => u.thread_watch?.watch_active === false,
      ),
    ).toBe(true);
  });

  it("includes sub-thread pause identity in `needs_input` `delivery_key` from the very first notification (`seq === 1`) so a second pause after gateway restart / new WatchHandle is not deduplicated (Codex r5 #5, Grok r5 #5)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const subStatusPause2 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_2",
    );
    const wakeupsArmed = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_armed",
    ).wakeups;

    const subThreadId = "22222222-3333-4444-5555-666666666666";

    const alreadySentI6 = loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_sent",
    );
    const seenDeliveryKeys = new Set<string>();
    const baseClaimImpl = vi
      .mocked(h.backend.authorizeAndClaimDelivery)
      .getMockImplementation()!;
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (seenDeliveryKeys.has(req.delivery_key)) {
          return structuredClone(alreadySentI6);
        }
        seenDeliveryKeys.add(req.delivery_key);
        return await baseClaimImpl(req);
      },
    );

    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    let subThreadStatusCall = 0;
    let mainThreadStatusCall = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          subThreadStatusCall++;
          return subThreadStatusCall === 1 ? subStatusPause1 : subStatusPause2;
        }
        mainThreadStatusCall++;
        if (mainThreadStatusCall === 1 || mainThreadStatusCall === 3) {
          return {
            ...baseIdle,
            owed_threads: activeReportBack.owed_threads,
          };
        }
        return baseIdle;
      },
    );

    vi.mocked(h.backend.getThreadWakeups)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([]);

    const handleBeforeRestart = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handleBeforeRestart.promise;

    // 2nd watch session (simulating gateway restart: brand-new WatchHandle where in-memory `needsInputSeqBySubThread` starts at `seq === 1` again!):
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    const handleAfterRestart = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handleAfterRestart.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_1:1:cp_1`,
    );
    expect(needsInputClaims[1]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_2:2:cp_1`,
    );
    expect(needsInputClaims[0]?.delivery_key).not.toBe(
      needsInputClaims[1]?.delivery_key,
    );
    expect(h.feishuAdapter.sentTexts).toHaveLength(2);
  });

  it("does NOT close `/watch` (`watch_active: false`) from inner `consumeBackgroundRunStream` `finally` during `syncWatchReconnectState` when an attached live background run disconnects and replay fallback fails, and propagates `false` from `recoverSingleBackgroundRun` (Codex r6 #2)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const recoverWatchReconnect = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect",
    );
    const livenessRunning = loadFixtureKey<{
      runs: Array<{ run_id: string; status: string }>;
    }>("status_liveness_wakeups_watch.json", "dispatches_liveness_running");
    const livenessCompleted = loadFixtureKey<{
      runs: Array<{ run_id: string; status: string }>;
    }>("status_liveness_wakeups_watch.json", "dispatches_liveness_completed");
    const unavailableEvent = loadFixtureKey<{ event: string; data: unknown }>(
      "replay_events.json",
      "stub_replay_unavailable_event",
    );
    const settledSinglePage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "recover_bg_rec_1_page");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    // 1st watch cycle: `recover?mode=watch_reconnect` returns `bg_rec_1` (`status: "streaming"`).
    // `getDispatchesLiveness` reports `bg_rec_1` is still `running`, so `recoverSingleBackgroundRun` enters `consumeBackgroundRunStream`.
    // The live stream disconnects at bare EOF, and `replayThreadMessages` returns `replay_unavailable` (`settled === false`).
    // Meanwhile the thread is now `idle` (`getThreadStatus` returns `idle`, `wakeups` empty).
    // The inner `evaluateWatchCloseConditions` in `consumeBackgroundRunStream`'s `finally` MUST NOT close `/watch` (`watch_active: false`)!
    // 2nd watch cycle: `recover?mode=watch_reconnect` runs again, `replayThreadMessages` succeeds (`default_single_page`), delivers `"Replayed assistant output."`, and THEN closes `/watch`!
    vi.mocked(h.backend.watchThread)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
      });

    vi.mocked(h.backend.recoverState)
      .mockResolvedValueOnce(recoverWatchReconnect)
      .mockResolvedValueOnce(recoverWatchReconnect);

    vi.mocked(h.backend.getDispatchesLiveness)
      .mockResolvedValueOnce({
        bg_rec_1: {
          ...livenessRunning.runs[0]!,
          run_id: "bg_rec_1",
        },
      })
      .mockResolvedValueOnce({
        bg_rec_1: {
          ...livenessCompleted.runs[0]!,
          run_id: "bg_rec_1",
        },
      });

    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEvents([]),
    });

    let replayCall = 0;
    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => {
      replayCall++;
      // First 4 calls (Pass 1..4 of the 1st cycle) fail with `replay_unavailable`; 5th call (2nd cycle) succeeds!
      if (replayCall <= 4) {
        return {
          status: 200,
          isSse: true,
          events: makeSseEventsFromFixture([unavailableEvent]),
        };
      }
      return {
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledSinglePage),
      };
    });

    vi.mocked(h.backend.getThreadStatus).mockResolvedValue(baseIdle);
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    expect(h.backend.watchThread).toHaveBeenCalledTimes(2);
    expect(h.backend.recoverState).toHaveBeenCalledTimes(2);
    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toContain(
      "Replayed assistant output.",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) => u.thread_watch?.watch_active === false,
      ),
    ).toBe(true);
  });

  it("keeps `fallback_pending: true`, returns `false`, and refuses to close `/watch` when `executeDelivery` returns `{ status: \"failed\" }` (e.g. channel adapter temporarily disconnected), and re-delivers once the adapter reconnects (Codex r6 #3)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const recoverSettledTurn2 = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect_settled_turn_2",
    );
    const backfilledReplayPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "backfilled_turn_2_single_page");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );

    // Temporarily disconnect the Feishu adapter during the 1st watch cycle so `executeDelivery` returns `{ status: "failed" }` without throwing
    h.feishuAdapter.connected = false;

    vi.mocked(h.backend.watchThread)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEvents([]),
      })
      .mockImplementationOnce(async () => {
        // Reconnect adapter before the 2nd watch cycle
        h.feishuAdapter.connected = true;
        return {
          status: 200,
          isSse: true,
          events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
        };
      });

    vi.mocked(h.backend.recoverState)
      .mockResolvedValueOnce(recoverSettledTurn2)
      .mockResolvedValueOnce(recoverSettledTurn2);
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(backfilledReplayPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(backfilledReplayPage),
      });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValue({
      ...baseIdle,
      latest_turn_index: 2,
      turn_index: 2,
    });
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    // 1st cycle kept `fallback_pending: true` when `executeDelivery` returned `status: "failed"`, and 2nd cycle re-delivered and cleared `fallback_pending: false`!
    expect(h.backend.watchThread).toHaveBeenCalledTimes(2);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.background_run?.run_id === "bg_backfill_2" &&
          u.background_run?.fallback_pending === true,
      ),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.background_run?.run_id === "bg_backfill_2" &&
          u.background_run?.fallback_pending === false,
      ),
    ).toBe(true);
    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toContain(
      "Backfilled wakeup report from disconnect window.",
    );
  });

  it("retries sub-thread status lookup with backoff on transient failure and defers until `/status` pause identity is available instead of falling back to process-local `:seq:1` or a secondary identity source (Codex r6 #4, Codex r9 #2)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const subStatusPause2 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_2",
    );
    const page1WithInterrupt = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "page_1_with_more_and_tip_interrupt");
    const alreadySentI6 = loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_sent",
    );
    const wakeupsArmed = loadFixtureKey<{ wakeups: ThreadWakeupItem[] }>(
      "status_liveness_wakeups_watch.json",
      "wakeups_armed",
    ).wakeups;

    const subThreadId = "22222222-3333-4444-5555-666666666666";
    const seenDeliveryKeys = new Set<string>();
    const baseClaimImpl = vi
      .mocked(h.backend.authorizeAndClaimDelivery)
      .getMockImplementation()!;
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (seenDeliveryKeys.has(req.delivery_key)) {
          return structuredClone(alreadySentI6);
        }
        seenDeliveryKeys.add(req.delivery_key);
        return await baseClaimImpl(req);
      },
    );

    // 1st watch session (before restart): `getThreadStatus` on `subThreadId` fails on all 4 attempts during `workflow_started_shape_3_needs_input` ->
    // does NOT use `:seq:1` or switch to `/messages/replay` (`interrupt:int_tip_5`), defers in `pendingNeedsInputThreads`,
    // and delivers with `/status` identity `sub_run_1:1:cp_1` once `/status` recovers on `workflow_started_shape_4_cleared`!
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    let subStatusPhase: "fail_4_then_pause1" | "fail_once_then_pause2" =
      "fail_4_then_pause1";
    let subAttemptInPhase1 = 0;
    let subAttemptInPhase2 = 0;
    let mainStatusCall = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          if (subStatusPhase === "fail_4_then_pause1") {
            subAttemptInPhase1++;
            if (subAttemptInPhase1 <= 2) {
              throw new Error("HTTP 503 sub-thread status unavailable");
            }
            return subStatusPause1;
          }
          subAttemptInPhase2++;
          if (subAttemptInPhase2 === 1) {
            throw new Error("Transient 502 on first sub-thread status attempt");
          }
          return subStatusPause2;
        }
        mainStatusCall++;
        if (mainStatusCall === 1 || mainStatusCall === 3) {
          return {
            ...baseIdle,
            owed_threads: activeReportBack.owed_threads,
          };
        }
        return baseIdle;
      },
    );

    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(page1WithInterrupt),
    });

    vi.mocked(h.backend.getThreadWakeups)
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(wakeupsArmed)
      .mockResolvedValueOnce([]);

    const handleBeforeRestart = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handleBeforeRestart.promise;

    // 2nd watch session (after gateway restart): `getThreadStatus` on `subThreadId` fails once, retries with backoff, and succeeds with `subStatusPause2`!
    subStatusPhase = "fail_once_then_pause2";
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    const handleAfterRestart = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handleAfterRestart.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_1:1:cp_1`,
    );
    expect(needsInputClaims[1]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_2:2:cp_1`,
    );
    expect(
      needsInputClaims.some((c) => c.delivery_key.endsWith(":seq:1")),
    ).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(2);
  });

  it("does NOT construct a degraded turn-only `run:<turnIdx>` pause key when sub-thread `/status` has `run_id: null` and `progress: null` (`thread_status.py:176-192, 212-226`), defers the notice, and delivers once `progress.checkpoint_id` is available (Grok r6 suspected)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusNoProgress = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_interrupted_no_progress",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const unavailableEvent = loadFixtureKey<{ event: string; data: unknown }>(
      "replay_events.json",
      "stub_replay_unavailable_event",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";

    // 1st watch cycle: `workflow_started` emits `needs_input` (`subThreadId`), then `cleared: true`.
    // On the 1st cycle, sub-thread `/status` returns `status: "interrupted", run_id: null, progress: null, latest_turn_index: 1`
    // and `/messages/replay` is unavailable -> `computeNeedsInputDeliveryKey` returns `null` (must NOT degrade to `run:1`),
    // defers `subThreadId` in `pendingNeedsInputThreads`, and refuses to close `/watch`!
    // 2nd watch cycle: `evaluateWatchCloseConditions` retries `pendingNeedsInputThreads`, sub-thread `/status` returns `progress.checkpoint_id: "cp_1"`,
    // delivers the notice with `:cp_1`, and then closes `/watch`!
    vi.mocked(h.backend.watchThread)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([
          watchEvents.workflow_started_shape_3_needs_input!,
          watchEvents.workflow_started_shape_4_cleared!,
        ]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
      });

    let subStatusCallCount = 0;
    let mainStatusCallCount = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          subStatusCallCount++;
          // First 4 calls (all 4 attempts on `workflow_started_shape_3_needs_input`) return `progress: null, run_id: null`
          if (subStatusCallCount <= 4) {
            return subStatusNoProgress;
          }
          return {
            ...subStatusPause1,
            run_id: null,
          };
        }
        mainStatusCallCount++;
        if (mainStatusCallCount === 1) {
          return {
            ...baseIdle,
            owed_threads: activeReportBack.owed_threads,
          };
        }
        return baseIdle;
      },
    );

    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([unavailableEvent]),
    });
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(1);
    expect(needsInputClaims[0]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:run:1:cp_1`,
    );
    expect(
      needsInputClaims.some(
        (c) =>
          c.delivery_key ===
          `notice:needs_input:${THREAD_ID}:${subThreadId}:run:1`,
      ),
    ).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
  });

  it("rebuilds and delivers a deferred `needs_input` pause notice from `owed_threads` after gateway restart without needing `workflow_started` to replay (Codex r7 #2)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusNoProgress = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_interrupted_no_progress",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const unavailableEvent = loadFixtureKey<{ event: string; data: unknown }>(
      "replay_events.json",
      "stub_replay_unavailable_event",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";

    // Phase 1 (before gateway restart): `workflow_started` emits `needs_input`, but both sub-thread `/status`
    // (`run_id: null, progress: null`) and `/messages/replay` (`replay_unavailable`) fail to provide pause identity ->
    // notice is deferred in `handleBeforeRestart.pendingNeedsInputThreads` and NOT sent before restart!
    let phase: "before_restart" | "after_restart" = "before_restart";
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          return phase === "before_restart"
            ? subStatusNoProgress
            : subStatusPause1;
        }
        return phase === "before_restart"
          ? { ...baseIdle, owed_threads: activeReportBack.owed_threads }
          : baseIdle;
      },
    );
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([unavailableEvent]),
    });
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    vi.mocked(h.backend.watchThread).mockImplementationOnce(async () => ({
      status: 200,
      isSse: true,
      events: (async function* () {
        yield {
          event: watchEvents.workflow_started_shape_3_needs_input!.event,
          data: JSON.stringify(
            watchEvents.workflow_started_shape_3_needs_input!.data,
          ),
        };
        // Simulate process shutdown / restart right after the 1st `needs_input` event was deferred in memory
        h.app.orchestrator.activeWatches
          .get(THREAD_ID)
          ?.abortController.abort();
      })(),
    }));

    const handleBeforeRestart = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handleBeforeRestart.promise;

    expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    expect(
      handleBeforeRestart.pendingNeedsInputThreads?.has(subThreadId),
    ).toBe(true);

    // Phase 2 (after gateway restart): in-memory `pendingNeedsInputThreads` is gone (brand-new `ActiveWatchHandle`),
    // and `/watch` only emits `watch_snapshot_with_owed_threads` + `workflow_started_shape_4_cleared` (no historical `needs_input` event!).
    // The gateway inspects `owed_threads`, queries `getThreadStatus(subThreadId)` (`status: "interrupted"`), and delivers the notice!
    phase = "after_restart";
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.watch_snapshot_with_owed_threads!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    const handleAfterRestart = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handleAfterRestart.promise;

    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(
      `Analyst in Quant Workspace (Analyst Deep Dive) needs your input. Open https://app.ginlix.ai/chat/t/${subThreadId}`,
    );
    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(1);
    expect(needsInputClaims[0]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_1:1:cp_1`,
    );
  });

  it("reuses the exact same `delivery_key` when retrying a deferred `needs_input` notice after a failed/unknown delivery instead of appending `:2` (Codex r7 #3)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const inFlightOrUnknownI6 = loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "in_flight_or_unknown",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";
    const expectedKey = `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_1:1:cp_1`;

    // 1st watch cycle: `workflow_started_shape_3_needs_input` computes `expectedKey`,
    // `I-6` returns `in_flight_or_unknown` (`outcome: "unknown"`, `authorized: false`) on attempt 1 so `sendNoticeDelivery` returns `false`
    // and defers the notice in `pendingNeedsInputThreads`.
    // On `workflow_started_shape_4_cleared`, `evaluateWatchCloseConditions` retries `pendingNeedsInputThreads`:
    // it MUST reuse `expectedKey` (NOT `${expectedKey}:2`!), allowing `I-6` to recognize the same key!
    let claimCountForExpectedKey = 0;
    const baseClaimImpl = vi
      .mocked(h.backend.authorizeAndClaimDelivery)
      .getMockImplementation()!;
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (req.delivery_key === expectedKey) {
          claimCountForExpectedKey++;
          if (claimCountForExpectedKey === 1) {
            return structuredClone(inFlightOrUnknownI6);
          }
        }
        return await baseClaimImpl(req);
      },
    );

    let mainStatusCall = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          return subStatusPause1;
        }
        mainStatusCall++;
        if (mainStatusCall === 1) {
          return {
            ...baseIdle,
            owed_threads: activeReportBack.owed_threads,
          };
        }
        return baseIdle;
      },
    );
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).toBe(expectedKey);
    expect(needsInputClaims[1]?.delivery_key).toBe(expectedKey);
    expect(
      needsInputClaims.some((c) => c.delivery_key === `${expectedKey}:2`),
    ).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
  });

  it("delivers a second `needs_input` notice via `owed_threads` when the same sub-thread pauses a second time during a `/watch` disconnect window (`lastNeedsInputTokenByThread` compares pause identity instead of skipping by thread ID) (Codex r8 #4, Grok r8 High #2)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const activeReportBack = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_report_back_slice_active",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const subStatusPause2 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_2",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";

    // Connection 1: `workflow_started_shape_3_needs_input` delivers pause 1 (`sub_run_1:1:cp_1`),
    // setting `handle.lastNeedsInputTokenByThread.set(subThreadId, "sub_run_1:1:cp_1")`, and then disconnects (EOF).
    // During the disconnect window, the user answers pause 1 on Web and the sub-thread pauses a second time (`sub_run_2:2:cp_1`).
    // Connection 2: `/watch` reconnects and only emits `watch_snapshot_with_owed_threads` + `workflow_started_shape_4_cleared`.
    // `reconcileOwedThreadsNeedsInput` MUST NOT skip `subThreadId` just because `lastNeedsInputTokenByThread.has(subThreadId)`;
    // it checks `/status`, sees `"sub_run_2:2:cp_1" !== "sub_run_1:1:cp_1"`, and delivers the second pause notice!
    let watchConn = 0;
    let mainStatusAfterCleared = false;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          return watchConn <= 1 ? subStatusPause1 : subStatusPause2;
        }
        if (mainStatusAfterCleared) {
          return baseIdle;
        }
        return {
          ...baseIdle,
          owed_threads: activeReportBack.owed_threads,
        };
      },
    );
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    vi.mocked(h.backend.watchThread)
      .mockImplementationOnce(async () => {
        watchConn = 1;
        return {
          status: 200,
          isSse: true,
          events: makeSseEventsFromFixture([
            watchEvents.workflow_started_shape_3_needs_input!,
          ]),
        };
      })
      .mockImplementationOnce(async () => {
        watchConn = 2;
        return {
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: watchEvents.watch_snapshot_with_owed_threads!.event,
              data: JSON.stringify(
                watchEvents.watch_snapshot_with_owed_threads!.data,
              ),
            };
            mainStatusAfterCleared = true;
            yield {
              event: watchEvents.workflow_started_shape_4_cleared!.event,
              data: JSON.stringify(
                watchEvents.workflow_started_shape_4_cleared!.data,
              ),
            };
          })(),
        };
      });

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    expect(h.feishuAdapter.sentTexts).toHaveLength(2);
    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_1:1:cp_1`,
    );
    expect(needsInputClaims[1]?.delivery_key).toBe(
      `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_2:2:cp_1`,
    );
  });

  it("computes the identical `delivery_key` (without `:2`) when `watch_snapshot` (`owed_threads`) is immediately followed by `workflow_started: { needs_input }` for the same pause, so `I-6` deduplicates and sends only 1 message (Codex r8 #5)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const alreadyCompletedSent = loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_sent",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";
    const expectedKey = `notice:needs_input:${THREAD_ID}:${subThreadId}:sub_run_1:1:cp_1`;

    const seenKeys = new Set<string>();
    const baseClaimImpl = vi
      .mocked(h.backend.authorizeAndClaimDelivery)
      .getMockImplementation()!;
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (seenKeys.has(req.delivery_key)) {
          return structuredClone(alreadyCompletedSent);
        }
        seenKeys.add(req.delivery_key);
        return await baseClaimImpl(req);
      },
    );

    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          return subStatusPause1;
        }
        return baseIdle;
      },
    );
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    // `/watch` emits `watch_snapshot_with_owed_threads` AND a queued `workflow_started_shape_3_needs_input` for the exact same pause!
    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.watch_snapshot_with_owed_threads!,
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).toBe(expectedKey);
    expect(needsInputClaims[1]?.delivery_key).toBe(expectedKey);
    expect(
      needsInputClaims.some((c) => c.delivery_key === `${expectedKey}:2`),
    ).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
  });

  it("does NOT put an `owed_threads` entry into `pendingNeedsInputThreads` (or send a false `needs_input` notice) when all 4 `getThreadStatus(subThreadId)` attempts fail during `reconcileOwedThreadsNeedsInput`, instead keeping `needsReconnectSync = true` (Grok r8 suspected #1)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const statusRunning = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_running",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";

    // On `watch_snapshot_with_owed_threads`, all 4 `getThreadStatus(subThreadId)` calls fail with HTTP 503.
    // The sub-thread is actually `running` (NOT `interrupted`!).
    // If `reconcileOwedThreadsNeedsInput` mistakenly added `subThreadId` to `pendingNeedsInputThreads`,
    // then when `workflow_started_shape_4_cleared` runs `evaluateWatchCloseConditions`, it would call `deliverOrDeferNeedsInputNotice`
    // and send a false "needs your input" notice for a `running` thread!
    // Instead, `reconcileOwedThreadsNeedsInput` must NOT add `subThreadId` to `pendingNeedsInputThreads` and must set `needsReconnectSync = true`.
    let subStatusCalls = 0;
    let mainStatusAfterCleared = false;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          subStatusCalls++;
          if (subStatusCalls <= 4) {
            throw new Error("HTTP 503 Service Unavailable");
          }
          return {
            ...statusRunning,
            thread_id: subThreadId,
          };
        }
        if (mainStatusAfterCleared) {
          return baseIdle;
        }
        return {
          ...baseIdle,
          owed_threads: [
            {
              thread_id: subThreadId,
              workspace_id: "99999999-8888-7777-6666-555555555555",
              workspace_name: "Quant Workspace",
              title: "Analyst Deep Dive",
            },
          ],
        };
      },
    );
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    vi.mocked(h.backend.watchThread)
      .mockImplementationOnce(async () => ({
        status: 200,
        isSse: true,
        events: (async function* () {
          yield {
            event: watchEvents.watch_snapshot_with_owed_threads!.event,
            data: JSON.stringify(
              watchEvents.watch_snapshot_with_owed_threads!.data,
            ),
          };
          // Even if `workflow_started_shape_4_cleared` arrives while `needsReconnectSync` is true,
          // `evaluateWatchCloseConditions` refuses to close until reconnect sync succeeds!
          mainStatusAfterCleared = true;
          yield {
            event: watchEvents.workflow_started_shape_4_cleared!.event,
            data: JSON.stringify(
              watchEvents.workflow_started_shape_4_cleared!.data,
            ),
          };
        })(),
      }))
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([watchEvents.watch_snapshot!]),
      });

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    expect(subStatusCalls).toBe(4);
    expect(
      handle.pendingNeedsInputThreads?.has(subThreadId) ?? false,
    ).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    expect(h.backend.recoverState).toHaveBeenCalledWith({
      mode: "watch_reconnect",
      thread_id: THREAD_ID,
    });
  });

  it("never switches pause identity source from `/status` (`run:1:cp_1`) to `/messages/replay` (`interrupt:int_tip_5`) when `watch_snapshot` succeeds via `/status` and the subsequent `workflow_started: { needs_input }` experiences 4 `/status` failures or missing checkpoint (Codex r9 #2)", async () => {
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const baseIdle = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_idle",
    );
    const subStatusPause1 = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_pause_1",
    );
    const subStatusNoProgress = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "analyst_subthread_status_interrupted_no_progress",
    );
    const page1WithInterrupt = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "page_1_with_more_and_tip_interrupt");
    const alreadyCompletedSent = loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_sent",
    );

    const subThreadId = "22222222-3333-4444-5555-666666666666";
    // Real backend interrupted `/status` does not carry `run_id`, so canonical token is `run:1:cp_1`
    const realPausedSubStatus: ThreadStatusInfo = {
      ...subStatusPause1,
      run_id: null,
    };
    const expectedKey = `notice:needs_input:${THREAD_ID}:${subThreadId}:run:1:cp_1`;

    const seenKeys = new Set<string>();
    const baseClaimImpl = vi
      .mocked(h.backend.authorizeAndClaimDelivery)
      .getMockImplementation()!;
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockImplementation(
      async (req) => {
        if (seenKeys.has(req.delivery_key)) {
          return structuredClone(alreadyCompletedSent);
        }
        seenKeys.add(req.delivery_key);
        return await baseClaimImpl(req);
      },
    );

    // Call 1 (from `watch_snapshot_with_owed_threads`): returns `realPausedSubStatus` (`run:1:cp_1`) -> sends notice.
    // Calls 2..5 (all 4 attempts from `workflow_started_shape_3_needs_input`): return `subStatusNoProgress` (`progress: null`)!
    // Meanwhile `replayThreadMessages` has `interrupt_id: "int_tip_5"`.
    // Call 6+ (from `syncWatchReconnectState` on `workflow_started_shape_4_cleared`): returns `realPausedSubStatus` (`run:1:cp_1`) -> computes identical `expectedKey` and `I-6` deduplicates!
    let subStatusCalls = 0;
    vi.mocked(h.backend.getThreadStatus).mockImplementation(
      async (_uid, tid) => {
        if (tid === subThreadId) {
          subStatusCalls++;
          if (subStatusCalls >= 2 && subStatusCalls <= 5) {
            return subStatusNoProgress;
          }
          return realPausedSubStatus;
        }
        return baseIdle;
      },
    );
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(page1WithInterrupt),
    });
    vi.mocked(h.backend.getThreadWakeups).mockResolvedValue([]);

    vi.mocked(h.backend.watchThread).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        watchEvents.watch_snapshot_with_owed_threads!,
        watchEvents.workflow_started_shape_3_needs_input!,
        watchEvents.workflow_started_shape_4_cleared!,
      ]),
    });

    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 1,
    });
    await handle.promise;

    const needsInputClaims = h.backend.deliveryClaims.filter((c) =>
      c.delivery_key.startsWith(
        `notice:needs_input:${THREAD_ID}:${subThreadId}`,
      ),
    );
    expect(needsInputClaims).toHaveLength(2);
    expect(needsInputClaims[0]?.delivery_key).toBe(expectedKey);
    expect(needsInputClaims[1]?.delivery_key).toBe(expectedKey);
    expect(
      needsInputClaims.some((c) => c.delivery_key.includes("interrupt:")),
    ).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
  });
});
