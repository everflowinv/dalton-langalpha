import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { vi } from "vitest";
import type {
  BackendClient,
  DispatchLivenessRunInfo,
  I1RegisterAndResolveRequest,
  I1RegisterAndResolveResponse,
  I2BindCodeConsumeRequest,
  I2BindCodeConsumeResponse,
  I3SessionCommandRequest,
  I3SessionCommandResponse,
  I4StateResponse,
  I5SettingsPutRequest,
  I5SettingsPutResult,
  I6AuthorizeAndClaimRequest,
  I6AuthorizeAndClaimResponse,
  I7DeliveriesProgressRequest,
  I7DeliveriesProgressResponse,
  I7InboundProgressRequest,
  I7InboundProgressResponse,
  I7RecoverResponse,
  I7SteersReconcileRequest,
  I7SteersReconcileResponse,
  I8AutomationRunFinishRequest,
  I8AutomationRunFinishResponse,
  I8AutomationRunStartRequest,
  I8AutomationRunStartResponse,
  I9WeixinStateGetResponse,
  I9WeixinStatePutRequest,
  I9WeixinStatePutResponse,
  SseEvent,
  SseOrErrorResponse,
  ThreadMessageDispatchPayload,
  ThreadRetryPayload,
  ThreadStatusInfo,
} from "../src/backend-client.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { type GatewayConfig, loadConfig } from "../src/config.js";
import { createGatewayApp, type GatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
export const FIXTURES_DIR = resolve(__dirname, "fixtures");

const fixtureFileCache = new Map<string, Record<string, unknown>>();

export function loadFixture<T = Record<string, unknown>>(filename: string): T {
  let cached = fixtureFileCache.get(filename);
  if (!cached) {
    const raw = readFileSync(join(FIXTURES_DIR, filename), "utf8");
    cached = JSON.parse(raw) as Record<string, unknown>;
    fixtureFileCache.set(filename, cached);
  }
  return structuredClone(cached) as T;
}

export function loadFixtureKey<T>(filename: string, key: string): T {
  const fileObj = loadFixture<Record<string, unknown>>(filename);
  if (!(key in fileObj)) {
    throw new Error(`Fixture key "${key}" not found in ${filename}`);
  }
  return structuredClone(fileObj[key]) as T;
}

export const TEST_SERVICE_TOKEN =
  "0123456789abcdef0123456789abcdef_valid_token";
export const USER_ID = "user_alice_123";
export const THREAD_ID = "11111111-2222-3333-4444-555555555555";
export const RUN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
export const WORKSPACE_ID = "99999999-8888-7777-6666-555555555555";
export const HOME_WS_ID = "00000000-0000-0000-0000-000000000001";

export async function* makeSseEvents(
  events: SseEvent[],
): AsyncGenerator<SseEvent, void, unknown> {
  for (const ev of events) {
    yield ev;
  }
}

export async function* makeSseEventsFromFixture(
  items: Array<{ event: string; data: unknown }>,
): AsyncGenerator<SseEvent, void, unknown> {
  for (const item of items) {
    yield {
      event: item.event,
      data:
        typeof item.data === "string" ? item.data : JSON.stringify(item.data),
    };
  }
}

export function makeCompletedTurnSseEvents(params: {
  threadId?: string;
  runId?: string;
  turnIndex?: number;
  content: string;
}): AsyncGenerator<SseEvent, void, unknown> {
  const attachedTemplate = loadFixtureKey<
    Array<{ event: string; data: Record<string, unknown> }>
  >("conflict_and_sse_events.json", "sse_stream_existing_thread");
  const threadId = params.threadId ?? THREAD_ID;
  const runId = params.runId ?? RUN_ID;
  const turnIndex = params.turnIndex ?? 1;
  return makeSseEventsFromFixture(
    attachedTemplate.map((item) => {
      if (
        item.event === "metadata" ||
        item.event === "turn_start" ||
        item.event === "run_end"
      ) {
        return {
          event: item.event,
          data: {
            ...item.data,
            thread_id: threadId,
            run_id: runId,
            turn_index: turnIndex,
          },
        };
      }
      if (item.event === "message_chunk") {
        return {
          event: item.event,
          data: {
            ...item.data,
            content: params.content,
          },
        };
      }
      return item;
    }),
  );
}

export async function* makeAbortableOpenWatchSse(
  signal?: AbortSignal,
): AsyncGenerator<SseEvent, void, unknown> {
  if (signal?.aborted) {
    return;
  }
  await new Promise<void>((resolveWait) => {
    if (!signal) {
      resolveWait();
      return;
    }
    signal.addEventListener("abort", () => resolveWait(), { once: true });
  });
}

export type MockBackend = BackendClient & {
  readonly progressUpdates: I7InboundProgressRequest[];
  readonly deliveryClaims: I6AuthorizeAndClaimRequest[];
  readonly deliveryUpdates: I7DeliveriesProgressRequest[];
  readonly appendCalls: Array<{
    userId: string;
    threadId: string;
    payload: ThreadMessageDispatchPayload;
  }>;
  readonly createCalls: Array<{
    userId: string;
    payload: ThreadMessageDispatchPayload;
  }>;
  readonly retryCalls: Array<{
    userId: string;
    threadId: string;
    payload: ThreadRetryPayload;
  }>;
};

export function createMockBackend(): MockBackend {
  const defaultState = loadFixtureKey<I4StateResponse>(
    "i4_state.json",
    "default_state",
  );
  const defaultI1Resolved = loadFixtureKey<I1RegisterAndResolveResponse>(
    "i1_register_and_resolve.json",
    "default_resolved",
  );
  const defaultI2BindSuccess = loadFixtureKey<I2BindCodeConsumeResponse>(
    "i2_bind_code_consume.json",
    "bind_success",
  );
  const defaultI3WsList = loadFixtureKey<I3SessionCommandResponse>(
    "i3_session_command.json",
    "workspace_list",
  );
  const defaultI3WsSwitch = loadFixtureKey<I3SessionCommandResponse>(
    "i3_session_command.json",
    "workspace_switch_dm",
  );
  const defaultI3NewSession = loadFixtureKey<I3SessionCommandResponse>(
    "i3_session_command.json",
    "new_session",
  );
  const defaultI3StopSession = loadFixtureKey<I3SessionCommandResponse>(
    "i3_session_command.json",
    "stop_session",
  );
  const defaultI5PutSuccess = loadFixtureKey<
    NonNullable<I5SettingsPutResult["data"]>
  >("i5_settings_put.json", "put_success");
  const defaultI6Claimed = loadFixtureKey<I6AuthorizeAndClaimResponse>(
    "i6_authorize_and_claim.json",
    "claimed_feishu_dm",
  );
  const defaultI6AlreadyCompletedSent =
    loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_sent",
    );
  const defaultI6AlreadyCompletedPartial =
    loadFixtureKey<I6AuthorizeAndClaimResponse>(
      "i6_authorize_and_claim.json",
      "already_completed_partial",
    );
  const defaultI7DeliverySent = loadFixtureKey<I7DeliveriesProgressResponse>(
    "i7_progress_and_reconcile.json",
    "delivery_progress_sent",
  );
  const defaultI7InboundProgress = loadFixtureKey<I7InboundProgressResponse>(
    "i7_progress_and_reconcile.json",
    "inbound_progress_default",
  );
  const defaultI7ReconcileConsumed = loadFixtureKey<I7SteersReconcileResponse>(
    "i7_progress_and_reconcile.json",
    "reconcile_consumed",
  );
  const defaultI8Register = loadFixtureKey<I8AutomationRunStartResponse>(
    "i8_automation_runs.json",
    "register_dm",
  );
  const defaultI8FinishClaim = loadFixtureKey<I8AutomationRunFinishResponse>(
    "i8_automation_runs.json",
    "finish_claim_finishing",
  );
  const defaultI9Unconfigured = loadFixtureKey<I9WeixinStateGetResponse>(
    "i9_weixin_state.json",
    "get_unconfigured",
  );
  const defaultI9PutSaved = loadFixtureKey<I9WeixinStatePutResponse>(
    "i9_weixin_state.json",
    "put_saved",
  );
  const defaultReplaySinglePage = loadFixtureKey<
    Array<{ event: string; data: Record<string, unknown> }>
  >("replay_events.json", "default_single_page");
  const defaultStatusIdle = loadFixtureKey<ThreadStatusInfo>(
    "status_liveness_wakeups_watch.json",
    "status_full_idle",
  );
  const defaultStatusInterrupted = loadFixtureKey<ThreadStatusInfo>(
    "status_liveness_wakeups_watch.json",
    "status_full_interrupted",
  );
  const defaultLivenessCompletedRun = loadFixtureKey<{
    runs: DispatchLivenessRunInfo[];
  }>("status_liveness_wakeups_watch.json", "dispatches_liveness_completed")
    .runs[0]!;

  const registerAndResolveInbound = vi.fn(
    async (
      req: I1RegisterAndResolveRequest,
    ): Promise<I1RegisterAndResolveResponse> => ({
      ...structuredClone(defaultI1Resolved),
      request_key: req.request_key,
      raw_payload: req.raw_payload ?? {},
    }),
  );

  const authorizeAndClaimDelivery = vi.fn(
    async (
      req: I6AuthorizeAndClaimRequest,
    ): Promise<I6AuthorizeAndClaimResponse> => {
      const matchedDiscordSource = (() => {
        if (req.inbound_message_id) {
          const foundIn = [...registerAndResolveInbound.mock.calls]
            .map(([inReq]) => inReq)
            .reverse()
            .find(
              (inReq) =>
                inReq.message_id === req.inbound_message_id &&
                (!req.platform || inReq.platform === req.platform),
            );
          if (foundIn && foundIn.platform === "discord") {
            return {
              platform: foundIn.platform,
              targetAddress: foundIn.chat_address,
              threadScopeKey: foundIn.thread_scope_key,
            };
          }
        }
        if (req.run_id || req.thread_id) {
          for (const [inReq] of [...registerAndResolveInbound.mock.calls].reverse()) {
            if (inReq.platform !== "discord") continue;
            if (req.platform && inReq.platform !== req.platform) continue;
            const progForMsg = [...updateInboundProgress.mock.calls]
              .map(([u]) => u)
              .reverse()
              .find(
                (u) =>
                  u.platform === inReq.platform &&
                  u.message_id === inReq.message_id &&
                  (u.run_id || u.thread_id),
              );
            const effRunId = progForMsg?.run_id ?? RUN_ID;
            const effThreadId = progForMsg?.thread_id ?? THREAD_ID;
            if (
              (!req.run_id || req.run_id === effRunId) &&
              (!req.thread_id || req.thread_id === effThreadId)
            ) {
              return {
                platform: inReq.platform,
                targetAddress: inReq.chat_address,
                threadScopeKey: inReq.thread_scope_key,
              };
            }
          }
          for (const [upd] of [...updateInboundProgress.mock.calls].reverse()) {
            const tw = upd.thread_watch;
            if (
              tw &&
              tw.platform === "discord" &&
              (!req.thread_id || tw.thread_id === req.thread_id)
            ) {
              return {
                platform: tw.platform,
                targetAddress: tw.target_address,
                threadScopeKey: tw.scope_key,
              };
            }
            const bg = upd.background_run;
            if (
              bg &&
              bg.platform === "discord" &&
              (!req.run_id || bg.run_id === req.run_id) &&
              (!req.thread_id || bg.thread_id === req.thread_id)
            ) {
              const matchingW = [...updateInboundProgress.mock.calls]
                .map(([u]) => u.thread_watch)
                .reverse()
                .find((w) => w && w.thread_id === bg.thread_id);
              const matchingIn = [...registerAndResolveInbound.mock.calls]
                .map(([inReq]) => inReq)
                .reverse()
                .find((inReq) => inReq.platform === "discord");
              return {
                platform: bg.platform,
                targetAddress: bg.target_address,
                threadScopeKey:
                  matchingW?.scope_key ??
                  matchingIn?.thread_scope_key ??
                  bg.target_address,
              };
            }
          }
        }
        return null;
      })();

      const plat = (
        req.platform ??
        req.target?.split(":")[0] ??
        matchedDiscordSource?.platform ??
        "feishu"
      ).toLowerCase();
      const targetAddr =
        req.target ?? matchedDiscordSource?.targetAddress ?? `${plat}:@me`;
      const isCurrent =
        !req.target ||
        targetAddr === "feishu:@me" ||
        (matchedDiscordSource !== null &&
          targetAddr === matchedDiscordSource.targetAddress);
      const resolvedThreadScopeKey =
        isCurrent && matchedDiscordSource?.threadScopeKey
          ? matchedDiscordSource.threadScopeKey
          : null;
      const lastUpdate = [...updateDeliveryProgress.mock.calls]
        .map(([u]) => u)
        .reverse()
        .find(
          (u) => u.delivery_key === req.delivery_key && u.status !== undefined,
        );
      if (lastUpdate?.status === "unknown") {
        return {
          claim_status: "in_flight_or_unknown",
          status: "unknown",
          code: "in_flight",
          message:
            "An earlier attempt of this same call is still delivering it.",
          platform: plat,
          target_address: targetAddr,
          thread_scope_key: resolvedThreadScopeKey,
          is_current: isCurrent,
        };
      }
      if (lastUpdate?.status === "partial") {
        return {
          ...structuredClone(defaultI6AlreadyCompletedPartial),
          platform: plat,
          target_address: targetAddr,
          thread_scope_key: resolvedThreadScopeKey,
          is_current: isCurrent,
          ...(lastUpdate.first_response_json
            ? {
                first_response_json: structuredClone(
                  lastUpdate.first_response_json,
                ),
              }
            : {}),
        };
      }
      const prevClaimForKey = [...authorizeAndClaimDelivery.mock.calls]
        .slice(0, -1)
        .map(([c]) => c)
        .reverse()
        .find((c) => c.delivery_key === req.delivery_key);
      if (
        lastUpdate?.status === "sent" &&
        (req.delivery_key.startsWith("notice:partial:") ||
          (req.delivery_key.startsWith("fallback:") &&
            prevClaimForKey &&
            (req.inbound_message_id ?? null) ===
              (prevClaimForKey.inbound_message_id ?? null) &&
            (req.inbound_message_id ? true : req.run_id !== RUN_ID)))
      ) {
        return {
          ...structuredClone(defaultI6AlreadyCompletedSent),
          platform: plat,
          target_address: targetAddr,
          thread_scope_key: resolvedThreadScopeKey,
          is_current: isCurrent,
          ...(lastUpdate.first_response_json
            ? {
                first_response_json: structuredClone(
                  lastUpdate.first_response_json,
                ),
              }
            : {}),
        };
      }
      const platformIdentityMap: Record<string, string> = {
        feishu: "ou_alice",
        discord: "dc_alice",
        weixin: "wx_alice",
      };
      const recipientId =
        targetAddr.endsWith(":@me") || !targetAddr.includes(":")
          ? (platformIdentityMap[plat] ?? "ou_alice")
          : (targetAddr.split(":", 2)[1]?.split("/").at(-1) ??
            platformIdentityMap[plat] ??
            "ou_alice");
      return {
        ...structuredClone(defaultI6Claimed),
        recipient_id: recipientId,
        platform: plat,
        target_address: targetAddr,
        thread_scope_key: resolvedThreadScopeKey,
        is_current: isCurrent,
        weixin_context_token: plat === "weixin" ? "ctx_tok_123" : null,
      };
    },
  );

  const updateDeliveryProgress = vi.fn(
    async (
      req: I7DeliveriesProgressRequest,
    ): Promise<I7DeliveriesProgressResponse> => ({
      ...structuredClone(defaultI7DeliverySent),
      delivery_key: req.delivery_key,
      status: req.status ?? "in_flight",
      sent_to_current_count:
        req.status === "sent" || req.status === "partial" ? 1 : 0,
    }),
  );

  const updateInboundProgress = vi.fn(
    async (
      req: I7InboundProgressRequest,
    ): Promise<I7InboundProgressResponse> => {
      const effectiveRunId = req.run_id ?? req.background_run?.run_id ?? RUN_ID;
      const fbKey = `fallback:${effectiveRunId}`;
      const lastFbUpdate = [...updateDeliveryProgress.mock.calls]
        .map(([u]) => u)
        .reverse()
        .find((u) => u.delivery_key === fbKey && u.status !== undefined);
      const lastFbClaim = [...authorizeAndClaimDelivery.mock.calls]
        .map(([c]) => c)
        .reverse()
        .find((c) => c.delivery_key === fbKey);
      const hasCompletedCurrentFallback =
        lastFbUpdate?.status === "partial" ||
        (lastFbUpdate?.status === "sent" &&
          lastFbClaim &&
          (req.message_id ?? null) ===
            (lastFbClaim.inbound_message_id ?? null) &&
          (req.message_id ? true : effectiveRunId !== RUN_ID));
      const hasCompletedDiscordCurrentSend = [
        ...authorizeAndClaimDelivery.mock.calls,
      ].some(([c]) => {
        if (
          c.kind !== "agent_send" ||
          c.run_id !== effectiveRunId ||
          !c.has_non_empty_text ||
          c.target ||
          c.platform !== "discord"
        ) {
          return false;
        }
        const upd = [...updateDeliveryProgress.mock.calls]
          .map(([u]) => u)
          .reverse()
          .find(
            (u) => u.delivery_key === c.delivery_key && u.status !== undefined,
          );
        return upd?.status === "sent" || upd?.status === "partial";
      });
      return {
        ...structuredClone(defaultI7InboundProgress),
        run_id: effectiveRunId,
        sent_to_current_count:
          hasCompletedCurrentFallback || hasCompletedDiscordCurrentSend ? 1 : 0,
      };
    },
  );

  const defaultSseAttached = loadFixtureKey<
    Array<{ event: string; data: Record<string, unknown> }>
  >("conflict_and_sse_events.json", "sse_stream_attached");

  const createThreadMessages = vi.fn(
    async (
      _userId: string,
      _payload: ThreadMessageDispatchPayload,
    ): Promise<SseOrErrorResponse> => ({
      status: 200,
      isSse: true,
      events: makeCompletedTurnSseEvents({
        threadId: THREAD_ID,
        runId: RUN_ID,
        turnIndex: 0,
        content: "Hello from new thread!",
      }),
    }),
  );

  const appendThreadMessages = vi.fn(
    async (
      _userId: string,
      threadId: string,
      _payload: ThreadMessageDispatchPayload,
    ): Promise<SseOrErrorResponse> => ({
      status: 200,
      isSse: true,
      events: makeCompletedTurnSseEvents({
        threadId,
        runId: RUN_ID,
        turnIndex: 1,
        content: "Assistant reply on existing thread.",
      }),
    }),
  );

  const retryThreadTurn = vi.fn(
    async (
      _userId: string,
      threadId: string,
      _payload: ThreadRetryPayload,
    ): Promise<SseOrErrorResponse> => ({
      status: 200,
      isSse: true,
      events: makeCompletedTurnSseEvents({
        threadId,
        runId: RUN_ID,
        turnIndex: 1,
        content: "Retried failed turn!",
      }),
    }),
  );

  const getThreadStatus = vi.fn(
    async (_userId: string, threadId: string): Promise<ThreadStatusInfo> => {
      // Check if an interrupt is currently active on `threadId` from the latest `I-1` or `I-7` call
      let activeInterrupt = null;
      const lastResolveRes = registerAndResolveInbound.mock.results.at(-1);
      if (lastResolveRes && lastResolveRes.type === "return") {
        try {
          const val =
            (await lastResolveRes.value) as I1RegisterAndResolveResponse;
          if (val?.pending_interrupt?.thread_id === threadId) {
            activeInterrupt = val.pending_interrupt;
          }
        } catch {
          // ignore
        }
      }
      for (const [upd] of updateInboundProgress.mock.calls) {
        if (upd.session_interrupt_update) {
          if (upd.session_interrupt_update.pending_interrupt === null) {
            activeInterrupt = null;
          } else if (
            upd.session_interrupt_update.pending_interrupt.thread_id ===
            threadId
          ) {
            activeInterrupt = upd.session_interrupt_update.pending_interrupt;
          }
        }
      }

      if (activeInterrupt) {
        return {
          ...structuredClone(defaultStatusInterrupted),
          run_id: activeInterrupt.run_id,
          latest_turn_index: activeInterrupt.turn_index ?? 1,
          turn_index: activeInterrupt.turn_index ?? 1,
        };
      }

      return {
        ...structuredClone(defaultStatusIdle),
        run_id: RUN_ID,
        latest_turn_index: 1,
        turn_index: 1,
      };
    },
  );

  const mock: MockBackend = {
    get progressUpdates() {
      return updateInboundProgress.mock.calls.map(([req]) => req);
    },
    get deliveryClaims() {
      return authorizeAndClaimDelivery.mock.calls.map(([req]) => req);
    },
    get deliveryUpdates() {
      return updateDeliveryProgress.mock.calls.map(([req]) => req);
    },
    get appendCalls() {
      return appendThreadMessages.mock.calls.map(
        ([userId, threadId, payload]) => ({ userId, threadId, payload }),
      );
    },
    get createCalls() {
      return createThreadMessages.mock.calls.map(([userId, payload]) => ({
        userId,
        payload,
      }));
    },
    get retryCalls() {
      return retryThreadTurn.mock.calls.map(([userId, threadId, payload]) => ({
        userId,
        threadId,
        payload,
      }));
    },

    registerAndResolveInbound,

    consumeBindCode: vi.fn(
      async (
        _req: I2BindCodeConsumeRequest,
      ): Promise<I2BindCodeConsumeResponse> =>
        structuredClone(defaultI2BindSuccess),
    ),

    sessionCommand: vi.fn(
      async (
        req: I3SessionCommandRequest,
      ): Promise<I3SessionCommandResponse> => {
        if (req.cmd === "stop") {
          return structuredClone(defaultI3StopSession);
        }
        if (req.cmd === "workspace_list") {
          return structuredClone(defaultI3WsList);
        }
        if (req.cmd === "workspace_switch") {
          return structuredClone(defaultI3WsSwitch);
        }
        return {
          ...structuredClone(defaultI3WsList),
          ...structuredClone(defaultI3WsSwitch),
          ...structuredClone(defaultI3NewSession),
          workspaces: structuredClone(defaultI3WsList.workspaces),
        };
      },
    ),

    getChannelState: vi.fn(
      async (): Promise<I4StateResponse> => structuredClone(defaultState),
    ),

    putChannelSettings: vi.fn(
      async (req: I5SettingsPutRequest): Promise<I5SettingsPutResult> => ({
        status: 200,
        data: {
          ...structuredClone(defaultI5PutSuccess),
          settings: req.settings ?? defaultState.settings.apps,
          address: req.automation_output_update?.address ?? null,
          name: req.automation_output_update?.address ?? null,
        },
      }),
    ),

    authorizeAndClaimDelivery,
    updateDeliveryProgress,
    updateInboundProgress,

    reconcileSteer: vi.fn(
      async (
        _req: I7SteersReconcileRequest,
      ): Promise<I7SteersReconcileResponse> =>
        structuredClone(defaultI7ReconcileConsumed),
    ),

    recoverState: vi.fn(
      async (): Promise<I7RecoverResponse> => ({
        mode: "startup",
        inbound_events: [],
        background_runs: [],
        thread_watches: [],
        automation_runs: [],
      }),
    ),

    startAutomationRun: vi.fn(
      async (
        req: I8AutomationRunStartRequest,
      ): Promise<I8AutomationRunStartResponse> => ({
        targets: req.entries.map((entry) => ({
          ...structuredClone(defaultI8Register.targets[0]!),
          entry,
          address: entry.includes(":") ? entry : `${entry}:@me`,
        })),
      }),
    ),

    finishAutomationRun: vi.fn(
      async (
        executionId: string,
        req: I8AutomationRunFinishRequest,
      ): Promise<{
        status: number;
        data: I8AutomationRunFinishResponse | null;
      }> => ({
        status: 200,
        data: {
          ...structuredClone(defaultI8FinishClaim),
          execution_id: executionId,
          status: req.mark_finished ? "finished" : "finishing",
          thread_id: req.thread_id ?? THREAD_ID,
          finish_status: req.status ?? "completed",
          final_text: req.final_text ?? null,
          targets: req.targets ?? structuredClone(defaultI8FinishClaim.targets),
        },
      }),
    ),

    getWeixinState: vi.fn(
      async (): Promise<I9WeixinStateGetResponse> =>
        structuredClone(defaultI9Unconfigured),
    ),

    putWeixinState: vi.fn(
      async (
        req: I9WeixinStatePutRequest,
      ): Promise<I9WeixinStatePutResponse> => ({
        ...structuredClone(defaultI9PutSaved),
        ilink_bot_id: req.ilink_bot_id ?? defaultI9PutSaved.ilink_bot_id,
        owner_user_id: req.owner_user_id ?? USER_ID,
        base_url: req.base_url ?? defaultI9PutSaved.base_url,
        get_updates_buf: req.get_updates_buf ?? "",
        status: req.status ?? "active",
      }),
    ),

    createThreadMessages,
    appendThreadMessages,
    retryThreadTurn,

    cancelThreadTurn: vi.fn(async () => ({ status: 200 })),

    streamThreadMessages: vi.fn(
      async (): Promise<SseOrErrorResponse> => ({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(defaultSseAttached),
      }),
    ),

    replayThreadMessages: vi.fn(
      async (): Promise<SseOrErrorResponse> => ({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture(defaultReplaySinglePage),
      }),
    ),

    watchThread: vi.fn(
      async (
        _userId: string,
        _threadId: string,
        signal?: AbortSignal,
      ): Promise<SseOrErrorResponse> => ({
        status: 200,
        isSse: true,
        events: makeAbortableOpenWatchSse(signal),
      }),
    ),

    getDispatchesLiveness: vi.fn(
      async (
        _userId: string,
        runIds: string[],
      ): Promise<Record<string, DispatchLivenessRunInfo>> =>
        Object.fromEntries(
          runIds.map((id) => [
            id,
            {
              ...structuredClone(defaultLivenessCompletedRun),
              run_id: id,
            },
          ]),
        ),
    ),

    getThreadStatus,

    getThreadWakeups: vi.fn(async () => []),

    ensureHomeWorkspace: vi.fn(async () => ({
      workspace_id: HOME_WS_ID,
      status: "running",
    })),

    startWorkspace: vi.fn(async () => ({ status: "running" })),

    uploadWorkspaceFile: vi.fn(async () => undefined),

    downloadWorkspaceFile: vi.fn(async () => ({
      content: Buffer.from("PDF_REPORT_CONTENT", "utf8"),
      mimeType: "application/pdf",
    })),
  };

  return mock;
}

export interface TestHarness {
  tempDir: string;
  config: GatewayConfig;
  backend: MockBackend;
  feishuAdapter: FakeChannelAdapter;
  discordAdapter: FakeChannelAdapter;
  weixinAdapter: FakeChannelAdapter;
  adapters: Map<ChannelPlatform, FakeChannelAdapter>;
  app: GatewayApp;
  cleanup(): Promise<void>;
}

export function createTestHarness(options?: {
  nowFn?: () => number;
  backoffDelayFn?: (ms: number) => Promise<void>;
  busyThreadMaxWaitMs?: number;
}): TestHarness {
  const tempDir = mkdtempSync(join(tmpdir(), "dalton-gw-test-"));
  const config = loadConfig({
    GATEWAY_HOST: "127.0.0.1",
    GATEWAY_PORT: "0",
    INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
    DALTON_BACKEND_URL: "http://127.0.0.1:8000",
    WEB_BASE_URL: "https://app.ginlix.ai",
    DALTON_WEIXIN_CACHE_DIR: join(tempDir, "weixin"),
  });
  const backend = createMockBackend();
  const feishuAdapter = new FakeChannelAdapter("feishu", 2000);
  const discordAdapter = new FakeChannelAdapter("discord", 2000);
  const weixinAdapter = new FakeChannelAdapter("weixin", 2000);
  const adapters = new Map<ChannelPlatform, FakeChannelAdapter>([
    ["feishu", feishuAdapter],
    ["discord", discordAdapter],
    ["weixin", weixinAdapter],
  ]);
  const app = createGatewayApp({
    config,
    backend,
    adapters,
    backoffDelayFn: options?.backoffDelayFn ?? (async () => undefined),
    busyThreadMaxWaitMs: options?.busyThreadMaxWaitMs,
    nowFn: options?.nowFn,
  });

  return {
    tempDir,
    config,
    backend,
    feishuAdapter,
    discordAdapter,
    weixinAdapter,
    adapters,
    app,
    async cleanup(): Promise<void> {
      await app.stop();
      rmSync(tempDir, { recursive: true, force: true });
    },
  };
}

export async function fetchGatewayServer(
  app: GatewayApp,
  url: string,
  init?: RequestInit,
): Promise<{ status: number; json(): Promise<unknown> }> {
  try {
    return await fetch(url, init);
  } catch (err) {
    const cause = (err as { cause?: { code?: string } })?.cause;
    if (cause?.code !== "EPERM") {
      throw err;
    }
    // Fallback when running inside a restricted sandbox that blocks loopback TCP `connect()`
    const parsed = new URL(url);
    const headers: Record<string, string> = {};
    if (init?.headers) {
      const h = new Headers(init.headers);
      h.forEach((v, k) => {
        headers[k.toLowerCase()] = v;
      });
    }
    const bodyText = typeof init?.body === "string" ? init.body : "";
    return await new Promise<{ status: number; json(): Promise<unknown> }>(
      (resolveRes) => {
        const reqIter = async function* () {
          if (bodyText.length > 0) {
            yield Buffer.from(bodyText, "utf8");
          }
        };
        const fakeReq = Object.assign(reqIter(), {
          method: init?.method ?? "GET",
          url: `${parsed.pathname}${parsed.search}`,
          headers,
        });
        let statusCode = 200;
        let responseBody = "";
        const fakeRes = {
          writeHead(code: number) {
            statusCode = code;
            return this;
          },
          end(chunk?: string | Buffer) {
            if (chunk !== undefined) {
              responseBody += Buffer.isBuffer(chunk)
                ? chunk.toString("utf8")
                : String(chunk);
            }
            resolveRes({
              status: statusCode,
              async json() {
                return responseBody ? JSON.parse(responseBody) : {};
              },
            });
          },
        };
        app.server.emit("request", fakeReq, fakeRes);
      },
    );
  }
}
