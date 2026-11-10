import { lstatSync, readFileSync } from "node:fs";
import type { SubsystemLogger } from "./shared/logger.js";

export const DEDICATED_FEISHU_VAULT_KEYS = [
  "DALTON_FEISHU_APP_ID",
  "DALTON_FEISHU_APP_SECRET",
] as const;

export type DedicatedFeishuVaultKey =
  (typeof DEDICATED_FEISHU_VAULT_KEYS)[number];

const ALLOWED_VAULT_KEY_SET: ReadonlySet<string> = new Set<string>(
  DEDICATED_FEISHU_VAULT_KEYS,
);

const MAX_VAULT_RESPONSE_BYTES = 16 * 1024;
const MAX_ENV_FILE_BYTES = 64 * 1024;

export type VaultBootstrapErrorCode =
  | "vault_auth_failed"
  | "vault_credentials_missing"
  | "vault_credentials_invalid"
  | "vault_redirect_rejected"
  | "vault_timeout"
  | "vault_malformed_response"
  | "vault_unavailable"
  | "env_file_insecure"
  | "plaintext_feishu_credentials_forbidden";

/**
 * Redacted startup/credential error containing at most classification codes
 * and whitelisted credential key names (`DALTON_FEISHU_APP_ID`, `DALTON_FEISHU_APP_SECRET`).
 * Never includes secret values, response bodies, or raw error causes.
 */
export class VaultBootstrapError extends Error {
  readonly code: VaultBootstrapErrorCode;
  readonly missing: readonly DedicatedFeishuVaultKey[];

  constructor(
    code: VaultBootstrapErrorCode,
    message: string,
    missing: readonly DedicatedFeishuVaultKey[] = [],
  ) {
    super(message);
    this.name = "VaultBootstrapError";
    this.code = code;
    this.missing = missing;
  }
}

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null && !Array.isArray(val);
}

function filterAllowedKeyNames(rawList: unknown): DedicatedFeishuVaultKey[] {
  if (!Array.isArray(rawList)) {
    return [];
  }
  const result: DedicatedFeishuVaultKey[] = [];
  for (const item of rawList) {
    if (typeof item === "string" && ALLOWED_VAULT_KEY_SET.has(item)) {
      const typed = item as DedicatedFeishuVaultKey;
      if (!result.includes(typed)) {
        result.push(typed);
      }
    }
  }
  return result;
}

function isAbortOrTimeoutError(
  err: unknown,
  timedOut: boolean,
  signal: AbortSignal,
): boolean {
  if (timedOut || signal.aborted) {
    return true;
  }
  if (err instanceof Error) {
    if (err.name === "AbortError" || err.name === "TimeoutError") {
      return true;
    }
  }
  if (typeof err === "object" && err !== null && "name" in err) {
    const errName = (err as { name?: unknown }).name;
    if (errName === "AbortError" || errName === "TimeoutError") {
      return true;
    }
  }
  return false;
}

async function cancelResponseBodySafe(
  resp: Response | undefined,
): Promise<void> {
  if (!resp?.body) {
    return;
  }
  try {
    if (!resp.bodyUsed) {
      await resp.body.cancel();
    }
  } catch {
    // Ignore stream cancellation errors on already-closed or aborted sockets.
  }
}

async function readBoundedUtf8Response(
  resp: Response,
  maxBytes: number,
  deadlineCtx: {
    timeoutMs: number;
    isTimedOut: () => boolean;
    signal: AbortSignal;
  },
): Promise<string> {
  const declaredLenRaw = resp.headers.get("content-length");
  if (declaredLenRaw) {
    const declaredLen = Number.parseInt(declaredLenRaw, 10);
    if (Number.isFinite(declaredLen) && declaredLen > maxBytes) {
      await cancelResponseBodySafe(resp);
      throw new VaultBootstrapError(
        "vault_malformed_response",
        "Dalton Vault credential response exceeded maximum permitted byte size.",
      );
    }
  }

  const chunks: Buffer[] = [];
  let total = 0;

  if (resp.body) {
    const reader = resp.body.getReader();
    const onAbort = () => {
      void reader.cancel().catch(() => undefined);
    };
    if (deadlineCtx.signal.aborted) {
      onAbort();
    } else {
      deadlineCtx.signal.addEventListener("abort", onAbort, { once: true });
    }
    try {
      while (true) {
        if (deadlineCtx.isTimedOut() || deadlineCtx.signal.aborted) {
          throw new VaultBootstrapError(
            "vault_timeout",
            `Timed out after ${deadlineCtx.timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
          );
        }
        const { done, value } = await reader.read();
        if (deadlineCtx.isTimedOut() || deadlineCtx.signal.aborted) {
          throw new VaultBootstrapError(
            "vault_timeout",
            `Timed out after ${deadlineCtx.timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
          );
        }
        if (done) {
          break;
        }
        if (!value) {
          continue;
        }
        const buf = Buffer.from(value);
        total += buf.byteLength;
        if (total > maxBytes) {
          await reader.cancel().catch(() => undefined);
          throw new VaultBootstrapError(
            "vault_malformed_response",
            "Dalton Vault credential response exceeded maximum permitted byte size.",
          );
        }
        chunks.push(buf);
      }
    } catch (err) {
      await reader.cancel().catch(() => undefined);
      if (err instanceof VaultBootstrapError) {
        throw err;
      }
      if (
        isAbortOrTimeoutError(
          err,
          deadlineCtx.isTimedOut(),
          deadlineCtx.signal,
        )
      ) {
        throw new VaultBootstrapError(
          "vault_timeout",
          `Timed out after ${deadlineCtx.timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
        );
      }
      throw new VaultBootstrapError(
        "vault_unavailable",
        "Failed to read response body from Dalton Vault credential endpoint.",
      );
    } finally {
      deadlineCtx.signal.removeEventListener("abort", onAbort);
      try {
        reader.releaseLock();
      } catch {
        // Ignore if lock was already released.
      }
    }
  } else {
    try {
      const ab = await resp.arrayBuffer();
      if (ab.byteLength > maxBytes) {
        throw new VaultBootstrapError(
          "vault_malformed_response",
          "Dalton Vault credential response exceeded maximum permitted byte size.",
        );
      }
      chunks.push(Buffer.from(ab));
    } catch (err) {
      if (err instanceof VaultBootstrapError) {
        throw err;
      }
      if (
        isAbortOrTimeoutError(
          err,
          deadlineCtx.isTimedOut(),
          deadlineCtx.signal,
        )
      ) {
        throw new VaultBootstrapError(
          "vault_timeout",
          `Timed out after ${deadlineCtx.timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
        );
      }
      throw new VaultBootstrapError(
        "vault_unavailable",
        "Failed to read response body from Dalton Vault credential endpoint.",
      );
    }
  }

  return Buffer.concat(chunks).toString("utf8");
}

/**
 * Load non-Feishu service environment settings (such as `INTERNAL_SERVICE_TOKEN`)
 * from an owner-only (`0600`) regular file when `DALTON_CHANNEL_GATEWAY_ENV_FILE` is configured.
 * Refuses symlinks, group/world-accessible files, and plaintext Feishu credentials when Vault mode is active.
 */
export function loadSecureGatewayEnvFile(
  filePath: string,
  options?: {
    forbidPlaintextFeishuCredentials?: boolean;
  },
): Record<string, string> {
  const cleanedPath = filePath.trim();
  if (!cleanedPath) {
    return {};
  }

  let stat;
  try {
    stat = lstatSync(cleanedPath);
  } catch {
    throw new VaultBootstrapError(
      "env_file_insecure",
      `Gateway env file is missing or unreadable: ${cleanedPath}`,
    );
  }

  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new VaultBootstrapError(
      "env_file_insecure",
      `Gateway env file must be a regular file (symlinks forbidden): ${cleanedPath}`,
    );
  }

  if (process.platform !== "win32" && (stat.mode & 0o077) !== 0) {
    throw new VaultBootstrapError(
      "env_file_insecure",
      `Gateway env file must have owner-only permissions (0600): ${cleanedPath}`,
    );
  }

  if (stat.size > MAX_ENV_FILE_BYTES) {
    throw new VaultBootstrapError(
      "env_file_insecure",
      `Gateway env file exceeds maximum permitted byte size: ${cleanedPath}`,
    );
  }

  let content: string;
  try {
    content = readFileSync(cleanedPath, "utf8");
  } catch {
    throw new VaultBootstrapError(
      "env_file_insecure",
      `Failed to read gateway env file: ${cleanedPath}`,
    );
  }

  const parsed: Record<string, string> = {};
  for (const rawLine of content.split(/\r?\n/)) {
    const trimmed = rawLine.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const normalizedLine = trimmed.startsWith("export ")
      ? trimmed.slice("export ".length).trim()
      : trimmed;
    const eqIdx = normalizedLine.indexOf("=");
    if (eqIdx <= 0) {
      continue;
    }
    const key = normalizedLine.slice(0, eqIdx).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      continue;
    }
    let val = normalizedLine.slice(eqIdx + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"') && val.length >= 2) ||
      (val.startsWith("'") && val.endsWith("'") && val.length >= 2)
    ) {
      val = val.slice(1, -1);
    }
    parsed[key] = val;
  }

  if (options?.forbidPlaintextFeishuCredentials) {
    const forbiddenPresent = DEDICATED_FEISHU_VAULT_KEYS.filter(
      (k) => Object.prototype.hasOwnProperty.call(parsed, k) && parsed[k]?.trim(),
    );
    if (forbiddenPresent.length > 0) {
      throw new VaultBootstrapError(
        "plaintext_feishu_credentials_forbidden",
        `Plaintext Feishu credentials are forbidden in gateway env file when Vault mode is active: ${forbiddenPresent.join(", ")}`,
        forbiddenPresent,
      );
    }
  }

  return parsed;
}

export interface LoadFeishuCredentialsFromVaultOptions {
  backendUrl: string;
  serviceToken: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  logger?: SubsystemLogger;
}

export interface LoadedFeishuVaultCredentials {
  appId: string;
  appSecret: string;
}

/**
 * Fetch `DALTON_FEISHU_APP_ID` and `DALTON_FEISHU_APP_SECRET` from the Dalton backend's
 * service-authenticated Vault endpoint into process memory before reporting service readiness.
 * Enforces a single end-to-end deadline across connection, headers, response-body consumption,
 * and validation without resetting the timeout after headers.
 */
export async function loadFeishuCredentialsFromVault(
  options: LoadFeishuCredentialsFromVaultOptions,
): Promise<LoadedFeishuVaultCredentials> {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const timeoutMs =
    typeof options.timeoutMs === "number" && options.timeoutMs > 0
      ? options.timeoutMs
      : 10_000;

  let endpointUrl: URL;
  try {
    endpointUrl = new URL(
      `${options.backendUrl.replace(/\/+$/, "")}/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials`,
    );
  } catch {
    throw new VaultBootstrapError(
      "vault_unavailable",
      "Invalid DALTON_BACKEND_URL configured for Vault credential bootstrap.",
    );
  }

  if (endpointUrl.protocol !== "http:" && endpointUrl.protocol !== "https:") {
    throw new VaultBootstrapError(
      "vault_unavailable",
      "Unsupported protocol in DALTON_BACKEND_URL for Vault credential bootstrap.",
    );
  }

  if (!options.serviceToken || options.serviceToken.trim().length < 32) {
    throw new VaultBootstrapError(
      "vault_auth_failed",
      "INTERNAL_SERVICE_TOKEN must be at least 32 characters long for Vault bootstrap.",
    );
  }

  const controller = new AbortController();
  let timedOut = false;
  let activeResponse: Response | undefined;
  let rejectOnDeadline!: (err: VaultBootstrapError) => void;

  const deadlinePromise = new Promise<never>((_resolve, reject) => {
    rejectOnDeadline = reject;
  });
  // Guard against unhandled rejection if request finishes synchronously alongside timer fire.
  deadlinePromise.catch(() => undefined);

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
    void cancelResponseBodySafe(activeResponse);
    rejectOnDeadline(
      new VaultBootstrapError(
        "vault_timeout",
        `Timed out after ${timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
      ),
    );
  }, timeoutMs);

  const executeWithinSingleDeadline =
    async (): Promise<LoadedFeishuVaultCredentials> => {
      let resp: Response;
      try {
        resp = await fetchImpl(endpointUrl, {
          method: "POST",
          headers: {
            "X-Service-Token": options.serviceToken.trim(),
            "Content-Type": "application/json",
            Accept: "application/json",
            "Cache-Control": "no-store",
          },
          body: "{}",
          redirect: "error",
          cache: "no-store",
          signal: controller.signal,
        });
        activeResponse = resp;
      } catch (err) {
        if (isAbortOrTimeoutError(err, timedOut, controller.signal)) {
          throw new VaultBootstrapError(
            "vault_timeout",
            `Timed out after ${timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
          );
        }
        const errMsgLower = (
          err instanceof Error
            ? `${err.message} ${err.cause instanceof Error ? err.cause.message : ""}`
            : String(err)
        ).toLowerCase();
        if (errMsgLower.includes("redirect")) {
          throw new VaultBootstrapError(
            "vault_redirect_rejected",
            "Refused HTTP redirect while loading dedicated Feishu credentials from Vault.",
          );
        }
        throw new VaultBootstrapError(
          "vault_unavailable",
          "Failed to connect to Dalton backend Vault credential endpoint.",
        );
      }

      if (resp.redirected || (resp.status >= 300 && resp.status < 400)) {
        await cancelResponseBodySafe(resp);
        throw new VaultBootstrapError(
          "vault_redirect_rejected",
          `Refused HTTP redirect (${resp.status}) while loading dedicated Feishu credentials from Vault.`,
        );
      }

      if (resp.status === 401 || resp.status === 403) {
        await cancelResponseBodySafe(resp);
        throw new VaultBootstrapError(
          "vault_auth_failed",
          `Service authentication failed (${resp.status}) while loading dedicated Feishu credentials from Vault.`,
        );
      }

      const rawText = await readBoundedUtf8Response(
        resp,
        MAX_VAULT_RESPONSE_BYTES,
        {
          timeoutMs,
          isTimedOut: () => timedOut,
          signal: controller.signal,
        },
      );

      if (!resp.ok) {
        let remoteCode = "";
        let missingKeys: DedicatedFeishuVaultKey[] = [];
        if (rawText) {
          try {
            const errParsed = JSON.parse(rawText) as unknown;
            if (isPlainRecord(errParsed)) {
              remoteCode =
                typeof errParsed.code === "string" ? errParsed.code.trim() : "";
              missingKeys = filterAllowedKeyNames(errParsed.missing);
            }
          } catch {
            // Ignore unparseable error body; never echo raw response text.
          }
        }
        if (remoteCode === "vault_credentials_missing") {
          const suffix =
            missingKeys.length > 0 ? `: ${missingKeys.join(", ")}` : "";
          throw new VaultBootstrapError(
            "vault_credentials_missing",
            `Dedicated Feishu credentials missing in primary administrator Vault${suffix}`,
            missingKeys,
          );
        }
        if (remoteCode === "vault_credentials_invalid") {
          const suffix =
            missingKeys.length > 0 ? `: ${missingKeys.join(", ")}` : "";
          throw new VaultBootstrapError(
            "vault_credentials_invalid",
            `Dedicated Feishu credentials in primary administrator Vault have invalid format${suffix}`,
            missingKeys,
          );
        }
        throw new VaultBootstrapError(
          "vault_unavailable",
          `Dalton Vault credential endpoint returned HTTP ${resp.status}.`,
        );
      }

      let parsedBody: unknown;
      try {
        parsedBody = JSON.parse(rawText);
      } catch {
        throw new VaultBootstrapError(
          "vault_malformed_response",
          "Received malformed JSON from Dalton Vault credential endpoint.",
        );
      }

      if (!isPlainRecord(parsedBody) || !isPlainRecord(parsedBody.credentials)) {
        throw new VaultBootstrapError(
          "vault_malformed_response",
          "Dalton Vault credential response is missing the expected credentials object.",
        );
      }

      const credsRecord = parsedBody.credentials;
      const rawAppId = credsRecord.DALTON_FEISHU_APP_ID;
      const rawAppSecret = credsRecord.DALTON_FEISHU_APP_SECRET;

      const appId = typeof rawAppId === "string" ? rawAppId.trim() : "";
      const appSecret =
        typeof rawAppSecret === "string" ? rawAppSecret.trim() : "";

      const missing: DedicatedFeishuVaultKey[] = [];
      if (!appId) {
        missing.push("DALTON_FEISHU_APP_ID");
      }
      if (!appSecret) {
        missing.push("DALTON_FEISHU_APP_SECRET");
      }
      if (missing.length > 0) {
        throw new VaultBootstrapError(
          "vault_credentials_missing",
          `Dedicated Feishu credentials missing or empty in Vault response: ${missing.join(", ")}`,
          missing,
        );
      }

      const invalid: DedicatedFeishuVaultKey[] = [];
      if (/\s|\0/.test(appId)) {
        invalid.push("DALTON_FEISHU_APP_ID");
      }
      if (/\s|\0/.test(appSecret)) {
        invalid.push("DALTON_FEISHU_APP_SECRET");
      }
      if (invalid.length > 0) {
        throw new VaultBootstrapError(
          "vault_credentials_invalid",
          `Dedicated Feishu credentials contain invalid whitespace or control characters: ${invalid.join(", ")}`,
          invalid,
        );
      }

      options.logger?.info(
        "Loaded dedicated Feishu credentials from Dalton Vault",
        {
          source: "vault",
          keys: [...DEDICATED_FEISHU_VAULT_KEYS],
        },
      );

      return { appId, appSecret };
    };

  try {
    return await Promise.race([executeWithinSingleDeadline(), deadlinePromise]);
  } catch (err) {
    await cancelResponseBodySafe(activeResponse);
    if (err instanceof VaultBootstrapError) {
      throw err;
    }
    if (isAbortOrTimeoutError(err, timedOut, controller.signal)) {
      throw new VaultBootstrapError(
        "vault_timeout",
        `Timed out after ${timeoutMs}ms while loading dedicated Feishu credentials from Vault.`,
      );
    }
    throw new VaultBootstrapError(
      "vault_unavailable",
      "Failed to load dedicated Feishu credentials from Dalton Vault.",
    );
  } finally {
    clearTimeout(timer);
  }
}
