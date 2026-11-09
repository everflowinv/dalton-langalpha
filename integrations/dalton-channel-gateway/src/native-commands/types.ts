import type { SubsystemLogger } from "../shared/logger.js";

export type NativeCommandPlatform = "feishu" | "discord";

export type NativeCommandName = "main" | "workspace" | "new" | "stop";

export interface NativeCommandOptionDefinition {
  readonly name: string;
  readonly description: string;
  readonly type: "string";
  readonly required: boolean;
  readonly autocomplete: boolean;
}

export interface NativeCommandDefinition {
  readonly name: NativeCommandName;
  readonly description: string;
  readonly descriptionI18n?: Readonly<Record<string, string>>;
  readonly iconKey?: string;
  readonly options: readonly NativeCommandOptionDefinition[];
}

export type DiscordRegistrationScope =
  | { readonly kind: "global" }
  | { readonly kind: "guild"; readonly guildId: string };

export type NativeCommandActionKind = "create" | "update" | "unchanged";

export interface PlannedCommandChange {
  readonly name: NativeCommandName;
  readonly action: NativeCommandActionKind;
  readonly remoteId?: string;
  readonly changedFields: readonly string[];
  readonly requestMethod?: "POST" | "PATCH";
  readonly requestPath?: string;
  readonly requestBody?: Record<string, unknown>;
}

export interface PreservedRemoteCommand {
  readonly name: string;
  readonly remoteId?: string;
  readonly commandType?: number;
}

export type AppliedCommandStatus =
  | "created"
  | "updated"
  | "unchanged"
  | "unconfirmed"
  | "skipped";

export interface AppliedCommandOutcome {
  readonly name: NativeCommandName;
  readonly status: AppliedCommandStatus;
  readonly remoteId?: string;
  readonly changedFields: readonly string[];
  readonly verifiedAfterDisconnect?: boolean;
  readonly unconfirmedReason?: string;
}

export interface NativeCommandSyncResult {
  readonly ok: boolean;
  readonly status: "preview" | "applied" | "unconfirmed";
  readonly platform: NativeCommandPlatform;
  readonly mode: "preview" | "apply";
  readonly scopeLabel: string;
  readonly applicationId?: string;
  readonly applicationName?: string;
  readonly plan: readonly PlannedCommandChange[];
  readonly outcomes?: readonly AppliedCommandOutcome[];
  readonly preservedCommands: readonly PreservedRemoteCommand[];
  readonly notes: readonly string[];
  readonly errorMessage?: string;
}

export interface CommonRegistrationRuntimeOptions {
  readonly mode?: "preview" | "apply";
  readonly logger?: SubsystemLogger;
  /**
   * Loopback-only (`http://127.0.0.1:<port>` / `http://[::1]:<port>` / `http://localhost:<port>`)
   * transport override for local HTTP server tests. Production callers must omit this so requests
   * strictly target the fixed trusted platform origin.
   */
  readonly testApiOrigin?: string;
  readonly requestTimeoutMs?: number;
  readonly totalTimeoutMs?: number;
  readonly maxRequests?: number;
  readonly maxPages?: number;
  readonly maxRateLimitRetries?: number;
  readonly fetchImpl?: typeof fetch;
  readonly sleepFn?: (ms: number, signal?: AbortSignal) => Promise<void>;
  readonly nowFn?: () => number;
}

export type NativeCommandErrorCode =
  | "usage_error"
  | "missing_config"
  | "invalid_config"
  | "disallowed_origin"
  | "auth_error"
  | "permission_denied"
  | "identity_mismatch"
  | "duplicate_remote_command"
  | "invalid_remote_command"
  | "pagination_error"
  | "rate_limited"
  | "daily_create_limit"
  | "timeout"
  | "request_limit_exceeded"
  | "http_error"
  | "network_error"
  | "malformed_response"
  | "unconfirmed_write";

export class NativeCommandRegistrationError extends Error {
  readonly code: NativeCommandErrorCode;
  readonly httpStatus?: number;
  readonly platform?: NativeCommandPlatform;

  constructor(
    code: NativeCommandErrorCode,
    message: string,
    options?: {
      httpStatus?: number;
      platform?: NativeCommandPlatform;
      cause?: unknown;
    },
  ) {
    super(
      message,
      options?.cause !== undefined ? { cause: options.cause } : undefined,
    );
    this.name = "NativeCommandRegistrationError";
    this.code = code;
    this.httpStatus = options?.httpStatus;
    this.platform = options?.platform;
  }
}

export function exitCodeForNativeCommandError(
  error: unknown,
): 1 | 2 | 3 | 4 {
  if (error instanceof NativeCommandRegistrationError) {
    switch (error.code) {
      case "usage_error":
        return 2;
      case "missing_config":
      case "invalid_config":
      case "auth_error":
      case "permission_denied":
      case "identity_mismatch":
        return 3;
      case "unconfirmed_write":
        return 4;
      default:
        return 1;
    }
  }
  return 1;
}
