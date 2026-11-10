/**
 * Real HTTP plus a Discord REST stub for a confirmed thread and its body phase.
 *
 * The Python harness applies migration d036 and serves real claim, progress,
 * and startup recovery. A new adapter and DeliveryService stand in for a
 * restarted gateway process. Wire counts come from the requests this stub
 * receives. This file is separate from the existing real HTTP suite selector.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { delimiter, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import {
  HttpBackendClient,
  type I6AuthorizeAndClaimRequest,
  type I7DeliveriesProgressRequest,
} from "../src/backend-client.js";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import {
  DiscordChannelAdapter,
  RequestClient,
  discordThreadRequestMark,
} from "../src/channels/discord/index.js";
import { DeliveryService } from "../src/delivery-service.js";
import type { ChannelPlatform } from "../src/types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "../../..");
const BOT_ID = "999000111";
const REJECTED_THREAD_ID = "700000555";
const LOST_THREAD_ID = "700000556";
const PROGRESS_THREAD_ID = "700000558";
const CONFLICT_THREAD_ID = "700000559";
const BODY_LOST_THREAD_ID = "700000560";
const STARTUP_THREAD_ID = "700000561";
const MULTI_THREAD_ID = "700000562";
const DISPATCH_LOST_THREAD_ID = "700000563";
const DECOY_THREAD_ID = "700000777";

const suite = (process.env.DALTON_REAL_HTTP_SUITE ?? "").trim().toLowerCase();

function resolveTestDsn(): string | null {
  for (const key of [
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
  ]) {
    const val = process.env[key]?.trim();
    if (!val) continue;
    const dbName = new URL(val).pathname.replace(/^\/+/, "");
    if (!dbName.endsWith("_test")) {
      throw new Error(
        `Refusing to run confirmed-thread HTTP test against non-test DB "${dbName}" from ${key}`,
      );
    }
    return val;
  }
  return null;
}

function resolvePythonBin(): string {
  const configured = process.env.DALTON_TEST_PYTHON_BIN?.trim();
  if (configured && existsSync(configured)) return configured;
  const venv = resolve(REPO_ROOT, ".venv/bin/python");
  if (existsSync(venv)) return venv;
  return "python3";
}

function hasRealEnv(): boolean {
  return Boolean(process.env.DALTON_REAL_BACKEND_URL?.trim() || resolveTestDsn());
}

interface Harness {
  baseUrl: string;
  stop: () => Promise<void>;
}

async function startOrAttachBackend(): Promise<Harness> {
  const preconfigured = process.env.DALTON_REAL_BACKEND_URL?.trim();
  if (preconfigured) {
    return { baseUrl: preconfigured.replace(/\/+$/, ""), stop: async () => {} };
  }
  const dsn = resolveTestDsn();
  if (!dsn) {
    throw new Error(
      "Neither DALTON_REAL_BACKEND_URL nor a test DSN is set",
    );
  }
  const pythonPath = (process.env.PYTHONPATH ?? "")
    .split(delimiter)
    .filter((part) => part.length > 0);
  if (!pythonPath.includes(REPO_ROOT)) pythonPath.unshift(REPO_ROOT);
  const proc = spawn(
    resolvePythonBin(),
    [
      "-m",
      "tests.integration.dalton.test_discord_thread_persistence_db",
      "--serve-http-harness",
    ],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        DALTON_CHANNEL_GATEWAY_TEST_DSN: dsn,
        PYTHONPATH: pythonPath.join(delimiter),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  try {
    const baseUrl = await waitForHarness(proc);
    return {
      baseUrl,
      stop: async () => {
        await stopProcess(proc);
      },
    };
  } catch (err) {
    await stopProcess(proc);
    throw err;
  }
}

function waitForHarness(proc: ChildProcess): Promise<string> {
  return new Promise((resolveUrl, rejectUrl) => {
    let stdoutBuf = "";
    let stderrBuf = "";
    const timer = setTimeout(() => {
      rejectUrl(
        new Error(
          `Timed out waiting for HARNESS_READY.\nstdout: ${stdoutBuf}\nstderr: ${stderrBuf}`,
        ),
      );
    }, 60_000);
    proc.stdout?.on("data", (chunk: Buffer) => {
      stdoutBuf += chunk.toString("utf8");
      const match = /HARNESS_READY\s+(http:\/\/127\.0\.0\.1:\d+)/.exec(stdoutBuf);
      if (match?.[1]) {
        clearTimeout(timer);
        resolveUrl(match[1]);
      }
    });
    proc.stderr?.on("data", (chunk: Buffer) => {
      stderrBuf += chunk.toString("utf8");
    });
    proc.once("exit", (code) => {
      clearTimeout(timer);
      rejectUrl(
        new Error(
          `Python HTTP harness exited before ready (${code}).\nstdout: ${stdoutBuf}\nstderr: ${stderrBuf}`,
        ),
      );
    });
  });
}

async function stopProcess(proc: ChildProcess): Promise<void> {
  if (proc.exitCode !== null) return;
  proc.kill("SIGTERM");
  await new Promise<void>((done) => {
    const timer = setTimeout(() => {
      if (proc.exitCode === null) proc.kill("SIGKILL");
      done();
    }, 5_000);
    proc.once("exit", () => {
      clearTimeout(timer);
      done();
    });
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function routeOf(url: string): string {
  return new URL(url).pathname.replace(/^\/api\/v10/, "");
}

function methodOf(call: FetchCall): string {
  return (call.init.method ?? "GET").toUpperCase();
}

interface Boot {
  prefix: string;
  service_token: string;
  user_a: string;
  parent_addr: string;
  guild_id: string;
  parent_cid: string;
  other_cid: string;
}

interface StoredThread {
  id: string;
  name: string;
  archived: boolean;
  listed: boolean;
}

interface DiscordWorld {
  calls: FetchCall[];
  rest: RequestClient;
  threads: Map<string, StoredThread>;
  failRejectedMessage: boolean;
  /** Count the POST, then drop the response. Discord already accepted the body. */
  loseAcceptedBody: Set<string>;
  /** Count the POST, return the first, and drop every later response. */
  loseAcceptedBodyAfter: Set<string>;
}

function threadAddress(boot: Boot, threadId: string): string {
  return `discord:${boot.guild_id}/${boot.parent_cid}:thread:${threadId}`;
}

function buildDiscordWorld(
  boot: Boot,
  marks: Array<[string, string]>,
  ambiguousMark: string,
): DiscordWorld {
  const threads = new Map<string, StoredThread>();
  const calls: FetchCall[] = [];
  const world: DiscordWorld = {
    calls,
    rest: undefined as unknown as RequestClient,
    threads,
    failRejectedMessage: true,
    loseAcceptedBody: new Set<string>(),
    loseAcceptedBodyAfter: new Set<string>(),
  };
  const fetchImpl = async (url: string, init: RequestInit): Promise<Response> => {
    const call = { url, init };
    calls.push(call);
    const route = routeOf(url);
    const method = methodOf(call);
    if (route === `/channels/${boot.parent_cid}` && method === "GET") {
      return jsonResponse({
        id: boot.parent_cid,
        type: 0,
        guild_id: boot.guild_id,
        name: "Equities Channel",
      });
    }
    if (route === `/guilds/${boot.guild_id}/threads/active` && method === "GET") {
      const listed = [...threads.values()].filter((thread) => thread.listed);
      const decoy = {
        id: DECOY_THREAD_ID,
        name: "other-active-thread",
        type: 11,
        guild_id: boot.guild_id,
        parent_id: boot.parent_cid,
        owner_id: BOT_ID,
      };
      return jsonResponse({
        threads: [
          ...listed.map((thread) => ({
            id: thread.id,
            name: thread.name,
            type: 11,
            guild_id: boot.guild_id,
            parent_id: boot.parent_cid,
            owner_id: BOT_ID,
          })),
          decoy,
        ],
        members: [
          ...listed.map((thread) => ({
            id: thread.id,
            user_id: BOT_ID,
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 0,
          })),
          {
            id: DECOY_THREAD_ID,
            user_id: BOT_ID,
            join_timestamp: "2020-01-01T00:00:00.000Z",
            flags: 0,
          },
        ],
      });
    }
    if (route === `/channels/${boot.parent_cid}/threads` && method === "POST") {
      const body = JSON.parse(String(init.body)) as { name?: string; type?: number };
      if (body.type !== 11 || typeof body.name !== "string") {
        throw new Error("thread create body is missing type 11 or name");
      }
      if (body.name.endsWith(ambiguousMark)) {
        throw new Error("socket hang up");
      }
      const threadName = body.name;
      const matched = marks.find(([, mark]) => threadName.endsWith(mark));
      const id = matched?.[0] ?? "";
      if (!id) throw new Error(`unexpected thread create name ${body.name}`);
      if (threads.has(id)) throw new Error(`second create for ${id}`);
      threads.set(id, { id, name: body.name, archived: false, listed: true });
      return jsonResponse({
        id,
        type: 11,
        guild_id: boot.guild_id,
        parent_id: boot.parent_cid,
        owner_id: BOT_ID,
        name: body.name,
      });
    }
    const thread = threads.get(route.split("/")[2] ?? "");
    if (route.startsWith("/channels/") && thread && method === "GET" && route === `/channels/${thread.id}`) {
      return jsonResponse({
        id: thread.id,
        type: 11,
        guild_id: boot.guild_id,
        parent_id: boot.parent_cid,
        owner_id: BOT_ID,
        name: thread.name,
        thread_metadata: { archived: thread.archived },
      });
    }
    if (route === `/channels/${thread?.id}` && method === "PATCH" && thread) {
      const body = JSON.parse(String(init.body)) as { archived?: boolean };
      if (body.archived === false) thread.archived = false;
      return jsonResponse({
        id: thread.id,
        type: 11,
        guild_id: boot.guild_id,
        parent_id: boot.parent_cid,
        owner_id: BOT_ID,
        name: thread.name,
        thread_metadata: { archived: thread.archived },
      });
    }
    if (method === "POST" && route.endsWith("/messages")) {
      const channelId = route.split("/")[2] ?? "";
      if (channelId === REJECTED_THREAD_ID && world.failRejectedMessage) {
        world.failRejectedMessage = false;
        return jsonResponse({ message: "Missing Access", code: 50001 }, 403);
      }
      const priorMessages = calls.filter(
        (call) =>
          methodOf(call) === "POST" &&
          routeOf(call.url) === `/channels/${channelId}/messages`,
      ).length - 1;
      if (
        world.loseAcceptedBody.has(channelId) ||
        (world.loseAcceptedBodyAfter.has(channelId) && priorMessages >= 1)
      ) {
        throw new Error("accepted body response dropped");
      }
      return jsonResponse({ id: `msg-${calls.length}` });
    }
    throw new Error(`unexpected Discord ${method} ${route}`);
  };
  world.rest = new RequestClient("test-bot-token", { fetchImpl });
  return world;
}

function countMessages(world: DiscordWorld, channelId: string): number {
  return world.calls.filter(
    (call) =>
      methodOf(call) === "POST" &&
      routeOf(call.url) === `/channels/${channelId}/messages`,
  ).length;
}

function countCreates(world: DiscordWorld, boot: Boot, mark: string): number {
  return world.calls.filter((call) => {
    if (methodOf(call) !== "POST") return false;
    if (routeOf(call.url) !== `/channels/${boot.parent_cid}/threads`) return false;
    const body = JSON.parse(String(call.init.body)) as { name?: string };
    return typeof body.name === "string" && body.name.endsWith(mark);
  }).length;
}

function connectedAdapter(rest: RequestClient): DiscordChannelAdapter {
  const adapter = new DiscordChannelAdapter({
    botToken: "test-bot-token",
    botUserId: BOT_ID,
    rest,
  });
  vi.spyOn(adapter, "start").mockResolvedValue(undefined);
  vi.spyOn(adapter, "isConnected").mockReturnValue(true);
  return adapter;
}

function deliveryFor(backend: HttpBackendClient, rest: RequestClient): DeliveryService {
  const adapter = connectedAdapter(rest);
  const adapters = new Map<ChannelPlatform, ChannelAdapter>([["discord", adapter]]);
  return new DeliveryService({ backend, adapters });
}

function groupSend(boot: Boot, deliveryKey: string, text = "Thread title line\nbody") {
  return {
    deliveryKey,
    kind: "agent_send" as const,
    userId: boot.user_a,
    platform: "discord",
    target: boot.parent_addr,
    text,
    newThread: true,
  };
}

function dropProgressAck(
  inner: HttpBackendClient,
  shouldDrop: (req: I7DeliveriesProgressRequest) => boolean,
): HttpBackendClient {
  let pending = true;
  return new Proxy(inner, {
    get(target, prop, receiver) {
      if (prop === "updateDeliveryProgress") {
        return async (req: I7DeliveriesProgressRequest) => {
          const ack = await target.updateDeliveryProgress(req);
          if (pending && shouldDrop(req)) {
            pending = false;
            throw new Error("lost persistence response");
          }
          return ack;
        };
      }
      const value = Reflect.get(target, prop, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as HttpBackendClient;
}

function watchDeliveryAttempt(
  inner: HttpBackendClient,
  deliveryKey: string,
  seen: { attempt?: number; statuses: string[] },
): HttpBackendClient {
  return new Proxy(inner, {
    get(target, prop, receiver) {
      if (prop === "authorizeAndClaimDelivery") {
        return async (req: I6AuthorizeAndClaimRequest) => {
          const claim = await target.authorizeAndClaimDelivery(req);
          if (
            req.delivery_key === deliveryKey &&
            seen.attempt === undefined &&
            typeof claim.delivery_attempt === "number"
          ) {
            seen.attempt = claim.delivery_attempt;
          }
          return claim;
        };
      }
      if (prop === "updateDeliveryProgress") {
        return async (req: I7DeliveriesProgressRequest) => {
          if (req.delivery_key === deliveryKey && req.status !== undefined) {
            seen.statuses.push(req.status);
          }
          return target.updateDeliveryProgress(req);
        };
      }
      const value = Reflect.get(target, prop, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as HttpBackendClient;
}

function dropFinalProgress(
  inner: HttpBackendClient,
  deliveryKey: string,
): HttpBackendClient {
  return new Proxy(inner, {
    get(target, prop, receiver) {
      if (prop === "updateDeliveryProgress") {
        return async (req: I7DeliveriesProgressRequest) => {
          if (req.delivery_key === deliveryKey && req.status !== undefined) {
            throw new Error("process exited before final progress");
          }
          return target.updateDeliveryProgress(req);
        };
      }
      const value = Reflect.get(target, prop, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as HttpBackendClient;
}

async function inspectDelivery(
  baseUrl: string,
  deliveryKey: string,
): Promise<Record<string, unknown>> {
  const url = new URL("/test-harness/inspect", baseUrl);
  url.searchParams.set("delivery_key", deliveryKey);
  const response = await fetch(url);
  const body = (await response.json()) as { delivery?: Record<string, unknown> | null };
  expect(response.status).toBe(200);
  expect(body.delivery).toBeTruthy();
  return body.delivery ?? {};
}

function detailCode(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const detail = (data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return undefined;
  const code = (detail as { code?: unknown }).code;
  return typeof code === "string" ? code : undefined;
}

describe.skipIf(suite === "feishu")("confirmed Discord thread persistence (d036)", () => {
  it.skipIf(!hasRealEnv())(
    "reuses one confirmed thread and does not repeat an uncertain body",
    async () => {
      if (suite && suite !== "discord") {
        throw new Error(
          `DALTON_REAL_HTTP_SUITE=${suite} cannot select this Discord thread test`,
        );
      }
      const harness = await startOrAttachBackend();
      try {
        const bootResponse = await fetch(`${harness.baseUrl}/test-harness/bootstrap`);
        expect(bootResponse.status).toBe(200);
        const boot = (await bootResponse.json()) as Boot;
        const rejectedKey = `${boot.prefix}_body403`;
        const lostKey = `${boot.prefix}_lostack`;
        const ambiguousKey = `${boot.prefix}_ambiguous`;
        const progressKey = `${boot.prefix}_progress`;
        const dmKey = `${boot.prefix}_dm`;
        const bodyLostKey = `${boot.prefix}_bodylost`;
        const startupKey = `${boot.prefix}_startup`;
        const multiKey = `${boot.prefix}_multichunk`;
        const dispatchLostKey = `${boot.prefix}_dispatchlost`;
        const markFor = (deliveryKey: string) =>
          discordThreadRequestMark({
            deliveryKey,
            parentChannelId: boot.parent_cid,
            botUserId: BOT_ID,
          });
        const marks = {
          rejected: markFor(rejectedKey),
          lost: markFor(lostKey),
          ambiguous: markFor(ambiguousKey),
          bodyLost: markFor(bodyLostKey),
          startup: markFor(startupKey),
          multi: markFor(multiKey),
          dispatchLost: markFor(dispatchLostKey),
        };
        const world = buildDiscordWorld(
          boot,
          [
            [REJECTED_THREAD_ID, marks.rejected],
            [LOST_THREAD_ID, marks.lost],
            [BODY_LOST_THREAD_ID, marks.bodyLost],
            [STARTUP_THREAD_ID, marks.startup],
            [MULTI_THREAD_ID, marks.multi],
            [DISPATCH_LOST_THREAD_ID, marks.dispatchLost],
          ],
          marks.ambiguous,
        );
        const client = new HttpBackendClient({
          baseUrl: harness.baseUrl,
          serviceToken: boot.service_token,
        });
        const freshDelivery = () =>
          deliveryFor(
            new HttpBackendClient({
              baseUrl: harness.baseUrl,
              serviceToken: boot.service_token,
            }),
            world.rest,
          );
        const postProgress = async (body: Record<string, unknown>) => {
          const response = await fetch(
            `${harness.baseUrl}/api/v1/dalton/channels/internal/deliveries/progress`,
            {
              method: "POST",
              headers: {
                "content-type": "application/json",
                "X-Service-Token": boot.service_token,
              },
              body: JSON.stringify(body),
            },
          );
          return { status: response.status, data: await response.json() };
        };

        const ambiguous = await deliveryFor(client, world.rest).executeDelivery(
          groupSend(boot, ambiguousKey),
        );
        expect(ambiguous.status).toBe("unknown");
        expect(ambiguous.code).toBe("unavailable");
        expect(countCreates(world, boot, marks.ambiguous)).toBe(1);
        const ambiguousRow = await inspectDelivery(harness.baseUrl, ambiguousKey);
        expect(ambiguousRow.status).toBe("unknown");
        expect(ambiguousRow.confirmed_thread_address).toBeNull();
        expect(ambiguousRow.discord_body_phase).toBeNull();
        const callsAfterAmbiguous = world.calls.length;
        const ambiguousAgain = await deliveryFor(client, world.rest).executeDelivery(
          groupSend(boot, ambiguousKey),
        );
        expect(ambiguousAgain.status).toBe("unknown");
        expect(ambiguousAgain.code).toBe("in_flight");
        expect(world.calls.length).toBe(callsAfterAmbiguous);
        expect(countCreates(world, boot, marks.ambiguous)).toBe(1);

        const lostWatch = { statuses: [] as string[] } as {
          attempt?: number;
          statuses: string[];
        };
        const losing = dropProgressAck(
          watchDeliveryAttempt(client, lostKey, lostWatch),
          (req) =>
            req.delivery_key === lostKey &&
            typeof req.confirmed_thread_address === "string" &&
            req.confirmed_thread_address.length > 0,
        );
        const lost = await deliveryFor(losing, world.rest).executeDelivery(
          groupSend(boot, lostKey),
        );
        expect(lost.status).toBe("unknown");
        expect(countCreates(world, boot, marks.lost)).toBe(1);
        expect(countMessages(world, LOST_THREAD_ID)).toBe(0);
        const lostRow = await inspectDelivery(harness.baseUrl, lostKey);
        expect(lostRow.confirmed_thread_address).toBe(threadAddress(boot, LOST_THREAD_ID));
        expect(lostRow.discord_body_phase).toBe("before_body");
        expect(lostRow.target_address).toBe(boot.parent_addr);
        const lostRetry = await freshDelivery().executeDelivery(groupSend(boot, lostKey));
        expect(lostRetry.status).toBe("sent");
        expect(lostRetry.address).toBe(threadAddress(boot, LOST_THREAD_ID));
        expect(countCreates(world, boot, marks.lost)).toBe(1);
        expect(countMessages(world, LOST_THREAD_ID)).toBe(1);
        const lostSent = await inspectDelivery(harness.baseUrl, lostKey);
        expect(lostSent.discord_body_phase).toBe("dispatching");
        expect(lostSent.status).toBe("sent");
        expect(lostWatch.statuses).toEqual(["unknown"]);
        expect(lostWatch.attempt).toEqual(expect.any(Number));
        const staleLost = await postProgress({
          delivery_key: lostKey,
          delivery_attempt: lostWatch.attempt,
          status: "unknown",
          error_code: "unavailable",
          error_message: "stale older attempt",
          platform_message_ids: [],
          first_response_json: { status: "unknown" },
        });
        expect(staleLost.status).toBe(409);
        expect(detailCode(staleLost.data)).toBe("conflict");
        const stillSent = await inspectDelivery(harness.baseUrl, lostKey);
        expect(stillSent.status).toBe("sent");
        expect(stillSent.platform_message_ids).toEqual(lostSent.platform_message_ids);
        expect(stillSent.first_response_json).toEqual(lostSent.first_response_json);
        expect(stillSent.confirmed_thread_address).toBe(threadAddress(boot, LOST_THREAD_ID));
        expect(stillSent.discord_body_phase).toBe("dispatching");
        expect(stillSent.delivery_attempt).toBe((lostWatch.attempt ?? 0) + 1);

        const dispatchWatch = { statuses: [] as string[] } as {
          attempt?: number;
          statuses: string[];
        };
        const losingDispatch = dropProgressAck(
          watchDeliveryAttempt(client, dispatchLostKey, dispatchWatch),
          (req) =>
            req.delivery_key === dispatchLostKey && req.discord_body_phase === "dispatching",
        );
        const lostDispatch = await deliveryFor(losingDispatch, world.rest).executeDelivery(
          groupSend(boot, dispatchLostKey),
        );
        expect(lostDispatch.status).toBe("unknown");
        expect(dispatchWatch.statuses).toEqual(["unknown"]);
        expect(countCreates(world, boot, marks.dispatchLost)).toBe(1);
        expect(countMessages(world, DISPATCH_LOST_THREAD_ID)).toBe(0);
        const dispatchRow = await inspectDelivery(harness.baseUrl, dispatchLostKey);
        expect(dispatchRow.confirmed_thread_address).toBe(
          threadAddress(boot, DISPATCH_LOST_THREAD_ID),
        );
        expect(dispatchRow.discord_body_phase).toBe("dispatching");
        expect(dispatchRow.status).toBe("unknown");
        expect(dispatchRow.target_address).toBe(boot.parent_addr);
        const dispatchRetry = await freshDelivery().executeDelivery(
          groupSend(boot, dispatchLostKey),
        );
        expect(dispatchRetry.status).toBe("unknown");
        expect(dispatchRetry.code).toBe("in_flight");
        expect(countCreates(world, boot, marks.dispatchLost)).toBe(1);
        expect(countMessages(world, DISPATCH_LOST_THREAD_ID)).toBe(0);
        expect((await inspectDelivery(harness.baseUrl, dispatchLostKey)).status).toBe("unknown");

        world.loseAcceptedBody.add(BODY_LOST_THREAD_ID);
        const bodyLost = await deliveryFor(client, world.rest).executeDelivery(
          groupSend(boot, bodyLostKey),
        );
        expect(bodyLost.status).toBe("unknown");
        expect(countCreates(world, boot, marks.bodyLost)).toBe(1);
        expect(countMessages(world, BODY_LOST_THREAD_ID)).toBe(1);
        const bodyLostRow = await inspectDelivery(harness.baseUrl, bodyLostKey);
        expect(bodyLostRow.status).toBe("unknown");
        expect(bodyLostRow.discord_body_phase).toBe("dispatching");
        expect(bodyLostRow.confirmed_thread_address).toBe(
          threadAddress(boot, BODY_LOST_THREAD_ID),
        );
        expect(bodyLostRow.sent_chunks).toBe(0);
        expect(bodyLostRow.target_address).toBe(boot.parent_addr);
        const bodyLostRetry = await freshDelivery().executeDelivery(
          groupSend(boot, bodyLostKey),
        );
        expect(bodyLostRetry.status).toBe("unknown");
        expect(bodyLostRetry.code).toBe("in_flight");
        expect(countCreates(world, boot, marks.bodyLost)).toBe(1);
        expect(countMessages(world, BODY_LOST_THREAD_ID)).toBe(1);

        world.loseAcceptedBodyAfter.add(MULTI_THREAD_ID);
        const multiText = `Thread title line\n${"x".repeat(2100)}`;
        await expect(
          deliveryFor(dropFinalProgress(client, multiKey), world.rest).executeDelivery(
            groupSend(boot, multiKey, multiText),
          ),
        ).rejects.toThrow(/process exited before final progress/);
        expect(countCreates(world, boot, marks.multi)).toBe(1);
        expect(countMessages(world, MULTI_THREAD_ID)).toBe(2);
        const multiRow = await inspectDelivery(harness.baseUrl, multiKey);
        expect(multiRow.status).toBe("in_flight");
        expect(multiRow.discord_body_phase).toBe("dispatching");
        expect(multiRow.sent_chunks).toBe(1);
        expect(multiRow.confirmed_thread_address).toBe(threadAddress(boot, MULTI_THREAD_ID));

        await expect(
          deliveryFor(dropFinalProgress(client, startupKey), world.rest).executeDelivery(
            groupSend(boot, startupKey),
          ),
        ).rejects.toThrow(/process exited before final progress/);
        expect(countCreates(world, boot, marks.startup)).toBe(1);
        expect(countMessages(world, STARTUP_THREAD_ID)).toBe(1);
        const startupRow = await inspectDelivery(harness.baseUrl, startupKey);
        expect(startupRow.status).toBe("in_flight");
        expect(startupRow.discord_body_phase).toBe("dispatching");
        expect(startupRow.sent_chunks).toBe(1);
        expect(startupRow.confirmed_thread_address).toBe(
          threadAddress(boot, STARTUP_THREAD_ID),
        );

        await client.recoverState({ mode: "startup" });
        const multiRecovered = await inspectDelivery(harness.baseUrl, multiKey);
        expect(multiRecovered.status).toBe("unknown");
        expect(multiRecovered.error_code).toBe("gateway_restarted");
        expect(multiRecovered.discord_body_phase).toBe("dispatching");
        expect(multiRecovered.sent_chunks).toBe(1);
        expect(multiRecovered.confirmed_thread_address).toBe(
          threadAddress(boot, MULTI_THREAD_ID),
        );
        const startupRecovered = await inspectDelivery(harness.baseUrl, startupKey);
        expect(startupRecovered.status).toBe("unknown");
        expect(startupRecovered.error_code).toBe("gateway_restarted");
        expect(startupRecovered.discord_body_phase).toBe("dispatching");
        expect(startupRecovered.sent_chunks).toBe(1);
        expect(startupRecovered.confirmed_thread_address).toBe(
          threadAddress(boot, STARTUP_THREAD_ID),
        );
        expect(startupRecovered.target_address).toBe(boot.parent_addr);
        const multiRetry = await freshDelivery().executeDelivery(
          groupSend(boot, multiKey, multiText),
        );
        expect(multiRetry.status).toBe("unknown");
        expect(multiRetry.code).toBe("in_flight");
        expect(countCreates(world, boot, marks.multi)).toBe(1);
        expect(countMessages(world, MULTI_THREAD_ID)).toBe(2);
        const startupRetry = await freshDelivery().executeDelivery(
          groupSend(boot, startupKey),
        );
        expect(startupRetry.status).toBe("unknown");
        expect(startupRetry.code).toBe("in_flight");
        expect(countCreates(world, boot, marks.startup)).toBe(1);
        expect(countMessages(world, STARTUP_THREAD_ID)).toBe(1);
        expect((await inspectDelivery(harness.baseUrl, startupKey)).status).toBe("unknown");
        expect((await inspectDelivery(harness.baseUrl, multiKey)).status).toBe("unknown");

        const rejected = await deliveryFor(client, world.rest).executeDelivery(
          groupSend(boot, rejectedKey),
        );
        expect(rejected.status).toBe("failed");
        expect(rejected.code).toBe("not_allowed");
        expect(countCreates(world, boot, marks.rejected)).toBe(1);
        const rejectedRow = await inspectDelivery(harness.baseUrl, rejectedKey);
        expect(rejectedRow.status).toBe("failed");
        expect(rejectedRow.error_code).toBe("not_allowed");
        expect(rejectedRow.target_address).toBe(boot.parent_addr);
        expect(rejectedRow.confirmed_thread_address).toBe(
          threadAddress(boot, REJECTED_THREAD_ID),
        );
        const stored = world.threads.get(REJECTED_THREAD_ID);
        expect(stored).toBeTruthy();
        stored!.listed = false;
        stored!.archived = true;
        stored!.name = "renamed-away-from-mark";
        const retryFrom = world.calls.length;
        const reclaimed = await freshDelivery().executeDelivery(groupSend(boot, rejectedKey));
        expect(reclaimed.status).toBe("sent");
        expect(reclaimed.address).toBe(threadAddress(boot, REJECTED_THREAD_ID));
        const retryOps = world.calls.slice(retryFrom).map(
          (call) => `${methodOf(call)} ${routeOf(call.url)}`,
        );
        expect(retryOps).toContain(`GET /channels/${REJECTED_THREAD_ID}`);
        expect(retryOps).toContain(`PATCH /channels/${REJECTED_THREAD_ID}`);
        expect(retryOps).toContain(`POST /channels/${REJECTED_THREAD_ID}/messages`);
        expect(retryOps.some((op) => op.includes("/threads/active"))).toBe(false);
        expect(retryOps).not.toContain(`POST /channels/${boot.parent_cid}/threads`);
        expect(countCreates(world, boot, marks.rejected)).toBe(1);
        expect(countCreates(world, boot, marks.ambiguous)).toBe(1);
        expect(countCreates(world, boot, marks.lost)).toBe(1);
        expect(countCreates(world, boot, marks.bodyLost)).toBe(1);
        expect(countCreates(world, boot, marks.startup)).toBe(1);
        expect(countCreates(world, boot, marks.multi)).toBe(1);
        expect(countCreates(world, boot, marks.dispatchLost)).toBe(1);
        expect(countMessages(world, REJECTED_THREAD_ID)).toBe(2);
        const afterRetry = await inspectDelivery(harness.baseUrl, rejectedKey);
        expect(afterRetry.target_address).toBe(boot.parent_addr);
        expect(afterRetry.confirmed_thread_address).toBe(
          threadAddress(boot, REJECTED_THREAD_ID),
        );
        expect(afterRetry.discord_body_phase).toBe("dispatching");
        expect(afterRetry.status).toBe("sent");

        const progressAddress = threadAddress(boot, PROGRESS_THREAD_ID);
        const progressClaim = await client.authorizeAndClaimDelivery({
          delivery_key: progressKey,
          kind: "agent_send",
          user_id: boot.user_a,
          platform: "discord",
          target: boot.parent_addr,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(progressClaim.claim_status).toBe("claimed");
        expect(progressClaim.delivery_attempt).toEqual(expect.any(Number));
        const firstProgress = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: progressAddress,
        });
        expect(firstProgress.status).toBe(200);
        expect(firstProgress.data.confirmed_thread_address).toBe(progressAddress);
        expect(firstProgress.data.discord_body_phase).toBe("before_body");
        const sameProgress = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: progressAddress,
        });
        expect(sameProgress.status).toBe(200);
        expect(sameProgress.data.confirmed_thread_address).toBe(progressAddress);
        expect(sameProgress.data.discord_body_phase).toBe("before_body");
        const markedDispatch = await postProgress({
          delivery_key: progressKey,
          discord_body_phase: "dispatching",
        });
        expect(markedDispatch.status).toBe(200);
        expect(markedDispatch.data.discord_body_phase).toBe("dispatching");
        const backwards = await postProgress({
          delivery_key: progressKey,
          discord_body_phase: "before_body",
        });
        expect(backwards.status).toBe(409);
        expect(detailCode(backwards.data)).toBe("conflict");
        const conflict = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: threadAddress(boot, CONFLICT_THREAD_ID),
        });
        expect(conflict.status).toBe(409);
        expect(detailCode(conflict.data)).toBe("conflict");
        const wrongParent = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: `discord:${boot.guild_id}/${boot.other_cid}:thread:${PROGRESS_THREAD_ID}`,
        });
        expect(wrongParent.status).toBe(400);
        expect(detailCode(wrongParent.data)).toBe("invalid");
        const wrongGuild = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: `discord:700000009/${boot.parent_cid}:thread:${PROGRESS_THREAD_ID}`,
        });
        expect(wrongGuild.status).toBe(400);
        expect(detailCode(wrongGuild.data)).toBe("invalid");
        const replaced = await postProgress({
          delivery_key: progressKey,
          status: "failed",
          error_code: "not_allowed",
          error_message: "Discord message send failed",
          first_response_json: { status: "failed", address: progressAddress },
        });
        expect(replaced.status).toBe(200);
        expect(replaced.data.confirmed_thread_address).toBe(progressAddress);
        expect(replaced.data.discord_body_phase).toBe("dispatching");
        const progressRow = await inspectDelivery(harness.baseUrl, progressKey);
        expect(progressRow.confirmed_thread_address).toBe(progressAddress);
        expect(progressRow.discord_body_phase).toBe("dispatching");
        expect(progressRow.target_address).toBe(boot.parent_addr);
        expect(progressRow.status).toBe("failed");
        const sticky = await postProgress({
          delivery_key: bodyLostKey,
          status: "failed",
          error_code: "not_allowed",
          error_message: "stale worker",
        });
        expect(sticky.status).toBe(409);
        expect(detailCode(sticky.data)).toBe("conflict");
        const stickyRow = await inspectDelivery(harness.baseUrl, bodyLostKey);
        expect(stickyRow.status).toBe("unknown");
        expect(stickyRow.discord_body_phase).toBe("dispatching");
        expect(stickyRow.confirmed_thread_address).toBe(
          threadAddress(boot, BODY_LOST_THREAD_ID),
        );

        const dmClaim = await client.authorizeAndClaimDelivery({
          delivery_key: dmKey,
          kind: "agent_send",
          user_id: boot.user_a,
          platform: "discord",
          target: "discord:@me",
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(dmClaim.claim_status).toBe("claimed");
        const dmProgress = await postProgress({
          delivery_key: dmKey,
          confirmed_thread_address: progressAddress,
        });
        expect(dmProgress.status).toBe(400);
        expect(detailCode(dmProgress.data)).toBe("invalid");
        const dmRow = await inspectDelivery(harness.baseUrl, dmKey);
        expect(dmRow.confirmed_thread_address).toBeNull();
        expect(dmRow.discord_body_phase).toBeNull();

        const fenceKey = `${boot.prefix}_fence`;
        const fenceAddress = threadAddress(boot, "700000564");
        const fenceClaim = await client.authorizeAndClaimDelivery({
          delivery_key: fenceKey,
          kind: "agent_send",
          user_id: boot.user_a,
          platform: "discord",
          target: boot.parent_addr,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(fenceClaim.claim_status).toBe("claimed");
        expect(fenceClaim.delivery_attempt).toEqual(expect.any(Number));
        const fenceAttempt = fenceClaim.delivery_attempt as number;
        const fenceStored = await postProgress({
          delivery_key: fenceKey,
          delivery_attempt: fenceAttempt,
          confirmed_thread_address: fenceAddress,
        });
        expect(fenceStored.status).toBe(200);
        expect(fenceStored.data.discord_body_phase).toBe("before_body");
        const fenceUnknown = await postProgress({
          delivery_key: fenceKey,
          delivery_attempt: fenceAttempt,
          status: "unknown",
          error_code: "timeout",
          error_message: "older attempt lost the body response",
        });
        expect(fenceUnknown.status).toBe(200);
        const fenceRetry = await client.authorizeAndClaimDelivery({
          delivery_key: fenceKey,
          kind: "agent_send",
          user_id: boot.user_a,
          platform: "discord",
          target: boot.parent_addr,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(fenceRetry.claim_status).toBe("claimed");
        expect(fenceRetry.delivery_attempt).toBe(fenceAttempt + 1);
        expect(fenceRetry.discord_body_phase).toBe("before_body");
        const staleInFlight = await postProgress({
          delivery_key: fenceKey,
          delivery_attempt: fenceAttempt,
          status: "sent",
          platform_message_ids: ["old"],
          first_response_json: { status: "sent" },
        });
        expect(staleInFlight.status).toBe(409);
        expect(detailCode(staleInFlight.data)).toBe("conflict");
        const fenceFlying = await inspectDelivery(harness.baseUrl, fenceKey);
        expect(fenceFlying.status).toBe("in_flight");
        expect(fenceFlying.delivery_attempt).toBe(fenceAttempt + 1);
        expect(fenceFlying.platform_message_ids).toEqual([]);
        expect(fenceFlying.confirmed_thread_address).toBe(fenceAddress);
        expect(fenceFlying.discord_body_phase).toBe("before_body");
        const fenceSent = await postProgress({
          delivery_key: fenceKey,
          delivery_attempt: fenceRetry.delivery_attempt,
          status: "sent",
          platform_message_ids: ["newer"],
          first_response_json: { status: "sent" },
        });
        expect(fenceSent.status).toBe(200);
        const staleDone = await postProgress({
          delivery_key: fenceKey,
          delivery_attempt: fenceAttempt,
          status: "unknown",
          platform_message_ids: [],
          first_response_json: { status: "unknown" },
        });
        expect(staleDone.status).toBe(409);
        expect(detailCode(staleDone.data)).toBe("conflict");
        const fenceDone = await inspectDelivery(harness.baseUrl, fenceKey);
        expect(fenceDone.status).toBe("sent");
        expect(fenceDone.platform_message_ids).toEqual(["newer"]);
        expect(fenceDone.first_response_json).toEqual({ status: "sent" });
        expect(fenceDone.confirmed_thread_address).toBe(fenceAddress);
        expect(fenceDone.discord_body_phase).toBe("before_body");
        expect(fenceDone.delivery_attempt).toBe(fenceAttempt + 1);

        const state = await client.getChannelState({
          user_id: boot.user_a,
          platform: "discord",
        });
        const apps = structuredClone(state.settings.apps) as {
          discord?: {
            agent_messages?: { allowed?: string[] };
            chats?: Record<string, unknown>;
          };
        };
        const discordApp = apps.discord;
        expect(discordApp?.agent_messages?.allowed).toContain(boot.parent_addr);
        discordApp!.agent_messages!.allowed = discordApp!.agent_messages!.allowed!.filter(
          (address) => address !== boot.parent_addr,
        );
        delete discordApp!.chats![boot.parent_addr];
        const saved = await client.putChannelSettings({
          user_id: boot.user_a,
          version: state.settings.version,
          settings: apps as Record<string, unknown>,
        });
        expect(saved.status).toBeLessThan(400);
        const denied = await client.authorizeAndClaimDelivery({
          delivery_key: progressKey,
          kind: "agent_send",
          user_id: boot.user_a,
          platform: "discord",
          target: boot.parent_addr,
          has_non_empty_text: true,
          total_chunks: 1,
        });
        expect(denied.claim_status).toBe("denied");
        expect(denied.code).toBe("not_allowed");
        const deniedRow = await inspectDelivery(harness.baseUrl, progressKey);
        expect(deniedRow.status).toBe("failed");
        expect(deniedRow.target_address).toBe(boot.parent_addr);
        expect(deniedRow.confirmed_thread_address).toBe(progressAddress);
        expect(deniedRow.discord_body_phase).toBe("dispatching");
      } finally {
        await harness.stop();
      }
    },
    180_000,
  );
});
