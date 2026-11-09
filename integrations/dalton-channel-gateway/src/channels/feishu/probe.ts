/**
 * Feishu bot identity probe (`GET /open-apis/bot/v3/info`) with TTL cache.
 *
 * Adapted from `.agy/ref_openclaw_channels/feishu/dist/.setup/probe-DXnFxq5j.mjs`
 * (lines 14–169).
 * License: MIT, © OpenClaw contributors.
 *
 * Design §4.2 row 2 (`src/channels/feishu/probe.ts`).
 */

import type * as Lark from "@larksuiteoapi/node-sdk";
import { raceWithTimeoutAndAbort } from "../../shared/async.js";
import { createFeishuClient, type FeishuCredentials } from "./client.js";

export const FEISHU_PROBE_REQUEST_TIMEOUT_MS = 10_000;
const PROBE_SUCCESS_TTL_MS = 10 * 60 * 1000;
const PROBE_ERROR_TTL_MS = 60 * 1000;
const MAX_PROBE_CACHE_SIZE = 64;

export interface FeishuProbeResult {
  ok: boolean;
  appId?: string;
  botName?: string;
  botOpenId?: string;
  error?: string;
}

export interface FeishuProbeOptions {
  timeoutMs?: number;
  abortSignal?: AbortSignal;
  skipCache?: boolean;
  client?: Lark.Client;
}

interface ProbeCacheEntry {
  result: FeishuProbeResult;
  expiresAt: number;
}

const probeCache = new Map<string, ProbeCacheEntry>();

function pruneProbeCache(now: number): void {
  for (const [key, entry] of probeCache) {
    if (entry.expiresAt <= now) {
      probeCache.delete(key);
    }
  }
  while (probeCache.size > MAX_PROBE_CACHE_SIZE) {
    const oldestKey = probeCache.keys().next().value;
    if (oldestKey === undefined) {
      break;
    }
    probeCache.delete(oldestKey);
  }
}

function setCachedProbeResult(
  cacheKey: string,
  result: FeishuProbeResult,
  ttlMs: number,
): FeishuProbeResult {
  const now = Date.now();
  probeCache.delete(cacheKey);
  probeCache.set(cacheKey, {
    result,
    expiresAt: now + ttlMs,
  });
  pruneProbeCache(now);
  return result;
}

export async function probeFeishu(
  creds?: Partial<FeishuCredentials>,
  options: FeishuProbeOptions = {},
): Promise<FeishuProbeResult> {
  if (!creds?.appId || !creds?.appSecret) {
    return {
      ok: false,
      error: "missing credentials (appId, appSecret)",
    };
  }

  if (options.abortSignal?.aborted) {
    return {
      ok: false,
      appId: creds.appId,
      error: "probe aborted",
    };
  }

  const timeoutMs = options.timeoutMs ?? FEISHU_PROBE_REQUEST_TIMEOUT_MS;
  const cacheKey = `${creds.accountId ?? "default"}:${creds.appId}:${creds.domain ?? "feishu"}`;
  const now = Date.now();
  pruneProbeCache(now);

  if (!options.skipCache) {
    const cached = probeCache.get(cacheKey);
    if (cached && cached.expiresAt > now) {
      return cached.result;
    }
  }

  try {
    const client =
      options.client ??
      createFeishuClient({
        accountId: creds.accountId,
        appId: creds.appId,
        appSecret: creds.appSecret,
        domain: creds.domain,
        encryptKey: creds.encryptKey,
        verificationToken: creds.verificationToken,
      });

    const requestFn = (
      client as unknown as {
        request?: (opts: {
          method: string;
          url: string;
          data?: Record<string, unknown>;
          timeout?: number;
        }) => Promise<{
          code?: number;
          msg?: string;
          bot?: { bot_name?: string; open_id?: string };
          data?: { bot?: { bot_name?: string; open_id?: string } };
        }>;
      }
    ).request;

    if (typeof requestFn !== "function") {
      return {
        ok: false,
        appId: creds.appId,
        error: "Feishu client does not support generic request()",
      };
    }

    const probeResult = await raceWithTimeoutAndAbort(
      requestFn.call(client, {
        method: "GET",
        url: "/open-apis/bot/v3/info",
        data: {},
        timeout: timeoutMs,
      }),
      {
        timeoutMs,
        abortSignal: options.abortSignal,
      },
    );

    if (probeResult.status === "aborted") {
      return {
        ok: false,
        appId: creds.appId,
        error: "probe aborted",
      };
    }
    if (probeResult.status === "timeout") {
      return {
        ok: false,
        appId: creds.appId,
        error: `probe timed out after ${timeoutMs}ms`,
      };
    }
    if (probeResult.status === "rejected") {
      throw probeResult.error;
    }

    const response = probeResult.value;
    if (response.code !== undefined && response.code !== 0) {
      return setCachedProbeResult(
        cacheKey,
        {
          ok: false,
          appId: creds.appId,
          error: `API error: ${response.msg || `code ${response.code}`}`,
        },
        PROBE_ERROR_TTL_MS,
      );
    }

    const bot = response.bot ?? response.data?.bot;
    return setCachedProbeResult(
      cacheKey,
      {
        ok: true,
        appId: creds.appId,
        botName: bot?.bot_name,
        botOpenId: bot?.open_id,
      },
      PROBE_SUCCESS_TTL_MS,
    );
  } catch (err) {
    return setCachedProbeResult(
      cacheKey,
      {
        ok: false,
        appId: creds.appId,
        error: err instanceof Error ? err.message : String(err),
      },
      PROBE_ERROR_TTL_MS,
    );
  }
}

export function clearProbeCache(): void {
  probeCache.clear();
}
