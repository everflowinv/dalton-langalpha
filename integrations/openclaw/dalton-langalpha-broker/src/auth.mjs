// integrations/openclaw/dalton-langalpha-broker/src/auth.mjs
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const LOOPBACK_ADDRESSES = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);

const PROXY_HEADER_NAMES = [
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-real-ip",
  "cf-connecting-ip",
  "true-client-ip",
  "via",
];

export function isLoopbackAddress(addr) {
  if (typeof addr !== "string") return false;
  return LOOPBACK_ADDRESSES.has(addr.trim());
}

export function hasProxyHeaders(headers) {
  if (!headers || typeof headers !== "object") return false;
  for (const name of PROXY_HEADER_NAMES) {
    const val = headers[name];
    if (typeof val === "string" && val.trim().length > 0) return true;
    if (Array.isArray(val) && val.length > 0) return true;
  }
  return false;
}

export function verifyLoopbackAndOrigin(req) {
  const remoteAddress = req?.socket?.remoteAddress || req?.connection?.remoteAddress || "";
  if (!isLoopbackAddress(remoteAddress)) {
    return {
      ok: false,
      status: 403,
      code: "loop_only",
      message: `Forbidden: non-loopback remoteAddress (${remoteAddress || "unknown"})`,
    };
  }
  if (hasProxyHeaders(req?.headers)) {
    return {
      ok: false,
      status: 403,
      code: "loop_only",
      message: "Forbidden: proxy/forwarded headers are not permitted on loopback broker routes",
    };
  }
  return { ok: true, remoteAddress };
}

export function timingSafeTokenEqual(provided, expected) {
  if (typeof provided !== "string" || typeof expected !== "string") return false;
  const a = Buffer.from(provided.trim(), "utf8");
  const b = Buffer.from(expected.trim(), "utf8");
  if (a.length === 0 || a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

export function extractBearerToken(req) {
  const raw = req?.headers?.authorization || req?.headers?.Authorization;
  if (typeof raw === "string") {
    const m = raw.match(/^\s*Bearer\s+([^\s]+)\s*$/i);
    if (m) return m[1];
  }
  const altHeader = req?.headers?.["x-dalton-broker-token"];
  if (typeof altHeader === "string" && altHeader.trim()) {
    return altHeader.trim();
  }
  return null;
}

export function verifyBearerToken(req, expectedToken) {
  const token = extractBearerToken(req);
  if (!token) {
    return {
      ok: false,
      status: 401,
      code: "unauthorized",
      message: "Missing Authorization: Bearer <token> header",
    };
  }
  if (!timingSafeTokenEqual(token, expectedToken)) {
    return {
      ok: false,
      status: 401,
      code: "unauthorized",
      message: "Invalid bearer token",
    };
  }
  return { ok: true };
}

export async function ensureScopedTokenFile({ stateDir, explicitTokenPath }) {
  const tokenPath = explicitTokenPath
    ? path.resolve(explicitTokenPath)
    : path.join(stateDir, "broker.token");

  await fs.mkdir(path.dirname(tokenPath), { recursive: true, mode: 0o700 });

  let token = "";
  try {
    token = (await fs.readFile(tokenPath, "utf8")).trim();
  } catch {
    token = "";
  }

  if (token.length < 32) {
    token = `dlb_${crypto.randomBytes(32).toString("base64url")}`;
    const tmpPath = `${tokenPath}.tmp-${process.pid}`;
    await fs.writeFile(tmpPath, `${token}\n`, { encoding: "utf8", mode: 0o600 });
    await fs.chmod(tmpPath, 0o600);
    await fs.rename(tmpPath, tokenPath);
  } else {
    await fs.chmod(tokenPath, 0o600).catch(() => {});
  }

  return { token, tokenPath };
}
