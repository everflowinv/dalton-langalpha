/**
 * Isolated `undici` HTTP connection pools for Weixin iLink (§3.3):
 *
 * - Long-poll pool (`getupdates`): `connections = maxConcurrentAccounts + 16`
 *   so 200+ concurrent 35s long-poll sockets never starve outbound/control traffic.
 * - Control / outbound pool (`sendmessage`, `getuploadurl`, CDN, QR, `sendtyping`):
 *   `connections = 64` (separate Agent instance).
 *
 * Uses dynamic `import("undici")` so the gateway works seamlessly when `undici` is
 * installed in `node_modules` without breaking compilation/tests in environments
 * that rely on Node's built-in fetch without an explicit `undici` dependency.
 */

import {
  createSubsystemLogger,
  extractSafeLogErrorFields,
  type SubsystemLogger,
} from "../../shared/logger.js";

export const WEIXIN_POLL_POOL_HEADROOM = 16;
export const WEIXIN_CONTROL_POOL_CONNECTIONS = 64;
/** Must exceed max `longpolling_timeout_ms` (60s) with headroom so undici never aborts valid long polls (Round 3 Decision #8). */
export const WEIXIN_POLL_POOL_TIMEOUT_MS = 75_000;
export const WEIXIN_CONTROL_POOL_TIMEOUT_MS = 30_000;

export interface WeixinHttpAgentOptions {
  connections: number;
  pipelining: number;
  headersTimeout: number;
  bodyTimeout: number;
  keepAliveTimeout: number;
}

export interface ClosableHttpDispatcher {
  close?: () => Promise<void> | void;
  destroy?: () => Promise<void> | void;
}

export type WeixinHttpAgentFactory = (
  options: WeixinHttpAgentOptions,
) => ClosableHttpDispatcher;

type UndiciAgentCtor = new (
  options: WeixinHttpAgentOptions,
) => ClosableHttpDispatcher;

const poolLogger: SubsystemLogger = createSubsystemLogger("weixin/http-pool");

let cachedUndiciAgentCtor: UndiciAgentCtor | null | undefined;
let undiciLoadPromise: Promise<UndiciAgentCtor | null> | null = null;
let undiciImportWarned = false;

export async function loadUndiciAgentCtorWithImporter(
  importer: () => Promise<{ Agent?: UndiciAgentCtor }>,
  logger: SubsystemLogger = poolLogger,
): Promise<UndiciAgentCtor | null> {
  try {
    const mod = await importer();
    if (typeof mod?.Agent === "function") {
      return mod.Agent;
    }
    logger.warn(
      "Loaded undici module without Agent export; Weixin HTTP pools falling back to default fetch dispatcher",
    );
    return null;
  } catch (err) {
    logger.warn(
      "Failed to load undici Agent; Weixin HTTP pools falling back to default fetch dispatcher",
      extractSafeLogErrorFields(err),
    );
    return null;
  }
}

async function loadUndiciAgentCtor(
  logger: SubsystemLogger = poolLogger,
): Promise<UndiciAgentCtor | null> {
  if (cachedUndiciAgentCtor !== undefined) {
    return cachedUndiciAgentCtor;
  }
  if (!undiciLoadPromise) {
    undiciLoadPromise = (async () => {
      const modName = "undici";
      const ctor = await loadUndiciAgentCtorWithImporter(
        () => import(modName) as Promise<{ Agent?: UndiciAgentCtor }>,
        undiciImportWarned
          ? {
              ...logger,
              warn: () => undefined,
            }
          : logger,
      );
      if (!ctor) {
        undiciImportWarned = true;
      }
      cachedUndiciAgentCtor = ctor;
      return cachedUndiciAgentCtor;
    })();
  }
  return undiciLoadPromise;
}

export interface WeixinHttpPoolManagerOptions {
  maxConcurrentAccounts?: number;
  controlConnections?: number;
  /** Optional custom factory (primarily for unit tests or injected `undici.Agent`). */
  agentFactory?: WeixinHttpAgentFactory;
}

export class WeixinHttpPoolManager {
  private readonly controlConnections: number;
  private readonly customAgentFactory?: WeixinHttpAgentFactory;
  private pollConnections: number;
  private resolvedAgentFactory: WeixinHttpAgentFactory | null = null;
  private pollDispatcher: ClosableHttpDispatcher | null = null;
  private controlDispatcher: ClosableHttpDispatcher | null = null;
  private closed = false;

  constructor(options: WeixinHttpPoolManagerOptions = {}) {
    const initialMax =
      typeof options.maxConcurrentAccounts === "number" &&
      Number.isFinite(options.maxConcurrentAccounts) &&
      options.maxConcurrentAccounts > 0
        ? Math.trunc(options.maxConcurrentAccounts)
        : 50;
    this.pollConnections = initialMax + WEIXIN_POLL_POOL_HEADROOM;
    this.controlConnections =
      typeof options.controlConnections === "number" &&
      Number.isFinite(options.controlConnections) &&
      options.controlConnections > 0
        ? Math.trunc(options.controlConnections)
        : WEIXIN_CONTROL_POOL_CONNECTIONS;
    this.customAgentFactory = options.agentFactory;
    if (this.customAgentFactory) {
      this.resolvedAgentFactory = this.customAgentFactory;
      this.ensureDispatchers();
    } else {
      // Warm undici Agent constructor asynchronously so pools are ready immediately when installed.
      void this.initAsync();
    }
  }

  async initAsync(): Promise<void> {
    if (this.closed || this.resolvedAgentFactory) {
      return;
    }
    const AgentCtor = await loadUndiciAgentCtor();
    if (this.closed || !AgentCtor) {
      return;
    }
    this.resolvedAgentFactory = (opts) => new AgentCtor(opts);
    this.ensureDispatchers();
  }

  getPollPoolConnections(): number {
    return this.pollConnections;
  }

  getControlPoolConnections(): number {
    return this.controlConnections;
  }

  getPollDispatcher(): ClosableHttpDispatcher | undefined {
    if (this.closed) return undefined;
    this.ensureDispatchers();
    return this.pollDispatcher ?? undefined;
  }

  getControlDispatcher(): ClosableHttpDispatcher | undefined {
    if (this.closed) return undefined;
    this.ensureDispatchers();
    return this.controlDispatcher ?? undefined;
  }

  updateMaxConcurrentAccounts(maxConcurrentAccounts: number): void {
    if (
      !Number.isFinite(maxConcurrentAccounts) ||
      maxConcurrentAccounts <= 0
    ) {
      return;
    }
    const nextConnections =
      Math.trunc(maxConcurrentAccounts) + WEIXIN_POLL_POOL_HEADROOM;
    if (nextConnections === this.pollConnections) {
      return;
    }
    this.pollConnections = nextConnections;
    if (this.closed || !this.resolvedAgentFactory) {
      return;
    }
    const prev = this.pollDispatcher;
    this.pollDispatcher = this.createPollDispatcher(this.resolvedAgentFactory);
    if (prev) {
      void Promise.resolve(prev.close?.()).catch(() => undefined);
    }
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    const prevPoll = this.pollDispatcher;
    const prevControl = this.controlDispatcher;
    this.pollDispatcher = null;
    this.controlDispatcher = null;
    await Promise.allSettled([
      Promise.resolve(prevPoll?.close?.()),
      Promise.resolve(prevControl?.close?.()),
    ]);
  }

  private createPollDispatcher(
    factory: WeixinHttpAgentFactory,
  ): ClosableHttpDispatcher {
    return factory({
      connections: this.pollConnections,
      pipelining: 1,
      headersTimeout: WEIXIN_POLL_POOL_TIMEOUT_MS,
      bodyTimeout: WEIXIN_POLL_POOL_TIMEOUT_MS,
      keepAliveTimeout: 30_000,
    });
  }

  private createControlDispatcher(
    factory: WeixinHttpAgentFactory,
  ): ClosableHttpDispatcher {
    return factory({
      connections: this.controlConnections,
      pipelining: 1,
      headersTimeout: WEIXIN_CONTROL_POOL_TIMEOUT_MS,
      bodyTimeout: WEIXIN_CONTROL_POOL_TIMEOUT_MS,
      keepAliveTimeout: 30_000,
    });
  }

  private ensureDispatchers(): void {
    if (this.closed || !this.resolvedAgentFactory) {
      return;
    }
    if (!this.pollDispatcher) {
      this.pollDispatcher = this.createPollDispatcher(
        this.resolvedAgentFactory,
      );
    }
    if (!this.controlDispatcher) {
      this.controlDispatcher = this.createControlDispatcher(
        this.resolvedAgentFactory,
      );
    }
  }
}
