import { createHash } from "node:crypto";

/**
 * Fixed RFC 4122 / RFC 9562 namespace for Dalton Channel Gateway deterministic UUIDs (§1.3.1).
 */
export const DALTON_CHANNEL_NAMESPACE = "5c8b6e20-8a1f-4e92-b52c-1d9a40c80001";

function uuidToBytes(uuid: string): Uint8Array {
  const hex = uuid.replace(/-/g, "").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(hex)) {
    throw new Error(`Invalid UUID: ${uuid}`);
  }
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 16; i++) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function bytesToUuid(bytes: Uint8Array): string {
  const hex: string[] = [];
  for (let i = 0; i < 16; i++) {
    hex.push(bytes[i]!.toString(16).padStart(2, "0"));
  }
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

/**
 * Deterministic UUIDv5 (SHA-1) per RFC 9562 §5.5.
 */
export function uuidv5(namespace: string, name: string): string {
  const nsBytes = uuidToBytes(namespace);
  const hash = createHash("sha1");
  hash.update(nsBytes);
  hash.update(Buffer.from(name, "utf8"));
  const digest = hash.digest();
  const out = new Uint8Array(digest.subarray(0, 16));
  // Set version to 5 (0101xxxx)
  out[6] = (out[6]! & 0x0f) | 0x50;
  // Set RFC 4122 variant (10xxxxxx)
  out[8] = (out[8]! & 0x3f) | 0x80;
  return bytesToUuid(out);
}

/**
 * Compute initial inbound `requestKey` (§1.3.1):
 * `uuidv5(DALTON_CHANNEL_NAMESPACE, "${platform}:${message_id}")`
 */
export function makeInboundRequestKey(platform: string, messageId: string): string {
  return uuidv5(DALTON_CHANNEL_NAMESPACE, `${platform}:${messageId}`);
}

/**
 * Compute retry `retryRequestKey` (§1.3.1 Type A3 / Type B):
 * `uuidv5(DALTON_CHANNEL_NAMESPACE, "${platform}:${message_id}:retry:${failedRunIdOrAttempt}")`
 */
export function makeRetryRequestKey(
  platform: string,
  messageId: string,
  failedRunIdOrAttempt: string | number,
): string {
  return uuidv5(
    DALTON_CHANNEL_NAMESPACE,
    `${platform}:${messageId}:retry:${failedRunIdOrAttempt}`,
  );
}

/**
 * Compute normal turn request key after `steer_only` receives `not_running`:
 * `uuidv5(DALTON_CHANNEL_NAMESPACE, "${platform}:${message_id}:turn:${attempt}")`
 */
export function makeTurnAfterSteerRequestKey(
  platform: string,
  messageId: string,
  attempt: number | string = 1,
): string {
  return uuidv5(
    DALTON_CHANNEL_NAMESPACE,
    `${platform}:${messageId}:turn:${attempt}`,
  );
}

/**
 * Compute returned-steer resubmit `resubmittedRequestKey` (§1.3.2):
 * `uuidv5(DALTON_CHANNEL_NAMESPACE, "${platform}:${message_id}:returned:${steered_into_run_id}")`
 */
export function makeReturnedSteerRequestKey(
  platform: string,
  messageId: string,
  steeredIntoRunId: string,
): string {
  return uuidv5(
    DALTON_CHANNEL_NAMESPACE,
    `${platform}:${messageId}:returned:${steeredIntoRunId}`,
  );
}

/**
 * Compute `credit_pause` supplement steer key (§1.3.1 HITL #3):
 * `uuidv5(DALTON_CHANNEL_NAMESPACE, "${platform}:${message_id}:steer:${run_id}")`
 */
export function makeCreditSupplementSteerKey(
  platform: string,
  messageId: string,
  runId: string,
): string {
  return uuidv5(DALTON_CHANNEL_NAMESPACE, `${platform}:${messageId}:steer:${runId}`);
}

/**
 * Compute deterministic `external_thread_id` (§3.1 / §3.3):
 * `uuidv5(DALTON_CHANNEL_NAMESPACE, "${platform}:${scope_key}:${dalton_user_id}:gen:${generation}")`
 */
export function makeExternalThreadId(
  platform: string,
  scopeKey: string,
  daltonUserId: string,
  generation: number,
): string {
  return uuidv5(
    DALTON_CHANNEL_NAMESPACE,
    `${platform}:${scopeKey}:${daltonUserId}:gen:${generation}`,
  );
}
