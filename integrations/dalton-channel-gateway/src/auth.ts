import { timingSafeEqual } from "node:crypto";
import type { IncomingHttpHeaders } from "node:http";

export const MIN_SERVICE_TOKEN_LENGTH = 32;

/**
 * Constant-time `X-Service-Token` verification (§1.1).
 * Requires configured token >= 32 chars and exact match via `crypto.timingSafeEqual`.
 */
export function verifyServiceToken(
  headers: IncomingHttpHeaders | Record<string, string | string[] | undefined>,
  expectedToken: string,
): boolean {
  if (!expectedToken || expectedToken.length < MIN_SERVICE_TOKEN_LENGTH) {
    return false;
  }
  const rawHeader = headers["x-service-token"] ?? headers["X-Service-Token"];
  const provided = Array.isArray(rawHeader) ? rawHeader[0] : rawHeader;
  if (typeof provided !== "string" || provided.length < MIN_SERVICE_TOKEN_LENGTH) {
    return false;
  }
  const expectedBuf = Buffer.from(expectedToken, "utf8");
  const providedBuf = Buffer.from(provided, "utf8");
  if (expectedBuf.length !== providedBuf.length) {
    return false;
  }
  return timingSafeEqual(expectedBuf, providedBuf);
}

/**
 * Extract `X-User-Id` header from incoming request headers.
 */
export function extractUserIdHeader(
  headers: IncomingHttpHeaders | Record<string, string | string[] | undefined>,
): string | null {
  const raw = headers["x-user-id"] ?? headers["X-User-Id"];
  const val = Array.isArray(raw) ? raw[0] : raw;
  if (typeof val !== "string") return null;
  const trimmed = val.trim();
  return trimmed.length > 0 ? trimmed : null;
}
