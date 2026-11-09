import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I7RecoverResponse,
  ThreadStatusInfo,
} from "../src/backend-client.js";
import {
  createTestHarness,
  loadFixtureKey,
  makeCompletedTurnSseEvents,
  makeSseEventsFromFixture,
  RUN_ID,
  type TestHarness,
  THREAD_ID,
  USER_ID,
} from "./helpers.js";

describe("Unbounded `/messages/replay` Pagination, Reasoning/Subagent Filter & Tip Interrupt (§1.3.3, Codex r2 #1/#14, Grok r2 #5/#6)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("paginates backward using `history_page.first_turn_index`, skips `reasoning`, `subagent:*`, and pre-tool preamble chunks, and extracts tip `interrupt`", async () => {
    const page1 = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "page_1_with_more_and_tip_interrupt",
    );
    const page2 = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "page_2_oldest",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );

    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(page1),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(page2),
      });

    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: "run_tip_5",
      latest_turn_index: 5,
    });

    await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: "run_2",
      turnIndex: 2,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_replay_test",
      scopeKey: "dm:ou_alice",
    });

    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(2);
    expect(h.backend.replayThreadMessages).toHaveBeenNthCalledWith(
      1,
      USER_ID,
      THREAD_ID,
      undefined,
    );
    expect(h.backend.replayThreadMessages).toHaveBeenNthCalledWith(
      2,
      USER_ID,
      THREAD_ID,
      4,
    );

    const claimKeys = h.backend.deliveryClaims.map((c) => c.delivery_key);
    expect(claimKeys).toContain("interrupt:run_tip_5:int_tip_5");
    expect(claimKeys).toContain("fallback:run_2");

    const deliveredTexts = h.feishuAdapter.sentTexts.map((t) => t.text);
    expect(deliveredTexts).toEqual(
      expect.arrayContaining([
        expect.stringContaining("Which ticker?"),
        "Found turn 2 across pages!",
      ]),
    );
    // Reasoning content, pre-tool preamble, subagent:*, and task:* chunks were completely excluded (Codex r3 #10, Grok r3 #6)
    for (const t of deliveredTexts) {
      expect(t).not.toContain("HIDDEN_CHAIN_OF_THOUGHT");
      expect(t).not.toContain("INTERNAL_THINKING");
      expect(t).not.toContain("PRE_TOOL_PREAMBLE");
      expect(t).not.toContain("SUBAGENT_INTERNAL");
      expect(t).not.toContain("TASK_SUBAGENT_INTERNAL");
    }
  });

  it("does NOT use `replay_done.latest_run_id` for older `turnIndex`, and preserves pre-tool text when no post-tool text follows `tool_calls` (Codex r3 #8, Grok r3 #7)", async () => {
    const page1 = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "page_1_with_more_and_tip_interrupt",
    );
    const page2 = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "page_2_oldest",
    );

    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(page1),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(page2),
      });

    // Replay older `turnIndex: 2` with `runId: null`: `replay_done.latest_run_id` is `"run_tip_5"` (for turn 5),
    // so `deliverReplayFallback` resolves `run_2` from turn 2 (`turnToRunId.get(2)`) and must NOT claim `fallback:run_tip_5` (Codex r3 #8)
    const settled = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: null,
      turnIndex: 2,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_older_turn_no_run_id",
      scopeKey: "dm:ou_alice",
    });
    expect(settled).toBe(true);
    const claimKeys = h.backend.deliveryClaims.map((c) => c.delivery_key);
    expect(claimKeys).toContain("fallback:run_2");
    expect(claimKeys).not.toContain("fallback:run_tip_5");

    // When `tool_calls` is the final step of the turn with no subsequent text chunk, pre-tool text is preserved,
    // while `task:<id>` lane chunks are excluded (Grok r3 #7, `single_page_with_task_and_pre_tool`)
    const taskAndPreToolPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "single_page_with_task_and_pre_tool");
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(taskAndPreToolPage),
    });

    const settledPreTool = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_pre_tool_preserved",
      scopeKey: "dm:ou_alice",
    });
    expect(settledPreTool).toBe(true);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Replayed assistant output.",
    );
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).not.toContain(
      "SUBAGENT_TASK_TEXT_MUST_NOT_LEAK",
    );
  });

  it("retries `replay_unavailable` and does NOT send another turn's text or clear `fallback_pending` when target `turn_index` is missing (Codex r2 #14, Grok r2 #6)", async () => {
    const singlePage = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "default_single_page",
    );
    const unavailableItem = loadFixtureKey<{ event: string; data: unknown }>(
      "replay_events.json",
      "stub_replay_unavailable_event",
    );

    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(singlePage),
    });

    const beforeMissingTurnUpdates = h.backend.progressUpdates.length;
    await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: "run_99",
      turnIndex: 99,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_missing_turn",
      scopeKey: "dm:ou_alice",
    });

    // Must not deliver Turn 1's text when Turn 99 was requested, nor mark `fallback_pending: false`
    expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    expect(h.backend.deliveryClaims).toHaveLength(0);
    expect(h.backend.progressUpdates.length).toBe(beforeMissingTurnUpdates);

    // When `/messages/replay` emits `error` (`type: "replay_unavailable"`) on first try and succeeds on retry
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([unavailableItem]),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(singlePage),
      });

    await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: null,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_retry_unavailable",
      scopeKey: "dm:ou_alice",
    });

    expect(h.feishuAdapter.sentTexts.map((t) => t.text)).toEqual([
      "Replayed assistant output.",
    ]);
  });

  it("does NOT mark an in-flight `user_message` stub or a truncated page without `replay_done` as settled, and attaches to the live stream when `snapshot.active_runs` is present (Codex r4 #1)", async () => {
    await h.cleanup();
    h = createTestHarness({ busyThreadMaxWaitMs: 3_000 });

    const inProgressStubPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_turn_stub_without_snapshot");
    const truncatedPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "truncated_page_without_replay_done");
    const inProgressWithSnapshotPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_turn_with_snapshot");

    // 1. Bare `turn_separator` + un-stamped `user_message` stub (`in_progress_turn_stub_without_snapshot`) that never settles:
    // must return `false` and NOT mark `status: "completed", fallback_pending: false`
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(inProgressStubPage),
    });

    const settledStub = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_in_flight_stub",
      scopeKey: "dm:ou_alice",
    });

    expect(settledStub).toBe(false);
    expect(
      h.backend.progressUpdates.some(
        (u) => u.message_id === "m_in_flight_stub" && u.status === "completed",
      ),
    ).toBe(false);

    // 2. Truncated page that ends at EOF without `event: replay_done` (`truncated_page_without_replay_done`):
    // must retry and return `false` if `replay_done` is never received, never delivering partial page or clearing `fallback_pending`
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(truncatedPage),
    });

    const settledTruncated = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_truncated_replay",
      scopeKey: "dm:ou_alice",
    });

    expect(settledTruncated).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_truncated_replay" && u.status === "completed",
      ),
    ).toBe(false);

    // 3. `in_progress_turn_with_snapshot`: `event: snapshot` reports `active_runs` with `lane: "main"` and integer `cursor.last_event_id: 2` ->
    // attaches to `streamThreadMessages(USER_ID, THREAD_ID, { runId: RUN_ID, lastEventId: "2" })` (which only returns events after cursor 2 per `run_stream_reader.py:183`),
    // and upon `run_end` re-polls `/messages/replay` to deliver the complete settled answer!
    const afterCursorTailStream = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("conflict_and_sse_events.json", "sse_stream_after_cursor_tail");
    const settledAfterCursorPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "settled_turn_1_after_cursor_page");
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressWithSnapshotPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledAfterCursorPage),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(afterCursorTailStream),
    });

    const settledViaAttach = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_snapshot_attach",
      scopeKey: "dm:ou_alice",
    });

    expect(settledViaAttach).toBe(true);
    expect(h.backend.streamThreadMessages).toHaveBeenCalledWith(
      USER_ID,
      THREAD_ID,
      { runId: RUN_ID, lastEventId: "2" },
    );
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Prefix before cursor. Tail after cursor.",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_snapshot_attach" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("continues re-polling `/messages/replay` when the turn finishes between replay page generation (`messaging.py:1256`) and snapshot build (`:1289`), or when the attached live stream disconnects before `run_end` (Codex r5 #1)", async () => {
    const inProgressStubPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_turn_stub_without_snapshot");
    const inProgressWithSnapshotPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_turn_with_snapshot");
    const settledSinglePage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "default_single_page");
    const partialSseStream = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("conflict_and_sse_events.json", "sse_stream_partial_eof");

    // 1. Race between `messaging.py:1256` (in-progress turn stub) and `messaging.py:1289` (`snapshot` has no active run because the turn just finished):
    // Pass 1 sees `in_progress_turn_stub_without_snapshot`, backs off, and Pass 2 re-polls `/messages/replay` (`default_single_page`) and delivers the settled output!
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressStubPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledSinglePage),
      });

    const settledAfterRace = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_race_stub_then_settled",
      scopeKey: "dm:ou_alice",
    });

    expect(settledAfterRace).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(2);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Replayed assistant output.",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_race_stub_then_settled" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);

    // 2. Attached live stream from `snapshot.active_runs` disconnects before `run_end` (`sse_stream_partial_eof`):
    // must NOT permanently stop recovery; backs off and re-polls `/messages/replay` on Pass 2 to deliver the settled turn!
    const inProgressAttachDropPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_attach_drop_with_snapshot");
    const settledAttachDropPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "settled_attach_drop_page");
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressAttachDropPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledAttachDropPage),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(partialSseStream),
    });

    const settledAfterAttachDrop =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: "run_attach_drop_recover",
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_attach_drop_then_replay",
        scopeKey: "dm:ou_alice",
      });

    expect(settledAfterAttachDrop).toBe(true);
    expect(
      h.feishuAdapter.sentTexts.some((t) =>
        t.text.includes("TRUNCATED_PARTIAL_TEXT_DO_NOT_SEND"),
      ),
    ).toBe(false);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_attach_drop_then_replay" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("does NOT treat an in-progress `system` turn's `report_run_id` (`items.py:40-48`) as a terminal `run_id`, and re-polls `/messages/replay` until the background turn settles (Grok r5 #1)", async () => {
    const inProgressSystemPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_system_turn_with_report_run_id");
    const settledBackfillPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "backfilled_turn_2_single_page");

    // Pass 1 returns `in_progress_system_turn_with_report_run_id` (`report_run_id: "bg_backfill_2"`, no `run_id`, no assistant chunks);
    // Pass 2 returns settled `backfilled_turn_2_single_page` (`run_id: "bg_backfill_2"` with assistant chunk).
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressSystemPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledBackfillPage),
      });

    const settledBg = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: "bg_backfill_2",
      turnIndex: 2,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      isBackgroundRun: true,
    });

    expect(settledBg).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(2);
    expect(h.backend.deliveryClaims.map((c) => c.delivery_key)).toContain(
      "fallback:bg_backfill_2",
    );
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Backfilled wakeup report from disconnect window.",
    );
  });

  it("re-polls `/messages/replay` after `run_end` when attaching to live stream via snapshot cursor `last_event_id` (`run_stream_reader.py:183`) so pre-cursor text is never lost and a post-cursor `run_end`-only stream is never settled as empty (Codex r6 #1)", async () => {
    const inProgressWithSnapshotPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "in_progress_turn_with_snapshot");
    const settledAfterCursorPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "settled_turn_1_after_cursor_page");
    const afterCursorTailStream = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("conflict_and_sse_events.json", "sse_stream_after_cursor_tail");
    const afterCursorRunEndOnlyStream = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("conflict_and_sse_events.json", "sse_stream_after_cursor_run_end_only");
    const unavailableEvent = loadFixtureKey<{ event: string; data: unknown }>(
      "replay_events.json",
      "stub_replay_unavailable_event",
    );

    // 1. Live stream after `last_event_id: 2` only returns tail chunk `"Tail after cursor."` + `run_end`:
    // gateway must NOT deliver only `"Tail after cursor."`; it re-polls `/messages/replay` after `run_end` and delivers
    // the full settled turn `"Prefix before cursor. Tail after cursor."`!
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressWithSnapshotPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledAfterCursorPage),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(afterCursorTailStream),
    });

    const settledFullFromTail =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_cursor_tail_full",
        scopeKey: "dm:ou_alice",
      });

    expect(settledFullFromTail).toBe(true);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(
      "Prefix before cursor. Tail after cursor.",
    );

    // 2. Live stream after `last_event_id: 2` only returns `run_end` (all text was emitted before cursor 2):
    // if post-`run_end` replay is temporarily unavailable, gateway must NOT treat empty text as settled (`completed, fallback_pending: false`)!
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressWithSnapshotPage),
      })
      .mockResolvedValue({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([unavailableEvent]),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(afterCursorRunEndOnlyStream),
    });

    const unsettledWhenReplayUnavailable =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_cursor_run_end_only_unsettled",
        scopeKey: "dm:ou_alice",
      });

    expect(unsettledWhenReplayUnavailable).toBe(false);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_cursor_run_end_only_unsettled" &&
          u.fallback_pending === false,
      ),
    ).toBe(false);

    // 3. When post-`run_end` replay succeeds after a `run_end`-only cursor stream, the full pre-cursor answer is delivered!
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(inProgressWithSnapshotPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(settledAfterCursorPage),
      });
    vi.mocked(h.backend.streamThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(afterCursorRunEndOnlyStream),
    });

    const settledAfterRunEndOnly =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_cursor_run_end_only_settled",
        scopeKey: "dm:ou_alice",
      });

    expect(settledAfterRunEndOnly).toBe(true);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toBe(
      "Prefix before cursor. Tail after cursor.",
    );
  });

  it("does NOT prematurely settle an in-progress `/retry` attempt (`run_retry_2`) when `/messages/replay` has no `snapshot` and stamps the previous failed attempt (`run_failed_1`, `run_completed_at`) on `user_message` (`stubs.py:86-98`, `items.py:56-60`), and delivers the new attempt's text once settled (Grok r6 #1)", async () => {
    const retryrecoverableEvent = loadFixtureKey<{
      event: string;
      data: unknown;
    }>("conflict_and_sse_events.json", "sse_retry_recoverable");
    const retryPartialEofStream = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("conflict_and_sse_events.json", "sse_stream_retry_partial_eof");
    const retryInProgressStubPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >(
      "replay_events.json",
      "retry_in_progress_with_prev_failed_attempt_page",
    );
    const retrySettledNewAttemptPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "retry_settled_new_attempt_page");

    // Verify generated fixture has `user_message.run_id === "run_failed_1"`, `run_completed_at` set, no `snapshot`, and `replay_done.latest_run_id === "run_retry_2"`
    const stubUserMsg = retryInProgressStubPage.find(
      (e) => e.event === "user_message",
    )?.data as Record<string, unknown> | undefined;
    expect(stubUserMsg?.run_id).toBe("run_failed_1");
    expect(typeof stubUserMsg?.run_completed_at).toBe("string");
    expect(
      retryInProgressStubPage.some((e) => e.event === "snapshot"),
    ).toBe(false);

    // 1. End-to-end `/retry` disconnect:
    // First attempt emits `event: retry` (`recovery: "retry"`), gateway calls `POST /retry` which emits `metadata` (`run_id: "run_retry_2", turn_index: 1`)
    // and then disconnects at bare EOF before `run_end`.
    // In `deliverReplayFallback`: Pass 1 returns `retry_in_progress_with_prev_failed_attempt_page` (`user_message.run_id = "run_failed_1"`, no assistant text),
    // Pass 2 returns `retry_settled_new_attempt_page` (`user_message.run_id = "run_retry_2"`, `"Recovered answer from retry attempt 2!"`).
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture([
        {
          event: "metadata",
          data: {
            thread_id: THREAD_ID,
            run_id: "run_failed_1",
            turn_index: 1,
          },
        },
        {
          ...retryrecoverableEvent,
          data: {
            ...(retryrecoverableEvent.data as Record<string, unknown>),
            run_id: "run_failed_1",
          },
        },
      ]),
    });
    vi.mocked(h.backend.retryThreadTurn).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(retryPartialEofStream),
    });
    vi.mocked(h.backend.replayThreadMessages)
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(retryInProgressStubPage),
      })
      .mockResolvedValueOnce({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(retrySettledNewAttemptPage),
      });

    await h.feishuAdapter.emitInbound({
      platform: "feishu",
      messageId: "m_retry_prev_failed_stub",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatId: "oc_dm_alice",
      chatAddress: "feishu:@me",
      threadScopeKey: "dm:ou_alice",
      text: "Turn 1 prompt",
      attachments: [],
      rawPayload: {},
    });

    expect(h.backend.retryThreadTurn).toHaveBeenCalledTimes(1);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(2);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(
      "Recovered answer from retry attempt 2!",
    );
    expect(
      h.backend.deliveryClaims.some(
        (c) => c.delivery_key === "fallback:run_retry_2",
      ),
    ).toBe(true);
    expect(
      h.backend.deliveryClaims.some(
        (c) => c.delivery_key === "fallback:run_failed_1",
      ),
    ).toBe(false);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_retry_prev_failed_stub" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);

    // 2. If `/messages/replay` continues returning `retry_in_progress_with_prev_failed_attempt_page` across all passes,
    // `deliverReplayFallback` returns `false` and NEVER marks `status: "completed", fallback_pending: false`!
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValue({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(retryInProgressStubPage),
    });

    const settledWhileStillInProgress =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: "run_retry_2",
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_retry_still_running",
        scopeKey: "dm:ou_alice",
      });

    expect(settledWhileStillInProgress).toBe(false);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_retry_still_running" &&
          u.fallback_pending === false,
      ),
    ).toBe(false);
  });

  it("keeps `fallback_pending: true` and returns `false` when tip `interrupt` delivery in `deliverReplayFallback` fails even if `sent_to_current_count > 0` (Codex r7 #1)", async () => {
    const page1 = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "page_1_with_more_and_tip_interrupt",
    );
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(page1),
    }));

    vi.mocked(h.backend.getThreadStatus).mockResolvedValue({
      ...statusInterrupted,
      run_id: "run_tip_5",
      latest_turn_index: 5,
    });

    const baseProgressImpl = vi
      .mocked(h.backend.updateInboundProgress)
      .getMockImplementation()!;
    vi.mocked(h.backend.updateInboundProgress).mockImplementation(
      async (req) => {
        const res = await baseProgressImpl(req);
        // Simulate a turn where `/agent/send` already delivered progress text (`sent_to_current_count: 1`),
        // so fallback text delivery in `deliverReplayFallback` is skipped (`canDeliver === false`)
        if (req.status === undefined && !req.session_interrupt_update) {
          return {
            ...res,
            sent_to_current_count: 1,
            has_in_flight_or_unknown_current_text: false,
          };
        }
        return res;
      },
    );

    // 1st attempt: channel adapter is disconnected when sending the tip interrupt card (`executeDelivery` returns `failed`)
    h.feishuAdapter.connected = false;

    const settledFirstAttempt =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: "run_tip_5",
        turnIndex: 5,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_tip_int_fail",
        scopeKey: "dm:ou_alice",
      });

    expect(settledFirstAttempt).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_tip_int_fail" &&
          u.status === "completed" &&
          u.fallback_pending === true,
      ),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_tip_int_fail" && u.fallback_pending === false,
      ),
    ).toBe(false);

    // 2nd attempt: channel adapter is connected again -> tip interrupt is delivered and `fallback_pending: false` is written
    h.feishuAdapter.connected = true;

    const settledSecondAttempt =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: "run_tip_5",
        turnIndex: 5,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_tip_int_fail",
        scopeKey: "dm:ou_alice",
      });

    expect(settledSecondAttempt).toBe(true);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toContain("Which ticker?");
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_tip_int_fail" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("settles terminal turns that have no assistant text in `/messages/replay` (`interrupted`, `cancelled`, `error`, `completed` with `sent_to_current_count > 0`) on the first pass without 30-minute backoff spinning (`items.py:56-60`, `stopped.py:45-54`) (Grok r7 #1)", async () => {
    const interruptedEmptyPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "interrupted_empty_turn_page");
    const cancelledEmptyPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "cancelled_empty_turn_page");
    const errorEmptyPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "error_empty_turn_page");
    const completedEmptyPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "completed_empty_turn_page");
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );

    // 1. `interrupted` turn with no assistant text before the interrupt:
    // settles on the 1st replay call, delivers HITL prompt, and preserves `pending_interrupt`.
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(interruptedEmptyPage),
    });
    vi.mocked(h.backend.getThreadStatus).mockResolvedValueOnce({
      ...statusInterrupted,
      run_id: RUN_ID,
      latest_turn_index: 1,
    });

    const settledInterrupted =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_int_empty",
        scopeKey: "dm:ou_alice",
      });

    expect(settledInterrupted).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(1);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "Which market should I check first?",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.session_interrupt_update?.pending_interrupt?.interrupt_id ===
          "int_empty_turn_1",
      ),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_int_empty" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);

    // 2. `cancelled` turn (`stopped.py:45-54` `finish_reason: "stopped"` with empty content):
    // settles on the 1st replay call as `status: "failed", fallback_pending: false` and notifies the user.
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(cancelledEmptyPage),
    });

    const settledCancelled =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_cancel_empty",
        scopeKey: "dm:ou_alice",
      });

    expect(settledCancelled).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(2);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      'The run ended with status "cancelled".',
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_cancel_empty" &&
          u.status === "failed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);

    // 3. `error` turn (`items.py:264-333` `event: error` with no assistant text):
    // settles on the 1st replay call as `status: "failed", fallback_pending: false` and delivers error notice.
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(errorEmptyPage),
    });

    const settledError = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_error_empty",
      scopeKey: "dm:ou_alice",
    });

    expect(settledError).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(3);
    expect(h.feishuAdapter.sentTexts.at(-1)?.text).toContain(
      "Sandbox execution timed out",
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_error_empty" &&
          u.status === "failed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);

    // 4. `completed` turn with no assistant text in replay where `sent_to_current_count > 0`
    // (all output was delivered via `/agent/send` tool): settles on 1st replay call without spinning!
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(completedEmptyPage),
    });
    const baseProgressImpl = vi
      .mocked(h.backend.updateInboundProgress)
      .getMockImplementation()!;
    vi.mocked(h.backend.updateInboundProgress).mockImplementationOnce(
      async (req) => {
        const res = await baseProgressImpl(req);
        return {
          ...res,
          sent_to_current_count: 1,
          has_in_flight_or_unknown_current_text: false,
        };
      },
    );

    const settledCompletedEmpty =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_completed_empty_sent",
        scopeKey: "dm:ou_alice",
      });

    expect(settledCompletedEmpty).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(4);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_completed_empty_sent" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);

    // 5. `completed` turn with no assistant text in replay and `sent_to_current_count === 0`
    // (Grok r8 suspected #2, §1.3.3): when there is no assistant text to fallback-deliver and
    // `!has_in_flight_or_unknown_current_text`, settles immediately as `completed` / `fallback_pending: false`
    // so startup recovery does not spin forever on an empty completed turn.
    vi.mocked(h.backend.replayThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(completedEmptyPage),
    });

    const settledCompletedZeroSent =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        inboundMessageId: "m_completed_empty_zero_sent",
        scopeKey: "dm:ou_alice",
      });

    expect(settledCompletedZeroSent).toBe(true);
    expect(h.backend.replayThreadMessages).toHaveBeenCalledTimes(5);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_completed_empty_zero_sent" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("treats `executeDelivery` returning `partial` as terminal for text chunks (§1.2.4 #4 / §1.3.3), sends `notice:partial:{run_id}`, and if that notice fails keeps `fallback_pending: true` (`partial_notice_pending`) to retry only the notice without re-sending chunks", async () => {
    const defaultPage = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "default_single_page",
    );
    const chunk0Text = "A".repeat(h.feishuAdapter.maxChunkChars);
    const chunk1Text = "B".repeat(600);
    const twoChunkPage = defaultPage.map((evt) => {
      if (evt.event === "message_chunk") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            content: chunk0Text + chunk1Text,
          },
        };
      }
      return evt;
    });

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(twoChunkPage),
    }));

    const origSendTextChunk = h.feishuAdapter.sendTextChunk.bind(
      h.feishuAdapter,
    );
    let failSecondChunk = true;
    let failPartialNotice = true;
    vi.spyOn(h.feishuAdapter, "sendTextChunk").mockImplementation(
      async (params) => {
        if (failSecondChunk && params.chunkIndex === 1) {
          throw new Error("Feishu rate limit on chunk 2");
        }
        if (
          failPartialNotice &&
          params.text.includes("后续内容未能发送，请在网页查看：")
        ) {
          throw new Error("Transient error sending partial notice");
        }
        return origSendTextChunk(params);
      },
    );

    // 1st attempt: chunk 0 succeeds, chunk 1 fails -> `executeDelivery` records `status: "partial"`.
    // `deliverReplayFallback` then attempts `notice:partial:${RUN_ID}` (`"后续内容未能发送，请在网页查看：..."`).
    // Because `failPartialNotice === true`, the notice fails, so `deliverReplayFallback` returns `false`
    // and keeps `fallback_pending: true` with `raw_payload_patch: { partial_notice_pending: true }`.
    const settledFirst = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_partial_terminal",
      scopeKey: "dm:ou_alice",
    });

    expect(settledFirst).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(chunk0Text);
    expect(
      h.backend.deliveryUpdates.some(
        (u) =>
          u.delivery_key === `fallback:${RUN_ID}` && u.status === "partial",
      ),
    ).toBe(true);
    expect(
      h.backend.deliveryClaims.some(
        (c) =>
          c.delivery_key === `notice:partial:${RUN_ID}` && c.kind === "notice",
      ),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_partial_terminal" &&
          u.status === "completed" &&
          u.fallback_pending === true,
      ),
    ).toBe(true);

    // 2nd attempt via startup recovery:
    // `deliverReplayFallback` queries `I-6` for `fallback:${RUN_ID}`, which returns `already_completed` with `status: "partial"`,
    // so neither chunk 0 nor chunk 1 is re-sent! Only `notice:partial:${RUN_ID}` is retried, and once it succeeds, `fallback_pending: false` is written.
    failSecondChunk = false;
    failPartialNotice = false;
    const fallbackClaimsBeforeRecovery = h.backend.deliveryClaims.filter(
      (c) => c.delivery_key === `fallback:${RUN_ID}`,
    ).length;

    const recoverStartup = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_startup_populated",
    );
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: [
        {
          ...structuredClone(recoverStartup.inbound_events![0]!),
          platform: "feishu",
          message_id: "m_partial_terminal",
          dalton_user_id: USER_ID,
          chat_type: "dm",
          chat_address: "feishu:@me",
          thread_scope_key: "dm:ou_alice",
          status: "completed",
          thread_id: THREAD_ID,
          run_id: RUN_ID,
          turn_index: 1,
          fallback_pending: true,
          raw_payload: {},
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });

    await h.app.orchestrator.runStartupRecovery();

    const fallbackClaimsAfterRecovery = h.backend.deliveryClaims.filter(
      (c) => c.delivery_key === `fallback:${RUN_ID}`,
    ).length;
    expect(fallbackClaimsAfterRecovery).toBe(fallbackClaimsBeforeRecovery + 1);
    expect(h.feishuAdapter.sentTexts).toHaveLength(2);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(chunk0Text);
    expect(h.feishuAdapter.sentTexts[1]?.text).toBe(
      `后续内容未能发送，请在网页查看：https://app.ginlix.ai/chat/t/${THREAD_ID}`,
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_partial_terminal" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("retries terminal empty `cancelled` / `error` notice delivery during startup recovery and `/watch` reconnect sync when the initial notice failed (`status: 'failed', fallback_pending: true`) (Codex r8 #2)", async () => {
    const cancelledEmptyPage = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "cancelled_empty_turn_page");

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(cancelledEmptyPage),
    }));

    const origSendTextChunk = h.feishuAdapter.sendTextChunk.bind(
      h.feishuAdapter,
    );
    let failCancelNotice = true;
    vi.spyOn(h.feishuAdapter, "sendTextChunk").mockImplementation(
      async (params) => {
        if (failCancelNotice) {
          throw new Error("Feishu outage while sending cancelled notice");
        }
        return origSendTextChunk(params);
      },
    );

    // 1st attempt: cancelled empty turn's notice fails -> writes `status: "failed", fallback_pending: true` and returns `false`.
    const settledFirst = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: RUN_ID,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_cancel_notice_retry",
      scopeKey: "dm:ou_alice",
    });

    expect(settledFirst).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(0);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_cancel_notice_retry" &&
          u.status === "failed" &&
          u.fallback_pending === true,
      ),
    ).toBe(true);

    // 2nd attempt via `runStartupRecovery`: the inbound row has `status: "failed", fallback_pending: true`.
    // Recovery MUST NOT skip `status === "failed"` when `fallback_pending === true`; it retries `deliverReplayFallback`,
    // delivers the cancelled notice, and clears `fallback_pending: false`.
    failCancelNotice = false;
    const recoverStartup = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_startup_populated",
    );
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: [
        {
          ...structuredClone(recoverStartup.inbound_events![0]!),
          platform: "feishu",
          message_id: "m_cancel_notice_retry",
          dalton_user_id: USER_ID,
          chat_type: "dm",
          chat_address: "feishu:@me",
          thread_scope_key: "dm:ou_alice",
          status: "failed",
          thread_id: THREAD_ID,
          run_id: RUN_ID,
          turn_index: 1,
          fallback_pending: true,
          raw_payload: {},
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });

    await h.app.orchestrator.runStartupRecovery();

    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toContain(
      'The run ended with status "cancelled".',
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_cancel_notice_retry" &&
          u.status === "failed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("recovers and delivers `notice:partial:{run_id}` after gateway restart (in-memory state cleared, `sent_to_current_count > 0`) for both background runs and normal inbound SSE without re-sending text chunks (Codex r9 #1)", async () => {
    const defaultPage = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "default_single_page",
    );
    const recoverWatchReconnect = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect",
    );
    const chunk0Text = "A".repeat(h.feishuAdapter.maxChunkChars);
    const chunk1Text = "B".repeat(600);
    const bgRunId = "bg_partial_restart_1";
    const twoChunkBgPage = defaultPage.map((evt) => {
      if (evt.event === "user_message") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            run_id: bgRunId,
          },
        };
      }
      if (evt.event === "message_chunk") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            content: chunk0Text + chunk1Text,
            response_id: bgRunId,
          },
        };
      }
      if (evt.event === "replay_done") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            latest_run_id: bgRunId,
          },
        };
      }
      return evt;
    });

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(twoChunkBgPage),
    }));

    const origSendTextChunk = h.feishuAdapter.sendTextChunk.bind(
      h.feishuAdapter,
    );
    let failSecondChunk = true;
    let failPartialNotice = true;
    vi.spyOn(h.feishuAdapter, "sendTextChunk").mockImplementation(
      async (params) => {
        if (failSecondChunk && params.chunkIndex === 1) {
          throw new Error("Feishu rate limit on chunk 2");
        }
        if (
          failPartialNotice &&
          params.text.includes("后续内容未能发送，请在网页查看：")
        ) {
          throw new Error("Transient error sending partial notice");
        }
        return origSendTextChunk(params);
      },
    );

    // 1. Background run replay fallback before restart:
    // chunk 0 succeeds, chunk 1 fails (`status: "partial"` -> increments `sent_to_current_count` to 1),
    // and `notice:partial:bg_partial_restart_1` also fails -> writes `background_run: { status: "completed", fallback_pending: true }`.
    const bgSettledBeforeRestart =
      await h.app.orchestrator.deliverReplayFallback({
        userId: USER_ID,
        threadId: THREAD_ID,
        runId: bgRunId,
        turnIndex: 1,
        platform: "feishu",
        targetAddress: "feishu:@me",
        scopeKey: "dm:ou_alice",
        isBackgroundRun: true,
      });

    expect(bgSettledBeforeRestart).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(chunk0Text);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.background_run?.run_id === bgRunId &&
          u.background_run?.status === "completed" &&
          u.background_run?.fallback_pending === true,
      ),
    ).toBe(true);

    // Simulate gateway restart: `updateInboundProgress` returns `sent_to_current_count: 1` (since `partial` incremented it in DB),
    // and `authorizeAndClaimDelivery` returns `already_completed_partial` (`status: "partial"`) for `fallback:${bgRunId}`.
    failSecondChunk = false;
    failPartialNotice = false;

    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: [],
      background_runs: [
        {
          ...structuredClone(recoverWatchReconnect.background_runs![0]!),
          run_id: bgRunId,
          thread_id: THREAD_ID,
          dalton_user_id: USER_ID,
          platform: "feishu",
          target_address: "feishu:@me",
          turn_index: 1,
          status: "completed",
          fallback_pending: true,
          sent_to_current_count: 1,
        },
      ],
      thread_watches: [],
      automation_runs: [],
    });

    const bgFallbackClaimsBefore = h.backend.deliveryClaims.filter(
      (c) => c.delivery_key === `fallback:${bgRunId}`,
    ).length;

    await h.app.orchestrator.runStartupRecovery();

    const bgFallbackClaimsAfter = h.backend.deliveryClaims.filter(
      (c) => c.delivery_key === `fallback:${bgRunId}`,
    ).length;
    expect(bgFallbackClaimsAfter).toBe(bgFallbackClaimsBefore + 1);
    expect(h.feishuAdapter.sentTexts).toHaveLength(2);
    expect(h.feishuAdapter.sentTexts[1]?.text).toBe(
      `后续内容未能发送，请在网页查看：https://app.ginlix.ai/chat/t/${THREAD_ID}`,
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.background_run?.run_id === bgRunId &&
          u.background_run?.status === "completed" &&
          u.background_run?.fallback_pending === false,
      ),
    ).toBe(true);

    // 2. Normal inbound SSE (`handleInboundEvent` -> `consumeTurnSseStream`) when `partial` notice fails:
    // keeps `status: "completed", fallback_pending: true` without any `partial_notice_pending` payload field!
    failSecondChunk = true;
    failPartialNotice = true;
    vi.mocked(h.backend.appendThreadMessages).mockResolvedValueOnce({
      status: 200,
      isSse: true,
      events: makeCompletedTurnSseEvents({
        threadId: THREAD_ID,
        runId: "run_sse_partial_1",
        turnIndex: 1,
        content: chunk0Text + chunk1Text,
      }),
    });

    await h.app.orchestrator.handleInboundEvent({
      platform: "feishu",
      messageId: "m_sse_partial_1",
      platformUserId: "ou_alice",
      chatType: "dm",
      chatId: "ou_alice",
      chatAddress: "feishu:@me",
      threadScopeKey: "dm:ou_alice",
      text: "Trigger long SSE reply",
      attachments: [],
      rawPayload: {},
    });

    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_sse_partial_1" &&
          u.status === "completed" &&
          u.fallback_pending === true &&
          u.raw_payload_patch === undefined,
      ),
    ).toBe(true);
  });

  it("does NOT clear `fallback_pending` via `watch_snapshot.recent_report_back_run_ids` when a background run's `partial` notice fails after restart, and delivers `notice:partial:{run_id}` via the watch snapshot path once the notice succeeds (Grok r10 High #1)", async () => {
    const defaultPage = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "default_single_page",
    );
    const recoverWatchReconnect = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect",
    );
    const watchEvents = loadFixtureKey<
      Record<string, { event: string; data: Record<string, unknown> }>
    >("status_liveness_wakeups_watch.json", "watch_events");
    const watchSnapshotIdle = watchEvents.watch_snapshot_with_recent_settled!;

    const chunk0Text = "A".repeat(h.feishuAdapter.maxChunkChars);
    const chunk1Text = "B".repeat(600);
    const bgRunId = "bg_snap_partial_retry_1";
    const twoChunkBgPage = defaultPage.map((evt) => {
      if (evt.event === "user_message") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            run_id: bgRunId,
          },
        };
      }
      if (evt.event === "message_chunk") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            content: chunk0Text + chunk1Text,
            response_id: bgRunId,
          },
        };
      }
      if (evt.event === "replay_done") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            latest_run_id: bgRunId,
          },
        };
      }
      return evt;
    });

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(twoChunkBgPage),
    }));

    const origSendTextChunk = h.feishuAdapter.sendTextChunk.bind(
      h.feishuAdapter,
    );
    let failSecondChunk = true;
    let failPartialNotice = true;
    vi.spyOn(h.feishuAdapter, "sendTextChunk").mockImplementation(
      async (params) => {
        if (failSecondChunk && params.chunkIndex === 1) {
          throw new Error("Feishu rate limit on chunk 2");
        }
        if (
          failPartialNotice &&
          params.text.includes("后续内容未能发送，请在网页查看：")
        ) {
          throw new Error("Transient error sending partial notice");
        }
        return origSendTextChunk(params);
      },
    );

    // 1. Before restart: background run `bg_snap_partial_retry_1` delivers chunk 0, fails chunk 1 (`status: "partial"`),
    // and `notice:partial:bg_snap_partial_retry_1` fails (`fallback_pending: true`).
    const settledInitial = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: bgRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      isBackgroundRun: true,
    });
    expect(settledInitial).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(chunk0Text);

    // 2. Gateway restarts while `failPartialNotice` is STILL `true`:
    // Startup recovery (`recoverSingleBackgroundRun`) tries `notice:partial:bg_snap_partial_retry_1` and fails again,
    // then `/watch` connects and `watch_snapshot` arrives with `recent_report_back_run_ids: [bgRunId]`.
    // The snapshot path MUST ALSO see `status === "partial"` from `I-6`, attempt the notice, and on failure keep `fallback_pending: true`!
    let watchAttempt = 0;
    vi.mocked(h.backend.watchThread).mockImplementation(
      async (_uid, _tid, signal) => {
        watchAttempt++;
        if (watchAttempt === 1) {
          // 1st watch connection: `failPartialNotice` is still `true` during `watch_snapshot`
          return {
            status: 200,
            isSse: true,
            events: makeSseEventsFromFixture([
              {
                event: "watch_snapshot",
                data: {
                  ...watchSnapshotIdle.data,
                  recent_report_back_run_ids: [bgRunId],
                  pending_report_back: false,
                },
              },
            ]),
          };
        }
        // Before 2nd watch connection's `watch_snapshot`, notice delivery recovers (`failPartialNotice = false`)
        failPartialNotice = false;
        return {
          status: 200,
          isSse: true,
          events: (async function* () {
            yield {
              event: "watch_snapshot",
              data: JSON.stringify({
                ...watchSnapshotIdle.data,
                recent_report_back_run_ids: [bgRunId],
                pending_report_back: false,
              }),
            };
            await new Promise<void>((resolve) => {
              if (signal?.aborted) return resolve();
              signal?.addEventListener("abort", () => resolve(), {
                once: true,
              });
            });
          })(),
        };
      },
    );

    vi.mocked(h.backend.recoverState)
      .mockResolvedValueOnce({
        mode: "startup",
        inbound_events: [],
        background_runs: [
          {
            ...structuredClone(recoverWatchReconnect.background_runs![0]!),
            run_id: bgRunId,
            thread_id: THREAD_ID,
            dalton_user_id: USER_ID,
            platform: "feishu",
            target_address: "feishu:@me",
            turn_index: 1,
            status: "completed",
            fallback_pending: true,
            sent_to_current_count: 1,
          },
        ],
        thread_watches: [],
        automation_runs: [],
      })
      .mockResolvedValue({
        mode: "watch_reconnect",
        inbound_events: [],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      });

    // Run startup recovery (where `failPartialNotice` is still `true` -> `fallback_pending` remains `true`)
    await h.app.orchestrator.runStartupRecovery();
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.background_run?.run_id === bgRunId &&
          u.background_run?.fallback_pending === false,
      ),
    ).toBe(false);

    // Start `/watch`: 1st `watch_snapshot` runs with `failPartialNotice === true` (must NOT write `fallback_pending: false`),
    // then reconnects and 2nd `watch_snapshot` runs with `failPartialNotice === false` -> delivers `notice:partial:${bgRunId}` and closes watch!
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
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(chunk0Text);
    expect(h.feishuAdapter.sentTexts[1]?.text).toBe(
      `后续内容未能发送，请在网页查看：https://app.ginlix.ai/chat/t/${THREAD_ID}`,
    );
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.background_run?.run_id === bgRunId &&
          u.background_run?.status === "completed" &&
          u.background_run?.fallback_pending === false,
      ),
    ).toBe(true);
  });

  it("does NOT falsely send `notice:partial:{run_id}` when a background turn's full text is delivered as `status: 'sent'` via `pendingReplayFallbacks` before `syncWatchReconnectState` processes the snapshot's `background_runs` row (Codex r10 #2)", async () => {
    const defaultPage = loadFixtureKey<Array<{ event: string; data: unknown }>>(
      "replay_events.json",
      "default_single_page",
    );
    const recoverWatchReconnect = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_watch_reconnect",
    );
    const bgRunId = "bg_full_sent_no_partial_notice";
    const bgPage = defaultPage.map((evt) => {
      if (evt.event === "user_message") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            run_id: bgRunId,
          },
        };
      }
      if (evt.event === "message_chunk") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            content: "Complete background report delivered in full.",
            response_id: bgRunId,
          },
        };
      }
      if (evt.event === "replay_done") {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            latest_run_id: bgRunId,
          },
        };
      }
      return evt;
    });

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(bgPage),
    }));

    // Start an active watch so `deliverReplayFallback` registers in `watchHandle.pendingReplayFallbacks` when chunk 0 fails
    const handle = h.app.orchestrator.ensureThreadWatch({
      threadId: THREAD_ID,
      userId: USER_ID,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      generation: 1,
      lastSeenTurnIndex: 0,
    });

    // 1. Initial attempt: adapter is temporarily disconnected (`status: "failed"`, `sent_to_current_count: 0`),
    // so `bgRunId` is stored in `handle.pendingReplayFallbacks` and `dalton_channel_background_runs` (`status: "completed", fallback_pending: true`).
    h.feishuAdapter.connected = false;
    const firstAttemptOk = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: bgRunId,
      turnIndex: 1,
      platform: "feishu",
      targetAddress: "feishu:@me",
      scopeKey: "dm:ou_alice",
      isBackgroundRun: true,
    });
    expect(firstAttemptOk).toBe(false);
    expect(handle.pendingReplayFallbacks?.has(bgRunId)).toBe(true);

    // 2. Adapter reconnects. `evaluateWatchCloseConditions` triggers `syncWatchReconnectState(handle)`:
    // `recoverState({ mode: "watch_reconnect" })` returns the snapshot `background_runs` row (`status: "completed", fallback_pending: true`).
    // Inside `syncWatchReconnectState`:
    //   (a) `handle.pendingReplayFallbacks` runs first and delivers the FULL text (`fallback:bg_full_sent_no_partial_notice` -> `status: "sent"`, `sent_to_current_count` becomes `1`).
    //   (b) `recoverSingleBackgroundRun(bg)` runs next: it queries `I-6` for `fallback:bg_full_sent_no_partial_notice`,
    //       which returns `already_completed` with `status: "sent"`, so it MUST NOT send `notice:partial:bg_full_sent_no_partial_notice`!
    h.feishuAdapter.connected = true;
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "watch_reconnect",
      inbound_events: [],
      background_runs: [
        {
          ...structuredClone(recoverWatchReconnect.background_runs![0]!),
          run_id: bgRunId,
          thread_id: THREAD_ID,
          dalton_user_id: USER_ID,
          platform: "feishu",
          target_address: "feishu:@me",
          turn_index: 1,
          status: "completed",
          fallback_pending: true,
          sent_to_current_count: 0,
        },
      ],
      thread_watches: [],
      automation_runs: [],
    });

    const closed = await h.app.orchestrator.evaluateWatchCloseConditions(
      USER_ID,
      THREAD_ID,
    );
    expect(closed).toBe(true);

    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(
      "Complete background report delivered in full.",
    );
    expect(
      h.backend.deliveryClaims.some(
        (c) => c.delivery_key === `notice:partial:${bgRunId}`,
      ),
    ).toBe(false);
    await handle.promise;
  });

  it("delivers Turn A's pending `notice:partial:{run_A}` during recovery even when a subsequent Turn B on the same thread is paused with a tip `interrupt` (Codex r10 #1)", async () => {
    const page1WithInterrupt = loadFixtureKey<
      Array<{ event: string; data: unknown }>
    >("replay_events.json", "page_1_with_more_and_tip_interrupt");
    const statusInterrupted = loadFixtureKey<ThreadStatusInfo>(
      "status_liveness_wakeups_watch.json",
      "status_full_interrupted",
    );
    const recoverStartup = loadFixtureKey<I7RecoverResponse>(
      "i7_progress_and_reconcile.json",
      "recover_startup_populated",
    );

    const chunk0Text = "A".repeat(h.feishuAdapter.maxChunkChars);
    const chunk1Text = "B".repeat(600);
    const runA = "run_4";

    // Build replay page where Turn 4 (`run_4`, Turn A) has 2-chunk assistant text,
    // and Turn 5 (`run_tip_5`, Turn B) is at the tip with `event: interrupt` (`int_tip_5`).
    const turnAPartialAndTurnBInterruptPage = page1WithInterrupt.map((evt) => {
      if (
        evt.event === "message_chunk" &&
        (evt.data as Record<string, unknown>).turn_index === 4
      ) {
        return {
          ...evt,
          data: {
            ...(evt.data as Record<string, unknown>),
            content: chunk0Text + chunk1Text,
            response_id: runA,
          },
        };
      }
      return evt;
    });

    vi.mocked(h.backend.replayThreadMessages).mockImplementation(async () => ({
      status: 200,
      isSse: true,
      events: makeSseEventsFromFixture(turnAPartialAndTurnBInterruptPage),
    }));

    const origSendTextChunk = h.feishuAdapter.sendTextChunk.bind(
      h.feishuAdapter,
    );
    let failSecondChunk = true;
    let failPartialNotice = true;
    vi.spyOn(h.feishuAdapter, "sendTextChunk").mockImplementation(
      async (params) => {
        if (failSecondChunk && params.chunkIndex === 1) {
          throw new Error("Feishu rate limit on Turn A chunk 2");
        }
        if (
          failPartialNotice &&
          params.text.includes("后续内容未能发送，请在网页查看：")
        ) {
          throw new Error("Transient error sending Turn A partial notice");
        }
        return origSendTextChunk(params);
      },
    );

    // 1. Turn A (`turnIndex: 4, runId: "run_4"`) delivers chunk 0, fails chunk 1 (`fallback:run_4` -> `status: "partial"`),
    // and fails `notice:partial:run_4` -> `m_turn_a` stays `status: "completed", fallback_pending: true`.
    const settledTurnAFirst = await h.app.orchestrator.deliverReplayFallback({
      userId: USER_ID,
      threadId: THREAD_ID,
      runId: runA,
      turnIndex: 4,
      platform: "feishu",
      targetAddress: "feishu:@me",
      inboundMessageId: "m_turn_a",
      scopeKey: "dm:ou_alice",
    });
    expect(settledTurnAFirst).toBe(false);
    expect(h.feishuAdapter.sentTexts).toHaveLength(1);
    expect(h.feishuAdapter.sentTexts[0]?.text).toBe(chunk0Text);

    // 2. Later on the same thread, Turn B (`turnIndex: 5, runId: "run_tip_5"`) pauses with an active tip `interrupt`
    // (`/status` is `interrupted`, `run_id: "run_tip_5", latest_turn_index: 5`).
    // When startup recovery runs for Turn A (`m_turn_a`), `deliverReplayFallback` delivers Turn B's tip interrupt (`tipInterruptDelivered = true`)
    // AND still checks `I-6` for `fallback:run_4` (`already_completed_partial`), delivering `notice:partial:run_4`!
    failSecondChunk = false;
    failPartialNotice = false;
    vi.mocked(h.backend.getThreadStatus).mockResolvedValue({
      ...statusInterrupted,
      run_id: "run_tip_5",
      latest_turn_index: 5,
    });
    vi.mocked(h.backend.recoverState).mockResolvedValueOnce({
      mode: "startup",
      inbound_events: [
        {
          ...structuredClone(recoverStartup.inbound_events![0]!),
          platform: "feishu",
          message_id: "m_turn_a",
          dalton_user_id: USER_ID,
          chat_type: "dm",
          chat_address: "feishu:@me",
          thread_scope_key: "dm:ou_alice",
          status: "completed",
          thread_id: THREAD_ID,
          run_id: runA,
          turn_index: 4,
          fallback_pending: true,
          sent_to_current_count: 1,
          raw_payload: {},
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    });

    await h.app.orchestrator.runStartupRecovery();

    const sentTextsAfter = h.feishuAdapter.sentTexts.map((t) => t.text);
    expect(sentTextsAfter).toContain(
      `后续内容未能发送，请在网页查看：https://app.ginlix.ai/chat/t/${THREAD_ID}`,
    );
    expect(
      sentTextsAfter.some((t) => t.includes("Which ticker?")),
    ).toBe(true);
    expect(
      h.backend.progressUpdates.some(
        (u) =>
          u.message_id === "m_turn_a" &&
          u.status === "completed" &&
          u.fallback_pending === false,
      ),
    ).toBe(true);
  });
});
