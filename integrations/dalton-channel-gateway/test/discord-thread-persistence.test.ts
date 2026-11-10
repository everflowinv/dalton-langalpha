/**
 * Real HTTP plus a Discord REST stub for one confirmed thread.
 *
 * The Python harness applies migration d036 and serves real claim/progress.
 * A new adapter and DeliveryService stand in for a restarted gateway process.
 * This file is separate from the existing real HTTP suite selector.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import {
  HttpBackendClient,
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
  const proc = spawn(
    resolvePythonBin(),
    [
      resolve(
        REPO_ROOT,
        "tests/integration/dalton/test_discord_thread_persistence_db.py",
      ),
      "--serve-http-harness",
    ],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, DALTON_CHANNEL_GATEWAY_TEST_DSN: dsn },
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
}

function threadAddress(boot: Boot, threadId: string): string {
  return `discord:${boot.guild_id}/${boot.parent_cid}:thread:${threadId}`;
}

function buildDiscordWorld(boot: Boot, marks: Record<string, string>): DiscordWorld {
  const threads = new Map<string, StoredThread>();
  const calls: FetchCall[] = [];
  const world: DiscordWorld = {
    calls,
    rest: undefined as unknown as RequestClient,
    threads,
    failRejectedMessage: true,
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
      if (body.name.endsWith(marks.ambiguous)) {
        throw new Error("socket hang up");
      }
      const id = body.name.endsWith(marks.rejected)
        ? REJECTED_THREAD_ID
        : body.name.endsWith(marks.lost)
          ? LOST_THREAD_ID
          : "";
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
      return jsonResponse({ id: `msg-${calls.length}` });
    }
    throw new Error(`unexpected Discord ${method} ${route}`);
  };
  world.rest = new RequestClient("test-bot-token", { fetchImpl });
  return world;
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

function groupSend(boot: Boot, deliveryKey: string) {
  return {
    deliveryKey,
    kind: "agent_send" as const,
    userId: boot.user_a,
    platform: "discord",
    target: boot.parent_addr,
    text: "Thread title line\nbody",
    newThread: true,
  };
}

function dropConfirmedAck(
  inner: HttpBackendClient,
  keys: Set<string>,
): HttpBackendClient {
  return new Proxy(inner, {
    get(target, prop, receiver) {
      if (prop === "updateDeliveryProgress") {
        return async (req: I7DeliveriesProgressRequest) => {
          const ack = await target.updateDeliveryProgress(req);
          if (
            typeof req.confirmed_thread_address === "string" &&
            req.confirmed_thread_address.length > 0 &&
            keys.delete(req.delivery_key)
          ) {
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
    "stores the created thread before the first message and reuses it after rejection, a lost ack, and a restart",
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
        const marks = {
          rejected: discordThreadRequestMark({
            deliveryKey: rejectedKey,
            parentChannelId: boot.parent_cid,
            botUserId: BOT_ID,
          }),
          lost: discordThreadRequestMark({
            deliveryKey: lostKey,
            parentChannelId: boot.parent_cid,
            botUserId: BOT_ID,
          }),
          ambiguous: discordThreadRequestMark({
            deliveryKey: ambiguousKey,
            parentChannelId: boot.parent_cid,
            botUserId: BOT_ID,
          }),
        };
        const world = buildDiscordWorld(boot, marks);
        const client = new HttpBackendClient({
          baseUrl: harness.baseUrl,
          serviceToken: boot.service_token,
        });

        const ambiguous = await deliveryFor(client, world.rest).executeDelivery(
          groupSend(boot, ambiguousKey),
        );
        expect(ambiguous.status).toBe("unknown");
        expect(ambiguous.code).toBe("unavailable");
        expect(countCreates(world, boot, marks.ambiguous)).toBe(1);
        const ambiguousRow = await inspectDelivery(harness.baseUrl, ambiguousKey);
        expect(ambiguousRow.status).toBe("unknown");
        expect(ambiguousRow.confirmed_thread_address).toBeNull();
        const callsAfterAmbiguous = world.calls.length;
        const ambiguousAgain = await deliveryFor(client, world.rest).executeDelivery(
          groupSend(boot, ambiguousKey),
        );
        expect(ambiguousAgain.status).toBe("unknown");
        expect(ambiguousAgain.code).toBe("in_flight");
        expect(world.calls.length).toBe(callsAfterAmbiguous);
        expect(countCreates(world, boot, marks.ambiguous)).toBe(1);

        const losing = dropConfirmedAck(client, new Set([lostKey]));
        const lost = await deliveryFor(losing, world.rest).executeDelivery(
          groupSend(boot, lostKey),
        );
        expect(lost.status).toBe("unknown");
        expect(countCreates(world, boot, marks.lost)).toBe(1);
        expect(
          world.calls.some(
            (call) =>
              methodOf(call) === "POST" &&
              routeOf(call.url) === `/channels/${LOST_THREAD_ID}/messages`,
          ),
        ).toBe(false);
        const lostRow = await inspectDelivery(harness.baseUrl, lostKey);
        expect(lostRow.confirmed_thread_address).toBe(threadAddress(boot, LOST_THREAD_ID));
        expect(lostRow.target_address).toBe(boot.parent_addr);
        const lostRetry = await deliveryFor(
          new HttpBackendClient({
            baseUrl: harness.baseUrl,
            serviceToken: boot.service_token,
          }),
          world.rest,
        ).executeDelivery(groupSend(boot, lostKey));
        expect(lostRetry.status).toBe("sent");
        expect(lostRetry.address).toBe(threadAddress(boot, LOST_THREAD_ID));
        expect(countCreates(world, boot, marks.lost)).toBe(1);

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
        const reclaimed = await deliveryFor(
          new HttpBackendClient({
            baseUrl: harness.baseUrl,
            serviceToken: boot.service_token,
          }),
          world.rest,
        ).executeDelivery(groupSend(boot, rejectedKey));
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
        const createPosts = world.calls.filter(
          (call) =>
            methodOf(call) === "POST" &&
            routeOf(call.url) === `/channels/${boot.parent_cid}/threads`,
        );
        expect(createPosts).toHaveLength(3);
        const afterRetry = await inspectDelivery(harness.baseUrl, rejectedKey);
        expect(afterRetry.target_address).toBe(boot.parent_addr);
        expect(afterRetry.confirmed_thread_address).toBe(
          threadAddress(boot, REJECTED_THREAD_ID),
        );
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
        const firstProgress = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: progressAddress,
        });
        expect(firstProgress.status).toBe(200);
        expect(firstProgress.data.confirmed_thread_address).toBe(progressAddress);
        const sameProgress = await postProgress({
          delivery_key: progressKey,
          confirmed_thread_address: progressAddress,
        });
        expect(sameProgress.status).toBe(200);
        expect(sameProgress.data.confirmed_thread_address).toBe(progressAddress);
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
        const progressRow = await inspectDelivery(harness.baseUrl, progressKey);
        expect(progressRow.confirmed_thread_address).toBe(progressAddress);
        expect(progressRow.target_address).toBe(boot.parent_addr);
        expect(progressRow.status).toBe("failed");

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
      } finally {
        await harness.stop();
      }
    },
    120_000,
  );
});
