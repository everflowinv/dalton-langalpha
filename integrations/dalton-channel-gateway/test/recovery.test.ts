import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  I7RecoverResponse,
  I9WeixinStateGetResponse,
  ThreadStatusInfo,
} from "../src/backend-client.js";
import { makeInboundRequestKey } from "../src/uuid.js";
import {
  createTestHarness,
  fetchGatewayServer,
  loadFixtureKey,
  makeSseEventsFromFixture,
  RUN_ID,
  TEST_SERVICE_TOKEN,
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
});
