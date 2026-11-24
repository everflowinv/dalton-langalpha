/**
 * Discord REST v10 client: route-bucket scheduler, global/bucket 429 handling,
 * multipart attachment upload, bounded response reads, and redacted errors.
 *
 * Adapted from `.agy/ref_openclaw_channels/discord/dist/.setup/discord-BGqgx-yd.mjs`
 * (lines 1175-1845: `serializeRequestBody`, `DiscordError`, `RateLimitError`,
 * `RestScheduler`, `RequestClient`). License: MIT, Copyright (c) 2025 Peter Steinberger.
 * OpenClaw host permission/effect fencing (`getDiscordEndpointRuntime`,
 * `capture*Authority`, `effect.run/initiate`) is removed; requests go directly
 * through the injected fetch transport.
 *
 * Failure classification: a Discord 4xx rejection is confirmed `failed`, a 429
 * is `rate_limited` (the request was not executed, so a scheduler retry keeps
 * the original body, including its nonce), and a timeout/disconnect/5xx on a
 * message create is `unknown` because Discord may already have stored it.
 * Non-idempotent writes are never retried after an ambiguous failure.
 */

import { gunzipSync } from "node:zlib";
import { readStreamWithLimit } from "../../shared/safe-fetch.js";
import { redactCredentialString } from "../../shared/logger.js";
import { ChannelDeliveryError } from "../adapter.js";

export const DISCORD_API_BASE_URL = "https://discord.com/api/v10";
export const DISCORD_REST_RESPONSE_MAX_BYTES = 8 * 1024 * 1024;
export const MAX_RATE_LIMIT_RETRIES = 3;

export type DiscordRequestPriority = "critical" | "standard" | "background";
export type DiscordFetch = (url: string, init: RequestInit) => Promise<Response>;

export interface DiscordFilePart {
  name: string;
  data: Buffer | Uint8Array | Blob;
  contentType?: string;
  description?: string;
}

export interface DiscordRequestOptions {
  data?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  priority?: DiscordRequestPriority;
  /** Safe to retry after an ambiguous (timeout/5xx) failure. */
  idempotent?: boolean;
  /** Optional abort signal to cancel a queued (or in-flight) request early. */
  signal?: AbortSignal;
}

interface QueuedRequest {
  method: string;
  path: string;
  priority: DiscordRequestPriority;
  data?: unknown;
  query?: DiscordRequestOptions["query"];
  idempotent: boolean;
  signal?: AbortSignal;
  cleanupAbort?: () => void;
  routeKey: string;
  enqueuedAt: number;
  generation: number;
  retryCount: number;
  resolve: (value: unknown) => void;
  reject: (err: unknown) => void;
}

interface Bucket {
  active: number;
  pending: Record<DiscordRequestPriority, QueuedRequest[]>;
  resetAt: number;
  remaining?: number;
  limit?: number;
  routeKeys: Set<string>;
}

export class DiscordError extends Error {
  readonly status: number;
  readonly discordCode?: number;
  readonly rawBody: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(redactCredentialString(message));
    this.name = "DiscordError";
    this.status = status;
    this.rawBody = body;
    if (body && typeof body === "object" && "code" in body) {
      const code = (body as { code?: unknown }).code;
      if (typeof code === "number") this.discordCode = code;
    }
  }
}

export class RateLimitError extends DiscordError {
  readonly retryAfter: number;
  readonly global: boolean;
  readonly bucket: string | null;
  constructor(status: number, body: unknown, retryAfter: number, global: boolean, bucket: string | null) {
    super(status, body, "Discord rate limit exceeded");
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
    this.global = global;
    this.bucket = bucket;
  }
}

export type DiscordQueueErrorReason =
  | "queue_full"
  | "queue_cleared"
  | "queue_aborted"
  | "queue_stale";

/**
 * Raised when a request is rejected by the local scheduler before any HTTP
 * request is dispatched to Discord. Because the payload never left the gateway,
 * message creates are confirmed `failed` rather than `unknown`.
 */
export class DiscordQueueError extends Error {
  readonly reason: DiscordQueueErrorReason;
  readonly dispatched = false as const;
  constructor(reason: DiscordQueueErrorReason, message: string) {
    super(redactCredentialString(message));
    this.name = "DiscordQueueError";
    this.reason = reason;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function parseRetryAfterSeconds(body: unknown, headers: Headers, fallback = 1): number {
  if (isRecord(body) && typeof body.retry_after === "number" && Number.isFinite(body.retry_after)) {
    return Math.max(0, body.retry_after);
  }
  const header = headers.get("retry-after");
  if (header) {
    const n = Number(header);
    if (Number.isFinite(n)) return Math.max(0, n);
    const date = Date.parse(header);
    if (Number.isFinite(date)) return Math.max(0, (date - Date.now()) / 1000);
  }
  return fallback;
}

export function createRouteKey(method: string, path: string): string {
  const pathname = path.split("?")[0] ?? path;
  const segments = pathname.replace(/^\/+/, "").split("/");
  if ((segments[0] === "webhooks" || segments[0] === "interactions") && segments[2]) {
    segments[2] = "[redacted]";
  }
  // Discord rate-limit buckets are keyed by channel major parameter, not message_id or emoji.
  // Normalize reaction endpoints so per-message reactions do not accumulate unbounded route mappings.
  if (
    segments[0] === "channels" &&
    segments[1] &&
    segments[2] === "messages" &&
    segments[3] &&
    segments[4] === "reactions"
  ) {
    segments[3] = ":message_id";
    if (segments[5]) {
      segments[5] = ":emoji";
    }
  }
  return `${method.toUpperCase()} /${segments.join("/")}`;
}

function readTopLevelRouteKey(path: string): string {
  const [pathname = path] = path.split("?");
  const [first, id] = pathname.replace(/^\/+/, "").split("/");
  if (!first) return pathname;
  if ((first === "channels" || first === "guilds" || first === "webhooks") && id) return `${first}/${id}`;
  return first;
}

function headerNumber(headers: Headers, name: string): number | undefined {
  const raw = headers.get(name);
  if (!raw || !/^\d+(?:\.\d+)?$/.test(raw.trim())) return undefined;
  const n = Number(raw.trim());
  return Number.isFinite(n) ? n : undefined;
}

function readResetAt(headers: Headers, now: number): number | undefined {
  const after = headerNumber(headers, "x-ratelimit-reset-after");
  if (after !== undefined) return now + Math.ceil(after * 1000);
  const reset = headerNumber(headers, "x-ratelimit-reset");
  return reset !== undefined ? Math.floor(reset * 1000) : undefined;
}

/** Serializes a Discord request body; `files` become multipart `files[i]` + `payload_json`. */
export function serializeRequestBody(
  data: unknown,
  headers: Headers,
): string | FormData | undefined {
  if (data === undefined || data === null) return undefined;
  if (isRecord(data) && Array.isArray(data.files) && data.files.length > 0) {
    const files = data.files as DiscordFilePart[];
    const { files: _ignored, ...payload } = data;
    const existing = Array.isArray(payload.attachments) ? [...(payload.attachments as unknown[])] : [];
    const form = new FormData();
    const uploaded = files.map((file, index) => {
      const id = existing.length + index;
      const blob =
        file.data instanceof Blob
          ? file.data
          : new Blob([file.data as BlobPart], file.contentType ? { type: file.contentType } : undefined);
      form.append(`files[${id}]`, blob, file.name || `file-${index}`);
      return {
        id,
        filename: file.name || `file-${index}`,
        ...(file.description ? { description: file.description } : {}),
      };
    });
    form.append("payload_json", JSON.stringify({ ...payload, attachments: [...existing, ...uploaded] }));
    return form;
  }
  headers.set("Content-Type", "application/json");
  return typeof data === "string" ? data : JSON.stringify(data);
}

const PRIORITIES: DiscordRequestPriority[] = ["critical", "standard", "background"];
const LANE_SCHEDULE: DiscordRequestPriority[] = [
  "critical", "critical", "critical", "critical", "critical",
  "critical", "standard", "standard", "standard", "background",
];
const MAX_QUEUE_SIZE = 1000;
const MAX_CONCURRENT_WORKERS = 4;
/** Cap background (reaction/typing status feedback) concurrency so standard/critical requests (including channel/user GETs, replies, and error notices) always have slots. */
const MAX_BACKGROUND_CONCURRENT_WORKERS = 1;
const BACKGROUND_STALE_MS = 20_000;

export interface RestSchedulerOptions {
  maxQueueSize?: number;
  now?: () => number;
  setTimeoutFn?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeoutFn?: (t: ReturnType<typeof setTimeout>) => void;
}

export class RestScheduler {
  private readonly buckets = new Map<string, Bucket>();
  private readonly routeBuckets = new Map<string, string>();
  private globalRateLimitUntil = 0;
  private activeWorkers = 0;
  private activeBackgroundWorkers = 0;
  private queuedRequests = 0;
  private readonly queuedByLane: Record<DiscordRequestPriority, number> = {
    critical: 0, standard: 0, background: 0,
  };
  private laneCursor = 0;
  private queueGeneration = 0;
  private drainTimer: ReturnType<typeof setTimeout> | undefined;
  private drainTimerAt: number | undefined;
  private readonly maxQueueSize: number;
  private readonly now: () => number;
  private readonly setTimeoutFn: NonNullable<RestSchedulerOptions["setTimeoutFn"]>;
  private readonly clearTimeoutFn: NonNullable<RestSchedulerOptions["clearTimeoutFn"]>;

  constructor(
    private readonly executor: (req: QueuedRequest) => Promise<unknown>,
    options: RestSchedulerOptions = {},
  ) {
    this.maxQueueSize = options.maxQueueSize ?? MAX_QUEUE_SIZE;
    this.now = options.now ?? Date.now;
    this.setTimeoutFn = options.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimeoutFn = options.clearTimeoutFn ?? ((t) => clearTimeout(t));
  }

  get queueSize(): number { return this.queuedRequests; }
  get routeBucketCount(): number { return this.routeBuckets.size; }
  get bucketCount(): number { return this.buckets.size; }

  enqueue(params: {
    method: string; path: string; priority?: DiscordRequestPriority;
    data?: unknown; query?: DiscordRequestOptions["query"]; idempotent?: boolean;
    signal?: AbortSignal;
  }): Promise<unknown> {
    if (params.signal?.aborted) {
      return Promise.reject(new DiscordQueueError("queue_aborted", "Discord request aborted before dispatch"));
    }
    if (this.queuedRequests >= this.maxQueueSize) {
      return Promise.reject(new DiscordQueueError("queue_full", "Discord request queue is full"));
    }
    const routeKey = createRouteKey(params.method, params.path);
    const bucket = this.getBucket(this.routeBuckets.get(routeKey) ?? routeKey);
    // Only explicit `"background"` status feedback requests (reactions/typing) share the
    // single-concurrency background lane; all other requests (including GET /channels/{id}
    // and GET /users/@me) default to `"standard"` so status requests never block replies or inbound.
    const priority = params.priority ?? "standard";
    return new Promise<unknown>((resolve, reject) => {
      this.queuedRequests += 1;
      this.queuedByLane[priority] += 1;
      const queued: QueuedRequest = {
        method: params.method, path: params.path, priority, data: params.data,
        query: params.query, idempotent: params.idempotent ?? params.method.toUpperCase() === "GET",
        signal: params.signal,
        routeKey, enqueuedAt: this.now(), generation: this.queueGeneration,
        retryCount: 0, resolve, reject,
      };
      if (params.signal) {
        const onAbort = (): void => {
          this.cancelQueuedRequest(queued);
        };
        params.signal.addEventListener("abort", onAbort, { once: true });
        queued.cleanupAbort = () => {
          params.signal?.removeEventListener("abort", onAbort);
        };
      }
      bucket.pending[priority].push(queued);
      this.drainQueues();
    });
  }

  private cancelQueuedRequest(target: QueuedRequest): void {
    for (const bucket of this.buckets.values()) {
      const queue = bucket.pending[target.priority];
      const idx = queue.indexOf(target);
      if (idx !== -1) {
        queue.splice(idx, 1);
        target.cleanupAbort?.();
        target.cleanupAbort = undefined;
        this.queuedRequests = Math.max(0, this.queuedRequests - 1);
        this.queuedByLane[target.priority] = Math.max(0, this.queuedByLane[target.priority] - 1);
        target.reject(new DiscordQueueError("queue_aborted", "Discord request aborted before dispatch"));
        return;
      }
    }
  }

  clearQueue(): void {
    this.queueGeneration += 1;
    if (this.drainTimer) { this.clearTimeoutFn(this.drainTimer); this.drainTimer = undefined; this.drainTimerAt = undefined; }
    this.rejectPending(new DiscordQueueError("queue_cleared", "Discord request queue cleared"));
  }

  abortPending(): void {
    this.queueGeneration += 1;
    this.rejectPending(new DiscordQueueError("queue_aborted", "Discord request aborted before dispatch"));
  }

  private rejectPending(error: unknown): void {
    for (const bucket of this.buckets.values()) {
      for (const lane of PRIORITIES) {
        for (const queued of bucket.pending[lane].splice(0)) {
          queued.cleanupAbort?.();
          queued.cleanupAbort = undefined;
          queued.reject(error);
          this.queuedRequests = Math.max(0, this.queuedRequests - 1);
          this.queuedByLane[lane] = Math.max(0, this.queuedByLane[lane] - 1);
        }
      }
    }
  }

  private getBucket(key: string): Bucket {
    let bucket = this.buckets.get(key);
    if (!bucket) {
      bucket = { active: 0, pending: { critical: [], standard: [], background: [] }, resetAt: 0, routeKeys: new Set([key]) };
      this.buckets.set(key, bucket);
    }
    return bucket;
  }

  private bindRouteToBucket(routeKey: string, bucketKey: string): Bucket {
    const target = this.getBucket(bucketKey);
    target.routeKeys.add(routeKey);
    this.routeBuckets.set(routeKey, bucketKey);
    const routeBucket = this.buckets.get(routeKey);
    if (routeBucket && routeBucket !== target) {
      for (const lane of PRIORITIES) {
        target.pending[lane].push(...routeBucket.pending[lane]);
        routeBucket.pending[lane] = [];
      }
      if (routeBucket.active === 0) this.buckets.delete(routeKey);
    }
    return target;
  }

  recordResponse(routeKey: string, path: string, response: Response, parsed: unknown): void {
    const bucketHeader = response.headers.get("x-ratelimit-bucket")?.trim();
    const bucket = bucketHeader
      ? this.bindRouteToBucket(routeKey, `${bucketHeader}:${readTopLevelRouteKey(path)}`)
      : this.getBucket(this.routeBuckets.get(routeKey) ?? routeKey);
    const limit = headerNumber(response.headers, "x-ratelimit-limit");
    if (limit !== undefined) bucket.limit = limit;
    const remaining = headerNumber(response.headers, "x-ratelimit-remaining");
    if (remaining !== undefined) bucket.remaining = remaining;
    const resetAt = readResetAt(response.headers, this.now());
    if (resetAt !== undefined) bucket.resetAt = resetAt;
    if (response.status !== 429) return;
    const retryAt = this.now() + Math.ceil(parseRetryAfterSeconds(parsed, response.headers, 1) * 1000);
    const isGlobal =
      response.headers.get("x-ratelimit-global") === "true" ||
      (isRecord(parsed) && parsed.global === true) ||
      response.headers.get("x-ratelimit-scope") === "global";
    if (isGlobal) {
      this.globalRateLimitUntil = Math.max(this.globalRateLimitUntil, retryAt);
      return;
    }
    bucket.remaining = 0;
    bucket.resetAt = Math.max(bucket.resetAt, retryAt);
  }

  private scheduleDrain(delayMs: number): void {
    // Keep the earliest wake-up: a later one set for the status lane must not hold
    // back a standard request that can go sooner.
    const at = this.now() + Math.max(0, delayMs);
    if (this.drainTimer && this.drainTimerAt !== undefined && this.drainTimerAt <= at) return;
    if (this.drainTimer) this.clearTimeoutFn(this.drainTimer);
    this.drainTimerAt = at;
    this.drainTimer = this.setTimeoutFn(() => {
      this.drainTimer = undefined;
      this.drainTimerAt = undefined;
      this.drainQueues();
    }, Math.max(0, delayMs));
    (this.drainTimer as { unref?: () => void }).unref?.();
  }

  private drainQueues(): void {
    // Status requests (background lane) have a slot of their own and never take one of
    // the standard slots that replies, notices and lookups use.
    while (
      this.activeWorkers < MAX_CONCURRENT_WORKERS ||
      this.activeBackgroundWorkers < MAX_BACKGROUND_CONCURRENT_WORKERS
    ) {
      const next = this.takeNext();
      if (!next.queued) {
        if (next.waitMs !== undefined && Number.isFinite(next.waitMs)) this.scheduleDrain(next.waitMs);
        break;
      }
      const { bucket, queued } = next;
      if (bucket.remaining !== undefined && bucket.remaining > 0) bucket.remaining -= 1;
      bucket.active += 1;
      if (queued.priority === "background") {
        this.activeBackgroundWorkers += 1;
      } else {
        this.activeWorkers += 1;
      }
      void this.runQueued(queued, bucket);
    }
  }

  private takeNext():
    | { bucket: Bucket; queued: QueuedRequest; waitMs?: undefined }
    | { bucket?: undefined; queued?: undefined; waitMs?: number } {
    const now = this.now();
    if (this.globalRateLimitUntil > now) return { waitMs: this.globalRateLimitUntil - now };
    let nextDelay: number | undefined;
    const buckets = [...this.buckets.values()].filter(
      (b) => PRIORITIES.reduce((n, lane) => n + b.pending[lane].length, 0) > 0,
    );
    if (buckets.length === 0) return {};
    for (let offset = 0; offset < LANE_SCHEDULE.length; offset += 1) {
      const lane = LANE_SCHEDULE[(this.laneCursor + offset) % LANE_SCHEDULE.length]!;
      if (this.queuedByLane[lane] <= 0) continue;
      if (lane === "background" && this.activeBackgroundWorkers >= MAX_BACKGROUND_CONCURRENT_WORKERS) {
        continue;
      }
      if (lane !== "background" && this.activeWorkers >= MAX_CONCURRENT_WORKERS) {
        continue;
      }
      for (const bucket of buckets) {
        const queue = bucket.pending[lane];
        while (queue.length > 0 && queue[0]!.signal?.aborted) {
          const aborted = queue.shift()!;
          aborted.cleanupAbort?.();
          aborted.cleanupAbort = undefined;
          this.queuedRequests = Math.max(0, this.queuedRequests - 1);
          this.queuedByLane[lane] = Math.max(0, this.queuedByLane[lane] - 1);
          aborted.reject(
            new DiscordQueueError("queue_aborted", "Discord request aborted before dispatch"),
          );
        }
        if (lane === "background") {
          while (queue.length > 0 && now - queue[0]!.enqueuedAt > BACKGROUND_STALE_MS) {
            const stale = queue.shift()!;
            stale.cleanupAbort?.();
            stale.cleanupAbort = undefined;
            this.queuedRequests = Math.max(0, this.queuedRequests - 1);
            this.queuedByLane[lane] = Math.max(0, this.queuedByLane[lane] - 1);
            stale.reject(
              new DiscordQueueError(
                "queue_stale",
                `Dropped stale background request after ${now - stale.enqueuedAt}ms`,
              ),
            );
          }
        }
        if (queue.length === 0 || bucket.active > 0) continue;
        const waitMs = bucket.remaining === 0 && bucket.resetAt > now ? bucket.resetAt - now : 0;
        if (waitMs > 0) { nextDelay = Math.min(nextDelay ?? waitMs, waitMs); continue; }
        if (bucket.remaining === 0 && bucket.resetAt <= now) bucket.remaining = bucket.limit;
        const queued = queue.shift()!;
        queued.cleanupAbort?.();
        queued.cleanupAbort = undefined;
        this.queuedByLane[lane] = Math.max(0, this.queuedByLane[lane] - 1);
        this.laneCursor = (this.laneCursor + offset + 1) % LANE_SCHEDULE.length;
        return { bucket, queued };
      }
    }
    return { waitMs: nextDelay };
  }

  private async runQueued(queued: QueuedRequest, bucket: Bucket): Promise<void> {
    let requeued = false;
    let failed = false;
    try {
      if (queued.signal?.aborted) {
        queued.reject(new DiscordQueueError("queue_aborted", "Discord request aborted before dispatch"));
        return;
      }
      queued.resolve(await this.executor(queued));
    } catch (error) {
      // A 429 means Discord rejected the request before executing it, so
      // requeueing is safe for any method and reuses the original body/nonce.
      if (
        error instanceof RateLimitError &&
        !queued.signal?.aborted &&
        queued.generation === this.queueGeneration &&
        queued.retryCount < MAX_RATE_LIMIT_RETRIES
      ) {
        requeued = true;
        const bucketKey = this.routeBuckets.get(queued.routeKey) ?? queued.routeKey;
        const retried: QueuedRequest = {
          ...queued,
          enqueuedAt: this.now(),
          retryCount: queued.retryCount + 1,
        };
        if (retried.signal) {
          const onAbort = (): void => {
            this.cancelQueuedRequest(retried);
          };
          retried.signal.addEventListener("abort", onAbort, { once: true });
          retried.cleanupAbort = () => {
            retried.signal?.removeEventListener("abort", onAbort);
          };
        }
        this.getBucket(bucketKey).pending[queued.priority].push(retried);
        this.queuedByLane[queued.priority] += 1;
        const waitMs = Math.max(0, (error.global ? this.globalRateLimitUntil : bucket.resetAt) - this.now(), error.retryAfter * 1000);
        this.scheduleDrain(waitMs);
        return;
      }
      failed = true;
      queued.reject(error);
    } finally {
      bucket.active = Math.max(0, bucket.active - 1);
      if (queued.priority === "background") {
        this.activeBackgroundWorkers = Math.max(0, this.activeBackgroundWorkers - 1);
      } else {
        this.activeWorkers = Math.max(0, this.activeWorkers - 1);
      }
      if (!requeued) this.queuedRequests = Math.max(0, this.queuedRequests - 1);
      if (failed && queued.priority === "background") {
        // Yield one microtask so the caller's `.catch()` (e.g. Discord 404 withdrawal)
        // can abort queued follow-up background requests before the next one dispatches.
        queueMicrotask(() => this.drainQueues());
      } else {
        this.drainQueues();
      }
    }
  }
}

async function readResponseBodyText(response: Response, idleTimeoutMs: number): Promise<string> {
  if (!response.body) return "";
  const buffer = await readStreamWithLimit(response.body, {
    maxBytes: DISCORD_REST_RESPONSE_MAX_BYTES,
    chunkIdleTimeoutMs: idleTimeoutMs,
  });
  if (!buffer.byteLength) return "";
  if (buffer[0] === 0x1f && buffer[1] === 0x8b) {
    return gunzipSync(buffer, { maxOutputLength: DISCORD_REST_RESPONSE_MAX_BYTES }).toString("utf8");
  }
  return buffer.toString("utf8");
}

function coerceBody(raw: string): unknown {
  if (!raw) return undefined;
  try { return JSON.parse(raw) as unknown; } catch { return raw; }
}

function appendQuery(path: string, query?: DiscordRequestOptions["query"]): string {
  if (!query) return path;
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined) search.set(k, String(v));
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

export interface RequestClientOptions {
  apiBaseUrl?: string;
  timeoutMs?: number;
  queueRequests?: boolean;
  fetchImpl?: DiscordFetch;
  schedulerOptions?: RestSchedulerOptions;
}

export class RequestClient {
  readonly scheduler: RestScheduler;
  private readonly token: string;
  private readonly apiBaseUrl: string;
  private readonly timeoutMs: number;
  private readonly queueRequests: boolean;
  private readonly fetchImpl?: DiscordFetch;
  private readonly controllers = new Set<AbortController>();

  constructor(token: string, options: RequestClientOptions = {}) {
    this.token = token.replace(/^Bot\s+/i, "");
    this.apiBaseUrl = (options.apiBaseUrl ?? DISCORD_API_BASE_URL).replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? 15_000;
    this.queueRequests = options.queueRequests ?? true;
    this.fetchImpl = options.fetchImpl;
    this.scheduler = new RestScheduler(
      async (req) => this.executeRequest(req.method, req.path, { data: req.data, query: req.query, signal: req.signal }, req.routeKey),
      options.schedulerOptions,
    );
  }

  get queueSize(): number { return this.scheduler.queueSize; }
  clearQueue(): void { this.scheduler.clearQueue(); }
  abortAllRequests(): void {
    this.scheduler.abortPending();
    for (const c of this.controllers) c.abort();
    this.controllers.clear();
  }

  get(path: string, query?: DiscordRequestOptions["query"]): Promise<unknown> {
    return this.request("GET", path, { query, idempotent: true });
  }
  post(path: string, data?: unknown, opts: Omit<DiscordRequestOptions, "data"> = {}): Promise<unknown> {
    return this.request("POST", path, { ...opts, data });
  }
  put(path: string, data?: unknown, opts: Omit<DiscordRequestOptions, "data"> = {}): Promise<unknown> {
    return this.request("PUT", path, { ...opts, data });
  }
  patch(path: string, data?: unknown, opts: Omit<DiscordRequestOptions, "data"> = {}): Promise<unknown> {
    return this.request("PATCH", path, { ...opts, data });
  }
  delete(path: string, opts: DiscordRequestOptions = {}): Promise<unknown> {
    return this.request("DELETE", path, opts);
  }

  request(method: string, path: string, params: DiscordRequestOptions = {}): Promise<unknown> {
    const upper = method.toUpperCase();
    if (!this.queueRequests) {
      return this.executeRequest(upper, path, params, createRouteKey(upper, path));
    }
    return this.scheduler.enqueue({
      method: upper, path, priority: params.priority, data: params.data,
      query: params.query, idempotent: params.idempotent ?? upper === "GET",
      signal: params.signal,
    });
  }

  async executeRequest(
    method: string, path: string, params: DiscordRequestOptions = {},
    routeKey: string = createRouteKey(method, path),
  ): Promise<unknown> {
    if (params.signal?.aborted) {
      throw new DiscordQueueError("queue_aborted", "Discord request aborted before dispatch");
    }
    const url = `${this.apiBaseUrl}${appendQuery(path, params.query)}`;
    const headers = new Headers({ "User-Agent": "DaltonChannelGateway (discord)" });
    if (this.token && this.token !== "webhook") headers.set("Authorization", `Bot ${this.token}`);
    const body = serializeRequestBody(params.data, headers);
    const controller = new AbortController();
    const onExternalAbort = (): void => controller.abort();
    params.signal?.addEventListener("abort", onExternalAbort, { once: true });
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    (timer as { unref?: () => void }).unref?.();
    this.controllers.add(controller);
    try {
      const fetchFn = this.fetchImpl ?? globalThis.fetch;
      const response = await fetchFn(url, { method, headers, body, signal: controller.signal });
      const text = await readResponseBodyText(response, this.timeoutMs).catch((err: unknown) => {
        if (response.ok) throw err;
        return "";
      });
      const parsed = coerceBody(text);
      this.scheduler.recordResponse(routeKey, path, response, parsed);
      if (response.status === 204) return undefined;
      if (response.status === 429) {
        const isGlobal =
          response.headers.get("x-ratelimit-global") === "true" ||
          (isRecord(parsed) && parsed.global === true);
        throw new RateLimitError(
          response.status, parsed,
          parseRetryAfterSeconds(parsed, response.headers, 1),
          isGlobal,
          response.headers.get("x-ratelimit-bucket"),
        );
      }
      if (!response.ok) {
        const message = isRecord(parsed) && typeof parsed.message === "string"
          ? parsed.message : `Discord API request failed (${response.status})`;
        throw new DiscordError(response.status, parsed, message);
      }
      return parsed;
    } finally {
      clearTimeout(timer);
      params.signal?.removeEventListener("abort", onExternalAbort);
      this.controllers.delete(controller);
    }
  }
}

/**
 * Maps a Discord REST/transport error to the delivery outcome contract.
 * `stage: "read"` failures happen before any message is dispatched.
 */
export function classifyDiscordDeliveryError(
  err: unknown,
  options: { stage?: "send" | "read" | "upload"; message?: string } = {},
): ChannelDeliveryError {
  if (err instanceof ChannelDeliveryError) return err;
  const prefix = options.message ?? "Discord message send failed";
  const stage = options.stage ?? "send";
  if (err instanceof DiscordQueueError) {
    return new ChannelDeliveryError(`${prefix}: ${err.message}`, {
      outcome: "failed",
      code: "unavailable",
    });
  }
  if (err instanceof RateLimitError) {
    return new ChannelDeliveryError(`${prefix}: rate limited`, { outcome: "rate_limited", code: "rate_limited" });
  }
  if (err instanceof DiscordError) {
    if (err.status === 429) {
      return new ChannelDeliveryError(`${prefix}: rate limited`, { outcome: "rate_limited", code: "rate_limited" });
    }
    if (err.status === 404 || err.discordCode === 10003 || err.discordCode === 10008 || err.discordCode === 10013) {
      return new ChannelDeliveryError(`${prefix}: ${err.message}`, { outcome: "failed", code: "target_not_found" });
    }
    if (err.status === 401 || err.status === 403 || err.discordCode === 50001 || err.discordCode === 50013) {
      return new ChannelDeliveryError(`${prefix}: ${err.message}`, { outcome: "failed", code: "not_allowed" });
    }
    if (err.status >= 500) {
      return new ChannelDeliveryError(`${prefix}: ${err.message}`, {
        outcome: stage === "send" ? "unknown" : "failed", code: "unavailable",
      });
    }
    return new ChannelDeliveryError(`${prefix}: ${err.message}`, { outcome: "failed", code: "unavailable" });
  }
  const message = err instanceof Error ? err.message : String(err);
  return new ChannelDeliveryError(`${prefix}: ${redactCredentialString(message)}`, {
    outcome: stage === "send" ? "unknown" : "failed",
    code: "unavailable",
  });
}
