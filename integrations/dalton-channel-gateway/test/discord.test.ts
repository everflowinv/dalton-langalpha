import { describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError, type ChannelAdapter } from "../src/channels/adapter.js";
import {
  chunkDiscordText,
  classifyDiscordDeliveryError,
  createDiscordMessageNonce,
  DISCORD_MAX_NONCE_CHARS,
  DiscordChannelAdapter,
  DiscordError,
  DiscordGatewayClient,
  DiscordQueueError,
  downloadDiscordAttachment,
  fetchDiscordGatewayInfo,
  normalizeDiscordBold,
  parseDiscordInboundMessage,
  parseDiscordTarget,
  RateLimitError,
  renderDiscordMarkdown,
  RequestClient,
  RESUMABLE_GATEWAY_CLOSE_CODE,
  createThreadDiscord,
  DISCORD_THREAD_NAME_LIMIT,
  discordStandaloneThreadName,
  discordThreadRequestMark,
  sanitizeDiscordThreadName,
  type DiscordGatewayInfo,
  type DiscordWebSocketLike,
} from "../src/channels/discord/index.js";
import { loadConfig } from "../src/config.js";
import { DeliveryService } from "../src/delivery-service.js";
import { buildConfiguredAdapters } from "../src/main.js";
import { createGatewayApp } from "../src/server.js";
import { SafeFetchError } from "../src/shared/safe-fetch.js";
import type { ChannelPlatform, InboundChannelEvent } from "../src/types.js";
import { makeInboundRequestKey } from "../src/uuid.js";
import { createMockBackend, fetchGatewayServer, HOME_WS_ID, makeSseEventsFromFixture, TEST_SERVICE_TOKEN } from "./helpers.js";

const BOT_ID = "999000111";
const USER_ID = "111222333";
const GUILD_ID = "700000001";
const CHANNEL_ID = "700000002";
const THREAD_ID = "700000003";

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

interface FetchCall { url: string; init: RequestInit }
type FetchHandler = (call: FetchCall, index: number) => Promise<Response> | Response;

function makeRest(handler: FetchHandler) {
  const calls: FetchCall[] = [];
  const fetchImpl = async (url: string, init: RequestInit): Promise<Response> => {
    const call = { url, init };
    calls.push(call);
    return handler(call, calls.length - 1);
  };
  const rest = new RequestClient("test-bot-token", { fetchImpl });
  return { rest, calls };
}

function routeOf(url: string): string {
  return new URL(url).pathname.replace(/^\/api\/v10/, "");
}

// ---------------------------------------------------------------------------
// Fake WebSocket + fake timers for the gateway client
// ---------------------------------------------------------------------------

class FakeWebSocket implements DiscordWebSocketLike {
  readyState = 0;
  readonly sent: string[] = [];
  closeCalls = 0;
  lastCloseCode: number | undefined;
  lastCloseReason: string | undefined;
  readonly closeCodes: number[] = [];
  private readonly listeners = new Map<string, Array<(...args: never[]) => void>>();
  constructor(readonly url: string) {}
  on(event: string, listener: (...args: never[]) => void): this {
    const list = this.listeners.get(event) ?? [];
    list.push(listener);
    this.listeners.set(event, list);
    return this;
  }
  send(data: string): void { this.sent.push(data); }
  close(code = 1000, reason = ""): void {
    this.closeCalls += 1;
    this.lastCloseCode = code;
    this.lastCloseReason = reason;
    this.closeCodes.push(code);
    this.readyState = 3;
    this.emit("close", code as never);
  }
  open(): void { this.readyState = 1; this.emit("open"); }
  receive(payload: unknown): void {
    this.emit("message", (typeof payload === "string" ? payload : JSON.stringify(payload)) as never);
  }
  serverClose(code: number): void { this.readyState = 3; this.emit("close", code as never); }
  private emit(event: string, ...args: never[]): void {
    for (const fn of this.listeners.get(event) ?? []) fn(...args);
  }
}

class FakeTimers {
  private seq = 1;
  private pending = new Map<number, { fn: () => void; at: number }>();
  nowMs = 0;
  readonly setTimeoutFn = (fn: () => void, ms: number): ReturnType<typeof setTimeout> => {
    const id = this.seq++;
    this.pending.set(id, { fn, at: this.nowMs + Math.max(0, ms) });
    return id as unknown as ReturnType<typeof setTimeout>;
  };
  readonly clearTimeoutFn = (t: ReturnType<typeof setTimeout>): void => {
    this.pending.delete(t as unknown as number);
  };
  advance(ms: number): void {
    const target = this.nowMs + ms;
    for (;;) {
      const next = [...this.pending.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > target) break;
      this.pending.delete(next[0]);
      this.nowMs = next[1].at;
      next[1].fn();
    }
    this.nowMs = target;
  }
  get pendingCount(): number { return this.pending.size; }
}

async function flushMicrotasks(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

/** A missed heartbeat ACK reconnects. Ack while fake time moves so that reconnect is not the one under test. */
function advanceAckingHeartbeats(timers: FakeTimers, socket: FakeWebSocket, ms: number): void {
  let left = ms;
  while (left > 0) {
    const step = Math.min(500, left);
    timers.advance(step);
    if (socket.readyState === 1) {
      socket.receive({ op: 11, d: null });
    }
    left -= step;
  }
}

function makeGateway(timers: FakeTimers, sockets: FakeWebSocket[]) {
  const dispatched: Array<{ name: string; data: unknown }> = [];
  const errors: Error[] = [];
  const client = new DiscordGatewayClient({
    token: "test-bot-token",
    webSocketFactory: (url) => {
      const ws = new FakeWebSocket(url);
      sockets.push(ws);
      return ws;
    },
    timerDeps: {
      setTimeoutFn: timers.setTimeoutFn,
      clearTimeoutFn: timers.clearTimeoutFn,
      now: () => timers.nowMs,
      random: () => 0,
    },
    onDispatch: async (name, data) => { dispatched.push({ name, data }); },
    onError: (err) => { errors.push(err); },
  });
  return { client, dispatched, errors };
}

async function helloAndReady(ws: FakeWebSocket, seq = 1): Promise<void> {
  ws.open();
  ws.receive({ op: 10, d: { heartbeat_interval: 1000 } });
  await flushMicrotasks();
  ws.receive({
    op: 0, s: seq, t: "READY",
    d: { session_id: "sess-1", resume_gateway_url: "wss://resume.example.gg", user: { id: BOT_ID } },
  });
  await flushMicrotasks();
}

// ---------------------------------------------------------------------------
// REST scheduler / client
// ---------------------------------------------------------------------------

describe("Discord REST client", () => {
  it("uses v10, sends Bot auth, and retries a bucket 429 with the same nonce body", async () => {
    const bodies: string[] = [];
    const { rest, calls } = makeRest(async (call, index) => {
      bodies.push(String(call.init.body ?? ""));
      if (index === 0) {
        return jsonResponse({ message: "rate limited", retry_after: 0, global: false }, 429, {
          "x-ratelimit-bucket": "bucket-1",
        });
      }
      return jsonResponse({ id: "msg-1" });
    });
    const result = (await rest.post(`/channels/${CHANNEL_ID}/messages`, {
      content: "hello", nonce: "nonce-42",
    })) as { id: string };
    expect(result.id).toBe("msg-1");
    expect(calls).toHaveLength(2);
    expect(calls[0]!.url).toBe(`https://discord.com/api/v10/channels/${CHANNEL_ID}/messages`);
    expect(new Headers(calls[0]!.init.headers).get("Authorization")).toBe("Bot test-bot-token");
    expect(bodies[0]).toContain("nonce-42");
    expect(bodies[1]).toBe(bodies[0]);
  });

  it("honours a global 429 before dispatching the next request", async () => {
    let secondDispatchedAt = -1;
    const { rest } = makeRest(async (_call, index) => {
      if (index === 0) return jsonResponse({ message: "global", retry_after: 0, global: true }, 429);
      secondDispatchedAt = index;
      return jsonResponse({ id: "ok" });
    });
    const result = (await rest.post("/channels/1/messages", { content: "x", nonce: "n" })) as { id: string };
    expect(result.id).toBe("ok");
    expect(secondDispatchedAt).toBe(1);
  });

  it("serializes multipart file uploads as payload_json + files[0] with filename and MIME", async () => {
    const { rest, calls } = makeRest(async () => jsonResponse({ id: "file-msg" }));
    await rest.post(`/channels/${CHANNEL_ID}/messages`, {
      content: "",
      nonce: "n1",
      files: [{ name: "report.pdf", data: Buffer.from("pdf-bytes"), contentType: "application/pdf" }],
    });
    const body = calls[0]!.init.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    const payload = JSON.parse(String(body.get("payload_json"))) as {
      attachments: Array<{ id: number; filename: string }>;
    };
    expect(payload.attachments).toEqual([{ id: 0, filename: "report.pdf" }]);
    const file = body.get("files[0]") as File;
    expect(file.name).toBe("report.pdf");
    expect(file.type).toBe("application/pdf");
    expect(await file.text()).toBe("pdf-bytes");
  });

  it("classifies known rejections as failed, 429 as rate_limited, and ambiguous sends as unknown", async () => {
    expect(classifyDiscordDeliveryError(new DiscordError(403, { code: 50013 }, "missing")).outcome).toBe("failed");
    expect(classifyDiscordDeliveryError(new DiscordError(404, { code: 10003 }, "unknown channel")).code).toBe("target_not_found");
    expect(classifyDiscordDeliveryError(new RateLimitError(429, {}, 1, false, null)).outcome).toBe("rate_limited");
    expect(classifyDiscordDeliveryError(new DiscordError(500, {}, "boom"), { stage: "send" }).outcome).toBe("unknown");
    expect(classifyDiscordDeliveryError(new DiscordError(500, {}, "boom"), { stage: "read" }).outcome).toBe("failed");
    expect(classifyDiscordDeliveryError(new TypeError("fetch failed"), { stage: "send" }).outcome).toBe("unknown");
    const { rest } = makeRest(async () => { throw new TypeError("fetch failed"); });
    await expect(rest.post("/channels/1/messages", { content: "x" })).rejects.toBeInstanceOf(TypeError);
  });

  it("keeps nonces within Discord's 25-character v10 limit across text/file request bodies and 429 retries", async () => {
    const a = createDiscordMessageNonce();
    const b = createDiscordMessageNonce();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f]+$/);
    expect(a.length).toBeGreaterThan(0);
    expect(a.length).toBeLessThanOrEqual(DISCORD_MAX_NONCE_CHARS);
    expect(DISCORD_MAX_NONCE_CHARS).toBe(25);

    const textBodies: Array<{ content?: string; nonce?: string }> = [];
    const filePayloads: Array<{ content?: string; nonce?: string; attachments?: Array<{ id: number; filename: string }> }> = [];
    let textAttempts = 0;
    let fileAttempts = 0;

    const { rest } = makeRest(async (call) => {
      if (call.init.body instanceof FormData) {
        fileAttempts += 1;
        const payloadRaw = String(call.init.body.get("payload_json") ?? "{}");
        filePayloads.push(JSON.parse(payloadRaw) as { content?: string; nonce?: string; attachments?: Array<{ id: number; filename: string }> });
        if (fileAttempts === 1) {
          return jsonResponse({ message: "rate limited", retry_after: 0, global: false }, 429, {
            "x-ratelimit-bucket": "bucket-msg",
          });
        }
        return jsonResponse({ id: "file-msg-ok" });
      }
      textAttempts += 1;
      textBodies.push(JSON.parse(String(call.init.body ?? "{}")) as { content?: string; nonce?: string });
      if (textAttempts === 1) {
        return jsonResponse({ message: "rate limited", retry_after: 0, global: false }, 429, {
          "x-ratelimit-bucket": "bucket-msg",
        });
      }
      return jsonResponse({ id: "text-msg-ok" });
    });

    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    await adapter.sendTextChunk({
      targetAddress: `discord:${GUILD_ID}/${CHANNEL_ID}`,
      text: "hello nonce",
      chunkIndex: 0,
      totalChunks: 1,
    });
    expect(textBodies).toHaveLength(2);
    expect(typeof textBodies[0]!.nonce).toBe("string");
    expect(textBodies[0]!.nonce!.length).toBeGreaterThan(0);
    expect(textBodies[0]!.nonce!.length).toBeLessThanOrEqual(DISCORD_MAX_NONCE_CHARS);
    expect(textBodies[1]!.nonce).toBe(textBodies[0]!.nonce);

    await adapter.sendFile({
      targetAddress: `discord:${GUILD_ID}/${CHANNEL_ID}`,
      path: "reports/memo.pdf",
      filename: "memo.pdf",
      content: Buffer.from("pdf-bytes"),
      mimeType: "application/pdf",
    });
    expect(filePayloads).toHaveLength(2);
    expect(typeof filePayloads[0]!.nonce).toBe("string");
    expect(filePayloads[0]!.nonce!.length).toBeGreaterThan(0);
    expect(filePayloads[0]!.nonce!.length).toBeLessThanOrEqual(DISCORD_MAX_NONCE_CHARS);
    expect(filePayloads[1]!.nonce).toBe(filePayloads[0]!.nonce);
    expect(filePayloads[0]!.nonce).not.toBe(textBodies[0]!.nonce);
  });

  it("distinguishes pre-dispatch queue errors (failed, retryable, 0 HTTP requests) from post-dispatch timeout/abort (unknown, non-repeatable, 1 HTTP request)", async () => {
    expect(classifyDiscordDeliveryError(new DiscordQueueError("queue_full", "queue full")).outcome).toBe("failed");
    expect(classifyDiscordDeliveryError(new DiscordQueueError("queue_cleared", "queue cleared")).outcome).toBe("failed");
    expect(classifyDiscordDeliveryError(new DiscordQueueError("queue_aborted", "queue aborted")).outcome).toBe("failed");
    expect(classifyDiscordDeliveryError(new DiscordQueueError("queue_stale", "queue stale")).outcome).toBe("failed");
    expect(classifyDiscordDeliveryError(new DOMException("Aborted", "AbortError"), { stage: "send" }).outcome).toBe("unknown");

    const backend = createMockBackend();
    let releaseBlocker!: () => void;
    let blockerEntered!: () => void;
    const blockerEnteredPromise = new Promise<void>((r) => { blockerEntered = r; });
    let holdBlocker = true;
    let simulateSendTimeout = false;
    let messagePostAttempts = 0;

    const rest = new RequestClient("test-bot-token", {
      timeoutMs: 30,
      schedulerOptions: { maxQueueSize: 1 },
      fetchImpl: async (url, init) => {
        const route = routeOf(url);
        if (route === "/channels/blocker") {
          blockerEntered();
          if (holdBlocker) {
            await new Promise<void>((r) => { releaseBlocker = r; });
          }
          return jsonResponse({ id: "blocker-ok" });
        }
        if (route === `/channels/${CHANNEL_ID}`) {
          return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID });
        }
        if (route === `/channels/${CHANNEL_ID}/messages` && init.method === "POST") {
          messagePostAttempts += 1;
          if (simulateSendTimeout) {
            return await new Promise<Response>((_resolve, reject) => {
              init.signal?.addEventListener(
                "abort",
                () => reject(new DOMException("The operation was aborted", "AbortError")),
                { once: true },
              );
            });
          }
          return jsonResponse({ id: `msg-${messagePostAttempts}` });
        }
        return jsonResponse({});
      },
    });

    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    vi.spyOn(adapter, "start").mockResolvedValue(undefined);
    vi.spyOn(adapter, "isConnected").mockReturnValue(true);
    adapter.handleDispatch("CHANNEL_CREATE", { id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, name: "general" });

    const app = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
        DALTON_BACKEND_URL: "http://127.0.0.1:8000",
        WEB_BASE_URL: "https://app.dalton.test",
        DALTON_WEIXIN_CACHE_DIR: "/tmp/dalton-wx-discord-queue-test",
        DALTON_DISCORD_ENABLED: "true",
        DALTON_DISCORD_BOT_TOKEN: "t",
        DALTON_DISCORD_APPLICATION_ID: BOT_ID,
      }),
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["discord", adapter]]),
    });

    try {
      const { host, port } = await app.start();
      const postAgentSend = async (payload: Record<string, unknown>) => {
        const res = await fetchGatewayServer(app, `http://${host}:${port}/agent/send`, {
          method: "POST",
          headers: {
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "X-User-Id": "11111111-1111-4111-8111-111111111111",
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        return {
          statusCode: res.status,
          body: (await res.json()) as Record<string, unknown>,
        };
      };

      // 1. Occupy the single scheduler queue slot (maxQueueSize=1) so the next request is rejected pre-dispatch with queue_full.
      const blockerPromise = rest.get("/channels/blocker");
      await blockerEnteredPromise;

      const preDispatchFullRes = await postAgentSend({
        thread_id: "33333333-3333-4333-8333-333333333333",
        run_id: "run-queue-test",
        tool_call_id: "tc-pre-dispatch-1",
        turn_platform: "discord",
        target: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        text: "hello after queue full",
      });
      expect(preDispatchFullRes.statusCode).toBe(200);
      expect(preDispatchFullRes.body).toMatchObject({
        status: "failed",
        code: "unavailable",
      });
      // Request was rejected before dispatch -> 0 message POST requests sent to Discord.
      expect(messagePostAttempts).toBe(0);

      // Release the blocker and retry the exact same tool_call_id: because the first attempt was `failed`,
      // I-6 allows re-claiming and the retry succeeds with exactly 1 HTTP POST.
      holdBlocker = false;
      releaseBlocker();
      await blockerPromise;

      const retryAfterPreDispatchRes = await postAgentSend({
        thread_id: "33333333-3333-4333-8333-333333333333",
        run_id: "run-queue-test",
        tool_call_id: "tc-pre-dispatch-1",
        turn_platform: "discord",
        target: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        text: "hello after queue full",
      });
      expect(retryAfterPreDispatchRes.statusCode).toBe(200);
      expect(retryAfterPreDispatchRes.body).toMatchObject({
        status: "sent",
        code: null,
      });
      expect(messagePostAttempts).toBe(1);

      // 2. Post-dispatch timeout: HTTP POST reaches fetchImpl (messagePostAttempts increments to 2) and times out.
      //    Must be classified as `unknown`, and a subsequent retry with the same tool_call_id must NOT re-send!
      simulateSendTimeout = true;
      const postDispatchTimeoutRes = await postAgentSend({
        thread_id: "33333333-3333-4333-8333-333333333333",
        run_id: "run-queue-test",
        tool_call_id: "tc-post-dispatch-timeout",
        turn_platform: "discord",
        target: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        text: "ambiguous timeout message",
      });
      expect(postDispatchTimeoutRes.statusCode).toBe(200);
      expect(postDispatchTimeoutRes.body).toMatchObject({
        status: "unknown",
        code: "unavailable",
      });
      expect(messagePostAttempts).toBe(2);

      simulateSendTimeout = false;
      const duplicateAfterUnknownRes = await postAgentSend({
        thread_id: "33333333-3333-4333-8333-333333333333",
        run_id: "run-queue-test",
        tool_call_id: "tc-post-dispatch-timeout",
        turn_platform: "discord",
        target: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        text: "ambiguous timeout message",
      });
      expect(duplicateAfterUnknownRes.statusCode).toBe(200);
      expect(duplicateAfterUnknownRes.body).toMatchObject({
        status: "unknown",
        code: "in_flight",
      });
      // Request count stays fixed at 2: no duplicate HTTP POST after post-dispatch timeout!
      expect(messagePostAttempts).toBe(2);
    } finally {
      await app.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Gateway lifecycle
// ---------------------------------------------------------------------------

describe("DiscordGatewayClient", () => {
  it("identifies, marks connected on READY, and heartbeats with the last sequence", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { client } = makeGateway(timers, sockets);
    await client.start();
    const ws = sockets[0]!;
    await helloAndReady(ws);
    expect(client.isConnected()).toBe(true);
    const identify = ws.sent.map((s) => JSON.parse(s) as { op: number; d?: { intents?: number } }).find((p) => p.op === 2);
    expect(identify?.d?.intents).toBe(1 | 512 | 1024 | 4096 | 8192 | 32768);
    timers.advance(0);
    await flushMicrotasks();
    const heartbeat = ws.sent.map((s) => JSON.parse(s) as { op: number; d?: unknown }).find((p) => p.op === 1);
    expect(heartbeat?.d).toBe(1);
    await client.stop();
  });

  it("reconnects as a zombie when a heartbeat ACK never arrives using a resumable close code", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { client, errors } = makeGateway(timers, sockets);
    await client.start();
    const ws = sockets[0]!;
    await helloAndReady(ws);
    timers.advance(0); // first heartbeat
    await flushMicrotasks();
    timers.advance(1000); // ACK window expires
    await flushMicrotasks();
    expect(errors.some((e) => /ACK timeout/.test(e.message))).toBe(true);
    expect(ws.lastCloseCode).toBe(RESUMABLE_GATEWAY_CLOSE_CODE);
    expect(ws.lastCloseCode).not.toBe(1000);
    expect(ws.lastCloseCode).not.toBe(1001);
    timers.advance(2000);
    await flushMicrotasks();
    expect(sockets.length).toBeGreaterThanOrEqual(2);
    const ws2 = sockets[1]!;
    ws2.open();
    ws2.receive({ op: 10, d: { heartbeat_interval: 1000 } });
    await flushMicrotasks();
    const ops = ws2.sent.map((s) => (JSON.parse(s) as { op: number }).op);
    expect(ops).toContain(6); // Resume
    await client.stop();
  });

  it("resumes with session id and sequence after a resumable close", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { client } = makeGateway(timers, sockets);
    await client.start();
    const ws = sockets[0]!;
    await helloAndReady(ws, 7);
    ws.serverClose(1001);
    await flushMicrotasks();
    timers.advance(2000);
    await flushMicrotasks();
    const ws2 = sockets[1]!;
    ws2.open();
    ws2.receive({ op: 10, d: { heartbeat_interval: 1000 } });
    await flushMicrotasks();
    const resume = ws2.sent.map((s) => JSON.parse(s) as { op: number; d?: { session_id?: string; seq?: number } }).find((p) => p.op === 6);
    expect(resume?.d?.session_id).toBe("sess-1");
    expect(resume?.d?.seq).toBe(7);
    await client.stop();
  });

  it("falls back to a fresh Identify after a non-resumable Invalid Session", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { client } = makeGateway(timers, sockets);
    await client.start();
    const ws = sockets[0]!;
    await helloAndReady(ws, 3);
    ws.receive({ op: 9, d: false });
    await flushMicrotasks();
    expect(ws.lastCloseCode).toBe(RESUMABLE_GATEWAY_CLOSE_CODE);
    timers.advance(5000);
    await flushMicrotasks();
    const ws2 = sockets[sockets.length - 1]!;
    if (ws2 !== ws) {
      ws2.open();
      ws2.receive({ op: 10, d: { heartbeat_interval: 1000 } });
      await flushMicrotasks();
      const ops = ws2.sent.map((s) => (JSON.parse(s) as { op: number }).op);
      expect(ops).toContain(2);
      expect(ops).not.toContain(6);
    } else {
      throw new Error("expected a reconnect socket after Invalid Session");
    }
    await client.stop();
  });

  it("retains received messages across a fresh Identify until inbound persistence succeeds", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    let blocked = true;
    const accepted: unknown[] = [];
    const client = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: { setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn, now: () => timers.nowMs, random: () => 0 },
      onDispatch: async (name, data) => {
        if (name !== "MESSAGE_CREATE") return;
        if (blocked) throw new Error("inbound persistence unavailable");
        accepted.push(data);
      },
      onError: () => {},
    });
    try {
      await client.start();
      await helloAndReady(sockets[0]!);
      sockets[0]!.receive({ op: 0, s: 2, t: "MESSAGE_CREATE", d: { id: "old-message" } });
      await new Promise<void>((resolve) => setImmediate(resolve));
      sockets[0]!.receive({ op: 9, d: false });
      timers.advance(5000);
      await new Promise<void>((resolve) => setImmediate(resolve));
      blocked = false;
      const nextSocket = sockets[sockets.length - 1]!;
      expect(nextSocket).not.toBe(sockets[0]);
      await helloAndReady(nextSocket);
      nextSocket.receive({ op: 0, s: 2, t: "MESSAGE_CREATE", d: { id: "new-message" } });
      await new Promise<void>((resolve) => setImmediate(resolve));
      expect(accepted).toEqual([{ id: "old-message" }, { id: "new-message" }]);
    } finally {
      await client.stop();
    }
  });

  it("preserves single ordered delivery when an in-flight handler outlives a new READY, reuses sequence numbers, and recovers after consecutive I-1 failures", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const accepted: string[] = [];

    let resolveInflightOld!: () => void;
    const inflightOldGate = new Promise<void>((r) => { resolveInflightOld = r; });
    let resolveInflightStarted!: () => void;
    const inflightStarted = new Promise<void>((r) => { resolveInflightStarted = r; });

    let resolveNewSeq2Gate!: () => void;
    const newSeq2Gate = new Promise<void>((r) => { resolveNewSeq2Gate = r; });
    let resolveNewSeq2Started!: () => void;
    const newSeq2Started = new Promise<void>((r) => { resolveNewSeq2Started = r; });

    let consecutiveFailuresLeft = 3;
    let resolveRetry4Accepted!: () => void;
    const retry4Accepted = new Promise<void>((r) => { resolveRetry4Accepted = r; });

    const client = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onDispatch: async (name, data) => {
        if (name !== "MESSAGE_CREATE") return;
        const msgId = (data as { id: string }).id;
        if (msgId === "old-inflight-seq5") {
          resolveInflightStarted();
          await inflightOldGate;
          accepted.push(msgId);
          return;
        }
        if (msgId === "new-seq2") {
          resolveNewSeq2Started();
          await newSeq2Gate;
          accepted.push(msgId);
          return;
        }
        if (msgId === "retry-after-3-failures") {
          if (consecutiveFailuresLeft > 0) {
            consecutiveFailuresLeft -= 1;
            throw new Error(`consecutive I-1 failure (${consecutiveFailuresLeft} remaining)`);
          }
          accepted.push(msgId);
          resolveRetry4Accepted();
          return;
        }
        accepted.push(msgId);
      },
      onError: () => {},
    });

    try {
      await client.start();
      const ws1 = sockets[0]!;
      await helloAndReady(ws1, 1);

      // 1. Old session receives s=5 and its async handler remains in-flight across InvalidSession(false) + fresh READY.
      ws1.receive({ op: 0, s: 5, t: "MESSAGE_CREATE", d: { id: "old-inflight-seq5" } });
      await inflightStarted;
      expect(client.getLastReceivedSequence()).toBe(5);
      expect(client.getSequence()).toBe(1);

      ws1.receive({ op: 9, d: false });
      timers.advance(5000);
      await flushMicrotasks();

      const ws2 = sockets[sockets.length - 1]!;
      expect(ws2).not.toBe(ws1);
      ws2.open();
      ws2.receive({ op: 10, d: { heartbeat_interval: 60000 } });
      await flushMicrotasks();
      ws2.receive({
        op: 0,
        s: 1,
        t: "READY",
        d: { session_id: "sess-2", resume_gateway_url: "wss://resume2.example.gg", user: { id: BOT_ID } },
      });
      // New session reuses sequence numbers s=2 and s=5 while old-inflight-seq5 is still awaiting.
      ws2.receive({ op: 0, s: 2, t: "MESSAGE_CREATE", d: { id: "new-seq2" } });
      ws2.receive({ op: 0, s: 5, t: "MESSAGE_CREATE", d: { id: "new-reused-seq5" } });
      await flushMicrotasks();

      expect(accepted).toEqual([]);
      expect(client.getLastReceivedSequence()).toBe(5);
      expect(client.getSequence()).toBe(1);

      // 2. Finish in-flight old-session message (s=5): it must NOT advance the new session's acked sequence (still 1).
      resolveInflightOld();
      await newSeq2Started;
      expect(accepted).toEqual(["old-inflight-seq5"]);
      expect(client.getSequence()).toBe(1);

      // 3. Finish new-seq2 (s=2) and new-reused-seq5 (s=5): acked sequence advances to 5 in the new session.
      resolveNewSeq2Gate();
      await flushMicrotasks();
      expect(accepted).toEqual(["old-inflight-seq5", "new-seq2", "new-reused-seq5"]);
      expect(client.getSequence()).toBe(5);

      // 4. Consecutive I-1 failures (3 failures + 4th attempt succeeds via retry backoff timers).
      ws2.receive({ op: 0, s: 6, t: "MESSAGE_CREATE", d: { id: "retry-after-3-failures" } });
      await flushMicrotasks();
      expect(consecutiveFailuresLeft).toBe(2);
      expect(client.getSequence()).toBe(5);

      timers.advance(500);
      await flushMicrotasks();
      expect(consecutiveFailuresLeft).toBe(1);
      expect(client.getSequence()).toBe(5);

      timers.advance(1000);
      await flushMicrotasks();
      expect(consecutiveFailuresLeft).toBe(0);
      expect(client.getSequence()).toBe(5);

      timers.advance(2000);
      await retry4Accepted;
      await flushMicrotasks();
      expect(accepted).toEqual([
        "old-inflight-seq5",
        "new-seq2",
        "new-reused-seq5",
        "retry-after-3-failures",
      ]);
      expect(client.getSequence()).toBe(6);
    } finally {
      await client.stop();
      expect(timers.pendingCount).toBe(0);
    }
  });

  it("does not reconnect after a fatal close code", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { client, errors } = makeGateway(timers, sockets);
    await client.start();
    const ws = sockets[0]!;
    await helloAndReady(ws);
    ws.serverClose(4004); // AuthenticationFailed
    await flushMicrotasks();
    timers.advance(60_000);
    await flushMicrotasks();
    expect(sockets).toHaveLength(1);
    expect(client.isConnected()).toBe(false);
    expect(errors.some((e) => /Fatal/.test(e.message))).toBe(true);
    await client.stop();
  });

  it("stop closes the socket with 1000 and prevents any later reconnect", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { client } = makeGateway(timers, sockets);
    await client.start();
    const ws = sockets[0]!;
    await helloAndReady(ws);
    await client.stop();
    expect(ws.closeCalls).toBeGreaterThanOrEqual(1);
    expect(ws.lastCloseCode).toBe(1000);
    expect(client.isConnected()).toBe(false);
    timers.advance(120_000);
    await flushMicrotasks();
    expect(sockets).toHaveLength(1);
    expect(timers.pendingCount).toBe(0);
  });

  it("enforces /gateway/bot session_start_limit, waits on remaining=0, skips budget for Resume, and cancels waits on stop()", async () => {
    // 1. Validate /gateway/bot response structure: missing/invalid session_start_limit must fail explicitly.
    const { rest: badRest } = makeRest(async () => jsonResponse({ url: "wss://gateway.discord.gg" }));
    await expect(fetchDiscordGatewayInfo(badRest)).rejects.toThrow(/invalid gateway info/i);

    // 2. Exhausted quota (remaining: 0) waits for reset_after, refreshes budget, then Identifies;
    //    subsequent Resume does not consume session_start_limit or wait on identifyLimiter.
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const errors: Error[] = [];
    let gatewayInfoCalls = 0;
    const client = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets.push(ws);
        return ws;
      },
      fetchGatewayInfo: async (): Promise<DiscordGatewayInfo> => {
        gatewayInfoCalls += 1;
        if (gatewayInfoCalls === 1) {
          throw new Error("gateway/bot temporary 503");
        }
        if (gatewayInfoCalls === 2) {
          return {
            url: "wss://gateway.discord.gg",
            session_start_limit: { total: 1000, remaining: 0, reset_after: 8000, max_concurrency: 1 },
          };
        }
        return {
          url: "wss://gateway.discord.gg",
          session_start_limit: { total: 1000, remaining: 1, reset_after: 86_400_000, max_concurrency: 1 },
        };
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onError: (err) => { errors.push(err); },
    });

    // Call 1 fails -> does not pretend budget exists, does not open a socket yet, schedules retry.
    await client.start();
    expect(sockets).toHaveLength(0);
    expect(errors.some((e) => /503/.test(e.message))).toBe(true);

    // Reconnect timer fires -> Call 2 returns remaining: 0, reset_after: 8000 -> opens socket.
    timers.advance(2000);
    await flushMicrotasks();
    expect(sockets).toHaveLength(1);
    const ws1 = sockets[0]!;
    ws1.open();
    ws1.receive({ op: 10, d: { heartbeat_interval: 30_000 } });
    await flushMicrotasks();

    // Must NOT send Identify while remaining === 0.
    expect(ws1.sent.map((s) => (JSON.parse(s) as { op: number }).op)).not.toContain(2);
    timers.advance(7999);
    await flushMicrotasks();
    expect(ws1.sent.map((s) => (JSON.parse(s) as { op: number }).op)).not.toContain(2);

    // At 8000ms, reset_after completes -> Call 3 refreshes remaining: 1 -> sends Identify and decrements remaining to 0.
    timers.advance(1);
    await flushMicrotasks();
    expect(gatewayInfoCalls).toBe(3);
    expect(ws1.sent.map((s) => (JSON.parse(s) as { op: number }).op)).toContain(2);
    expect(client.getSessionStartLimit()?.remaining).toBe(0);

    ws1.receive({
      op: 0,
      s: 4,
      t: "READY",
      d: { session_id: "sess-quota", resume_gateway_url: "wss://resume.example.gg", user: { id: BOT_ID } },
    });
    await flushMicrotasks();

    // Disconnect and Resume while remaining is 0: Resume must succeed immediately without waiting or consuming quota.
    ws1.receive({ op: 7 }); // OP.Reconnect
    await flushMicrotasks();
    expect(ws1.lastCloseCode).toBe(RESUMABLE_GATEWAY_CLOSE_CODE);
    timers.advance(2000);
    await flushMicrotasks();
    const ws2 = sockets[1]!;
    ws2.open();
    ws2.receive({ op: 10, d: { heartbeat_interval: 30_000 } });
    await flushMicrotasks();
    const resumePayload = ws2.sent
      .map((s) => JSON.parse(s) as { op: number; d?: { session_id?: string; seq?: number } })
      .find((p) => p.op === 6);
    expect(resumePayload?.d).toEqual({
      token: "test-bot-token",
      session_id: "sess-quota",
      seq: 4,
    });
    expect(gatewayInfoCalls).toBe(3);
    await client.stop();
    expect(timers.pendingCount).toBe(0);

    // 3. stop() while waiting for session_start_limit reset_after cancels pending wait timers cleanly.
    const timers2 = new FakeTimers();
    const sockets2: FakeWebSocket[] = [];
    const client2 = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 0, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets2.push(ws);
        return ws;
      },
      timerDeps: {
        setTimeoutFn: timers2.setTimeoutFn,
        clearTimeoutFn: timers2.clearTimeoutFn,
        now: () => timers2.nowMs,
        random: () => 0,
      },
    });
    await client2.start();
    const pendingWs = sockets2[0]!;
    pendingWs.open();
    pendingWs.receive({ op: 10, d: { heartbeat_interval: 30_000 } });
    await flushMicrotasks();
    expect(timers2.pendingCount).toBeGreaterThan(0);
    await client2.stop();
    expect(timers2.pendingCount).toBe(0);
    timers2.advance(120_000);
    await flushMicrotasks();
    expect(pendingWs.sent.map((s) => (JSON.parse(s) as { op: number }).op)).not.toContain(2);
  });

  it("retains unacknowledged I-1 dispatches across transient failure and Resume without skipping or reordering", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const errors: Error[] = [];
    let resolveFirstFailure!: () => void;
    const firstFailureObserved = new Promise<void>((r) => { resolveFirstFailure = r; });
    let resolveBothRegistered!: () => void;
    const bothRegistered = new Promise<void>((r) => { resolveBothRegistered = r; });

    const { rest } = makeRest(async (call) => {
      if (routeOf(call.url) === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (routeOf(call.url) === `/channels/${CHANNEL_ID}`) {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, name: "general" });
      }
      return jsonResponse({});
    });

    const gatewayClient = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 100, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets.push(ws);
        return ws;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onError: (err) => {
        errors.push(err);
        if (err.message.includes("transient I-1 failure on m-gw-1")) {
          resolveFirstFailure();
        }
      },
    });

    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      botUserId: BOT_ID,
      rest,
      gatewayClient,
    });

    const registered: string[] = [];
    const attemptsByMsg = new Map<string, number>();
    adapter.onInboundEvent(async (event: InboundChannelEvent) => {
      const count = (attemptsByMsg.get(event.messageId) ?? 0) + 1;
      attemptsByMsg.set(event.messageId, count);
      if (event.messageId === "m-gw-1" && count === 1) {
        throw new Error("transient I-1 failure on m-gw-1");
      }
      registered.push(`${event.messageId}:${event.text}`);
      if (registered.length === 2) {
        resolveBothRegistered();
      }
    });

    await adapter.start();
    const ws1 = sockets[0]!;
    await helloAndReady(ws1, 1);

    // Event s=2 arrives -> first I-1 attempt fails.
    ws1.receive({
      op: 0,
      s: 2,
      t: "MESSAGE_CREATE",
      d: guildMessage({ id: "m-gw-1", content: `<@${BOT_ID}> first` }),
    });
    // Event s=3 arrives right behind it while s=2 is still unacknowledged.
    ws1.receive({
      op: 0,
      s: 3,
      t: "MESSAGE_CREATE",
      d: guildMessage({ id: "m-gw-2", content: `<@${BOT_ID}> second` }),
    });
    await firstFailureObserved;

    // Heartbeat must report the latest received sequence (3), while Resume state stays at last acked sequence (1).
    expect(gatewayClient.getLastReceivedSequence()).toBe(3);
    expect(gatewayClient.getSequence()).toBe(1);
    expect(registered).toEqual([]);
    ws1.receive({ op: 1 }); // Server requests immediate heartbeat
    await flushMicrotasks();
    const heartbeats = ws1.sent
      .map((s) => JSON.parse(s) as { op: number; d?: unknown })
      .filter((p) => p.op === 1);
    expect(heartbeats[heartbeats.length - 1]?.d).toBe(3);

    // Disconnect before the retry timer fires; stale callbacks on ws1 must be ignored.
    ws1.serverClose(1001);
    ws1.receive({
      op: 0,
      s: 99,
      t: "MESSAGE_CREATE",
      d: guildMessage({ id: "m-stale", content: `<@${BOT_ID}> stale` }),
    });
    await flushMicrotasks();
    expect(gatewayClient.getLastReceivedSequence()).toBe(3);

    // Reconnect and Resume on ws2: Resume must request seq=1 (so s=2 is not skipped).
    timers.advance(2000);
    await flushMicrotasks();
    const ws2 = sockets[1]!;
    ws2.open();
    ws2.receive({ op: 10, d: { heartbeat_interval: 1000 } });
    await flushMicrotasks();

    const resumeMsg = ws2.sent
      .map((s) => JSON.parse(s) as { op: number; d?: { session_id?: string; seq?: number } })
      .find((p) => p.op === 6);
    expect(resumeMsg?.d).toEqual({
      token: "test-bot-token",
      session_id: "sess-1",
      seq: 1,
    });

    // Server replays s=2, s=3 and sends RESUMED.
    ws2.receive({
      op: 0,
      s: 2,
      t: "MESSAGE_CREATE",
      d: guildMessage({ id: "m-gw-1", content: `<@${BOT_ID}> first` }),
    });
    ws2.receive({
      op: 0,
      s: 3,
      t: "MESSAGE_CREATE",
      d: guildMessage({ id: "m-gw-2", content: `<@${BOT_ID}> second` }),
    });
    ws2.receive({ op: 0, s: 3, t: "RESUMED", d: {} });
    await bothRegistered;
    await vi.waitFor(() => expect(gatewayClient.getSequence()).toBe(3));

    // Original message registered once, in strict order before m-gw-2, and acked sequence advances to 3.
    expect(registered).toEqual(["m-gw-1:first", "m-gw-2:second"]);
    expect(attemptsByMsg.get("m-gw-1")).toBe(2);
    expect(attemptsByMsg.get("m-gw-2")).toBe(1);
    expect(gatewayClient.getSequence()).toBe(3);
    await adapter.stop();
  });

  it("acks one permanent dispatch failure and still delivers the following DM and guild event", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const seen: string[] = [];
    const client = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onDispatch: async (name, data) => {
        if (name !== "MESSAGE_CREATE") return;
        const id = (data as { id: string }).id;
        if (id === "perm-1") {
          throw new ChannelDeliveryError("missing channel", {
            outcome: "failed",
            code: "target_not_found",
          });
        }
        seen.push(id);
      },
      onError: () => {},
    });
    try {
      await client.start();
      const ws = sockets[0]!;
      await helloAndReady(ws, 1);
      ws.receive({ op: 0, s: 2, t: "MESSAGE_CREATE", d: { id: "perm-1" } });
      ws.receive({ op: 0, s: 3, t: "MESSAGE_CREATE", d: { id: "dm-2" } });
      ws.receive({ op: 0, s: 4, t: "MESSAGE_CREATE", d: { id: "guild-3" } });
      for (let i = 0; i < 4; i++) await flushMicrotasks();
      expect(seen).toEqual(["dm-2", "guild-3"]);
      expect(client.getSequence()).toBe(4);
      expect(client.isConnected()).toBe(true);
      expect(sockets).toHaveLength(1);
      advanceAckingHeartbeats(timers, sockets[0]!, 60_000);
      await flushMicrotasks();
      expect(sockets).toHaveLength(1);
      expect(seen).toEqual(["dm-2", "guild-3"]);
    } finally {
      await client.stop();
    }
  });

  it("retries a short 5xx without dropping the event or acknowledging it early", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    let failuresLeft = 2;
    const seen: string[] = [];
    const client = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onDispatch: async (name, data) => {
        if (name !== "MESSAGE_CREATE") return;
        const id = (data as { id: string }).id;
        if (id === "flaky" && failuresLeft > 0) {
          failuresLeft -= 1;
          throw new Error("temporary 503");
        }
        seen.push(id);
      },
      onError: () => {},
    });
    try {
      await client.start();
      const ws = sockets[0]!;
      await helloAndReady(ws, 1);
      ws.receive({ op: 0, s: 2, t: "MESSAGE_CREATE", d: { id: "flaky" } });
      ws.receive({ op: 0, s: 3, t: "MESSAGE_CREATE", d: { id: "after" } });
      await flushMicrotasks();
      expect(seen).toEqual([]);
      expect(failuresLeft).toBe(1);
      expect(client.getSequence()).toBe(1);

      advanceAckingHeartbeats(timers, ws, 500);
      await flushMicrotasks();
      expect(seen).toEqual([]);
      expect(failuresLeft).toBe(0);
      expect(client.getSequence()).toBe(1);

      advanceAckingHeartbeats(timers, ws, 1000);
      await flushMicrotasks();
      await flushMicrotasks();
      expect(seen).toEqual(["flaky", "after"]);
      expect(client.getSequence()).toBe(3);
      expect(sockets).toHaveLength(1);
      expect(client.isConnected()).toBe(true);
    } finally {
      await client.stop();
    }
  });

  it("does not give the same message a fresh retry budget after reconnect", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    let calls = 0;
    const client = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onDispatch: async (name) => {
        if (name !== "MESSAGE_CREATE") return;
        calls += 1;
        throw new Error("inbound 503");
      },
      onError: () => {},
    });
    try {
      await client.start();
      const ws = sockets[0]!;
      await helloAndReady(ws, 1);
      ws.receive({ op: 0, s: 2, t: "MESSAGE_CREATE", d: { id: "stuck-message" } });
      await flushMicrotasks();
      expect(calls).toBe(1);

      for (const delay of [500, 1000, 2000, 4000, 8000, 15_000, 15_000]) {
        advanceAckingHeartbeats(timers, sockets[0]!, delay);
        await flushMicrotasks();
      }
      expect(calls).toBe(8);
      expect(client.getSequence()).toBe(1);
      expect(sockets).toHaveLength(1);

      advanceAckingHeartbeats(timers, sockets[0]!, 2000);
      await flushMicrotasks();
      expect(sockets).toHaveLength(2);
      await helloAndReady(sockets[1]!, 1);
      const resume = sockets[1]!.sent
        .map((line) => JSON.parse(line) as { op: number; d?: { seq?: number } })
        .find((payload) => payload.op === 6);
      expect(resume?.d?.seq).toBe(1);
      expect(calls).toBe(9);
      expect(client.getSequence()).toBe(1);
      expect(client.isConnected()).toBe(true);

      advanceAckingHeartbeats(timers, sockets[1]!, 500);
      await flushMicrotasks();
      expect(calls).toBe(9);
      expect(sockets).toHaveLength(2);

      advanceAckingHeartbeats(timers, sockets[1]!, 14_499);
      await flushMicrotasks();
      expect(calls).toBe(9);
      advanceAckingHeartbeats(timers, sockets[1]!, 1);
      await flushMicrotasks();
      expect(calls).toBe(10);
      expect(client.getSequence()).toBe(1);
      expect(sockets).toHaveLength(2);
    } finally {
      await client.stop();
    }
  });

  it("tracks the absolute session_start_limit reset deadline across long-lived sessions, mid-wait disconnects, and static quotas", async () => {
    // 1. Exhausted after initial Identify (remaining: 1 -> 0, reset_after: 10_000 at t=0), stays online past t=10_000,
    //    then loses session (InvalidSession(false)): must refresh quota and Identify immediately without waiting another 10_000ms.
    const timers1 = new FakeTimers();
    const sockets1: FakeWebSocket[] = [];
    let infoCalls1 = 0;
    const client1 = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets1.push(ws);
        return ws;
      },
      fetchGatewayInfo: async (): Promise<DiscordGatewayInfo> => {
        infoCalls1 += 1;
        if (infoCalls1 === 1) {
          return {
            url: "wss://gateway.discord.gg",
            session_start_limit: { total: 1000, remaining: 1, reset_after: 10_000, max_concurrency: 1 },
          };
        }
        return {
          url: "wss://gateway.discord.gg",
          session_start_limit: { total: 1000, remaining: 5, reset_after: 86_400_000, max_concurrency: 1 },
        };
      },
      timerDeps: {
        setTimeoutFn: timers1.setTimeoutFn,
        clearTimeoutFn: timers1.clearTimeoutFn,
        now: () => timers1.nowMs,
        random: () => 0,
      },
    });

    await client1.start();
    const ws1a = sockets1[0]!;
    ws1a.open();
    ws1a.receive({ op: 10, d: { heartbeat_interval: 60_000 } });
    await flushMicrotasks();
    ws1a.receive({
      op: 0,
      s: 1,
      t: "READY",
      d: { session_id: "sess-long", resume_gateway_url: "wss://resume.example.gg", user: { id: BOT_ID } },
    });
    await flushMicrotasks();
    expect(client1.getSessionStartLimit()?.remaining).toBe(0);
    expect(infoCalls1).toBe(1);

    // Stay online for 15_000ms (past the t=10_000 reset deadline), then receive InvalidSession(false).
    timers1.nowMs = 15_000;
    ws1a.receive({ op: 9, d: false });
    await flushMicrotasks();
    timers1.advance(2_000); // Reconnect fires at t=17_000
    await flushMicrotasks();
    const ws1b = sockets1[1]!;
    ws1b.open();
    ws1b.receive({ op: 10, d: { heartbeat_interval: 60_000 } });
    await flushMicrotasks();

    // Deadline (10_000) has already passed at t=17_000 -> refreshes immediately and sends Identify without extra 10_000ms wait.
    expect(infoCalls1).toBe(2);
    expect(ws1b.sent.map((s) => (JSON.parse(s) as { op: number }).op)).toContain(2);
    expect(client1.getSessionStartLimit()?.remaining).toBe(4);
    await client1.stop();

    // 2. Socket disconnects while waiting for session_start_limit reset (deadline t=10_000):
    //    reconnect socket must only wait the remaining time until t=10_000, not a fresh 10_000ms cycle.
    const timers2 = new FakeTimers();
    const sockets2: FakeWebSocket[] = [];
    let infoCalls2 = 0;
    const client2 = new DiscordGatewayClient({
      token: "test-bot-token",
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets2.push(ws);
        return ws;
      },
      fetchGatewayInfo: async (): Promise<DiscordGatewayInfo> => {
        infoCalls2 += 1;
        if (infoCalls2 === 1) {
          return {
            url: "wss://gateway.discord.gg",
            session_start_limit: { total: 1000, remaining: 0, reset_after: 10_000, max_concurrency: 1 },
          };
        }
        return {
          url: "wss://gateway.discord.gg",
          session_start_limit: { total: 1000, remaining: 2, reset_after: 60_000, max_concurrency: 1 },
        };
      },
      timerDeps: {
        setTimeoutFn: timers2.setTimeoutFn,
        clearTimeoutFn: timers2.clearTimeoutFn,
        now: () => timers2.nowMs,
        random: () => 0,
      },
    });

    await client2.start();
    const ws2a = sockets2[0]!;
    ws2a.open();
    ws2a.receive({ op: 10, d: { heartbeat_interval: 60_000 } });
    await flushMicrotasks();

    // At t=3_000 (7_000ms remaining until t=10_000), ws2a disconnects; reconnect opens ws2b at t=5_000 (5_000ms remaining).
    timers2.advance(3_000);
    await flushMicrotasks();
    ws2a.serverClose(1006);
    await flushMicrotasks();
    timers2.advance(2_000);
    await flushMicrotasks();
    expect(timers2.nowMs).toBe(5_000);

    const ws2b = sockets2[1]!;
    ws2b.open();
    ws2b.receive({ op: 10, d: { heartbeat_interval: 60_000 } });
    await flushMicrotasks();

    // At t=9_999 (4_999ms after ws2b Hello), still before original deadline t=10_000 -> must NOT Identify yet.
    timers2.advance(4_999);
    await flushMicrotasks();
    expect(infoCalls2).toBe(1);
    expect(ws2b.sent.map((s) => (JSON.parse(s) as { op: number }).op)).not.toContain(2);

    // At t=10_000 (+1ms), original deadline is reached -> refreshes quota and sends Identify on ws2b!
    timers2.advance(1);
    await flushMicrotasks();
    expect(infoCalls2).toBe(2);
    expect(ws2b.sent.map((s) => (JSON.parse(s) as { op: number }).op)).toContain(2);
    expect(client2.getSessionStartLimit()?.remaining).toBe(1);
    await client2.stop();

    // 3. Static gatewayInfo without fetchGatewayInfo must never fabricate unlimited quota or re-issue the same quota.
    const timers3 = new FakeTimers();
    const sockets3: FakeWebSocket[] = [];
    const errors3: Error[] = [];
    const client3 = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 1, reset_after: 5_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets3.push(ws);
        return ws;
      },
      timerDeps: {
        setTimeoutFn: timers3.setTimeoutFn,
        clearTimeoutFn: timers3.clearTimeoutFn,
        now: () => timers3.nowMs,
        random: () => 0,
      },
      onError: (err) => { errors3.push(err); },
    });

    await client3.start();
    const ws3a = sockets3[0]!;
    await helloAndReady(ws3a, 1);
    expect(client3.getSessionStartLimit()?.remaining).toBe(0);

    // Lose session after reset_after; without fetchGatewayInfo, must NOT mint new quota or send Identify on ws3b.
    timers3.nowMs = 10_000;
    ws3a.receive({ op: 9, d: false });
    await flushMicrotasks();
    timers3.advance(2_000);
    await flushMicrotasks();
    const ws3b = sockets3[1]!;
    ws3b.open();
    ws3b.receive({ op: 10, d: { heartbeat_interval: 60_000 } });
    await flushMicrotasks();
    expect(ws3b.sent.map((s) => (JSON.parse(s) as { op: number }).op)).not.toContain(2);
    expect(errors3.some((e) => /session_start_limit exhausted/i.test(e.message))).toBe(true);
    await client3.stop();
  });

  it("cancels start() when stop() runs during /users/@me, deduplicates concurrent start(), and allows clean restart without nesting onReady", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    let meCalls = 0;
    let releaseFirstMe!: () => void;
    const firstMeEntered = new Promise<void>((resolve) => {
      releaseFirstMe = resolve;
    });

    const { rest } = makeRest(async (call) => {
      if (routeOf(call.url) === "/users/@me") {
        meCalls += 1;
        if (meCalls === 1) {
          releaseFirstMe();
          await new Promise<void>((_resolve, reject) => {
            call.init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
          });
        }
        return jsonResponse({ id: BOT_ID });
      }
      return jsonResponse({});
    });

    const readyEvents: string[] = [];
    const gatewayClient = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 10, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets.push(ws);
        return ws;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onReady: (info) => {
        readyEvents.push(info.sessionId);
      },
    });

    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      rest,
      gatewayClient,
    });

    // 1. Start adapter, then call stop() while /users/@me is still in-flight.
    const start1 = adapter.start();
    await firstMeEntered;
    await adapter.stop();
    await start1;

    // Cancelled start1 must NOT mark adapter started or open a gateway socket.
    expect(adapter.isConnected()).toBe(false);
    expect(sockets).toHaveLength(0);

    // 2. Concurrent start() calls after stop() must share a single startup and open only one socket.
    await Promise.all([adapter.start(), adapter.start(), adapter.start()]);
    expect(meCalls).toBe(2);
    expect(sockets).toHaveLength(1);
    await helloAndReady(sockets[0]!, 1);
    expect(adapter.isConnected()).toBe(true);
    expect(readyEvents).toEqual(["sess-1"]);

    // 3. Stop and start again: onReady must NOT be nested/duplicated across restarts.
    await adapter.stop();
    expect(adapter.isConnected()).toBe(false);
    await adapter.start();
    expect(sockets).toHaveLength(2);
    await helloAndReady(sockets[1]!, 1);
    expect(readyEvents).toEqual(["sess-1", "sess-1"]);
    await adapter.stop();
  });

  it("ignores late /gateway/bot success or failure from a stopped start() after a subsequent start() has connected", async () => {
    for (const lateOutcome of ["late-success", "late-failure"] as const) {
      const timers = new FakeTimers();
      const sockets: FakeWebSocket[] = [];
      const errors: Error[] = [];
      let meCalls = 0;
      let gatewayInfoCalls = 0;

      let markFirstGatewayInfoEntered!: () => void;
      const firstGatewayInfoEntered = new Promise<void>((r) => {
        markFirstGatewayInfoEntered = r;
      });
      let resolveFirstGatewayInfo!: (info: DiscordGatewayInfo) => void;
      let rejectFirstGatewayInfo!: (err: Error) => void;
      const firstGatewayInfoPromise = new Promise<DiscordGatewayInfo>((resolve, reject) => {
        resolveFirstGatewayInfo = resolve;
        rejectFirstGatewayInfo = reject;
      });

      const { rest } = makeRest(async (call) => {
        if (routeOf(call.url) === "/users/@me") {
          meCalls += 1;
          return jsonResponse({ id: meCalls === 1 ? "stale-bot-user" : BOT_ID });
        }
        return jsonResponse({});
      });

      const fetchGatewayInfo = async (): Promise<DiscordGatewayInfo> => {
        gatewayInfoCalls += 1;
        if (gatewayInfoCalls === 1) {
          markFirstGatewayInfoEntered();
          return await firstGatewayInfoPromise;
        }
        return {
          url: "wss://fresh.discord.gg",
          session_start_limit: {
            total: 1000,
            remaining: 42,
            reset_after: 60_000,
            max_concurrency: 1,
          },
        };
      };

      const gatewayClient = new DiscordGatewayClient({
        token: "test-bot-token",
        fetchGatewayInfo,
        webSocketFactory: (url) => {
          const ws = new FakeWebSocket(url);
          sockets.push(ws);
          return ws;
        },
        timerDeps: {
          setTimeoutFn: timers.setTimeoutFn,
          clearTimeoutFn: timers.clearTimeoutFn,
          now: () => timers.nowMs,
          random: () => 0,
        },
        onError: (err) => {
          errors.push(err);
        },
      });

      const adapter = new DiscordChannelAdapter({
        botToken: "test-bot-token",
        rest,
        gatewayClient,
        fetchGatewayInfoFn: fetchGatewayInfo,
      });

      // 1. start1 finishes /users/@me and blocks inside /gateway/bot (refreshGatewayInfo).
      const start1 = adapter.start();
      await firstGatewayInfoEntered;
      expect(sockets).toHaveLength(0);

      // 2. stop() cancels start1's lifecycle; then start2 runs and connects cleanly.
      await adapter.stop();
      await adapter.start();
      expect(sockets).toHaveLength(1);
      const activeWs = sockets[0]!;
      expect(activeWs.url).toContain("wss://fresh.discord.gg");
      await helloAndReady(activeWs, 1);

      expect(adapter.isConnected()).toBe(true);
      expect(adapter.getBotUserId()).toBe(BOT_ID);
      expect(gatewayClient.getSessionStartLimit()?.remaining).toBe(41);

      // 3. Now start1's delayed /gateway/bot settles (either late success or late failure).
      if (lateOutcome === "late-success") {
        resolveFirstGatewayInfo({
          url: "wss://stale.discord.gg",
          session_start_limit: {
            total: 1000,
            remaining: 2,
            reset_after: 5_000,
            max_concurrency: 1,
          },
        });
      } else {
        rejectFirstGatewayInfo(new Error("late /gateway/bot failure"));
      }
      await start1;
      await flushMicrotasks();

      // Neither late outcome may stop the active gateway, close activeWs, open another socket,
      // overwrite bot identity or session_start_limit quota, emit stale errors, or schedule a reconnect timer.
      expect(adapter.isConnected()).toBe(true);
      expect(activeWs.readyState).toBe(1);
      expect(activeWs.closeCalls).toBe(0);
      expect(sockets).toHaveLength(1);
      expect(adapter.getBotUserId()).toBe(BOT_ID);
      expect(gatewayClient.getSessionStartLimit()?.remaining).toBe(41);
      expect(errors).toHaveLength(0);

      // Advance timers past any reconnect backoff window and ACK each heartbeat (including initial t=0 jitter heartbeat).
      timers.advance(0);
      activeWs.receive({ op: 11, d: null });
      timers.advance(1_000);
      activeWs.receive({ op: 11, d: null });
      timers.advance(1_000);
      activeWs.receive({ op: 11, d: null });
      timers.advance(1_000);
      activeWs.receive({ op: 11, d: null });
      await flushMicrotasks();
      expect(adapter.isConnected()).toBe(true);
      expect(activeWs.closeCalls).toBe(0);
      expect(sockets).toHaveLength(1);

      await adapter.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Inbound parsing, ordering, dedupe
// ---------------------------------------------------------------------------

function guildMessage(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "msg-1",
    channel_id: CHANNEL_ID,
    guild_id: GUILD_ID,
    content: `<@${BOT_ID}> hello team`,
    author: { id: USER_ID, username: "alice", global_name: "Alice" },
    mentions: [{ id: BOT_ID, username: "DaltonBot" }],
    attachments: [],
    ...overrides,
  };
}

describe("Discord inbound parsing", () => {
  it("parses a guild mention into a Dalton group event and strips the bot mention", () => {
    const event = parseDiscordInboundMessage(guildMessage(), {
      botUserId: BOT_ID,
      channelContext: { channelId: CHANNEL_ID, type: 0, guildId: GUILD_ID, name: "research" },
    });
    expect(event).not.toBeNull();
    expect(event!.chatType).toBe("group");
    expect(event!.chatAddress).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}`);
    expect(event!.threadScopeKey).toBe(`chat:${GUILD_ID}/${CHANNEL_ID}`);
    expect(event!.text).toBe("hello team");
    expect(event!.isGroupTrigger).toBe(true);
    expect(event!.platformUserId).toBe(USER_ID);
  });

  it("strips the bot token by structural id first while preserving same-named users and plain text", () => {
    const sameNameUserId = "222333444";
    const event = parseDiscordInboundMessage(
      guildMessage({
        content: `<@!${BOT_ID}> ask <@${sameNameUserId}> and literal @DaltonBot about Q4`,
        mentions: [
          { id: BOT_ID, username: "DaltonBot", global_name: "DaltonBot" },
          { id: sameNameUserId, username: "DaltonBot", global_name: "DaltonBot" },
        ],
      }),
      {
        botUserId: BOT_ID,
        channelContext: { channelId: CHANNEL_ID, type: 0, guildId: GUILD_ID },
      },
    );
    expect(event).not.toBeNull();
    expect(event!.isGroupTrigger).toBe(true);
    expect(event!.text).toBe("ask @DaltonBot and literal @DaltonBot about Q4");
  });

  it("parses DMs and requires explicit @bot mention for group slash commands", async () => {
    const dm = parseDiscordInboundMessage(
      guildMessage({ guild_id: undefined, content: "/workspace alpha", mentions: [] }),
      { botUserId: BOT_ID, channelContext: { channelId: CHANNEL_ID, type: 1 } },
    );
    expect(dm!.chatType).toBe("dm");
    expect(dm!.chatAddress).toBe("discord:@me");
    expect(dm!.threadScopeKey).toBe(`dm:${USER_ID}`);
    expect(dm!.text).toBe("/workspace alpha");
    expect(dm!.isGroupTrigger).toBeUndefined();

    const mentionedThreadSlash = parseDiscordInboundMessage(
      guildMessage({ channel_id: THREAD_ID, content: `<@${BOT_ID}> /new` }),
      {
        botUserId: BOT_ID,
        channelContext: { channelId: THREAD_ID, type: 11, guildId: GUILD_ID, parentId: CHANNEL_ID },
      },
    );
    expect(mentionedThreadSlash!.chatAddress).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}`);
    expect(mentionedThreadSlash!.threadScopeKey).toBe(`chat:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`);
    expect(mentionedThreadSlash!.text).toBe("/new");
    expect(mentionedThreadSlash!.isGroupTrigger).toBe(true);

    const mentionedMain = parseDiscordInboundMessage(
      guildMessage({ content: `<@${BOT_ID}> /main` }),
      { botUserId: BOT_ID, channelContext: { channelId: CHANNEL_ID, type: 0, guildId: GUILD_ID } },
    );
    expect(mentionedMain!.text).toBe("/main");
    expect(mentionedMain!.isGroupTrigger).toBe(true);

    const mentionedWorkspace = parseDiscordInboundMessage(
      guildMessage({ content: `<@${BOT_ID}> /workspace macro` }),
      { botUserId: BOT_ID, channelContext: { channelId: CHANNEL_ID, type: 0, guildId: GUILD_ID } },
    );
    expect(mentionedWorkspace!.text).toBe("/workspace macro");
    expect(mentionedWorkspace!.isGroupTrigger).toBe(true);

    // Unmentioned group slash commands, plain-text <@botId> without structured mention, and @everyone broadcasts do NOT trigger:
    const ctx = { channelId: CHANNEL_ID, type: 0, guildId: GUILD_ID };
    for (const unmentionedPayload of [
      guildMessage({ id: "u-main", content: "/main", mentions: [] }),
      guildMessage({ id: "u-new", content: "/new", mentions: [] }),
      guildMessage({ id: "u-ws", content: "/workspace macro", mentions: [] }),
      guildMessage({ id: "u-plain", content: `<@${BOT_ID}> /new`, mentions: [] }),
      guildMessage({ id: "u-everyone", content: "@everyone /new", mentions: [], mention_everyone: true }),
    ]) {
      const parsed = parseDiscordInboundMessage(unmentionedPayload, { botUserId: BOT_ID, channelContext: ctx });
      expect(parsed!.isGroupTrigger).toBe(false);
    }

    // Adapter + Orchestrator must neither invoke handler nor write I-1 for unmentioned group slash commands:
    const backend = createMockBackend();
    const { rest } = makeRest(async () => jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID }));
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const handlerSpy: InboundChannelEvent[] = [];
    adapter.onInboundEvent(async (ev) => { handlerSpy.push(ev); });
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_BACKEND_URL: "http://127.0.0.1:8000",
      WEB_BASE_URL: "https://app.ginlix.ai",
    });
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["discord", adapter]]),
      backoffDelayFn: async () => undefined,
    });

    await adapter.dispatchMessage(guildMessage({ id: "g-unm-1", content: "/main", mentions: [] }) as never);
    await adapter.dispatchMessage(guildMessage({ id: "g-unm-2", content: "/new", mentions: [] }) as never);
    await adapter.dispatchMessage(guildMessage({ id: "g-unm-3", content: "/workspace alpha", mentions: [] }) as never);
    await adapter.dispatchMessage(guildMessage({ id: "g-unm-4", content: `<@${BOT_ID}> /new`, mentions: [] }) as never);
    await adapter.dispatchMessage(
      guildMessage({ id: "g-unm-5", content: "@everyone /workspace alpha", mentions: [], mention_everyone: true }) as never,
    );

    expect(handlerSpy).toHaveLength(0);
    expect(backend.registerAndResolveInbound).not.toHaveBeenCalled();
    await app.stop();
  });

  it("ignores bot, webhook, and own messages, and non-trigger guild chatter", () => {
    const ctx = { channelId: CHANNEL_ID, type: 0, guildId: GUILD_ID };
    expect(parseDiscordInboundMessage(guildMessage({ author: { id: "x", bot: true } }), { botUserId: BOT_ID, channelContext: ctx })).toBeNull();
    expect(parseDiscordInboundMessage(guildMessage({ webhook_id: "wh-1" }), { botUserId: BOT_ID, channelContext: ctx })).toBeNull();
    expect(parseDiscordInboundMessage(guildMessage({ author: { id: BOT_ID } }), { botUserId: BOT_ID, channelContext: ctx })).toBeNull();
    const chatter = parseDiscordInboundMessage(guildMessage({ content: "no mention", mentions: [] }), { botUserId: BOT_ID, channelContext: ctx });
    expect(chatter!.isGroupTrigger).toBe(false);
  });

  it("keeps same-scope order, retries after an I-1 failure, and dedupes only after success", async () => {
    const { rest } = makeRest(async () => jsonResponse({ id: BOT_ID }));
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const seen: string[] = [];
    let failFirst = true;
    adapter.onInboundEvent(async (event: InboundChannelEvent) => {
      seen.push(event.messageId);
      if (failFirst && event.messageId === "m-order-1") {
        failFirst = false;
        throw new Error("temporary I-1 failure");
      }
    });
    const first = guildMessage({ id: "m-order-1", content: `<@${BOT_ID}> first` });
    await expect(adapter.dispatchMessage(first as never)).rejects.toThrow("temporary I-1 failure");
    // Not deduped: the same message can be delivered again after the failure.
    await adapter.dispatchMessage(first as never);
    await adapter.dispatchMessage(guildMessage({ id: "m-order-2", content: `<@${BOT_ID}> second` }) as never);
    // Deduped after success.
    await adapter.dispatchMessage(first as never);
    expect(seen).toEqual(["m-order-1", "m-order-1", "m-order-2"]);
  });

  it("preserves same-session order when an earlier /workspace command awaits REST channel lookup and rejects inaccurate scope on lookup failure", async () => {
    let resolveChannelLookup: ((res: Response) => void) | undefined;
    let failLookup = true;
    const { rest } = makeRest(async (call) => {
      if (routeOf(call.url) === `/channels/${THREAD_ID}`) {
        if (failLookup) {
          return jsonResponse({ message: "server error" }, 500);
        }
        return await new Promise<Response>((resolve) => {
          resolveChannelLookup = resolve;
        });
      }
      return jsonResponse({});
    });

    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const received: Array<{ id: string; text: string; scope: string; address: string }> = [];
    adapter.onInboundEvent(async (event) => {
      received.push({
        id: event.messageId,
        text: event.text,
        scope: event.threadScopeKey,
        address: event.chatAddress,
      });
    });

    const firstMsg = guildMessage({
      id: "m-seq-1",
      channel_id: THREAD_ID,
      content: `<@${BOT_ID}> /workspace quant`,
    });
    const secondMsg = guildMessage({
      id: "m-seq-2",
      channel_id: THREAD_ID,
      content: `<@${BOT_ID}> analyze nvda`,
    });

    // 1. When channel lookup fails, dispatchMessage must reject and never confirm with an inaccurate fallback scope.
    await expect(adapter.dispatchMessage(firstMsg as never)).rejects.toThrow(/channel lookup failed/i);
    expect(received).toHaveLength(0);

    // 2. Now allow REST lookup to succeed asynchronously; dispatch firstMsg (uncached, awaits REST)
    //    and while REST is in-flight, warm the cache via THREAD_CREATE and dispatch secondMsg.
    failLookup = false;
    const p1 = adapter.dispatchMessage(firstMsg as never);
    await flushMicrotasks();

    await adapter.handleDispatch("THREAD_CREATE", {
      id: THREAD_ID,
      type: 11,
      guild_id: GUILD_ID,
      parent_id: CHANNEL_ID,
      name: "nvda-thread",
    });
    const p2 = adapter.dispatchMessage(secondMsg as never);
    await flushMicrotasks();

    // secondMsg must not overtake firstMsg even though cache is now warm.
    expect(received).toHaveLength(0);

    resolveChannelLookup!(
      jsonResponse({
        id: THREAD_ID,
        type: 11,
        guild_id: GUILD_ID,
        parent_id: CHANNEL_ID,
        name: "nvda-thread",
      }),
    );
    await Promise.all([p1, p2]);

    expect(received).toEqual([
      {
        id: "m-seq-1",
        text: "/workspace quant",
        scope: `chat:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`,
        address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
      },
      {
        id: "m-seq-2",
        text: "analyze nvda",
        scope: `chat:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`,
        address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
      },
    ]);
  });

  it("acks a 404 and a thread with no parent, then recovers one 503 without losing the following DM", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const otherGuild = "700000009";
    const otherChannel = "700000010";
    const unavailableChannel = "700000020";
    let unavailableReads = 0;
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}`) {
        return jsonResponse({ message: "Unknown Channel", code: 10003 }, 404);
      }
      if (route === `/channels/${THREAD_ID}`) {
        return jsonResponse({ id: THREAD_ID, type: 11, guild_id: GUILD_ID, name: "orphan" });
      }
      if (route === `/channels/${otherChannel}`) {
        return jsonResponse({ id: otherChannel, type: 0, guild_id: otherGuild, name: "other" });
      }
      if (route === `/channels/${unavailableChannel}`) {
        unavailableReads += 1;
        if (unavailableReads === 1) return jsonResponse({ message: "upstream" }, 503);
        return jsonResponse({ id: unavailableChannel, type: 0, guild_id: GUILD_ID, name: "retry" });
      }
      return jsonResponse({});
    });
    const gatewayClient = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 100, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onError: () => {},
    });
    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      botUserId: BOT_ID,
      rest,
      gatewayClient,
    });
    const seen: string[] = [];
    adapter.onInboundEvent(async (event) => {
      seen.push(event.messageId);
    });
    await adapter.start();
    try {
      const ws = sockets[0]!;
      await helloAndReady(ws, 1);
      ws.receive({
        op: 0, s: 2, t: "MESSAGE_CREATE",
        d: guildMessage({ id: "m-404", channel_id: CHANNEL_ID }),
      });
      ws.receive({
        op: 0, s: 3, t: "MESSAGE_CREATE",
        d: guildMessage({ id: "m-orphan", channel_id: THREAD_ID }),
      });
      ws.receive({
        op: 0, s: 4, t: "MESSAGE_CREATE",
        d: {
          id: "m-dm",
          channel_id: "dm-1",
          content: "hello dm",
          author: { id: USER_ID, username: "alice" },
        },
      });
      ws.receive({
        op: 0, s: 5, t: "MESSAGE_CREATE",
        d: guildMessage({
          id: "m-other",
          guild_id: otherGuild,
          channel_id: otherChannel,
          content: `<@${BOT_ID}> other`,
        }),
      });
      // Each guild lookup waits on the REST scheduler, and the gateway
      // dispatches the queue one event at a time. Wait until that chain
      // finishes instead of counting microtasks.
      await vi.waitFor(() => {
        expect(seen).toEqual(["m-dm", "m-other"]);
        expect(gatewayClient.getSequence()).toBe(5);
      });
      expect(adapter.isConnected()).toBe(true);
      expect(sockets).toHaveLength(1);

      const timersBeforeRetry = timers.pendingCount;
      ws.receive({
        op: 0, s: 6, t: "MESSAGE_CREATE",
        d: guildMessage({
          id: "m-503",
          channel_id: unavailableChannel,
          content: `<@${BOT_ID}> retry me`,
        }),
      });
      ws.receive({
        op: 0, s: 7, t: "MESSAGE_CREATE",
        d: {
          id: "m-dm-2",
          channel_id: "dm-2",
          content: "after",
          author: { id: USER_ID, username: "alice" },
        },
      });
      await vi.waitFor(() => {
        expect(unavailableReads).toBe(1);
        expect(seen).toEqual(["m-dm", "m-other"]);
        expect(gatewayClient.getSequence()).toBe(5);
        expect(timers.pendingCount).toBe(timersBeforeRetry + 1);
      });

      timers.advance(500);
      await vi.waitFor(() => {
        expect(unavailableReads).toBe(2);
        expect(seen).toEqual(["m-dm", "m-other", "m-503", "m-dm-2"]);
        expect(gatewayClient.getSequence()).toBe(7);
        expect(sockets).toHaveLength(1);
      });
    } finally {
      await adapter.stop();
    }
  });
});

// ---------------------------------------------------------------------------
// Attachments
// ---------------------------------------------------------------------------

describe("Discord attachments", () => {
  it("downloads from the Discord CDN allowlist without sending the bot token", async () => {
    const { rest } = makeRest(async () => jsonResponse({}));
    const seenHeaders: Headers[] = [];
    const fetchImpl: typeof fetch = async (_input: string | URL | Request, init?: RequestInit) => {
      seenHeaders.push(new Headers(init?.headers));
      return new Response(Buffer.from("file-bytes"), { status: 200, headers: { "content-type": "application/pdf" } });
    };
    const event = parseDiscordInboundMessage(guildMessage(), { botUserId: BOT_ID })!;
    const result = await downloadDiscordAttachment(rest, {
      id: "att-1", filename: "a.pdf", mimeType: "application/pdf",
      url: "https://cdn.discordapp.com/attachments/1/2/a.pdf?ex=abc",
    }, event, { fetchImpl });
    expect(result.content.toString()).toBe("file-bytes");
    expect(seenHeaders[0]!.get("Authorization")).toBeNull();
  });

  it("rejects off-allowlist hosts, redirect escapes, and oversize bodies", async () => {
    const { rest } = makeRest(async () => jsonResponse({}));
    const event = parseDiscordInboundMessage(guildMessage(), { botUserId: BOT_ID })!;
    const descriptor = { id: "a", filename: "a.bin", mimeType: "application/octet-stream" };
    const okFetch: typeof fetch = async () => new Response("x");
    await expect(downloadDiscordAttachment(rest, {
      ...descriptor, url: "https://evil.example.com/a.bin",
    }, event, { fetchImpl: okFetch })).rejects.toThrow();

    const redirectFetch: typeof fetch = async () => new Response(null, {
      status: 302, headers: { location: "https://evil.example.com/steal" },
    });
    await expect(downloadDiscordAttachment(rest, {
      ...descriptor, url: "https://cdn.discordapp.com/a.bin",
    }, event, { fetchImpl: redirectFetch })).rejects.toThrow();

    const bigFetch: typeof fetch = async () => new Response(Buffer.alloc(64), {
      status: 200, headers: { "content-length": "64" },
    });
    await expect(downloadDiscordAttachment(rest, {
      ...descriptor, url: "https://cdn.discordapp.com/a.bin",
    }, event, { fetchImpl: bigFetch, maxBytes: 16 })).rejects.toThrow();
  });

  it("re-fetches the original message by persisted ids when no URL was saved", async () => {
    const { rest, calls } = makeRest(async (call) => {
      expect(routeOf(call.url)).toBe(`/channels/${CHANNEL_ID}/messages/msg-9`);
      return jsonResponse({
        id: "msg-9",
        attachments: [{ id: "att-9", filename: "fresh.png", content_type: "image/png", url: "https://media.discordapp.net/fresh.png" }],
      });
    });
    const event = parseDiscordInboundMessage(guildMessage({ id: "msg-9" }), { botUserId: BOT_ID })!;
    const pngFetch: typeof fetch = async () => new Response(Buffer.from("png"), { status: 200 });
    const result = await downloadDiscordAttachment(rest, {
      id: "att-9", filename: "fresh.png", mimeType: "image/png", channelId: CHANNEL_ID, messageId: "msg-9",
    }, event, { fetchImpl: pngFetch });
    expect(result.content.toString()).toBe("png");
    expect(calls).toHaveLength(1);
  });

  it("fails explicitly when the target attachment id is missing and re-fetches when a signed URL expires", async () => {
    // 1. Missing target attachment id must NOT fall back to attachments[0].
    let cdnCalled = false;
    const { rest: missingRest } = makeRest(async () =>
      jsonResponse({
        id: "msg-missing-att",
        attachments: [
          { id: "other-att-id", filename: "wrong.pdf", content_type: "application/pdf", url: "https://cdn.discordapp.com/wrong.pdf" },
        ],
      }),
    );
    const event = parseDiscordInboundMessage(guildMessage({ id: "msg-missing-att" }), { botUserId: BOT_ID })!;
    const spyFetch: typeof fetch = async () => {
      cdnCalled = true;
      return new Response(Buffer.from("wrong-bytes"), { status: 200 });
    };
    await expect(
      downloadDiscordAttachment(
        missingRest,
        { id: "target-att-id", filename: "expected.pdf", mimeType: "application/pdf", channelId: CHANNEL_ID, messageId: "msg-missing-att" },
        event,
        { fetchImpl: spyFetch },
      ),
    ).rejects.toBeInstanceOf(ChannelDeliveryError);
    expect(cdnCalled).toBe(false);

    // 2. Expired signed URL (HTTP 403) re-fetches /channels/:channelId/messages/:messageId by saved ids,
    //    validates allowlist per redirect hop, and never sends the bot token.
    const { rest: refreshRest, calls: refreshCalls } = makeRest(async (call) => {
      expect(routeOf(call.url)).toBe(`/channels/${CHANNEL_ID}/messages/msg-expired`);
      return jsonResponse({
        id: "msg-expired",
        attachments: [
          { id: "other-att", filename: "other.bin", url: "https://cdn.discordapp.com/other.bin" },
          { id: "att-exp", filename: "report.pdf", content_type: "application/pdf", url: "https://cdn.discordapp.com/attachments/1/2/report.pdf?ex=fresh" },
        ],
      });
    });
    const cdnUrls: string[] = [];
    const cdnHeaders: Headers[] = [];
    const refreshFetch: typeof fetch = async (input: string | URL | Request, init?: RequestInit) => {
      const targetUrl = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      cdnUrls.push(targetUrl);
      cdnHeaders.push(new Headers(init?.headers));
      const exParam = new URL(targetUrl).searchParams.get("ex");
      if (exParam === "expired") {
        return new Response("expired signature", { status: 403 });
      }
      if (exParam === "fresh") {
        return new Response(null, {
          status: 302,
          headers: { location: "https://media.discordapp.net/attachments/1/2/report.pdf?ex=fresh2" },
        });
      }
      return new Response(Buffer.from("fresh-pdf-content"), {
        status: 200,
        headers: { "content-type": "application/pdf" },
      });
    };
    const expiredEvent = parseDiscordInboundMessage(guildMessage({ id: "msg-expired" }), { botUserId: BOT_ID })!;
    const refreshed = await downloadDiscordAttachment(
      refreshRest,
      {
        id: "att-exp",
        filename: "report.pdf",
        mimeType: "application/pdf",
        url: "https://cdn.discordapp.com/attachments/1/2/report.pdf?ex=expired",
      },
      expiredEvent,
      { fetchImpl: refreshFetch },
    );
    expect(refreshed.content.toString()).toBe("fresh-pdf-content");
    expect(refreshCalls).toHaveLength(1);
    expect(cdnUrls).toEqual([
      "https://cdn.discordapp.com/attachments/1/2/report.pdf?ex=expired",
      "https://cdn.discordapp.com/attachments/1/2/report.pdf?ex=fresh",
      "https://media.discordapp.net/attachments/1/2/report.pdf?ex=fresh2",
    ]);
    for (const h of cdnHeaders) {
      expect(h.get("Authorization")).toBeNull();
    }
  });

  it("recovers a guild thread attachment through the real orchestrator using persisted raw_payload.discord_message.channel_id", async () => {
    const backend = createMockBackend();

    const restRoutes: string[] = [];
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      restRoutes.push(`${call.init.method ?? "GET"} ${route}`);
      if (route === `/channels/${THREAD_ID}` && (call.init.method ?? "GET") === "GET") {
        return jsonResponse({
          id: THREAD_ID,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          name: "q1-thread",
          thread_metadata: { archived: false },
        });
      }
      if (route === `/channels/${THREAD_ID}/messages/msg-thread-att`) {
        return jsonResponse({
          id: "msg-thread-att",
          channel_id: THREAD_ID,
          attachments: [
            { id: "att-other", filename: "wrong.txt", content_type: "text/plain", url: "https://cdn.discordapp.com/wrong.txt" },
            {
              id: "att-thread-target",
              filename: "model.pdf",
              content_type: "application/pdf",
              size: 9,
              url: "https://media.discordapp.net/attachments/recovered/model.pdf?ex=fresh",
            },
          ],
        });
      }
      if (route === `/channels/${THREAD_ID}/messages` && call.init.method === "POST") {
        return jsonResponse({ id: "out-thread-reply-1" });
      }
      throw new Error(`Unexpected Discord REST call: ${call.init.method ?? "GET"} ${route}`);
    });

    let allowMediaDownload = false;
    let initialCrashAttempts = 0;
    let resolveInitialCrashSettled!: () => void;
    const initialCrashSettled = new Promise<void>((r) => { resolveInitialCrashSettled = r; });
    const mediaHeaders: Headers[] = [];
    const mediaFetchImpl: typeof fetch = async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      mediaHeaders.push(new Headers(init?.headers));
      if (!allowMediaDownload) {
        initialCrashAttempts += 1;
        if (initialCrashAttempts >= 2) {
          resolveInitialCrashSettled();
        }
        throw new Error("simulated crash before initial attachment download completes");
      }
      expect(url).toBe("https://media.discordapp.net/attachments/recovered/model.pdf?ex=fresh");
      return new Response(Buffer.from("pdf-model"), {
        status: 200,
        headers: { "content-type": "application/pdf" },
      });
    };

    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      botUserId: BOT_ID,
      rest,
      mediaFetchImpl,
    });
    const config = loadConfig({
      GATEWAY_HOST: "127.0.0.1",
      GATEWAY_PORT: "0",
      INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
      DALTON_BACKEND_URL: "http://127.0.0.1:8000",
      WEB_BASE_URL: "https://app.ginlix.ai",
    });
    const app = createGatewayApp({
      config,
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["discord", adapter]]),
      backoffDelayFn: async () => undefined,
    });

    // 1. Dispatch guild thread message through the real adapter + orchestrator so I-1 persists raw_payload.
    await adapter.dispatchMessage(
      guildMessage({
        id: "msg-thread-att",
        channel_id: THREAD_ID,
        guild_id: GUILD_ID,
        content: `<@${BOT_ID}> review this model`,
        attachments: [
          {
            id: "att-thread-target",
            filename: "model.pdf",
            content_type: "application/pdf",
            size: 9,
            url: "https://cdn.discordapp.com/attachments/initial/model.pdf?ex=initial",
          },
        ],
      }) as never,
    );
    await initialCrashSettled;
    await flushMicrotasks();

    const registerCalls = vi.mocked(backend.registerAndResolveInbound).mock.calls;
    expect(registerCalls.length).toBeGreaterThanOrEqual(1);
    const persistedReq = registerCalls[0]![0];
    const persistedRawPayload = persistedReq.raw_payload ?? {};
    expect(persistedReq.chat_address).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}`);
    expect(persistedReq.thread_scope_key).toBe(`chat:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`);
    // Verify orchestrator's persisted raw_payload stripped attachment URL/channelId but retained discord_message.channel_id.
    expect(persistedRawPayload.chat_id).toBe(`${GUILD_ID}/${CHANNEL_ID}`);
    expect((persistedRawPayload.discord_message as { channel_id?: string }).channel_id).toBe(THREAD_ID);
    expect(persistedRawPayload.attachments).toEqual([
      { id: "att-thread-target", filename: "model.pdf", mimeType: "application/pdf", sizeBytes: 9 },
    ]);
    expect(vi.mocked(backend.uploadWorkspaceFile)).not.toHaveBeenCalled();

    // 2. Simulate startup recovery of this in-flight row (crashed before attachment upload completed).
    vi.mocked(backend.recoverState).mockImplementationOnce(async () => ({
      mode: "startup" as const,
      inbound_events: [
        {
          platform: "discord" as const,
          message_id: "msg-thread-att",
          platform_user_id: USER_ID,
          dalton_user_id: "user_alice_123",
          chat_type: "group" as const,
          chat_address: persistedReq.chat_address,
          thread_scope_key: persistedReq.thread_scope_key,
          request_key: makeInboundRequestKey("discord", "msg-thread-att"),
          retry_request_key: null,
          resubmitted_request_key: null,
          steered_into_run_id: null,
          thread_id: "11111111-2222-3333-4444-555555555555",
          run_id: null,
          turn_index: null,
          workspace_id: HOME_WS_ID,
          external_thread_id: `discord:${persistedReq.thread_scope_key}:gen:1`,
          generation: 1,
          status: "pending" as const,
          fallback_pending: false,
          sent_to_current_count: 0,
          uploaded_attachments: [],
          raw_payload: structuredClone(persistedRawPayload),
          pending_interrupt: null,
        },
      ],
      background_runs: [],
      thread_watches: [],
      automation_runs: [],
    }));
    allowMediaDownload = true;

    await app.orchestrator.runStartupRecovery();

    // Verify recovery looked up the message in THREAD_ID (not parent CHANNEL_ID), matched att-thread-target, and uploaded it.
    expect(restRoutes).toContain(`GET /channels/${THREAD_ID}/messages/msg-thread-att`);
    expect(restRoutes).not.toContain(`GET /channels/${CHANNEL_ID}/messages/msg-thread-att`);
    const uploadCalls = vi.mocked(backend.uploadWorkspaceFile).mock.calls;
    expect(uploadCalls).toHaveLength(1);
    const uploadArgs = uploadCalls[0]!;
    expect(uploadArgs[0]).toBe("user_alice_123");
    expect(uploadArgs[2]).toBe("uploads/discord/msg-thread-att/model.pdf");
    expect(uploadArgs[3]).toBe("model.pdf");
    expect((uploadArgs[4] as Buffer).toString("utf8")).toBe("pdf-model");
    expect(uploadArgs[5]).toBe("application/pdf");
    expect(backend.appendCalls).toHaveLength(1);
    expect(
      backend.progressUpdates.some(
        (u) => u.platform === "discord" && u.message_id === "msg-thread-att" && u.status === "completed",
      ),
    ).toBe(true);
    for (const h of mediaHeaders) {
      expect(h.get("Authorization")).toBeNull();
    }
    await app.stop();
  });

  it("rejects initial http:// CDN URLs and https->http downgrade redirects before issuing HTTP requests", async () => {
    const { rest } = makeRest(async () => jsonResponse({}));
    const event = parseDiscordInboundMessage(guildMessage(), { botUserId: BOT_ID })!;
    const descriptor = { id: "att-https", filename: "a.pdf", mimeType: "application/pdf" };

    // 1. Initial http:// URL on an allowed Discord CDN host must be rejected before fetchImpl is called.
    const initialHttpCalls: string[] = [];
    const initialHttpFetch: typeof fetch = async (input: string | URL | Request) => {
      initialHttpCalls.push(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
      return new Response("should not be reached", { status: 200 });
    };
    await expect(
      downloadDiscordAttachment(
        rest,
        { ...descriptor, url: "http://cdn.discordapp.com/attachments/1/2/a.pdf" },
        event,
        { fetchImpl: initialHttpFetch },
      ),
    ).rejects.toMatchObject({
      name: "SafeFetchError",
      code: "invalid_url",
    });
    expect(initialHttpCalls).toHaveLength(0);

    // 2. https:// initial URL that redirects (302) to http:// on an allowed CDN host must be rejected before the second request.
    const redirectCalls: string[] = [];
    const redirectHeaders: Headers[] = [];
    const downgradeFetch: typeof fetch = async (input: string | URL | Request, init?: RequestInit) => {
      const targetUrl = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      redirectCalls.push(targetUrl);
      redirectHeaders.push(new Headers(init?.headers));
      return new Response(null, {
        status: 302,
        headers: { location: "http://media.discordapp.net/attachments/1/2/a.pdf" },
      });
    };
    let caughtError: unknown;
    try {
      await downloadDiscordAttachment(
        rest,
        { ...descriptor, url: "https://cdn.discordapp.com/attachments/1/2/a.pdf" },
        event,
        { fetchImpl: downgradeFetch },
      );
    } catch (err) {
      caughtError = err;
    }
    expect(caughtError).toBeInstanceOf(SafeFetchError);
    expect((caughtError as SafeFetchError).code).toBe("invalid_url");
    expect(redirectCalls).toEqual(["https://cdn.discordapp.com/attachments/1/2/a.pdf"]);
    expect(redirectHeaders[0]!.get("Authorization")).toBeNull();
  });

  it("aborts in-flight CDN streams on adapter.stop(), prevents post-stop REST refresh on HTTP error, and uses a fresh signal on restart", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const restRoutes: string[] = [];
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      restRoutes.push(route);
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}/messages/msg-att-stop`) {
        return jsonResponse({
          id: "msg-att-stop",
          attachments: [
            { id: "att-stop", filename: "doc.pdf", content_type: "application/pdf", url: "https://media.discordapp.net/doc.pdf?ex=fresh" },
          ],
        });
      }
      return jsonResponse({});
    });

    const gatewayClient = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 10, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const ws = new FakeWebSocket(url);
        sockets.push(ws);
        return ws;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
    });

    let mode: "slow-stream" | "http-error-on-stop" | "fresh-after-restart" = "slow-stream";
    let resolveSlowChunkEntered!: () => void;
    const slowChunkEntered = new Promise<void>((r) => { resolveSlowChunkEntered = r; });
    let streamCancelled = false;
    const cdnCalls: string[] = [];

    const mediaFetchImpl: typeof fetch = async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      cdnCalls.push(url);
      expect(new Headers(init?.headers).get("Authorization")).toBeNull();

      if (mode === "slow-stream") {
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array([1, 2, 3]));
            resolveSlowChunkEntered();
          },
          cancel() {
            streamCancelled = true;
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "content-type": "application/pdf" },
        });
      }

      if (mode === "http-error-on-stop") {
        // Simulate stop() happening right as CDN returns HTTP 403; must NOT trigger a REST refresh or second CDN call.
        await adapter.stop();
        return new Response("expired", { status: 403 });
      }

      expect(init?.signal?.aborted).toBe(false);
      return new Response(Buffer.from("restarted-ok"), {
        status: 200,
        headers: { "content-type": "application/pdf" },
      });
    };

    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      botUserId: BOT_ID,
      rest,
      gatewayClient,
      mediaFetchImpl,
    });

    const event = parseDiscordInboundMessage(guildMessage({ id: "msg-att-stop" }), { botUserId: BOT_ID })!;
    const descriptor = {
      id: "att-stop",
      filename: "doc.pdf",
      mimeType: "application/pdf",
      url: "https://cdn.discordapp.com/attachments/1/2/doc.pdf?ex=1",
    };

    // 1. Slow stream is aborted immediately when adapter.stop() is called.
    await adapter.start();
    const slowDownloadPromise = adapter.downloadAttachment(descriptor, event);
    await slowChunkEntered;
    await adapter.stop();
    await expect(slowDownloadPromise).rejects.toThrow();
    expect(streamCancelled).toBe(true);
    expect(restRoutes.filter((r) => r.startsWith("/channels/"))).toHaveLength(0);

    // 2. Restart, then encounter HTTP 403 while stopping: must NOT issue REST refresh or second CDN request.
    await adapter.start();
    mode = "http-error-on-stop";
    const cdnCountBefore = cdnCalls.length;
    await expect(adapter.downloadAttachment(descriptor, event)).rejects.toThrow(/aborted/i);
    expect(cdnCalls.length).toBe(cdnCountBefore + 1);
    expect(restRoutes.filter((r) => r.startsWith("/channels/"))).toHaveLength(0);

    // 3. Explicit start() after stop() creates a fresh non-aborted signal that downloads normally.
    mode = "fresh-after-restart";
    await adapter.start();
    const downloaded = await adapter.downloadAttachment(descriptor, event);
    expect(downloaded.content.toString("utf8")).toBe("restarted-ok");
    await adapter.stop();
  });
});

// ---------------------------------------------------------------------------
// Outbound text, threads, directory, targets, config
// ---------------------------------------------------------------------------

describe("Discord outbound", () => {
  it("chunks long text with fences, graphemes, bold, and tables within 2000 chars", () => {
    const code = "```ts\n" + "const x = 1;\n".repeat(200) + "```";
    const chunks = chunkDiscordText(code);
    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(2000);
      expect((chunk.match(/```/g) ?? []).length % 2).toBe(0);
    }
    const emoji = chunkDiscordText("😀".repeat(3000));
    for (const chunk of emoji) expect(chunk.length).toBeLessThanOrEqual(2000);
    expect(normalizeDiscordBold("a __bold__ b")).toBe("a **bold** b");
    expect(normalizeDiscordBold("`__code__`")).toBe("`__code__`");
    const table = renderDiscordMarkdown("| A | B |\n|---|---|\n| 1 | 2 |");
    expect(table).toContain("```");
    expect(sanitizeDiscordThreadName("  # Hello world  \nsecond", "m1")).toBe("Hello world");
    expect(sanitizeDiscordThreadName("", "m123")).toBe("Thread m123");
    const mark = discordThreadRequestMark({
      deliveryKey: "delivery-thread-1",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    expect(mark.length).toBeLessThan(DISCORD_THREAD_NAME_LIMIT);
    expect(discordThreadRequestMark({
      deliveryKey: "delivery-thread-1",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    })).toBe(mark);
    expect(discordThreadRequestMark({
      deliveryKey: "delivery-thread-2",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    })).not.toBe(mark);
    expect(discordThreadRequestMark({
      deliveryKey: "delivery-thread-1",
      parentChannelId: "700000099",
      botUserId: BOT_ID,
    })).not.toBe(mark);
    expect(discordThreadRequestMark({
      deliveryKey: "delivery-thread-1",
      parentChannelId: CHANNEL_ID,
      botUserId: "888000222",
    })).not.toBe(mark);
    const named = discordStandaloneThreadName("😀".repeat(80), mark);
    expect(named.length).toBeLessThanOrEqual(100);
    expect(named.endsWith(mark)).toBe(true);
    expect(named.slice(0, named.length - mark.length).trimEnd().length).toBeGreaterThan(0);
  });

  it("splits initial chunks on grapheme boundaries across combining marks, ZWJ/skin-tone/flag emoji, code fences, and overlong single graphemes", () => {
    // 1. "a".repeat(1999) + "e\u0301" (2001 UTF-16 code units): must not split 'e' from combining acute accent '\u0301'.
    const combiningText = "a".repeat(1999) + "e\u0301";
    const combiningChunks = chunkDiscordText(combiningText);
    expect(combiningChunks).toEqual(["a".repeat(1999), "e\u0301"]);

    // 2. ZWJ family emoji, skin-tone modifier, regional indicator flag, and Chinese + combining mark straddling 2000-char boundary.
    const zwj = "👨‍👩‍👧‍👦"; // 11 UTF-16 code units
    const skinTone = "👍🏽"; // 4 UTF-16 code units
    const flag = "🇺🇸"; // 4 UTF-16 code units
    for (const cluster of [zwj, skinTone, flag, "中\u0301"]) {
      const prefixLen = 1999;
      const input = "x".repeat(prefixLen) + cluster + "尾部中文";
      const out = chunkDiscordText(input);
      expect(out).toHaveLength(2);
      expect(out[0]).toBe("x".repeat(prefixLen));
      expect(out[1]).toBe(cluster + "尾部中文");
      for (const c of out) expect(c.length).toBeLessThanOrEqual(2000);
    }

    // 3. Inside fenced code blocks: grapheme clusters straddling the fenced line step must not be split, and fences stay balanced.
    for (const cluster of [zwj, skinTone, flag, "e\u0301"]) {
      // Opening "```txt\n" is 7 chars, closing "\n```" is 4 chars -> step is 1989 chars.
      // Prefix of 1988 chars puts `cluster` right across the 1989-char step boundary.
      const fencedInput = "```txt\n" + "b".repeat(1988) + cluster + "end\n```";
      const fencedChunks = chunkDiscordText(fencedInput);
      expect(fencedChunks).toHaveLength(2);
      for (const c of fencedChunks) {
        expect(c.length).toBeLessThanOrEqual(2000);
        expect((c.match(/```/g) ?? []).length % 2).toBe(0);
      }
      expect(fencedChunks[0]).toBe("```txt\n" + "b".repeat(1988) + "\n```");
      expect(fencedChunks[1]).toBe("```txt\n" + cluster + "end\n```");
    }

    // 4. Single grapheme cluster larger than the limit must make forward progress without infinite loops or lone surrogates.
    const giantCombiningGrapheme = "e" + "\u0301".repeat(2500);
    const giantChunks = chunkDiscordText(giantCombiningGrapheme);
    expect(giantChunks.length).toBe(2);
    for (const c of giantChunks) {
      expect(c.length).toBeGreaterThan(0);
      expect(c.length).toBeLessThanOrEqual(2000);
    }
    expect(giantChunks.join("")).toBe(giantCombiningGrapheme);

    const smallLimitZwj = chunkDiscordText(zwj, { maxChars: 5 });
    expect(smallLimitZwj.length).toBeGreaterThan(1);
    for (const c of smallLimitZwj) {
      expect(c.length).toBeGreaterThan(0);
      expect(c.length).toBeLessThanOrEqual(5);
      // Verify no lone surrogates were created when splitting the overlong grapheme.
      for (let i = 0; i < c.length; i++) {
        const code = c.charCodeAt(i);
        if (code >= 0xd800 && code <= 0xdbff) {
          const next = c.charCodeAt(i + 1);
          expect(next).toBeGreaterThanOrEqual(0xdc00);
          expect(next).toBeLessThanOrEqual(0xdfff);
          i += 1;
        } else {
          expect(code < 0xdc00 || code > 0xdfff).toBe(true);
        }
      }
    }
    expect(smallLimitZwj.join("")).toBe(zwj);
  });

  it("closes only a matching fence and reserves that fence's real length", () => {
    const fourTick = "````markdown\n```short```\n" + "x".repeat(2500) + "\n````";
    const fourChunks = chunkDiscordText(fourTick);
    expect(fourChunks.length).toBeGreaterThan(1);
    for (const chunk of fourChunks) {
      expect(chunk.length).toBeLessThanOrEqual(2000);
      expect(chunk.startsWith("````markdown\n")).toBe(true);
      expect(chunk.endsWith("\n````")).toBe(true);
    }
    const holder = fourChunks.find((chunk) => chunk.includes("```short```"));
    expect(holder).toBeTruthy();
    expect(holder!.endsWith("\n````")).toBe(true);
    expect(fourChunks[1]!.startsWith("````markdown\n")).toBe(true);

    const tilde = "~~~py\n" + "y".repeat(2500) + "\n~~~";
    const tildeChunks = chunkDiscordText(tilde);
    expect(tildeChunks.length).toBeGreaterThan(1);
    for (const chunk of tildeChunks) {
      expect(chunk.length).toBeLessThanOrEqual(2000);
      expect(chunk.startsWith("~~~py\n")).toBe(true);
      expect(chunk.endsWith("\n~~~")).toBe(true);
    }

    const mixed = "```js\n~~~\n" + "z".repeat(2500) + "\n~~~\n```";
    const mixedChunks = chunkDiscordText(mixed);
    expect(mixedChunks.length).toBeGreaterThan(1);
    expect(mixedChunks[0]!.includes("~~~")).toBe(true);
    for (const chunk of mixedChunks) {
      expect(chunk.length).toBeLessThanOrEqual(2000);
      expect(chunk.endsWith("\n```")).toBe(true);
      expect(chunk.endsWith("\n~~~")).toBe(false);
    }
  });

  it("resolves DM channels first, then sends; newThread address stays consistent for later chunks", async () => {
    let messageThreadReads = 0;
    const { rest, calls } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route.includes("/threads/active")) throw new Error(`unexpected active-thread route ${route}`);
      if (route === "/users/@me/channels") return jsonResponse({ id: "dm-channel-1" });
      if (route === `/channels/${CHANNEL_ID}/messages/msg-77/threads` && method === "POST") {
        return jsonResponse({
          id: "msg-77",
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          name: "Thread title line",
        });
      }
      if (route === "/channels/msg-77" && method === "GET") {
        messageThreadReads += 1;
        if (messageThreadReads === 1) return jsonResponse({ message: "Unknown Channel" }, 404);
        return jsonResponse({
          id: "msg-77",
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          thread_metadata: { archived: false },
        });
      }
      if (route === `/channels/${CHANNEL_ID}`) {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route.endsWith("/messages")) return jsonResponse({ id: `sent-${calls.length}` });
      throw new Error(`unexpected route ${route}`);
    });
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });

    const dm = await adapter.sendTextChunk({
      targetAddress: "discord:@me", recipientPlatformUserId: USER_ID, text: "hi", chunkIndex: 0, totalChunks: 1,
    });
    expect(dm.messageId).toBeTruthy();
    expect(routeOf(calls[1]!.url)).toBe("/channels/dm-channel-1/messages");

    const first = await adapter.sendTextChunk({
      targetAddress: `discord:${GUILD_ID}/${CHANNEL_ID}`, text: "Thread title line\nbody",
      chunkIndex: 0, totalChunks: 2, replyToMessageId: "msg-77", newThread: true,
    });
    expect(first.resolvedAddress).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:msg-77`);
    const second = await adapter.sendTextChunk({
      targetAddress: first.resolvedAddress!, text: "second chunk", chunkIndex: 1, totalChunks: 2,
    });
    expect(second.messageId).toBeTruthy();
    const messageRoutes = calls.map((c) => routeOf(c.url)).filter((r) => r.endsWith("/messages") && !r.includes("/threads"));
    expect(messageRoutes[messageRoutes.length - 1]).toBe("/channels/msg-77/messages");
    expect(calls.map((c) => routeOf(c.url))).toContain(`/channels/${CHANNEL_ID}/messages/msg-77/threads`);
  });

  it("adopts the thread Discord created when the create response is lost, and does not post a second thread", async () => {
    const deliveryKey = "delivery-thread-recover-1";
    const mark = discordThreadRequestMark({
      deliveryKey,
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    const created: Array<{ id: string; name: string }> = [];
    let activeGets = 0;
    let threadPosts = 0;
    let messagePosts = 0;
    const { rest, calls } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === `/channels/${CHANNEL_ID}/threads/active`) {
        throw new Error("channel active-thread route is not a Discord v10 endpoint");
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        activeGets += 1;
        return jsonResponse({
          threads: [
            {
              id: "other-user-same-title",
              name: "Thread title line",
              type: 11,
              guild_id: GUILD_ID,
              parent_id: CHANNEL_ID,
              owner_id: "someone-else",
            },
            {
              id: "other-parent-same-mark",
              name: `Thread title line ${mark}`,
              type: 11,
              guild_id: GUILD_ID,
              parent_id: "700000099",
              owner_id: BOT_ID,
            },
            ...created.map((thread) => ({
              id: thread.id,
              name: thread.name,
              type: 11,
              guild_id: GUILD_ID,
              parent_id: CHANNEL_ID,
              owner_id: BOT_ID,
            })),
          ],
          members: [
            { id: "other-user-same-title", user_id: "someone-else", join_timestamp: "2020-01-01T00:00:00.000Z", flags: 0 },
            { id: "other-parent-same-mark", user_id: BOT_ID, join_timestamp: "2020-01-01T00:00:00.000Z", flags: 0 },
            ...created.map((thread) => ({
              id: thread.id,
              user_id: BOT_ID,
              join_timestamp: "2020-01-01T00:00:00.000Z",
              flags: 0,
            })),
          ],
        });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        threadPosts += 1;
        const body = JSON.parse(String(call.init.body)) as { name: string };
        expect(body.name.endsWith(mark)).toBe(true);
        expect(body.name).not.toBe("Thread title line");
        created.push({ id: "thread-recovered-1", name: body.name });
        throw new Error("socket hang up");
      }
      if (route === "/channels/thread-recovered-1/messages" && method === "POST") {
        messagePosts += 1;
        return jsonResponse({ id: "msg-recovered-1" });
      }
      throw new Error(`unexpected ${method} ${route}`);
    });
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const sent = await adapter.sendTextChunk({
      targetAddress: `discord:999/${CHANNEL_ID}`,
      text: "Thread title line\nbody",
      chunkIndex: 0,
      totalChunks: 1,
      newThread: true,
      deliveryKey,
    });
    expect(sent.resolvedAddress).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:thread-recovered-1`);
    expect(sent.messageId).toBe("msg-recovered-1");
    expect(threadPosts).toBe(1);
    expect(messagePosts).toBe(1);
    expect(activeGets).toBeGreaterThanOrEqual(2);
    expect(created).toHaveLength(1);
    expect(calls.map((call) => routeOf(call.url))).toContain(`/guilds/${GUILD_ID}/threads/active`);
    expect(calls.map((call) => routeOf(call.url))).not.toContain("/guilds/999/threads/active");
  });

  it("blocks a second message send when the messages POST response is lost, without creating another thread", async () => {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    let threadPosts = 0;
    let messagePosts = 0;
    let activeGets = 0;
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}/threads/active`) {
        throw new Error("channel active-thread route is not a Discord v10 endpoint");
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        activeGets += 1;
        return jsonResponse({ threads: [], members: [] });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        threadPosts += 1;
        const body = JSON.parse(String(call.init.body)) as { name: string };
        const mark = discordThreadRequestMark({
          deliveryKey: "notice:discord:msg-lost:command",
          parentChannelId: CHANNEL_ID,
          botUserId: BOT_ID,
        });
        expect(body.name.endsWith(mark)).toBe(true);
        return jsonResponse({
          id: THREAD_ID,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          owner_id: BOT_ID,
          type: 11,
          name: body.name,
        });
      }
      if (route === `/channels/${THREAD_ID}/messages` && method === "POST") {
        messagePosts += 1;
        throw new Error("socket hang up");
      }
      throw new Error(`unexpected ${method} ${route}`);
    });
    const gatewayClient = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 100, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onError: () => {},
    });
    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      botUserId: BOT_ID,
      rest,
      gatewayClient,
    });
    const backend = createMockBackend();
    const delivery = new DeliveryService({
      backend,
      adapters: new Map([["discord", adapter]]),
    });
    await adapter.start();
    try {
      const ws = sockets[0]!;
      await helloAndReady(ws, 1);
      expect(adapter.isConnected()).toBe(true);
      const deliveryOptions = {
        deliveryKey: "notice:discord:msg-lost:command",
        kind: "notice" as const,
        userId: "user_alice_123",
        platform: "discord",
        target: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        text: "Thread title line\nbody",
        newThread: true,
      };
      const first = await delivery.executeDelivery(deliveryOptions);
      expect(first.status).toBe("unknown");
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(1);
      expect(activeGets).toBe(1);
      const callsAfterFirst = vi.mocked(backend.updateDeliveryProgress).mock.calls.length;

      const second = await delivery.executeDelivery(deliveryOptions);
      expect(second.status).toBe("unknown");
      expect(second.code).toBe("in_flight");
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(1);
      expect(activeGets).toBe(1);
      expect(vi.mocked(backend.updateDeliveryProgress).mock.calls.length).toBe(callsAfterFirst);
    } finally {
      await adapter.stop();
    }
  });

  function progressStatuses(
    backend: ReturnType<typeof createMockBackend>,
    deliveryKey: string,
  ): string[] {
    const statuses: string[] = [];
    for (const [req] of vi.mocked(backend.updateDeliveryProgress).mock.calls) {
      if (req.delivery_key !== deliveryKey || typeof req.status !== "string") continue;
      statuses.push(req.status);
    }
    return statuses;
  }

  function newThreadDelivery(deliveryKey: string, replyToMessageId?: string) {
    return {
      deliveryKey,
      kind: "notice" as const,
      userId: "user_alice_123",
      platform: "discord",
      target: `discord:${GUILD_ID}/${CHANNEL_ID}`,
      text: "Thread title line\nbody",
      newThread: true,
      ...(replyToMessageId ? { replyToMessageId } : {}),
    };
  }

  async function withDiscordDelivery(
    handler: FetchHandler,
    run: (ctx: {
      delivery: DeliveryService;
      backend: ReturnType<typeof createMockBackend>;
      restartGateway: () => Promise<DeliveryService>;
    }) => Promise<void>,
  ): Promise<void> {
    const timers = new FakeTimers();
    const sockets: FakeWebSocket[] = [];
    const { rest } = makeRest(handler);
    const gatewayClient = new DiscordGatewayClient({
      token: "test-bot-token",
      gatewayInfo: {
        url: "wss://gateway.discord.gg",
        session_start_limit: { total: 1000, remaining: 100, reset_after: 60_000, max_concurrency: 1 },
      },
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket;
      },
      timerDeps: {
        setTimeoutFn: timers.setTimeoutFn,
        clearTimeoutFn: timers.clearTimeoutFn,
        now: () => timers.nowMs,
        random: () => 0,
      },
      onError: () => {},
    });
    const adapter = new DiscordChannelAdapter({
      botToken: "test-bot-token",
      botUserId: BOT_ID,
      rest,
      gatewayClient,
    });
    const backend = createMockBackend();
    const delivery = new DeliveryService({
      backend,
      adapters: new Map([["discord", adapter]]),
    });
    await adapter.start();
    await helloAndReady(sockets[0]!, 1);
    expect(adapter.isConnected()).toBe(true);
    try {
      await run({
        delivery,
        backend,
        restartGateway: async () => {
          await adapter.stop();
          await adapter.start();
          await helloAndReady(sockets[sockets.length - 1]!, 1);
          expect(adapter.isConnected()).toBe(true);
          return new DeliveryService({
            backend,
            adapters: new Map([["discord", adapter]]),
          });
        },
      });
    } finally {
      await adapter.stop();
    }
  }

  it("keeps one thread POST when a lost create is followed by an empty list, including after restart", async () => {
    const deliveryKey = "notice:discord:thread-create-lost";
    const mark = discordThreadRequestMark({
      deliveryKey,
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    let threadPosts = 0;
    let messagePosts = 0;
    let activeGets = 0;
    const postedNames: string[] = [];
    await withDiscordDelivery(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}/threads/active`) {
        throw new Error("channel active-thread route is not a Discord v10 endpoint");
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        activeGets += 1;
        return jsonResponse({ threads: [], members: [], has_more: false });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        threadPosts += 1;
        const body = JSON.parse(String(call.init.body)) as { name: string };
        postedNames.push(body.name);
        throw new Error("socket hang up");
      }
      if (method === "POST" && route.endsWith("/messages")) {
        messagePosts += 1;
        throw new Error("message sent without a confirmed thread");
      }
      throw new Error(`unexpected ${method} ${route}`);
    }, async ({ delivery, backend, restartGateway }) => {
      const options = newThreadDelivery(deliveryKey);
      const first = await delivery.executeDelivery(options);
      expect(first.status).toBe("unknown");
      expect(first.code).toBe("unavailable");
      expect(first.message).toMatch(/was not confirmed/i);
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(0);
      expect(activeGets).toBe(2);
      expect(postedNames[0]!.endsWith(mark)).toBe(true);
      expect(postedNames[0]).not.toBe("Thread title line");
      expect(progressStatuses(backend, deliveryKey)).toEqual(["unknown"]);
      const progressAfterFirst = vi.mocked(backend.updateDeliveryProgress).mock.calls.length;

      const second = await delivery.executeDelivery(options);
      expect(second.status).toBe("unknown");
      expect(second.code).toBe("in_flight");
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(0);
      expect(activeGets).toBe(2);
      expect(vi.mocked(backend.updateDeliveryProgress).mock.calls.length).toBe(progressAfterFirst);

      const restarted = await restartGateway();
      const third = await restarted.executeDelivery(options);
      expect(third.status).toBe("unknown");
      expect(third.code).toBe("in_flight");
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(0);
      expect(activeGets).toBe(2);
      expect(progressStatuses(backend, deliveryKey)).toEqual(["unknown"]);
      expect(vi.mocked(backend.updateDeliveryProgress).mock.calls.length).toBe(progressAfterFirst);
    });
  });

  it("keeps a thread create unknown when recovery lookup fails after the attempt", async () => {
    const cases = ["forbidden", "missing", "down", "malformed", "bad-response"] as const;
    let threadPosts = 0;
    let messagePosts = 0;
    let listCalls = 0;
    let current: (typeof cases)[number] = "forbidden";
    await withDiscordDelivery(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        listCalls += 1;
        if (listCalls % 2 === 1) return jsonResponse({ threads: [], members: [] });
        if (current === "forbidden") return jsonResponse({ message: "Missing Access" }, 403);
        if (current === "missing") return jsonResponse({ message: "Unknown Guild" }, 404);
        if (current === "down") return jsonResponse({ message: "down" }, 500);
        if (current === "malformed") return jsonResponse({ threads: [{ id: 5 }], members: [] });
        return jsonResponse({ threads: [], members: [] });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        threadPosts += 1;
        if (current === "bad-response") return jsonResponse({ id: "not-a-thread", name: "no mark" });
        throw new Error("socket hang up");
      }
      if (method === "POST" && route.endsWith("/messages")) {
        messagePosts += 1;
        throw new Error("message sent without a confirmed thread");
      }
      throw new Error(`unexpected ${method} ${route}`);
    }, async ({ delivery, backend }) => {
      for (const [index, label] of cases.entries()) {
        current = label;
        const deliveryKey = `notice:discord:thread-recovery-${label}`;
        const options = newThreadDelivery(deliveryKey);
        const first = await delivery.executeDelivery(options);
        expect(first.status, label).toBe("unknown");
        expect(first.code, label).toBe("unavailable");
        expect(first.message, label).toMatch(/was not confirmed/i);
        expect(threadPosts, label).toBe(index + 1);
        expect(messagePosts, label).toBe(0);
        expect(progressStatuses(backend, deliveryKey), label).toEqual(["unknown"]);
        const progressAfter = vi.mocked(backend.updateDeliveryProgress).mock.calls.length;

        const second = await delivery.executeDelivery(options);
        expect(second.status, label).toBe("unknown");
        expect(second.code, label).toBe("in_flight");
        expect(threadPosts, label).toBe(index + 1);
        expect(messagePosts, label).toBe(0);
        expect(vi.mocked(backend.updateDeliveryProgress).mock.calls.length, label).toBe(progressAfter);
      }
    });
  });

  it("sends once when recovery finds the marked thread and does not adopt a title-only thread", async () => {
    const recoveredKey = "notice:discord:thread-recovered:command";
    const titleKey = "notice:discord:thread-title-only";
    const recoveredMark = discordThreadRequestMark({
      deliveryKey: recoveredKey,
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    let threadPosts = 0;
    let messagePosts = 0;
    let listCalls = 0;
    let mode: "marked" | "title-only" = "marked";
    let postedName = "";
    const titleOnly = {
      id: "title-only",
      name: "Thread title line",
      type: 11,
      guild_id: GUILD_ID,
      parent_id: CHANNEL_ID,
      owner_id: BOT_ID,
    };
    await withDiscordDelivery(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        listCalls += 1;
        if (listCalls % 2 === 1) return jsonResponse({ threads: [], members: [] });
        const marked = {
          id: "recovered-marked",
          name: postedName,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          owner_id: BOT_ID,
        };
        const threads = mode === "marked" ? [titleOnly, marked] : [titleOnly];
        return jsonResponse({
          threads,
          members: threads.map((thread) => ({
            id: thread.id,
            user_id: BOT_ID,
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 0,
          })),
        });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        threadPosts += 1;
        const body = JSON.parse(String(call.init.body)) as { name: string };
        postedName = body.name;
        throw new Error("socket hang up");
      }
      if (route === "/channels/recovered-marked/messages" && method === "POST") {
        messagePosts += 1;
        return jsonResponse({ id: "msg-recovered" });
      }
      if (method === "POST" && route.endsWith("/messages")) {
        messagePosts += 1;
        throw new Error("message sent without a confirmed thread");
      }
      throw new Error(`unexpected ${method} ${route}`);
    }, async ({ delivery, backend }) => {
      const recovered = newThreadDelivery(recoveredKey);
      const first = await delivery.executeDelivery(recovered);
      expect(first.status).toBe("sent");
      expect(first.address).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:recovered-marked`);
      expect(postedName.endsWith(recoveredMark)).toBe(true);
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(1);
      expect(progressStatuses(backend, recoveredKey)).toEqual(["sent"]);

      const again = await delivery.executeDelivery(recovered);
      expect(again.duplicate).toBe(true);
      expect(again.status).toBe("sent");
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(1);

      mode = "title-only";
      const titleOnlyDelivery = newThreadDelivery(titleKey);
      const missed = await delivery.executeDelivery(titleOnlyDelivery);
      expect(missed.status).toBe("unknown");
      expect(missed.code).toBe("unavailable");
      expect(threadPosts).toBe(2);
      expect(messagePosts).toBe(1);
      expect(progressStatuses(backend, titleKey)).toEqual(["unknown"]);

      const missedAgain = await delivery.executeDelivery(titleOnlyDelivery);
      expect(missedAgain.code).toBe("in_flight");
      expect(threadPosts).toBe(2);
      expect(messagePosts).toBe(1);
    });
  });

  it("retries a thread create after a pre-create failure or a definite rejection", async () => {
    let threadPosts = 0;
    let messagePosts = 0;
    let listMode: "down" | "empty" = "down";
    let rejectPost = false;
    await withDiscordDelivery(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        if (listMode === "down") return jsonResponse({ message: "down" }, 500);
        return jsonResponse({ threads: [], members: [] });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        threadPosts += 1;
        if (rejectPost) {
          rejectPost = false;
          return jsonResponse({ message: "bad request" }, 400);
        }
        const body = JSON.parse(String(call.init.body)) as { name: string };
        return jsonResponse({
          id: `thread-${threadPosts}`,
          name: body.name,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          owner_id: BOT_ID,
        });
      }
      if (method === "POST" && route.endsWith("/messages")) {
        messagePosts += 1;
        return jsonResponse({ id: `msg-${messagePosts}` });
      }
      throw new Error(`unexpected ${method} ${route}`);
    }, async ({ delivery, backend }) => {
      const preKey = "notice:discord:thread-pre-retry:command";
      const pre = newThreadDelivery(preKey);
      const blocked = await delivery.executeDelivery(pre);
      expect(blocked.status).toBe("failed");
      expect(blocked.code).toBe("unavailable");
      expect(blocked.message).toMatch(/did not confirm/i);
      expect(threadPosts).toBe(0);
      expect(messagePosts).toBe(0);
      expect(progressStatuses(backend, preKey)).toEqual(["failed"]);

      listMode = "empty";
      const created = await delivery.executeDelivery(pre);
      expect(created.status).toBe("sent");
      expect(created.address).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:thread-1`);
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(1);
      expect(progressStatuses(backend, preKey)).toEqual(["failed", "sent"]);

      const createdAgain = await delivery.executeDelivery(pre);
      expect(createdAgain.duplicate).toBe(true);
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(1);

      const rejectedKey = "notice:discord:thread-rejected:command";
      const rejected = newThreadDelivery(rejectedKey);
      rejectPost = true;
      const denied = await delivery.executeDelivery(rejected);
      expect(denied.status).toBe("failed");
      expect(denied.message).toMatch(/creation failed/i);
      expect(threadPosts).toBe(2);
      expect(messagePosts).toBe(1);
      expect(progressStatuses(backend, rejectedKey)).toEqual(["failed"]);

      const retried = await delivery.executeDelivery(rejected);
      expect(retried.status).toBe("sent");
      expect(retried.address).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:thread-3`);
      expect(threadPosts).toBe(3);
      expect(messagePosts).toBe(2);
      expect(progressStatuses(backend, rejectedKey)).toEqual(["failed", "sent"]);
    });
  });

  it("keeps a lost message-thread create unknown when the message channel is still missing", async () => {
    const deliveryKey = "notice:discord:message-thread-lost";
    let threadPosts = 0;
    let messagePosts = 0;
    let messageLookups = 0;
    await withDiscordDelivery(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === "/users/@me") return jsonResponse({ id: BOT_ID });
      if (route.endsWith("/threads/active")) {
        throw new Error(`message thread lookup must not list ${route}`);
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (route === "/channels/m-lost" && method === "GET") {
        messageLookups += 1;
        return jsonResponse({ message: "Unknown Channel" }, 404);
      }
      if (route === `/channels/${CHANNEL_ID}/messages/m-lost/threads` && method === "POST") {
        threadPosts += 1;
        throw new Error("socket hang up");
      }
      if (method === "POST" && route.endsWith("/messages")) {
        messagePosts += 1;
        throw new Error("message sent without a confirmed thread");
      }
      throw new Error(`unexpected ${method} ${route}`);
    }, async ({ delivery, backend }) => {
      const options = newThreadDelivery(deliveryKey, "m-lost");
      const first = await delivery.executeDelivery(options);
      expect(first.status).toBe("unknown");
      expect(first.code).toBe("unavailable");
      expect(first.message).toMatch(/was not confirmed/i);
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(0);
      expect(messageLookups).toBe(2);
      expect(progressStatuses(backend, deliveryKey)).toEqual(["unknown"]);

      const second = await delivery.executeDelivery(options);
      expect(second.code).toBe("in_flight");
      expect(threadPosts).toBe(1);
      expect(messagePosts).toBe(0);
      expect(messageLookups).toBe(2);
    });
  });

  it("identifies a thread by message id or delivery mark, never by display title", async () => {
    const otherParent = "700000088";
    const mark = discordThreadRequestMark({
      deliveryKey: "delivery-thread-alpha",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    const otherMark = discordThreadRequestMark({
      deliveryKey: "delivery-thread-beta",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    const textChannel = {
      id: CHANNEL_ID,
      type: 0,
      guild_id: GUILD_ID,
      parent_id: "category-1",
      name: "general",
    };
    const threadOf = (params: {
      id: string;
      name: string;
      parentId?: string;
      ownerId?: string;
      guildId?: string;
      type?: number;
    }) => ({
      id: params.id,
      name: params.name,
      type: params.type ?? 11,
      guild_id: params.guildId ?? GUILD_ID,
      parent_id: params.parentId ?? CHANNEL_ID,
      owner_id: params.ownerId ?? BOT_ID,
    });
    const withMembers = (threads: Array<ReturnType<typeof threadOf>>) => ({
      threads,
      members: threads.map((thread) => ({
        id: thread.id,
        user_id: thread.owner_id,
        join_timestamp: "2020-01-01T00:00:00.000Z",
        flags: 0,
      })),
    });

    const listed = [
      threadOf({ id: "title-only", name: "Same title", ownerId: "someone-else" }),
      threadOf({ id: "bot-same-title", name: "Same title", ownerId: BOT_ID }),
      threadOf({ id: "copied-mark", name: `Same title ${mark}`, ownerId: "someone-else" }),
      threadOf({ id: "other-parent", name: `Same title ${mark}`, parentId: otherParent }),
      threadOf({ id: "other-delivery", name: `Same title ${otherMark}`, parentId: otherParent }),
    ];
    const posts: string[] = [];
    const names: string[] = [];
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route.endsWith("/threads/active") && !route.startsWith(`/guilds/${GUILD_ID}/`)) {
        throw new Error(`wrong active-thread route ${route}`);
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") return jsonResponse(textChannel);
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        return jsonResponse(withMembers(listed));
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        posts.push(route);
        const body = JSON.parse(String(call.init.body)) as { name: string };
        names.push(body.name);
        const created = threadOf({ id: `created-${posts.length}`, name: body.name });
        listed.push(created);
        return jsonResponse(created);
      }
      throw new Error(`unexpected ${method} ${route}`);
    });

    const created = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-alpha",
      botUserId: BOT_ID,
    });
    expect(created.id).toBe("created-1");
    expect(posts).toEqual([`/channels/${CHANNEL_ID}/threads`]);
    expect(names[0]!.endsWith(mark)).toBe(true);
    expect(names[0]).not.toBe("Same title");

    const again = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      name: "Renamed display",
      deliveryKey: "delivery-thread-alpha",
      botUserId: BOT_ID,
    });
    expect(again.id).toBe("created-1");
    expect(posts).toHaveLength(1);

    const otherDelivery = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-beta",
      botUserId: BOT_ID,
    });
    expect(otherDelivery.id).toBe("created-2");
    expect(otherDelivery.id).not.toBe("other-delivery");
    expect(posts).toHaveLength(2);
    expect(names[1]!.endsWith(otherMark)).toBe(true);
    expect(names[1]).not.toBe(names[0]);
  });

  it("does not create a standalone thread when lookup is forbidden, missing, ambiguous, or incomplete", async () => {
    const mark = discordThreadRequestMark({
      deliveryKey: "delivery-thread-alpha",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    const textChannel = { id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" };
    const matching = (id: string) => ({
      id,
      name: `Same title ${mark}`,
      type: 11,
      guild_id: GUILD_ID,
      parent_id: CHANNEL_ID,
      owner_id: BOT_ID,
    });
    const cases: Array<{ label: string; status?: number; body: unknown; code: string; message: RegExp }> = [
      { label: "403", status: 403, body: { message: "Missing Access" }, code: "not_allowed", message: /lookup failed/i },
      { label: "404", status: 404, body: { message: "Unknown Guild" }, code: "target_not_found", message: /lookup failed/i },
      { label: "500", status: 500, body: { message: "down" }, code: "unavailable", message: /did not confirm/i },
      { label: "malformed", body: { threads: "nope" }, code: "unavailable", message: /did not confirm/i },
      {
        label: "ambiguous",
        body: {
          threads: [matching("one"), matching("two")],
          members: [
            { id: "one", user_id: BOT_ID, join_timestamp: "2020-01-01T00:00:00.000Z", flags: 0 },
            { id: "two", user_id: BOT_ID, join_timestamp: "2020-01-01T00:00:00.000Z", flags: 0 },
          ],
        },
        code: "unavailable",
        message: /more than one/i,
      },
      {
        label: "owner-without-member",
        body: { threads: [matching("owned-no-member")], members: [] },
        code: "unavailable",
        message: /did not confirm/i,
      },
      {
        label: "owner-with-other-member",
        body: {
          threads: [matching("owned-other-member")],
          members: [{
            id: "owned-other-member",
            user_id: "someone-else",
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 0,
          }],
        },
        code: "unavailable",
        message: /did not confirm/i,
      },
      {
        label: "mark-without-owner",
        body: {
          threads: [{ ...matching("no-owner"), owner_id: null }],
          members: [{
            id: "no-owner",
            user_id: BOT_ID,
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 0,
          }],
        },
        code: "unavailable",
        message: /did not confirm/i,
      },
      {
        label: "member-missing-flags",
        body: {
          threads: [],
          members: [{ id: "x", user_id: BOT_ID, join_timestamp: "2020-01-01T00:00:00.000Z" }],
        },
        code: "unavailable",
        message: /did not confirm/i,
      },
      {
        label: "member-bad-flags",
        body: {
          threads: [],
          members: [{
            id: "x",
            user_id: BOT_ID,
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 1.5,
          }],
        },
        code: "unavailable",
        message: /did not confirm/i,
      },
      {
        label: "thread-missing-id",
        body: {
          threads: [{
            type: 11,
            guild_id: GUILD_ID,
            parent_id: CHANNEL_ID,
            name: `Same title ${mark}`,
            owner_id: BOT_ID,
          }],
          members: [],
        },
        code: "unavailable",
        message: /did not confirm/i,
      },
    ];
    for (const item of cases) {
      let posts = 0;
      const { rest } = makeRest(async (call) => {
        const route = routeOf(call.url);
        const method = (call.init.method ?? "GET").toUpperCase();
        if (route === `/channels/${CHANNEL_ID}` && method === "GET") return jsonResponse(textChannel);
        if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
          return jsonResponse(item.body, item.status ?? 200);
        }
        if (method === "POST") {
          posts += 1;
          return jsonResponse({ id: "should-not-post" });
        }
        throw new Error(`${item.label} unexpected ${method} ${route}`);
      });
      await expect(createThreadDiscord(rest, {
        channelId: CHANNEL_ID,
        name: "Same title",
        deliveryKey: "delivery-thread-alpha",
        botUserId: BOT_ID,
      })).rejects.toMatchObject({ code: item.code, outcome: "failed" });
      await expect(createThreadDiscord(rest, {
        channelId: CHANNEL_ID,
        name: "Same title",
        deliveryKey: "delivery-thread-alpha",
        botUserId: BOT_ID,
      })).rejects.toThrow(item.message);
      expect(posts, item.label).toBe(0);
    }

    let barePosts = 0;
    const { rest: bareRest } = makeRest(async (call) => {
      const method = (call.init.method ?? "GET").toUpperCase();
      if (method === "POST") barePosts += 1;
      return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID });
    });
    await expect(createThreadDiscord(bareRest, {
      channelId: CHANNEL_ID,
      name: "Same title",
      botUserId: BOT_ID,
    })).rejects.toThrow(/delivery id/i);
    await expect(createThreadDiscord(bareRest, {
      channelId: CHANNEL_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-alpha",
    })).rejects.toThrow(/delivery id/i);
    expect(barePosts).toBe(0);

    let blockedPosts = 0;
    const { rest: forbiddenParent } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (method === "POST") {
        blockedPosts += 1;
        return jsonResponse({ id: "should-not-post" });
      }
      if (route === `/channels/${CHANNEL_ID}`) {
        return jsonResponse({ message: "Missing Access" }, 403);
      }
      if (route.endsWith("/threads/active")) {
        throw new Error("listed threads after a definite parent lookup failure");
      }
      throw new Error(`unexpected ${method} ${route}`);
    });
    await expect(createThreadDiscord(forbiddenParent, {
      channelId: CHANNEL_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-alpha",
      botUserId: BOT_ID,
    })).rejects.toMatchObject({ code: "not_allowed", outcome: "failed" });

    const { rest: missingGuild } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (method === "POST") {
        blockedPosts += 1;
        return jsonResponse({ id: "should-not-post" });
      }
      if (route.endsWith("/threads/active")) {
        throw new Error("listed threads before the parent guild was known");
      }
      return jsonResponse({ id: CHANNEL_ID, type: 0, parent_id: "category-1" });
    });
    const missingGuildAttempt = createThreadDiscord(missingGuild, {
      channelId: CHANNEL_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-alpha",
      botUserId: BOT_ID,
    });
    await expect(missingGuildAttempt).rejects.toMatchObject({ outcome: "failed", code: "unavailable" });
    await expect(missingGuildAttempt).rejects.toThrow(/did not confirm/i);
    expect(blockedPosts).toBe(0);
  });

  it("creates a standalone thread on the text parent, not on a thread or category id", async () => {
    const mark = discordThreadRequestMark({
      deliveryKey: "delivery-thread-alpha",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    const posts: string[] = [];
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === `/channels/${THREAD_ID}` && method === "GET") {
        return jsonResponse({
          id: THREAD_ID,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          name: "existing-thread",
        });
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({
          id: CHANNEL_ID,
          type: 0,
          guild_id: GUILD_ID,
          parent_id: "category-1",
          name: "general",
        });
      }
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        return jsonResponse({ threads: [], members: [] });
      }
      if (route.endsWith("/threads/active")) {
        throw new Error(`wrong active-thread route ${route}`);
      }
      if (method === "POST" && route.endsWith("/threads")) {
        posts.push(route);
        const body = JSON.parse(String(call.init.body)) as { name: string };
        return jsonResponse({
          id: "created-on-parent",
          name: body.name,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          owner_id: BOT_ID,
        });
      }
      throw new Error(`unexpected ${method} ${route}`);
    });
    const created = await createThreadDiscord(rest, {
      channelId: THREAD_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-alpha",
      botUserId: BOT_ID,
    });
    expect(created.id).toBe("created-on-parent");
    expect(posts).toEqual([`/channels/${CHANNEL_ID}/threads`]);
    expect(created.name?.endsWith(mark)).toBe(true);
  });

  it("reports a lost standalone create as unknown and adopts the same mark when a later lookup finds it", async () => {
    const mark = discordThreadRequestMark({
      deliveryKey: "delivery-thread-alpha",
      parentChannelId: CHANNEL_ID,
      botUserId: BOT_ID,
    });
    const textChannel = { id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" };
    const names: string[] = [];
    let posts = 0;
    let listCalls = 0;
    let mode: "down-after-post" | "stored" = "down-after-post";
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") return jsonResponse(textChannel);
      if (route === `/guilds/${GUILD_ID}/threads/active` && method === "GET") {
        listCalls += 1;
        if (mode === "down-after-post" && listCalls > 1) {
          return jsonResponse({ message: "down" }, 500);
        }
        const threads = mode === "stored"
          ? [{
            id: "stored-alpha",
            name: names[0],
            type: 11,
            guild_id: GUILD_ID,
            parent_id: CHANNEL_ID,
            owner_id: BOT_ID,
          }]
          : [];
        return jsonResponse({
          threads,
          members: threads.map((thread) => ({
            id: thread.id,
            user_id: BOT_ID,
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 0,
          })),
        });
      }
      if (route === `/channels/${CHANNEL_ID}/threads` && method === "POST") {
        posts += 1;
        const body = JSON.parse(String(call.init.body)) as { name: string };
        names.push(body.name);
        throw new Error("socket hang up");
      }
      throw new Error(`unexpected ${method} ${route}`);
    });
    const params = {
      channelId: CHANNEL_ID,
      name: "Same title",
      deliveryKey: "delivery-thread-alpha",
      botUserId: BOT_ID,
    };

    const lost = createThreadDiscord(rest, params);
    await expect(lost).rejects.toMatchObject({ outcome: "unknown", code: "unavailable" });
    await expect(lost).rejects.toThrow(/was not confirmed/i);
    expect(posts).toBe(1);
    expect(names[0]!.endsWith(mark)).toBe(true);
    // This second call has no delivery record. Its own lookup fails before it posts.
    const lookupFailed = createThreadDiscord(rest, params);
    await expect(lookupFailed).rejects.toMatchObject({ outcome: "failed", code: "unavailable" });
    await expect(lookupFailed).rejects.toThrow(/did not confirm/i);
    expect(posts).toBe(1);

    mode = "stored";
    const adopted = await createThreadDiscord(rest, params);
    expect(adopted.id).toBe("stored-alpha");
    expect(posts).toBe(1);
    expect(listCalls).toBeGreaterThanOrEqual(3);
  });

  it("uses the source message id as the thread id and ignores an equal title on another message", async () => {
    const channels = new Map<string, Record<string, unknown> | number>();
    const posts: string[] = [];
    const activeRoutes: string[] = [];
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      const method = (call.init.method ?? "GET").toUpperCase();
      if (route.endsWith("/threads/active")) {
        activeRoutes.push(route);
        throw new Error(`message thread lookup must not list ${route}`);
      }
      if (route === `/channels/${CHANNEL_ID}` && method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, parent_id: "category-1" });
      }
      if (method === "GET" && route.startsWith("/channels/")) {
        const id = route.slice("/channels/".length);
        const found = channels.get(id);
        if (typeof found === "number") return jsonResponse({ message: "lookup failed" }, found);
        if (!found) return jsonResponse({ message: "Unknown Channel" }, 404);
        return jsonResponse(found);
      }
      if (method === "POST" && route.includes("/messages/") && route.endsWith("/threads")) {
        posts.push(route);
        const messageId = route.split("/messages/")[1]!.replace(/\/threads$/, "");
        const body = JSON.parse(String(call.init.body)) as { name: string };
        if (messageId === "m-hang") {
          channels.set(messageId, {
            id: messageId,
            name: body.name,
            type: 11,
            guild_id: GUILD_ID,
            parent_id: CHANNEL_ID,
            owner_id: "someone-else",
          });
          throw new Error("socket hang up");
        }
        if (messageId === "m-wrong-response") {
          return jsonResponse({
            id: "not-the-message",
            name: body.name,
            type: 11,
            guild_id: GUILD_ID,
            parent_id: CHANNEL_ID,
          });
        }
        const created = {
          id: messageId,
          name: body.name,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
        };
        channels.set(messageId, created);
        return jsonResponse(created);
      }
      throw new Error(`unexpected ${method} ${route}`);
    });

    const first = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-a",
      name: "Same title",
    });
    const second = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-b",
      name: "Same title",
    });
    expect(first.id).toBe("m-a");
    expect(second.id).toBe("m-b");
    expect(posts).toEqual([
      `/channels/${CHANNEL_ID}/messages/m-a/threads`,
      `/channels/${CHANNEL_ID}/messages/m-b/threads`,
    ]);
    expect(activeRoutes).toEqual([]);

    const reused = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-a",
      name: "Different title",
    });
    expect(reused.id).toBe("m-a");
    expect(posts).toHaveLength(2);

    channels.set("m-wrong-parent", {
      id: "m-wrong-parent",
      name: "Same title",
      type: 11,
      guild_id: GUILD_ID,
      parent_id: "700000088",
    });
    channels.set("m-wrong-guild", {
      id: "m-wrong-guild",
      name: "Same title",
      type: 11,
      guild_id: "700000077",
      parent_id: CHANNEL_ID,
    });
    channels.set("m-not-thread", {
      id: "m-not-thread",
      name: "Same title",
      type: 0,
      guild_id: GUILD_ID,
      parent_id: CHANNEL_ID,
    });
    for (const messageId of ["m-wrong-parent", "m-wrong-guild", "m-not-thread"]) {
      await expect(createThreadDiscord(rest, {
        channelId: CHANNEL_ID,
        messageId,
        name: "Same title",
      })).rejects.toThrow(/did not match/i);
    }
    expect(posts).toHaveLength(2);

    channels.set("m-forbidden", 403);
    channels.set("m-down", 500);
    await expect(createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-forbidden",
      name: "Same title",
    })).rejects.toMatchObject({ code: "not_allowed", outcome: "failed" });
    const messageLookupDown = createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-down",
      name: "Same title",
    });
    await expect(messageLookupDown).rejects.toMatchObject({ outcome: "failed", code: "unavailable" });
    await expect(messageLookupDown).rejects.toThrow(/did not confirm/i);
    expect(posts).toHaveLength(2);

    const recovered = await createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-hang",
      name: "Same title",
    });
    expect(recovered.id).toBe("m-hang");
    expect(posts.filter((route) => route.endsWith("/m-hang/threads"))).toHaveLength(1);

    const wrongResponse = createThreadDiscord(rest, {
      channelId: CHANNEL_ID,
      messageId: "m-wrong-response",
      name: "Same title",
    });
    await expect(wrongResponse).rejects.toMatchObject({ outcome: "unknown", code: "unavailable" });
    await expect(wrongResponse).rejects.toThrow(/was not confirmed/i);
    expect(posts.filter((route) => route.endsWith("/m-wrong-response/threads"))).toHaveLength(1);
  });

  it("unarchives an archived thread before sending and uploads files as multipart", async () => {
    let patched = false;
    const { rest, calls } = makeRest(async (call) => {
      const route = routeOf(call.url);
      if (route === `/channels/${THREAD_ID}` && call.init.method === "GET") {
        return jsonResponse({ id: THREAD_ID, type: 11, guild_id: GUILD_ID, parent_id: CHANNEL_ID, thread_metadata: { archived: true } });
      }
      if (route === `/channels/${THREAD_ID}` && call.init.method === "PATCH") {
        patched = true;
        return jsonResponse({ id: THREAD_ID, thread_metadata: { archived: false } });
      }
      if (route === `/channels/${THREAD_ID}/messages`) return jsonResponse({ id: "file-msg-1" });
      throw new Error(`unexpected ${call.init.method} ${route}`);
    });
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const res = await adapter.sendFile({
      targetAddress: `discord:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`,
      path: "reports/q1.pdf", filename: "q1.pdf",
      content: Buffer.from("pdf"), mimeType: "application/pdf",
    });
    expect(res).toEqual({ messageId: "file-msg-1", status: "sent", reason: null });
    expect(patched).toBe(true);
    const body = calls[calls.length - 1]!.init.body as FormData;
    expect((body.get("files[0]") as File).name).toBe("q1.pdf");
  });

  it("reports directory pagination failure as incomplete instead of a complete empty list", async () => {
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      if (route === "/users/@me/guilds") return jsonResponse([{ id: GUILD_ID, name: "Research" }]);
      throw new Error("channels unavailable");
    });
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const listing = await adapter.listAvailableChats({ userId: "dalton-u", platformUserId: USER_ID });
    expect(listing.complete).toBe(false);
    expect(listing.error).toContain("channels unavailable");
    expect(listing.chats).toEqual([]);
  });

  it("returns only the guild channels the caller is allowed to see", async () => {
    const guildA = "700000011";
    const guildB = "700000012";
    const channelA = "700000013";
    const channelB = "700000014";
    const addressA = `discord:${guildA}/${channelA}`;
    const addressB = `discord:${guildB}/${channelB}`;
    const { rest } = makeRest(async (call) => {
      const route = routeOf(call.url);
      if (route === "/users/@me/guilds") {
        return jsonResponse([
          { id: guildA, name: "Guild Alpha Secret" },
          { id: guildB, name: "Guild Beta Secret" },
        ]);
      }
      if (route === `/guilds/${guildA}/channels`) {
        return jsonResponse([{ id: channelA, type: 0, name: "alpha-room", guild_id: guildA }]);
      }
      if (route === `/guilds/${guildB}/channels`) {
        return jsonResponse([{ id: channelB, type: 0, name: "beta-room", guild_id: guildB }]);
      }
      throw new Error(`unexpected ${route}`);
    });
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });

    const userA = await adapter.listAvailableChats({
      userId: "user-a",
      platformUserId: "p-a",
      userTier: "internal",
      viewableAddresses: new Set([addressA]),
    });
    expect(userA.chats.map((chat) => chat.address)).toEqual([addressA]);
    expect(JSON.stringify(userA.chats)).not.toContain("beta-room");
    expect(JSON.stringify(userA.chats)).not.toContain(guildB);
    expect(JSON.stringify(userA.chats)).not.toContain("Guild Beta");

    const userB = await adapter.listAvailableChats({
      userId: "user-b",
      platformUserId: "p-b",
      userTier: "internal",
      viewableAddresses: [addressB],
    });
    expect(userB.chats.map((chat) => chat.address)).toEqual([addressB]);
    expect(JSON.stringify(userB.chats)).not.toContain("alpha-room");
    expect(JSON.stringify(userB.chats)).not.toContain(guildA);

    const none = await adapter.listAvailableChats({
      userId: "user-c",
      platformUserId: "p-c",
      userTier: "internal",
      viewableAddresses: new Set<string>(),
    });
    expect(none.chats).toEqual([]);
    expect(JSON.stringify(none.chats)).not.toContain("Guild");

    const omitted = await adapter.listAvailableChats({
      userId: "user-d",
      platformUserId: "p-d",
    });
    expect(omitted.chats).toEqual([]);
    expect(JSON.stringify(omitted.chats)).not.toContain("alpha-room");
    expect(JSON.stringify(omitted.chats)).not.toContain("beta-room");

    const admin = await adapter.listAvailableChats({
      userId: "admin",
      platformUserId: "p-admin",
      userTier: "admin",
      viewableAddresses: new Set<string>(),
    });
    expect(admin.chats.map((chat) => chat.address).sort()).toEqual([addressA, addressB].sort());
  });

  it("checkTargetLive rejects a channel from the wrong guild", async () => {
    const { rest } = makeRest(async () => jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: "other-guild" }));
    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    const result = await adapter.checkTargetLive({
      userId: "dalton-u", address: `discord:${GUILD_ID}/${CHANNEL_ID}`, purpose: "automation",
    });
    expect(result.ok).toBe(false);
    expect(result.message).toContain("guild");
    expect(parseDiscordTarget(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`)?.threadId).toBe(THREAD_ID);
  });

  it("registers Discord only when enabled with a token, coexisting with Feishu", () => {
    const env = {
      INTERNAL_SERVICE_TOKEN: "x".repeat(32),
      DALTON_FEISHU_ENABLED: "true",
      DALTON_FEISHU_APP_ID: "app-1",
      DALTON_FEISHU_APP_SECRET: "secret-1",
      DALTON_DISCORD_ENABLED: "true",
      DALTON_DISCORD_BOT_TOKEN: "discord-token",
      DALTON_DISCORD_APPLICATION_ID: "1234567890",
    } as NodeJS.ProcessEnv;
    const adapters = buildConfiguredAdapters(loadConfig(env));
    expect([...adapters.keys()].sort()).toEqual(["discord", "feishu"]);
    expect(adapters.get("discord")!.isConfigured()).toBe(true);

    const disabled = buildConfiguredAdapters(loadConfig({
      ...env, DALTON_DISCORD_ENABLED: "false",
    } as NodeJS.ProcessEnv));
    expect(disabled.has("discord")).toBe(false);
    expect(disabled.has("feishu")).toBe(true);

    const noToken = buildConfiguredAdapters(loadConfig({
      ...env, DALTON_DISCORD_BOT_TOKEN: "",
    } as NodeJS.ProcessEnv));
    expect(noToken.has("discord")).toBe(false);
  });

  it("delivers current-conversation text and files to the Discord thread channel via I-6 claim, skips fallback after current send, preserves explicit non-current targets, and recovers thread scope across background/watch and retry claims", async () => {
    const OTHER_CHANNEL_ID = "700000099";
    const DALTON_USER_ID = "11111111-1111-4111-8111-111111111111";
    const TURN_THREAD_ID = "33333333-3333-4333-8333-333333333333";
    const TURN_RUN_ID = "44444444-4444-4444-8444-444444444444";
    const BG_RUN_ID = "55555555-5555-4555-8555-555555555555";
    const EXPECTED_SCOPE_KEY = `chat:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`;

    const backend = createMockBackend();
    const baseResolveImpl = vi.mocked(backend.registerAndResolveInbound).getMockImplementation()!;
    vi.spyOn(backend, "registerAndResolveInbound").mockImplementation(async (req) => {
      const base = await baseResolveImpl(req);
      return {
        ...base,
        dalton_user_id: DALTON_USER_ID,
        thread_id: null,
        workspace_id: HOME_WS_ID,
      };
    });
    vi.spyOn(backend, "downloadWorkspaceFile").mockResolvedValue({
      content: Buffer.from("chart-png-bytes"),
      mimeType: "image/png",
    });

    let releaseTurnSse!: () => void;
    const turnSseGate = new Promise<void>((r) => { releaseTurnSse = r; });
    let resolveTurnDispatched!: () => void;
    const turnDispatched = new Promise<void>((r) => { resolveTurnDispatched = r; });

    vi.spyOn(backend, "createThreadMessages").mockImplementation(async () => {
      resolveTurnDispatched();
      async function* gatedEvents() {
        yield {
          event: "metadata",
          data: JSON.stringify({
            thread_id: TURN_THREAD_ID,
            run_id: TURN_RUN_ID,
            turn_index: 0,
          }),
        };
        await turnSseGate;
        yield {
          event: "message_chunk",
          data: JSON.stringify({
            agent: "main",
            role: "assistant",
            content_type: "text",
            content: "Final assistant text that should be skipped because agent/send already sent to current thread.",
          }),
        };
        yield {
          event: "run_end",
          data: JSON.stringify({
            thread_id: TURN_THREAD_ID,
            run_id: TURN_RUN_ID,
            turn_index: 0,
            status: "completed",
          }),
        };
      }
      return { status: 200, isSse: true, events: gatedEvents() };
    });

    let failNextThreadPostWith400 = false;
    const sentMessages: Array<{ route: string; isMultipart: boolean; bodyText: string }> = [];

    const { rest, calls } = makeRest(async (call) => {
      const route = routeOf(call.url);
      if (route === `/channels/${THREAD_ID}` && call.init.method === "GET") {
        return jsonResponse({
          id: THREAD_ID,
          type: 11,
          guild_id: GUILD_ID,
          parent_id: CHANNEL_ID,
          name: "earnings-thread",
          thread_metadata: { archived: false },
        });
      }
      if (route === `/channels/${CHANNEL_ID}` && call.init.method === "GET") {
        return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID, name: "equities" });
      }
      if (route === `/channels/${OTHER_CHANNEL_ID}` && call.init.method === "GET") {
        return jsonResponse({ id: OTHER_CHANNEL_ID, type: 0, guild_id: GUILD_ID, name: "alerts" });
      }
      if (route.endsWith("/messages") && call.init.method === "POST") {
        if (route === `/channels/${THREAD_ID}/messages` && failNextThreadPostWith400) {
          failNextThreadPostWith400 = false;
          return jsonResponse({ code: 50035, message: "Invalid Form Body" }, 400);
        }
        const isMultipart = call.init.body instanceof FormData;
        const bodyText = isMultipart
          ? String((call.init.body as FormData).get("payload_json") ?? "")
          : String(call.init.body ?? "");
        sentMessages.push({ route, isMultipart, bodyText });
        return jsonResponse({ id: `discord-msg-${sentMessages.length}` });
      }
      throw new Error(`unexpected ${call.init.method} ${route}`);
    });
    const describeRest = (items: typeof calls) =>
      items.map((call) => ({
        method: String(call.init.method ?? "GET").toUpperCase(),
        route: routeOf(call.url),
      }));
    const isParentChannelRoute = (route: string) =>
      route === `/channels/${CHANNEL_ID}` || route.startsWith(`/channels/${CHANNEL_ID}/`);
    const messagePostRoutes = (items: Array<{ method: string; route: string }>) =>
      items
        .filter((call) => call.method === "POST" && call.route.endsWith("/messages"))
        .map((call) => call.route);

    const adapter = new DiscordChannelAdapter({ botToken: "t", botUserId: BOT_ID, rest });
    vi.spyOn(adapter, "start").mockResolvedValue(undefined);
    vi.spyOn(adapter, "isConnected").mockReturnValue(true);

    const app = createGatewayApp({
      config: loadConfig({
        GATEWAY_HOST: "127.0.0.1",
        GATEWAY_PORT: "0",
        INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
        DALTON_BACKEND_URL: "http://127.0.0.1:8000",
        WEB_BASE_URL: "https://app.dalton.test",
        DALTON_WEIXIN_CACHE_DIR: "/tmp/dalton-wx-discord-thread-test",
        DALTON_DISCORD_ENABLED: "true",
        DALTON_DISCORD_BOT_TOKEN: "t",
        DALTON_DISCORD_APPLICATION_ID: BOT_ID,
      }),
      backend,
      adapters: new Map<ChannelPlatform, ChannelAdapter>([["discord", adapter]]),
    });

    try {
      const { host, port } = await app.start();
      const postAgentSend = async (payload: Record<string, unknown>) => {
        const res = await fetchGatewayServer(app, `http://${host}:${port}/agent/send`, {
          method: "POST",
          headers: {
            "X-Service-Token": TEST_SERVICE_TOKEN,
            "X-User-Id": DALTON_USER_ID,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        return { statusCode: res.status, body: (await res.json()) as Record<string, unknown> };
      };

      // 1. User mentions the bot inside a Discord guild thread (channel_id = THREAD_ID, parent_id = CHANNEL_ID).
      const inboundPromise = adapter.dispatchMessage({
        id: "msg-in-thread-1",
        channel_id: THREAD_ID,
        guild_id: GUILD_ID,
        content: `<@${BOT_ID}> analyze Q4 margins and send the chart`,
        author: { id: USER_ID, username: "alice", global_name: "Alice" },
        mentions: [{ id: BOT_ID, username: "DaltonBot" }],
        thread: { id: THREAD_ID, type: 11, guild_id: GUILD_ID, parent_id: CHANNEL_ID, name: "earnings-thread" },
        attachments: [],
      } as never);

      await turnDispatched;
      // Wait for metadata SSE event to be processed into I-7 updateInboundProgress (`status: "streaming"`)
      await vi.waitFor(() => {
        expect(
          backend.progressUpdates.some(
            (u) => u.message_id === "msg-in-thread-1" && u.run_id === TURN_RUN_ID,
          ),
        ).toBe(true);
      });
      expect(backend.registerAndResolveInbound).toHaveBeenCalledWith(
        expect.objectContaining({
          platform: "discord",
          chat_address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
          thread_scope_key: EXPECTED_SCOPE_KEY,
          message_id: "msg-in-thread-1",
        }),
      );

      // 2. Agent calls POST /agent/send with omitted `target` (current conversation) for both text and a file.
      const currentSendRes = await postAgentSend({
        thread_id: TURN_THREAD_ID,
        run_id: TURN_RUN_ID,
        tool_call_id: "tc-thread-current-1",
        turn_platform: "discord",
        text: "Q4 gross margin expanded 180 bps.",
        files: [{ workspace_id: HOME_WS_ID, path: "reports/q4-margins.png" }],
      });
      expect(currentSendRes.statusCode).toBe(200);
      expect(currentSendRes.body).toEqual({
        status: "sent",
        code: null,
        address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        current: true,
        duplicate: false,
        message: `Delivered to discord:${GUILD_ID}/${CHANNEL_ID}.`,
        files: [{ path: "reports/q4-margins.png", status: "sent", reason: null }],
      });

      // The API address is the parent channel. thread_scope_key still selects the
      // thread for both the text POST and the multipart file POST.
      expect(sentMessages).toHaveLength(2);
      expect(sentMessages[0]!.route).toBe(`/channels/${THREAD_ID}/messages`);
      expect(sentMessages[0]!.isMultipart).toBe(false);
      expect(sentMessages[0]!.bodyText).toContain("Q4 gross margin expanded 180 bps.");
      expect(sentMessages[1]!.route).toBe(`/channels/${THREAD_ID}/messages`);
      expect(sentMessages[1]!.isMultipart).toBe(true);
      expect(sentMessages[1]!.bodyText).toContain("q4-margins.png");
      expect(sentMessages.some((m) => m.route === `/channels/${CHANNEL_ID}/messages`)).toBe(false);
      const currentRoutes = describeRest(calls);
      expect(messagePostRoutes(currentRoutes)).toEqual([
        `/channels/${THREAD_ID}/messages`,
        `/channels/${THREAD_ID}/messages`,
      ]);
      expect(currentRoutes.some((call) => isParentChannelRoute(call.route))).toBe(false);
      expect(
        currentRoutes.some((call) => call.method === "GET" && call.route === `/channels/${THREAD_ID}`),
      ).toBe(true);

      // 3. Complete the turn SSE stream: because current conversation already received text, fallback must be skipped.
      releaseTurnSse();
      await inboundPromise;
      await vi.waitFor(() => {
        expect(
          backend.progressUpdates.some(
            (u) => u.message_id === "msg-in-thread-1" && u.status === "completed",
          ),
        ).toBe(true);
      });
      expect(sentMessages).toHaveLength(2);
      expect(sentMessages.some((m) => m.route === `/channels/${CHANNEL_ID}/messages`)).toBe(false);
      const restCountBeforeExplicit = calls.length;

      // 4. Explicit `target` to another channel must deliver to that channel with `current: false` and not to THREAD_ID.
      const explicitOtherRes = await postAgentSend({
        thread_id: TURN_THREAD_ID,
        run_id: TURN_RUN_ID,
        tool_call_id: "tc-explicit-other-1",
        turn_platform: "discord",
        target: `discord:${GUILD_ID}/${OTHER_CHANNEL_ID}`,
        text: "Cross-channel alert",
      });
      expect(explicitOtherRes.statusCode).toBe(200);
      expect(explicitOtherRes.body).toMatchObject({
        status: "sent",
        address: `discord:${GUILD_ID}/${OTHER_CHANNEL_ID}`,
        current: false,
      });
      expect(sentMessages).toHaveLength(3);
      expect(sentMessages[2]!.route).toBe(`/channels/${OTHER_CHANNEL_ID}/messages`);
      const explicitRoutes = describeRest(calls.slice(restCountBeforeExplicit));
      expect(messagePostRoutes(explicitRoutes)).toEqual([
        `/channels/${OTHER_CHANNEL_ID}/messages`,
      ]);
      expect(explicitRoutes.some((call) => call.route.includes(THREAD_ID))).toBe(false);
      expect(explicitRoutes.some((call) => isParentChannelRoute(call.route))).toBe(false);
      const restCountBeforeRetry = calls.length;

      // 5. Retry claim after a failed delivery (HTTP 400) preserves thread_scope_key and delivers to THREAD_ID.
      failNextThreadPostWith400 = true;
      const failedAttemptRes = await postAgentSend({
        thread_id: TURN_THREAD_ID,
        run_id: TURN_RUN_ID,
        tool_call_id: "tc-thread-retry-1",
        turn_platform: "discord",
        text: "Retryable thread reply",
      });
      expect(failedAttemptRes.body).toMatchObject({ status: "failed" });
      expect(sentMessages).toHaveLength(3);

      const retriedAttemptRes = await postAgentSend({
        thread_id: TURN_THREAD_ID,
        run_id: TURN_RUN_ID,
        tool_call_id: "tc-thread-retry-1",
        turn_platform: "discord",
        text: "Retryable thread reply",
      });
      expect(retriedAttemptRes.body).toMatchObject({
        status: "sent",
        address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
        current: true,
        message: `Delivered to discord:${GUILD_ID}/${CHANNEL_ID}.`,
      });
      expect(sentMessages).toHaveLength(4);
      expect(sentMessages[3]!.route).toBe(`/channels/${THREAD_ID}/messages`);
      const retryRoutes = describeRest(calls.slice(restCountBeforeRetry));
      expect(messagePostRoutes(retryRoutes)).toEqual([
        `/channels/${THREAD_ID}/messages`,
        `/channels/${THREAD_ID}/messages`,
      ]);
      expect(retryRoutes.some((call) => isParentChannelRoute(call.route))).toBe(false);
      expect(retryRoutes.some((call) => call.route.includes(OTHER_CHANNEL_ID))).toBe(false);

      // 6. Background run / watch recovery preserves `thread_scope_key` and delivers replay fallback to THREAD_ID.
      await backend.updateInboundProgress({
        thread_watch: {
          thread_id: TURN_THREAD_ID,
          dalton_user_id: DALTON_USER_ID,
          platform: "discord",
          target_address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
          scope_key: EXPECTED_SCOPE_KEY,
          generation: 0,
          last_seen_turn_index: 0,
          watch_active: true,
        },
        background_run: {
          run_id: BG_RUN_ID,
          thread_id: TURN_THREAD_ID,
          dalton_user_id: DALTON_USER_ID,
          platform: "discord",
          target_address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
          turn_index: 1,
          status: "completed",
          fallback_pending: true,
        },
      });
      // Recovery asks replay for this background run at turn 1. The mock's
      // default page is a settled turn for a different run, so prepare this
      // run's own completed page and a matching idle status.
      const recoveredText = "Recovered background thread analysis.";
      vi.spyOn(backend, "replayThreadMessages").mockResolvedValue({
        status: 200,
        isSse: true,
        events: makeSseEventsFromFixture([
          {
            event: "history_page",
            data: { first_turn_index: 1, has_more: false },
          },
          {
            event: "user_message",
            data: {
              thread_id: TURN_THREAD_ID,
              turn_index: 1,
              content: "Background turn prompt",
              run_id: BG_RUN_ID,
              run_completed_at: "2026-11-08T00:00:01+00:00",
            },
          },
          {
            event: "message_chunk",
            data: {
              thread_id: TURN_THREAD_ID,
              agent: "main",
              role: "assistant",
              content: recoveredText,
              content_type: "text",
              finish_reason: "stop",
              turn_index: 1,
              response_id: BG_RUN_ID,
            },
          },
          {
            event: "replay_done",
            data: {
              thread_id: TURN_THREAD_ID,
              latest_run_id: BG_RUN_ID,
              latest_turn_index: 1,
              oldest_turn_index: 1,
              has_more: false,
            },
          },
        ]),
      });
      const baseGetThreadStatus = vi.mocked(backend.getThreadStatus).getMockImplementation()!;
      vi.spyOn(backend, "getThreadStatus").mockImplementation(async (userId, threadId) => {
        const base = await baseGetThreadStatus(userId, threadId);
        if (threadId !== TURN_THREAD_ID) return base;
        return {
          ...base,
          run_id: BG_RUN_ID,
          status: "idle",
          latest_turn_index: 1,
          turn_index: 1,
          pending_report_back: false,
        };
      });
      vi.spyOn(backend, "recoverState").mockResolvedValueOnce({
        mode: "startup",
        inbound_events: [],
        background_runs: [
          {
            run_id: BG_RUN_ID,
            thread_id: TURN_THREAD_ID,
            dalton_user_id: DALTON_USER_ID,
            platform: "discord",
            target_address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
            scope_key: EXPECTED_SCOPE_KEY,
            thread_scope_key: EXPECTED_SCOPE_KEY,
            turn_index: 1,
            status: "completed",
            fallback_pending: true,
            sent_to_current_count: 0,
          },
        ],
        thread_watches: [
          {
            thread_id: TURN_THREAD_ID,
            dalton_user_id: DALTON_USER_ID,
            platform: "discord",
            target_address: `discord:${GUILD_ID}/${CHANNEL_ID}`,
            scope_key: EXPECTED_SCOPE_KEY,
            thread_scope_key: EXPECTED_SCOPE_KEY,
            generation: 0,
            last_seen_turn_index: 1,
            watch_active: false,
          },
        ],
        automation_runs: [],
      });

      await app.orchestrator.runStartupRecovery();
      expect(sentMessages).toHaveLength(5);
      expect(sentMessages[4]!.route).toBe(`/channels/${THREAD_ID}/messages`);
      expect(sentMessages[4]!.bodyText).toContain(recoveredText);
      expect(sentMessages.some((m) => m.route === `/channels/${CHANNEL_ID}/messages`)).toBe(false);
      expect(messagePostRoutes(describeRest(calls))).toEqual([
        `/channels/${THREAD_ID}/messages`,
        `/channels/${THREAD_ID}/messages`,
        `/channels/${OTHER_CHANNEL_ID}/messages`,
        `/channels/${THREAD_ID}/messages`,
        `/channels/${THREAD_ID}/messages`,
        `/channels/${THREAD_ID}/messages`,
      ]);
      expect(describeRest(calls).some((call) => isParentChannelRoute(call.route))).toBe(false);
    } finally {
      // Let the inbound turn stream this test is holding finish, then stop.
      releaseTurnSse();
      resolveTurnDispatched();
      await app.stop();
    }
  });
});
