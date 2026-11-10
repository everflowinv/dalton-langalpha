import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { verifyServiceToken } from "../src/auth.js";
import { HttpBackendClient, parseSseStream, type SseEvent } from "../src/backend-client.js";
import { loadConfig } from "../src/config.js";
import {
  DALTON_CHANNEL_NAMESPACE,
  makeCreditSupplementSteerKey,
  makeInboundRequestKey,
  makeReturnedSteerRequestKey,
  makeRetryRequestKey,
  makeTurnAfterSteerRequestKey,
  uuidv5,
} from "../src/uuid.js";
import {
  createTestHarness,
  fetchGatewayServer,
  HOME_WS_ID,
  loadFixtureKey,
  RUN_ID,
  TEST_SERVICE_TOKEN,
  type TestHarness,
  THREAD_ID,
  USER_ID,
  WORKSPACE_ID,
} from "./helpers.js";

describe("Protocol Contract & Wire Payloads (PROTOCOL.md)", () => {
  let h: TestHarness;

  beforeEach(() => {
    h = createTestHarness();
  });

  afterEach(async () => {
    await h.cleanup();
  });

  it("enforces GATEWAY_HOST == 127.0.0.1 and constant-time X-Service-Token >= 32 chars", async () => {
    expect(() =>
      loadConfig({
        GATEWAY_HOST: "0.0.0.0",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      }),
    ).toThrow(/127\.0\.0\.1/);

    expect(verifyServiceToken({}, TEST_SERVICE_TOKEN)).toBe(false);
    expect(
      verifyServiceToken({ "x-service-token": "short_token" }, "short_token"),
    ).toBe(false);
    expect(
      verifyServiceToken(
        { "x-service-token": "x".repeat(TEST_SERVICE_TOKEN.length) },
        TEST_SERVICE_TOKEN,
      ),
    ).toBe(false);
    expect(
      verifyServiceToken(
        { "x-service-token": TEST_SERVICE_TOKEN },
        TEST_SERVICE_TOKEN,
      ),
    ).toBe(true);

    const { host, port } = await h.app.start();
    expect(host).toBe("127.0.0.1");

    const unauth = await fetchGatewayServer(
      h.app,
      `http://${host}:${port}/agent/settings`,
      {
        headers: { "X-User-Id": USER_ID },
      },
    );
    expect(unauth.status).toBe(401);

    const valid = await fetchGatewayServer(
      h.app,
      `http://${host}:${port}/agent/settings`,
      {
        headers: {
          "X-Service-Token": TEST_SERVICE_TOKEN,
          "X-User-Id": USER_ID,
        },
      },
    );
    expect(valid.status).toBe(200);
  });

  it("generates deterministic RFC 9562 UUIDv5 keys for inbound, :turn, :retry, :returned, and :steer", () => {
    const k1 = makeInboundRequestKey("feishu", "om_msg_1");
    const k2 = makeInboundRequestKey("feishu", "om_msg_1");
    expect(k1).toBe(k2);
    expect(k1).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );

    expect(makeTurnAfterSteerRequestKey("feishu", "om_msg_1", 1)).toBe(
      uuidv5(DALTON_CHANNEL_NAMESPACE, "feishu:om_msg_1:turn:1"),
    );
    expect(makeRetryRequestKey("feishu", "om_msg_1", RUN_ID)).toBe(
      uuidv5(DALTON_CHANNEL_NAMESPACE, `feishu:om_msg_1:retry:${RUN_ID}`),
    );
    expect(makeReturnedSteerRequestKey("feishu", "om_msg_1", RUN_ID)).toBe(
      uuidv5(DALTON_CHANNEL_NAMESPACE, `feishu:om_msg_1:returned:${RUN_ID}`),
    );
    expect(makeCreditSupplementSteerKey("feishu", "om_msg_1", RUN_ID)).toBe(
      uuidv5(DALTON_CHANNEL_NAMESPACE, `feishu:om_msg_1:steer:${RUN_ID}`),
    );
  });

  it("HttpBackendClient sends real backend paths, query params, and payload shapes", async () => {
    const capturedCalls: Array<{
      url: string;
      method: string;
      headers: Record<string, string>;
      body: unknown;
    }> = [];

    const fakeFetch: typeof fetch = vi.fn(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const body =
        typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      capturedCalls.push({ url, method, headers, body });

      if (url.includes("/dispatches/liveness")) {
        const livenessFixture = loadFixtureKey<Record<string, unknown>>(
          "status_liveness_wakeups_watch.json",
          "dispatches_liveness_running",
        );
        return new Response(JSON.stringify(livenessFixture), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/internal/automation-runs")) {
        const autoFixture = loadFixtureKey<Record<string, unknown>>(
          "i8_automation_runs.json",
          "register_dm",
        );
        return new Response(JSON.stringify(autoFixture), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/internal/session/command")) {
        const cmdFixture = loadFixtureKey<Record<string, unknown>>(
          "i3_session_command.json",
          "workspace_switch_dm",
        );
        return new Response(JSON.stringify(cmdFixture), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/internal/deliveries/authorize-and-claim")) {
        const claimFixture = loadFixtureKey<Record<string, unknown>>(
          "i6_authorize_and_claim.json",
          "claimed_feishu_dm",
        );
        return new Response(JSON.stringify(claimFixture), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/internal/inbound/progress")) {
        const progFixture = loadFixtureKey<Record<string, unknown>>(
          "i7_progress_and_reconcile.json",
          "inbound_progress_default",
        );
        return new Response(JSON.stringify(progFixture), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      return new Response("event: replay_done\ndata: {\"has_more\":false}\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    });

    const client = new HttpBackendClient({
      baseUrl: "http://127.0.0.1:8000",
      serviceToken: TEST_SERVICE_TOKEN,
      fetchFn: fakeFetch,
    });

    // 1. I-8 startAutomationRun -> `/api/v1/dalton/channels/internal/automation-runs`
    await client.startAutomationRun({
      execution_id: "exec_1",
      user_id: USER_ID,
      workspace_id: WORKSPACE_ID,
      automation_name: "Daily",
      entries: ["feishu"],
    });
    expect(capturedCalls[0]?.url).toBe(
      "http://127.0.0.1:8000/api/v1/dalton/channels/internal/automation-runs",
    );

    // 2. ChatRequest normalizes `platform` field
    await client.appendThreadMessages(USER_ID, THREAD_ID, {
      messages: [{ role: "user", content: "hi" }],
      request_key: "rk_1",
      turn_platform: "feishu",
    });
    expect(
      (capturedCalls[1]?.body as Record<string, unknown>)?.platform,
    ).toBe("feishu");

    // 3. `/retry` includes required `workspace_id` and `run_id`
    await client.retryThreadTurn(USER_ID, THREAD_ID, {
      workspace_id: WORKSPACE_ID,
      run_id: RUN_ID,
      request_key: "rk_retry_1",
      platform: "feishu",
    });
    expect(capturedCalls[2]?.url).toBe(
      `http://127.0.0.1:8000/api/v1/threads/${THREAD_ID}/retry`,
    );
    expect(capturedCalls[2]?.body).toMatchObject({
      workspace_id: WORKSPACE_ID,
      run_id: RUN_ID,
      request_key: "rk_retry_1",
      platform: "feishu",
    });

    // 4. `/messages/stream` passes `run_id` and `last_event_id` + `Last-Event-ID` header
    await client.streamThreadMessages(USER_ID, THREAD_ID, {
      runId: RUN_ID,
      lastEventId: "evt_42",
    });
    expect(capturedCalls[3]?.url).toBe(
      `http://127.0.0.1:8000/api/v1/threads/${THREAD_ID}/messages/stream?run_id=${RUN_ID}&last_event_id=evt_42`,
    );
    expect(capturedCalls[3]?.headers["Last-Event-ID"]).toBe("evt_42");

    // 5. `/messages/replay` passes `source=auto&limit=5&before_turn=3`
    await client.replayThreadMessages(USER_ID, THREAD_ID, 3);
    expect(capturedCalls[4]?.url).toBe(
      `http://127.0.0.1:8000/api/v1/threads/${THREAD_ID}/messages/replay?source=auto&limit=5&before_turn=3`,
    );

    // 6. `/dispatches/liveness` parses `data.runs` array into map by `run_id`
    const livenessMap = await client.getDispatchesLiveness(USER_ID, [RUN_ID]);
    const expectedRun = loadFixtureKey<{ runs: Array<Record<string, unknown>> }>(
      "status_liveness_wakeups_watch.json",
      "dispatches_liveness_running",
    ).runs[0];
    expect(livenessMap[RUN_ID]).toEqual(expectedRun);

    // 7. `I-3 sessionCommand` forwards atomic command fields (`platform_user_id`, `message_id`, `request_key`, `target_kind`, `workspace_query`, `is_internal_query`)
    await client.sessionCommand({
      cmd: "workspace_switch",
      platform: "feishu",
      platform_user_id: "ou_alice",
      chat_type: "dm",
      chat_address: "feishu:@me",
      chat_id: "oc_dm_1",
      scope_key: "dm:ou_alice",
      message_id: "om_cmd_wire_1",
      request_key: "rk_cmd_wire_1",
      raw_payload: { command: "/量化研究 A组" },
      target_kind: "shortcut_name",
      workspace_query: "量化研究 A组",
    });
    expect(capturedCalls[6]?.url).toBe(
      "http://127.0.0.1:8000/api/v1/dalton/channels/internal/session/command",
    );
    expect(capturedCalls[6]?.body).toMatchObject({
      cmd: "workspace_switch",
      platform: "feishu",
      platform_user_id: "ou_alice",
      message_id: "om_cmd_wire_1",
      request_key: "rk_cmd_wire_1",
      target_kind: "shortcut_name",
      workspace_query: "量化研究 A组",
    });

    // 8. `I-6 authorizeAndClaimDelivery` forwards `source_scope_key`, `source_generation`, `activity_origin`
    await client.authorizeAndClaimDelivery({
      delivery_key: "fallback:run_wire_1",
      kind: "fallback",
      user_id: USER_ID,
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      platform: "feishu",
      target: "feishu:@me",
      inbound_message_id: "om_wire_1",
      has_non_empty_text: true,
      total_chunks: 1,
      source_scope_key: "dm:ou_alice",
      source_generation: 3,
      activity_origin: "conversation",
    });
    expect(capturedCalls[7]?.body).toMatchObject({
      delivery_key: "fallback:run_wire_1",
      source_scope_key: "dm:ou_alice",
      source_generation: 3,
      activity_origin: "conversation",
    });

    // 9. `I-7 updateInboundProgress` forwards `command_effect_status`, `command_reply_pending`, and `session_interrupt_update` fields
    await client.updateInboundProgress({
      platform: "feishu",
      message_id: "om_cmd_wire_1",
      status: "completed",
      command_effect_status: "done",
      command_reply_pending: false,
      session_interrupt_update: {
        platform: "feishu",
        scope_key: "dm:ou_alice",
        dalton_user_id: USER_ID,
        generation: 3,
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        interrupt_id: "int_wire_1",
        pending_interrupt: null,
      },
    });
    expect(capturedCalls[8]?.body).toMatchObject({
      platform: "feishu",
      message_id: "om_cmd_wire_1",
      command_effect_status: "done",
      command_reply_pending: false,
      session_interrupt_update: {
        platform: "feishu",
        scope_key: "dm:ou_alice",
        dalton_user_id: USER_ID,
        generation: 3,
        thread_id: THREAD_ID,
        run_id: RUN_ID,
        interrupt_id: "int_wire_1",
        pending_interrupt: null,
      },
    });
  });

  it("DeliveryService uses I-6 recipient_id as platform recipient while keeping Dalton userId as X-User-Id for workspace file download", async () => {
    vi.mocked(h.backend.authorizeAndClaimDelivery).mockResolvedValueOnce({
      claim_status: "claimed",
      recipient_id: "ou_platform_recipient_777",
      platform: "feishu",
      target_address: "feishu:@me",
      is_current: true,
      home_workspace_id: HOME_WS_ID,
      weixin_context_token: null,
    });

    const res = await h.app.agentRoutes.handleAgentSend(USER_ID, {
      thread_id: THREAD_ID,
      run_id: RUN_ID,
      tool_call_id: "tc_id_split",
      turn_platform: "feishu",
      workspace_id: WORKSPACE_ID,
      text: "Delivering file",
      files: [{ path: "reports/summary.pdf", workspace_id: WORKSPACE_ID }],
    });

    expect(res.status).toBe(200);
    // `downloadWorkspaceFile` receives Dalton `USER_ID`, NOT `ou_platform_recipient_777`
    expect(h.backend.downloadWorkspaceFile).toHaveBeenCalledWith(
      USER_ID,
      WORKSPACE_ID,
      "reports/summary.pdf",
    );
    // Platform adapter receives `ou_platform_recipient_777` as `recipientPlatformUserId`
    expect(h.feishuAdapter.sentTexts[0]?.recipientPlatformUserId).toBe(
      "ou_platform_recipient_777",
    );
    expect(h.feishuAdapter.sentFiles[0]?.recipientPlatformUserId).toBe(
      "ou_platform_recipient_777",
    );
  });

  it("parses SSE multi-line data blocks and keepalive comments accurately", async () => {
    const rawSse = [
      ": keepalive",
      "",
      "id: 99",
      "event: message_chunk",
      'data: {"role":"assistant",',
      'data: "content_type":"text","content":"line1"}',
      "",
      "",
    ].join("\r\n");

    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(rawSse));
        controller.close();
      },
    });

    const parsed: SseEvent[] = [];
    for await (const ev of parseSseStream(stream)) {
      parsed.push(ev);
    }
    expect(parsed).toHaveLength(2);
    expect(parsed[0]?.event).toBe(":comment");
    expect(parsed[1]?.event).toBe("message_chunk");
    expect(parsed[1]?.id).toBe("99");
    expect(JSON.parse(parsed[1]!.data)).toEqual({
      role: "assistant",
      content_type: "text",
      content: "line1",
    });
  });
});
