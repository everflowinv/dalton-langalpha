import { scrubNativeCommandSecrets } from "../../native-commands/redact.js";
import { createSubsystemLogger, type SubsystemLogger } from "../../shared/logger.js";
import { renderDiscordMarkdown } from "./chunk.js";

export const DISCORD_API_V10_BASE = "https://discord.com/api/v10";

export const DISCORD_INTERACTION_CALLBACK_TYPE_DEFERRED_CHANNEL_MESSAGE = 5;
export const DISCORD_INTERACTION_CALLBACK_TYPE_AUTOCOMPLETE_RESULT = 8;
export const DISCORD_EPHEMERAL_MESSAGE_FLAG = 64;

export const DISCORD_INTERACTION_INITIAL_CALLBACK_TIMEOUT_MS = 2_500;
export const DISCORD_INTERACTION_MAX_CALLBACK_DEADLINE_MS = 3_000;
export const DISCORD_INTERACTION_WEBHOOK_EDIT_TIMEOUT_MS = 10_000;
export const DISCORD_INTERACTION_TOKEN_TTL_MS = 15 * 60 * 1000;
export const DISCORD_AUTOCOMPLETE_MAX_CHOICES = 25;
export const DISCORD_AUTOCOMPLETE_MAX_NAME_LENGTH = 100;
export const DISCORD_AUTOCOMPLETE_MAX_VALUE_LENGTH = 100;
export const DISCORD_INTERACTION_MAX_CONTENT_LENGTH = 2_000;
export const DISCORD_INTERACTION_MAX_RESPONSE_BODY_BYTES = 64 * 1024;

export type DiscordInteractionAckOutcome = "acknowledged" | "failed" | "unknown";
export type DiscordInteractionReplyOutcome = "sent" | "failed" | "expired" | "unknown";

export interface DiscordInteractionAckResult {
  outcome: DiscordInteractionAckOutcome;
  status?: number;
  discordCode?: number;
  retryAfterMs?: number;
  reason?: string;
}

export interface DiscordInteractionReplyResult {
  outcome: DiscordInteractionReplyOutcome;
  status?: number;
  discordCode?: number;
  retryAfterMs?: number;
  reason?: string;
}

export interface DiscordAutocompleteChoice {
  name: string;
  value: string;
}

export interface DiscordInteractionHttpOptions {
  apiBaseUrl?: string;
  fetchImpl?: typeof fetch;
  logger?: SubsystemLogger;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  initialCallbackTimeoutMs?: number;
  webhookEditTimeoutMs?: number;
  tokenTtlMs?: number;
  maxResponseBodyBytes?: number;
}

interface BoundedFetchResponse {
  status: number;
  headers: Headers;
  bodyJson: unknown;
}

export function redactDiscordInteractionText(
  input: string,
  extraSecrets: readonly (string | undefined)[] = [],
): string {
  let out = input
    .replace(/(\/interactions\/[^/\s?#]+\/)[^/\s?#"']+/gi, "$1[redacted]")
    .replace(/(\/webhooks\/[^/\s?#]+\/)[^/\s?#"']+/gi, "$1[redacted]");
  const nonEmptySecrets: string[] = [];
  for (const secret of extraSecrets) {
    if (typeof secret === "string" && secret.trim().length > 0) {
      const trimmed = secret.trim();
      nonEmptySecrets.push(trimmed);
      out = out.split(secret).join("[redacted]");
      out = out.split(trimmed).join("[redacted]");
      const encoded = encodeURIComponent(trimmed);
      if (encoded !== trimmed) {
        out = out.split(encoded).join("[redacted]");
      }
    }
  }
  return scrubNativeCommandSecrets(out, nonEmptySecrets, 4096);
}

export function formatDiscordAutocompleteChoices(
  choices: readonly DiscordAutocompleteChoice[],
): DiscordAutocompleteChoice[] {
  const result: DiscordAutocompleteChoice[] = [];
  const seenValues = new Set<string>();
  for (const item of choices) {
    if (result.length >= DISCORD_AUTOCOMPLETE_MAX_CHOICES) break;
    const rawName = typeof item.name === "string" ? item.name.trim() : "";
    const rawValue = typeof item.value === "string" ? item.value.trim() : "";
    if (!rawName || !rawValue || seenValues.has(rawValue)) continue;
    const nameChars = Array.from(rawName);
    const truncatedName =
      nameChars.length > DISCORD_AUTOCOMPLETE_MAX_NAME_LENGTH
        ? nameChars.slice(0, DISCORD_AUTOCOMPLETE_MAX_NAME_LENGTH).join("")
        : rawName;
    const valueChars = Array.from(rawValue);
    const truncatedValue =
      valueChars.length > DISCORD_AUTOCOMPLETE_MAX_VALUE_LENGTH
        ? valueChars.slice(0, DISCORD_AUTOCOMPLETE_MAX_VALUE_LENGTH).join("")
        : rawValue;
    seenValues.add(truncatedValue);
    result.push({ name: truncatedName, value: truncatedValue });
  }
  return result;
}

function truncateInteractionReplyContent(text: string): string {
  const rendered = renderDiscordMarkdown(text ?? "");
  const chars = Array.from(rendered);
  if (chars.length <= DISCORD_INTERACTION_MAX_CONTENT_LENGTH) {
    return rendered;
  }
  return `${chars.slice(0, DISCORD_INTERACTION_MAX_CONTENT_LENGTH - 1).join("")}…`;
}

function parseRetryAfterMs(headers: Headers, bodyJson: unknown): number | undefined {
  if (typeof bodyJson === "object" && bodyJson !== null && "retry_after" in bodyJson) {
    const raw = (bodyJson as { retry_after?: unknown }).retry_after;
    if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) {
      return Math.ceil(raw * 1000);
    }
  }
  const headerVal = headers.get("retry-after") ?? headers.get("x-ratelimit-reset-after");
  if (headerVal) {
    const parsed = Number(headerVal);
    if (Number.isFinite(parsed) && parsed >= 0) {
      return Math.ceil(parsed * 1000);
    }
  }
  return undefined;
}

function extractDiscordCode(bodyJson: unknown): number | undefined {
  if (typeof bodyJson === "object" && bodyJson !== null && "code" in bodyJson) {
    const code = (bodyJson as { code?: unknown }).code;
    if (typeof code === "number" && Number.isFinite(code)) {
      return code;
    }
  }
  return undefined;
}

export class DiscordInteractionHttpClient {
  private readonly apiBaseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly logger: SubsystemLogger;
  private readonly now: () => number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly initialCallbackTimeoutMs: number;
  private readonly webhookEditTimeoutMs: number;
  private readonly tokenTtlMs: number;
  private readonly maxResponseBodyBytes: number;

  constructor(options: DiscordInteractionHttpOptions = {}) {
    this.apiBaseUrl = (options.apiBaseUrl ?? DISCORD_API_V10_BASE).replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.logger = options.logger ?? createSubsystemLogger("discord:interactions");
    this.now = options.now ?? (() => Date.now());
    this.sleep =
      options.sleep ??
      ((ms: number) =>
        new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, Math.max(0, ms));
          (timer as { unref?: () => void }).unref?.();
        }));
    this.initialCallbackTimeoutMs = Math.min(
      DISCORD_INTERACTION_MAX_CALLBACK_DEADLINE_MS,
      Math.max(
        50,
        options.initialCallbackTimeoutMs ?? DISCORD_INTERACTION_INITIAL_CALLBACK_TIMEOUT_MS,
      ),
    );
    this.webhookEditTimeoutMs = Math.max(
      50,
      options.webhookEditTimeoutMs ?? DISCORD_INTERACTION_WEBHOOK_EDIT_TIMEOUT_MS,
    );
    this.tokenTtlMs = Math.max(1_000, options.tokenTtlMs ?? DISCORD_INTERACTION_TOKEN_TTL_MS);
    this.maxResponseBodyBytes = Math.max(
      256,
      options.maxResponseBodyBytes ?? DISCORD_INTERACTION_MAX_RESPONSE_BODY_BYTES,
    );
  }

  getTokenTtlMs(): number {
    return this.tokenTtlMs;
  }

  getInitialCallbackTimeoutMs(): number {
    return this.initialCallbackTimeoutMs;
  }

  getNow(): number {
    return this.now();
  }

  /**
   * Sends the initial ephemeral deferred callback (`type=5, flags=64`) within the 3-second budget.
   * Never attaches a Bot Authorization header and never uses the normal outbound message queue.
   */
  async deferEphemeralCommand(params: {
    interactionId: string;
    interactionToken: string;
    receivedAtMs?: number;
  }): Promise<DiscordInteractionAckResult> {
    const startMs = params.receivedAtMs ?? this.now();
    const deadlineMs = startMs + this.initialCallbackTimeoutMs;
    const url = `${this.apiBaseUrl}/interactions/${encodeURIComponent(params.interactionId)}/${encodeURIComponent(params.interactionToken)}/callback`;
    const routeSummary = `POST /interactions/${params.interactionId}/[redacted]/callback`;
    const body = JSON.stringify({
      type: DISCORD_INTERACTION_CALLBACK_TYPE_DEFERRED_CHANNEL_MESSAGE,
      data: {
        flags: DISCORD_EPHEMERAL_MESSAGE_FLAG,
      },
    });

    return this.sendCallbackWithBudget({
      url,
      routeSummary,
      body,
      deadlineMs,
      interactionId: params.interactionId,
      interactionToken: params.interactionToken,
    });
  }

  /**
   * Sends autocomplete choices (`type=8`, max 25 items) directly within the 3-second budget.
   * Autocomplete cannot be deferred.
   */
  async respondAutocompleteChoices(params: {
    interactionId: string;
    interactionToken: string;
    choices: readonly DiscordAutocompleteChoice[];
    receivedAtMs?: number;
  }): Promise<DiscordInteractionAckResult> {
    const startMs = params.receivedAtMs ?? this.now();
    const deadlineMs = startMs + this.initialCallbackTimeoutMs;
    const formattedChoices = formatDiscordAutocompleteChoices(params.choices);
    const url = `${this.apiBaseUrl}/interactions/${encodeURIComponent(params.interactionId)}/${encodeURIComponent(params.interactionToken)}/callback`;
    const routeSummary = `POST /interactions/${params.interactionId}/[redacted]/callback (autocomplete)`;
    const body = JSON.stringify({
      type: DISCORD_INTERACTION_CALLBACK_TYPE_AUTOCOMPLETE_RESULT,
      data: {
        choices: formattedChoices,
      },
    });

    return this.sendCallbackWithBudget({
      url,
      routeSummary,
      body,
      deadlineMs,
      interactionId: params.interactionId,
      interactionToken: params.interactionToken,
    });
  }

  /**
   * Updates the original ephemeral interaction response (`PATCH /webhooks/<application_id>/<token>/messages/@original`)
   * with `allowed_mentions: { parse: [] }` while the 15-minute interaction token remains valid.
   */
  async editOriginalEphemeralResponse(params: {
    applicationId: string;
    interactionId: string;
    interactionToken: string;
    content: string;
    expiresAtMs: number;
  }): Promise<DiscordInteractionReplyResult> {
    const currentNow = this.now();
    if (currentNow >= params.expiresAtMs) {
      this.logger.warn("Discord interaction token expired before @original edit", {
        interactionId: params.interactionId,
        route: `PATCH /webhooks/${params.applicationId}/[redacted]/messages/@original`,
      });
      return {
        outcome: "expired",
        reason: "interaction_token_expired",
      };
    }

    const url = `${this.apiBaseUrl}/webhooks/${encodeURIComponent(params.applicationId)}/${encodeURIComponent(params.interactionToken)}/messages/@original`;
    const routeSummary = `PATCH /webhooks/${params.applicationId}/[redacted]/messages/@original`;
    const body = JSON.stringify({
      content: truncateInteractionReplyContent(params.content),
      allowed_mentions: { parse: [] },
      flags: DISCORD_EPHEMERAL_MESSAGE_FLAG,
    });

    const maxEditDeadlineMs = Math.min(
      params.expiresAtMs,
      currentNow + this.webhookEditTimeoutMs,
    );

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const nowBeforeAttempt = this.now();
      if (nowBeforeAttempt >= params.expiresAtMs) {
        return {
          outcome: "expired",
          reason: "interaction_token_expired",
        };
      }
      const remainingMs = maxEditDeadlineMs - nowBeforeAttempt;
      if (remainingMs <= 0) {
        return {
          outcome: "unknown",
          reason: "webhook_edit_timeout",
        };
      }

      try {
        const res = await this.performFetchAndRead({
          url,
          method: "PATCH",
          body,
          deadlineMs: maxEditDeadlineMs,
          interactionToken: params.interactionToken,
        });

        if (res.status >= 200 && res.status < 300) {
          return { outcome: "sent", status: res.status };
        }

        const discordCode = extractDiscordCode(res.bodyJson);

        if (res.status === 429) {
          const retryAfterMs = parseRetryAfterMs(res.headers, res.bodyJson) ?? 1_000;
          const nowAfter429 = this.now();
          if (
            attempt === 0 &&
            nowAfter429 + retryAfterMs + 50 < maxEditDeadlineMs &&
            nowAfter429 + retryAfterMs < params.expiresAtMs
          ) {
            this.logger.warn("Discord interaction @original edit rate-limited (429); retrying within budget", {
              interactionId: params.interactionId,
              route: routeSummary,
              retryAfterMs,
            });
            await this.sleep(retryAfterMs);
            continue;
          }
          this.logger.warn("Discord interaction @original edit rate-limited (429) beyond budget", {
            interactionId: params.interactionId,
            route: routeSummary,
            retryAfterMs,
          });
          return {
            outcome: "unknown",
            status: 429,
            retryAfterMs,
            reason: "rate_limited",
          };
        }

        if (
          res.status === 401 ||
          discordCode === 50027 ||
          discordCode === 10062 ||
          discordCode === 10015
        ) {
          this.logger.warn("Discord interaction token rejected or expired during @original edit", {
            interactionId: params.interactionId,
            route: routeSummary,
            status: res.status,
            discordCode,
          });
          return {
            outcome: "expired",
            status: res.status,
            discordCode,
            reason: "invalid_webhook_token",
          };
        }

        if (res.status === 400 || res.status === 403 || res.status === 404) {
          this.logger.warn("Discord interaction @original edit rejected with client status", {
            interactionId: params.interactionId,
            route: routeSummary,
            status: res.status,
            discordCode,
          });
          return {
            outcome: "failed",
            status: res.status,
            discordCode,
            reason: `http_${res.status}`,
          };
        }

        this.logger.warn("Discord interaction @original edit returned unexpected status", {
          interactionId: params.interactionId,
          route: routeSummary,
          status: res.status,
          discordCode,
        });
        return {
          outcome: "unknown",
          status: res.status,
          discordCode,
          reason: `http_${res.status}`,
        };
      } catch (err) {
        const sanitizedMsg = redactDiscordInteractionText(
          err instanceof Error ? err.message : String(err),
          [params.interactionToken],
        );
        this.logger.warn("Discord interaction @original edit request failed", {
          interactionId: params.interactionId,
          route: routeSummary,
          error: sanitizedMsg,
        });
        return {
          outcome: "unknown",
          reason: sanitizedMsg,
        };
      }
    }

    return {
      outcome: "unknown",
      reason: "webhook_edit_exhausted",
    };
  }

  private async sendCallbackWithBudget(params: {
    url: string;
    routeSummary: string;
    body: string;
    deadlineMs: number;
    interactionId: string;
    interactionToken: string;
  }): Promise<DiscordInteractionAckResult> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const remainingMs = params.deadlineMs - this.now();
      if (remainingMs <= 0) {
        this.logger.warn("Discord interaction callback missed 3s deadline before send", {
          interactionId: params.interactionId,
          route: params.routeSummary,
        });
        return {
          outcome: "failed",
          reason: "callback_deadline_exceeded",
        };
      }

      try {
        const res = await this.performFetchAndRead({
          url: params.url,
          method: "POST",
          body: params.body,
          deadlineMs: params.deadlineMs,
          interactionToken: params.interactionToken,
        });

        if (res.status >= 200 && res.status < 300) {
          return {
            outcome: "acknowledged",
            status: res.status,
          };
        }

        const discordCode = extractDiscordCode(res.bodyJson);

        if (res.status === 429) {
          const retryAfterMs = parseRetryAfterMs(res.headers, res.bodyJson) ?? 1_000;
          const nowAfter429 = this.now();
          if (attempt === 0 && nowAfter429 + retryAfterMs + 50 < params.deadlineMs) {
            this.logger.warn("Discord interaction callback rate-limited (429); retrying within 3s budget", {
              interactionId: params.interactionId,
              route: params.routeSummary,
              retryAfterMs,
            });
            await this.sleep(retryAfterMs);
            continue;
          }
          this.logger.warn("Discord interaction callback rate-limited (429) beyond 3s budget", {
            interactionId: params.interactionId,
            route: params.routeSummary,
            retryAfterMs,
          });
          return {
            outcome: "failed",
            status: 429,
            retryAfterMs,
            reason: "rate_limited",
          };
        }

        if (res.status === 400 && discordCode === 40060) {
          this.logger.warn("Discord interaction callback returned 40060 (already acknowledged)", {
            interactionId: params.interactionId,
            route: params.routeSummary,
            status: res.status,
            discordCode,
          });
          return {
            outcome: "unknown",
            status: res.status,
            discordCode,
            reason: "interaction_already_acknowledged",
          };
        }

        if (res.status === 400 || res.status === 401 || res.status === 403 || res.status === 404) {
          this.logger.warn("Discord interaction callback rejected by Discord", {
            interactionId: params.interactionId,
            route: params.routeSummary,
            status: res.status,
            discordCode,
          });
          return {
            outcome: "failed",
            status: res.status,
            discordCode,
            reason: `http_${res.status}`,
          };
        }

        this.logger.warn("Discord interaction callback returned ambiguous server status", {
          interactionId: params.interactionId,
          route: params.routeSummary,
          status: res.status,
          discordCode,
        });
        return {
          outcome: "unknown",
          status: res.status,
          discordCode,
          reason: `http_${res.status}`,
        };
      } catch (err) {
        const sanitizedMsg = redactDiscordInteractionText(
          err instanceof Error ? err.message : String(err),
          [params.interactionToken],
        );
        this.logger.warn("Discord interaction callback request outcome unknown", {
          interactionId: params.interactionId,
          route: params.routeSummary,
          error: sanitizedMsg,
        });
        return {
          outcome: "unknown",
          reason: sanitizedMsg,
        };
      }
    }

    return {
      outcome: "failed",
      reason: "callback_retry_exhausted",
    };
  }

  /**
   * Executes the HTTP request and consumes/cancels the response body under a single
   * shared deadline (`deadlineMs`) and a strict byte limit (`maxResponseBodyBytes`).
   */
  private async performFetchAndRead(params: {
    url: string;
    method: "POST" | "PATCH";
    body: string;
    deadlineMs: number;
    interactionToken: string;
  }): Promise<BoundedFetchResponse> {
    const remainingMs = params.deadlineMs - this.now();
    if (remainingMs <= 0) {
      throw new Error("interaction_http_timeout");
    }

    const controller = new AbortController();
    let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
    const timeoutPromise = new Promise<never>((_, reject) => {
      timeoutHandle = setTimeout(() => {
        const err = new Error("interaction_http_timeout");
        controller.abort(err);
        reject(err);
      }, Math.max(1, remainingMs));
      (timeoutHandle as { unref?: () => void }).unref?.();
    });

    const operationPromise = (async (): Promise<BoundedFetchResponse> => {
      const res = await this.fetchImpl(params.url, {
        method: params.method,
        headers: {
          "Content-Type": "application/json",
          "User-Agent": "DaltonChannelGateway (https://ginlix.ai, 1.0)",
        },
        body: params.body,
        signal: controller.signal,
      });

      if (res.status >= 200 && res.status < 300) {
        void res.body?.cancel().catch(() => undefined);
        return {
          status: res.status,
          headers: res.headers,
          bodyJson: undefined,
        };
      }

      const text = await this.readBoundedResponseText(res, controller.signal);
      let bodyJson: unknown;
      if (text) {
        try {
          bodyJson = JSON.parse(text);
        } catch {
          bodyJson = undefined;
        }
      }
      return {
        status: res.status,
        headers: res.headers,
        bodyJson,
      };
    })();

    try {
      return await Promise.race([operationPromise, timeoutPromise]);
    } catch (err) {
      const sanitizedMsg = redactDiscordInteractionText(
        err instanceof Error ? err.message : String(err),
        [params.interactionToken],
      );
      throw new Error(sanitizedMsg);
    } finally {
      if (timeoutHandle) clearTimeout(timeoutHandle);
    }
  }

  private async readBoundedResponseText(
    res: Response,
    signal: AbortSignal,
  ): Promise<string> {
    if (!res.body) {
      if (signal.aborted) {
        throw new Error("interaction_http_timeout");
      }
      const raw = await res.text();
      if (Buffer.byteLength(raw, "utf8") > this.maxResponseBodyBytes) {
        throw new Error("interaction_response_body_too_large");
      }
      return raw;
    }

    const reader = res.body.getReader();
    const onAbort = (): void => {
      void reader.cancel().catch(() => undefined);
    };
    signal.addEventListener("abort", onAbort, { once: true });

    const chunks: Uint8Array[] = [];
    let totalBytes = 0;
    try {
      while (true) {
        if (signal.aborted) {
          throw new Error("interaction_http_timeout");
        }
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.byteLength > 0) {
          totalBytes += value.byteLength;
          if (totalBytes > this.maxResponseBodyBytes) {
            void reader.cancel().catch(() => undefined);
            throw new Error("interaction_response_body_too_large");
          }
          chunks.push(value);
        }
      }
      if (chunks.length === 0) return "";
      return Buffer.concat(chunks, totalBytes).toString("utf8");
    } finally {
      signal.removeEventListener("abort", onAbort);
    }
  }
}
