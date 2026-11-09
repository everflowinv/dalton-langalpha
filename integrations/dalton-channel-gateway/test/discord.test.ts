import { describe, expect, it, vi } from "vitest";
import { ChannelDeliveryError, type ChannelAdapter } from "../src/channels/adapter.js";
import {
  chunkDiscordText,
  classifyDiscordDeliveryError,
  createDiscordMessageNonce,
  DiscordChannelAdapter,
  DiscordError,
  DiscordGatewayClient,
  downloadDiscordAttachment,
  fetchDiscordGatewayInfo,
  normalizeDiscordBold,
  parseDiscordInboundMessage,
  parseDiscordTarget,
  RateLimitError,
  renderDiscordMarkdown,
  RequestClient,
  RESUMABLE_GATEWAY_CLOSE_CODE,
  sanitizeDiscordThreadName,
  type DiscordGatewayInfo,
  type DiscordWebSocketLike,
} from "../src/channels/discord/index.js";
import { loadConfig } from "../src/config.js";
import { buildConfiguredAdapters } from "../src/main.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelPlatform, InboundChannelEvent } from "../src/types.js";
import { makeInboundRequestKey } from "../src/uuid.js";
import { createMockBackend, HOME_WS_ID, TEST_SERVICE_TOKEN } from "./helpers.js";

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

  it("keeps nonces stable in shape and unique per create call", () => {
    const a = createDiscordMessageNonce();
    const b = createDiscordMessageNonce();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f]+$/);
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
      ws2.receive({ op: 10, d: { heartbeat_interval: 1000 } });
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
    await flushMicrotasks();

    // Original message registered once, in strict order before m-gw-2, and acked sequence advances to 3.
    expect(registered).toEqual(["m-gw-1:first", "m-gw-2:second"]);
    expect(attemptsByMsg.get("m-gw-1")).toBe(2);
    expect(attemptsByMsg.get("m-gw-2")).toBe(1);
    expect(gatewayClient.getSequence()).toBe(3);
    await adapter.stop();
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
  });

  it("resolves DM channels first, then sends; newThread address stays consistent for later chunks", async () => {
    const { rest, calls } = makeRest(async (call) => {
      const route = routeOf(call.url);
      if (route === "/users/@me/channels") return jsonResponse({ id: "dm-channel-1" });
      if (route === `/channels/${CHANNEL_ID}/messages/msg-77/threads`) return jsonResponse({ id: THREAD_ID, guild_id: GUILD_ID });
      if (route === `/channels/${CHANNEL_ID}`) return jsonResponse({ id: CHANNEL_ID, type: 0, guild_id: GUILD_ID });
      if (route === `/channels/${THREAD_ID}`) return jsonResponse({ id: THREAD_ID, type: 11, guild_id: GUILD_ID, parent_id: CHANNEL_ID, thread_metadata: { archived: false } });
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
    expect(first.resolvedAddress).toBe(`discord:${GUILD_ID}/${CHANNEL_ID}:thread:${THREAD_ID}`);
    const second = await adapter.sendTextChunk({
      targetAddress: first.resolvedAddress!, text: "second chunk", chunkIndex: 1, totalChunks: 2,
    });
    expect(second.messageId).toBeTruthy();
    const messageRoutes = calls.map((c) => routeOf(c.url)).filter((r) => r.endsWith("/messages"));
    expect(messageRoutes[messageRoutes.length - 1]).toBe(`/channels/${THREAD_ID}/messages`);
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
});
