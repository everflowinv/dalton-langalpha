#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  closeSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import http from "node:http";
import { homedir } from "node:os";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const VERSION = "1.1.0";
const API_BASE = "https://www.alphaengine.top/api/v1/kmpsummary";
const HOST = process.env.ALPHAENGINE_MCP_HOST || "127.0.0.1";
const PORT = Number(process.env.ALPHAENGINE_MCP_PORT || 8950);
const WORKSPACE = process.env.DALTON_RESEARCH_HOME || process.env.OPENCLAW_WORKSPACE || path.join(homedir(), ".openclaw", "workspace"); // 兼容旧服务默认目录
const MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024;
const CACHE_TTL_MS = 10 * 60 * 1000;
const SEARCH_CURSOR_TTL_MS = 15 * 60 * 1000;

const CATEGORY_DEFS = {
  sell_side: {
    label: "卖方观点（点评+境内外研报）",
    codes: ["comment", "chinaReport", "foreignReport"],
  },
  sell_side_comment: {
    label: "卖方点评",
    codes: ["comment"],
  },
  sell_side_report: {
    label: "卖方研报（境内+海外）",
    codes: ["chinaReport", "foreignReport"],
  },
  domestic_report: {
    label: "国内研报",
    codes: ["chinaReport"],
  },
  foreign_report: {
    label: "海外研报",
    codes: ["foreignReport"],
  },
  meeting_minutes: {
    label: "会议纪要",
    codes: ["summary"],
  },
  institution_meetings: { label: "本机构录制会议", codes: ["institutionMeeting"] },
  personal_meetings: { label: "个人录制会议", codes: ["personalMeeting"] },
  third_party_reports: { label: "三方报告", codes: ["thirdReport"] },
  expert_library: { label: "专家库（独立权限/额度）", codes: ["expert_summary"] },
  global_think_tank: { label: "全球智库", codes: ["globalThinkTank"] },
  announcement: {
    label: "公司公告",
    codes: ["companyAllAnnouncement"],
  },
  news: {
    label: "资讯",
    codes: ["news"],
  },
};

const SOURCE_ALIASES = {
  摩根士丹利: "Morgan Stanley",
  摩根大通: "J.P.Morgan",
  高盛: "Goldman Sachs",
  花旗: "Citi",
  瑞银: "UBS",
  瑞信: "Credit Suisse",
  美银: "Bank of America",
  德银: "Deutsche Bank",
  巴克莱: "Barclays",
  麦格理: "Macquarie",
};

const cache = new Map();
const searchCursorStates = new Map();

function cached(key) {
  const entry = cache.get(key);
  return entry && Date.now() - entry.time < CACHE_TTL_MS ? entry.value : undefined;
}

function setCached(key, value) {
  cache.set(key, { time: Date.now(), value });
  return value;
}

function decodeJsonBase64Url(value) {
  return JSON.parse(Buffer.from(value, "base64url").toString("utf8"));
}

function jwtPayload(token) {
  try {
    return decodeJsonBase64Url(token.split(".")[1]);
  } catch {
    return {};
  }
}

// Token 只随 webview 子进程的 additionalArguments 下发，主窗口 renderer 上没有。
// App 里没有打开任何模块页时，ps 里一个 --alphaclaw-token= 都不会有，此时登录态其实是好的。
const APP_EXECUTABLE = "/Applications/AlphaEngine.app/Contents/MacOS/AlphaEngine";
const KEYCHAIN_SERVICE = process.env.ALPHAENGINE_KEYCHAIN_SERVICE || "dalton-alphaengine";
const KEYCHAIN_ACCOUNT_RE = /^[A-Za-z0-9._:@-]{1,128}$/;
const SECURITY_CLI_TIMEOUT_MS = 5000;
const MAX_SECURITY_INTERACTIVE_BYTES = 4000;
const DEFAULT_QUOTA_COOLDOWN_MS = 6 * 60 * 60 * 1000;
const EXPIRY_ALERT_WINDOW_SECONDS = 3 * 24 * 60 * 60;
const EXPIRY_ALERT_GRACE_SECONDS = 7 * 24 * 60 * 60;
const GATEWAY_ENV_FILE = process.env.DALTON_CHANNEL_GATEWAY_ENV_FILE || path.join(homedir(), ".config", "dalton", "channel-gateway.env");
const GATEWAY_OPS_ALERTS_URL = process.env.DALTON_OPS_ALERTS_URL || "http://127.0.0.1:8962/internal/ops-alerts";
const SENSITIVE_KEY_RE = /(?:token|authorization|cookie|password|passcode|secret)/i;

const ENDPOINT_QUOTA_RULES = [
  { match: "prefix", pattern: "/summary/detail/", quotaType: "read" },
  { match: "prefix", pattern: "/summary/private/detail/", quotaType: "read" },
  { match: "prefix", pattern: "/summary/attachment/list/", quotaType: "read" },
  { match: "exact", pattern: "/summary/doc/base", quotaType: "read" },
  { match: "exact", pattern: "/summary/search/list/new", quotaType: "search" },
  { match: "exact", pattern: "/summary/search/category", quotaType: "search" },
  { match: "exact", pattern: "/summary/search/booked/list", quotaType: "search" },
  { match: "exact", pattern: "/summary/search/booked/count/list", quotaType: "search" },
  { match: "exact", pattern: "/summary/search/booked/preview", quotaType: "search" },
  { match: "prefix", pattern: "/summary/search/reservation/info/", quotaType: "search" },
  { match: "exact", pattern: "/summary/institution/list", quotaType: "search" },
  { match: "prefix", pattern: "/summary/type/list/", quotaType: "search" },
  { match: "exact", pattern: "/summary/industry/list", quotaType: "search" },
  { match: "exact", pattern: "/api/v1/kmpadmin/dic/company/list", quotaType: "search" },
  { match: "exact", pattern: "/api/v1/kmpstock/stock/self/group/list", quotaType: "search" },
  { match: "prefix", pattern: "/api/v1/kmpstock/stock/self/stock/list/all", quotaType: "search" },
  { match: "exact", pattern: "/region/list", quotaType: "search" },
];

function defaultLogWarning(message) {
  process.stderr.write(`${redactSecrets(String(message || ""))}\n`);
}

function resolveEndpointQuotaType(apiPath, logWarning = defaultLogWarning) {
  const pathname = String(apiPath || "").split("?")[0];
  for (const rule of ENDPOINT_QUOTA_RULES) {
    if (rule.match === "exact" && pathname === rule.pattern) return rule.quotaType;
    if (rule.match === "prefix" && pathname.startsWith(rule.pattern)) return rule.quotaType;
  }
  logWarning(`[alphaengine-mcp] unmapped endpoint "${pathname}", defaulting quotaType to "search"`);
  return "search";
}

function defaultAccountsStateFile() {
  if (process.env.ALPHAENGINE_ACCOUNTS_STATE_FILE) {
    return path.resolve(process.env.ALPHAENGINE_ACCOUNTS_STATE_FILE);
  }
  const cacheDir = process.env.DALTON_BRIDGE_CACHE_DIR
    ? path.resolve(process.env.DALTON_BRIDGE_CACHE_DIR)
    : process.env.DALTON_ALPHAENGINE_CACHE_DB
      ? path.dirname(path.resolve(process.env.DALTON_ALPHAENGINE_CACHE_DB))
      : path.join(homedir(), "Library", "Caches", "dalton-researchdata-bridge");
  return path.join(cacheDir, "alphaengine_accounts_state.json");
}

function resolveQuotaCooldownMs(overrideMs) {
  if (Number.isFinite(overrideMs) && overrideMs >= 0) return overrideMs;
  const rawSeconds = process.env.ALPHAENGINE_QUOTA_COOLDOWN_SECONDS;
  if (rawSeconds !== undefined && rawSeconds !== "") {
    const parsed = Number(rawSeconds);
    if (Number.isFinite(parsed) && parsed >= 0) return parsed * 1000;
  }
  return DEFAULT_QUOTA_COOLDOWN_MS;
}

function extractPhoneLast4(bundle, payload) {
  const rawPhone =
    payload?.mobile ??
    payload?.phone ??
    payload?.phone_number ??
    payload?.telephone ??
    bundle?.mobile ??
    bundle?.phone ??
    null;
  if (rawPhone === null || rawPhone === undefined) return null;
  const digits = String(rawPhone).replace(/\D/g, "");
  return digits.length >= 4 ? digits.slice(-4) : null;
}

function extractNickname(bundle, payload) {
  for (const candidate of [
    payload?.nickname,
    payload?.name,
    payload?.nick_name,
    payload?.nickName,
    bundle?.nickname,
    bundle?.name,
  ]) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return null;
}

function extractAccountProfile(bundle, payload = jwtPayload(bundle?.token || "")) {
  const rawUserId = bundle?.user_id ?? bundle?.userId ?? payload?.user_id ?? payload?.sub ?? payload?.id ?? "";
  const userId = String(rawUserId).trim();
  return {
    token: bundle?.token || "",
    refreshToken: bundle?.refresh_token ?? bundle?.refreshToken ?? null,
    exp: Number(payload?.exp || bundle?.exp || 0),
    userId,
    nickname: extractNickname(bundle, payload),
    phoneLast4: extractPhoneLast4(bundle, payload),
  };
}

function accountDisplayLabel(account) {
  if (account?.nickname) return account.nickname;
  if (account?.phoneLast4) return `尾号${account.phoneLast4}`;
  return `账号${account?.userId || "unknown"}`;
}

function redactSecrets(text, secrets = []) {
  let out = String(text || "");
  for (const secret of secrets) {
    if (typeof secret === "string" && secret.length >= 8) {
      out = out.split(secret).join("[REDACTED]");
    }
  }
  out = out.replace(/Bearer\s+[^\s"',}]+/gi, "Bearer [REDACTED]");
  out = out.replace(/--alphaclaw-token=[^\s"',}]+/gi, "--alphaclaw-token=[REDACTED]");
  out = out.replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/g, "[REDACTED]");
  out = out.replace(
    /("(?:refresh_?token|access_?token|token|authorization|cookie|password|passcode|secret)"\s*:\s*)"[^"]*"/gi,
    '$1"[REDACTED]"',
  );
  return out;
}

function sanitizeErrorValue(value, secrets = [], seen = new WeakSet()) {
  if (typeof value === "string") return redactSecrets(value, secrets);
  if (Array.isArray(value)) {
    if (seen.has(value)) return "[Circular]";
    seen.add(value);
    return value.map((item) => sanitizeErrorValue(item, secrets, seen));
  }
  if (value && typeof value === "object") {
    if (seen.has(value)) return "[Circular]";
    seen.add(value);
    const out = {};
    for (const [key, child] of Object.entries(value)) {
      if (SENSITIVE_KEY_RE.test(key)) {
        out[key] = "[REDACTED]";
      } else {
        out[key] = sanitizeErrorValue(child, secrets, seen);
      }
    }
    return out;
  }
  return value;
}

function formatSanitizedErrorSnippet(parsed, secrets = [], maxChars = 600) {
  const sanitized = sanitizeErrorValue(parsed, secrets);
  const serialized = typeof sanitized === "string" ? sanitized : JSON.stringify(sanitized);
  return redactSecrets(serialized, secrets).slice(0, maxChars);
}

function desktopProcessSnapshot() {
  const commands = execFileSync("ps", ["-axo", "command="], { encoding: "utf8" });
  let appRunning = false;
  const candidates = [];
  for (const command of commands.split("\n")) {
    if (command.startsWith(APP_EXECUTABLE)) appRunning = true;
    if (!command.includes("AlphaEngine Helper (Renderer)")) continue;
    const match = command.match(/--alphaclaw-token=([^ ]+)/);
    if (!match) continue;
    try {
      const bundle = decodeJsonBase64Url(match[1]);
      if (!bundle.token) continue;
      const profile = extractAccountProfile(bundle);
      candidates.push(profile);
    } catch {
      // Ignore stale or malformed renderer processes.
    }
  }
  candidates.sort((a, b) => b.exp - a.exp);
  return { appRunning, candidates };
}

function isValidKeychainAccount(userId) {
  return typeof userId === "string" && KEYCHAIN_ACCOUNT_RE.test(userId);
}

function quoteSecurityInteractiveArg(value) {
  const str = String(value ?? "");
  if (/[\r\n\0]/.test(str)) {
    throw new Error("E_KEYCHAIN_INVALID_ARG: invalid control character");
  }
  if (/^[A-Za-z0-9._:@-]+$/.test(str)) return str;
  return `"${str.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function defaultRunSecurity(args, input, timeoutMs = SECURITY_CLI_TIMEOUT_MS) {
  return execFileSync("security", args, {
    ...(input !== undefined ? { input } : {}),
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
    timeout: timeoutMs,
  });
}

function isSecurityTimeoutError(err) {
  return err?.code === "ETIMEDOUT" || Boolean(err?.killed && err?.signal === "SIGTERM");
}

function createMacOsKeychain({
  service = KEYCHAIN_SERVICE,
  timeoutMs = SECURITY_CLI_TIMEOUT_MS,
  runSecurity = defaultRunSecurity,
  logWarning = defaultLogWarning,
} = {}) {
  return {
    save(userId, bundle) {
      const account = String(userId || "").trim();
      if (!bundle?.token) return false;
      if (!isValidKeychainAccount(account)) {
        logWarning("[alphaengine-mcp] skipped saving keychain item: invalid user_id format");
        return false;
      }
      const payload = JSON.stringify({
        token: bundle.token,
        user_id: account,
        exp: Number(bundle.exp || 0),
      });
      const encoded = Buffer.from(payload, "utf8").toString("base64");
      // Pass command via stdin to `security -i` so tokens never appear in process argv.
      const command = `add-generic-password -U -a ${quoteSecurityInteractiveArg(account)} -s ${quoteSecurityInteractiveArg(service)} -w ${encoded}\n`;
      const commandBytes = Buffer.byteLength(command, "utf8");
      if (commandBytes > MAX_SECURITY_INTERACTIVE_BYTES) {
        logWarning(
          `[alphaengine-mcp] skipped saving keychain item for user ${account}: security -i command (${commandBytes} bytes) exceeds ${MAX_SECURITY_INTERACTIVE_BYTES}-byte limit`,
        );
        return false;
      }
      try {
        runSecurity(["-i"], command, timeoutMs);
        return true;
      } catch (err) {
        const reason = isSecurityTimeoutError(err) ? "timeout" : err?.code || "error";
        logWarning(`[alphaengine-mcp] keychain save failed (${reason}) for user ${account}`);
        return false;
      }
    },
    load(userId) {
      const account = String(userId || "").trim();
      if (!isValidKeychainAccount(account)) return null;
      try {
        const raw = String(
          runSecurity(["find-generic-password", "-s", service, "-a", account, "-w"], undefined, timeoutMs) || "",
        ).trim();
        if (!raw) return null;
        let jsonText = raw;
        if (!raw.startsWith("{")) {
          const base64Decoded = Buffer.from(raw, "base64").toString("utf8");
          if (base64Decoded.trim().startsWith("{")) {
            jsonText = base64Decoded;
          } else if (/^[0-9a-fA-F]+$/.test(raw) && raw.length % 2 === 0) {
            jsonText = Buffer.from(raw, "hex").toString("utf8");
          }
        }
        const parsed = JSON.parse(jsonText);
        if (!parsed?.token) return null;
        return extractAccountProfile({
          token: parsed.token,
          user_id: parsed.user_id ?? account,
          exp: parsed.exp,
        });
      } catch (err) {
        if (isSecurityTimeoutError(err)) {
          logWarning(`[alphaengine-mcp] keychain load timed out for user ${account}`);
        }
        return null;
      }
    },
  };
}

function readGatewayServiceToken(envFile = GATEWAY_ENV_FILE) {
  try {
    const raw = readFileSync(envFile, "utf8");
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      if (!trimmed.startsWith("INTERNAL_SERVICE_TOKEN=")) continue;
      return trimmed.slice("INTERNAL_SERVICE_TOKEN=".length).trim().replace(/^["']|["']$/g, "");
    }
  } catch {
    // Missing or unreadable env file degrades gracefully.
  }
  return "";
}

async function sendDefaultOpsAlert({ code, detail, gatewayUrl = GATEWAY_OPS_ALERTS_URL, envFile = GATEWAY_ENV_FILE, timeoutMs = 3000 }) {
  const serviceToken = readGatewayServiceToken(envFile);
  if (!serviceToken) return { ok: false, outcome: "failed" };
  try {
    const response = await fetch(gatewayUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Service-Token": serviceToken,
      },
      body: JSON.stringify({ code, detail }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.ok) return { ok: true, outcome: "sent" };
    // Partial delivery (HTTP 502 with at least one ok: true target) must NOT be retried globally.
    try {
      const payload = await response.json();
      if (payload?.any_succeeded === true) return { ok: true, outcome: "sent" };
      if (Array.isArray(payload?.results) && payload.results.some((r) => r?.ok === true)) {
        return { ok: true, outcome: "sent" };
      }
    } catch {
      // Non-JSON error response counts as failed delivery.
    }
    return { ok: false, outcome: "failed" };
  } catch (err) {
    if (err?.name === "TimeoutError" || err?.name === "AbortError" || err?.code === "ETIMEDOUT") {
      return { ok: false, outcome: "uncertain" };
    }
    return { ok: false, outcome: "failed" };
  }
}

function shanghaiDateKey(nowMs) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(nowMs));
}

function sanitizeQuotaBucket(raw) {
  return {
    exhausted_at: typeof raw?.exhausted_at === "string" && raw.exhausted_at ? raw.exhausted_at : null,
  };
}

function sanitizeAccountRecord(userId, raw) {
  return {
    user_id: String(userId),
    nickname: typeof raw?.nickname === "string" && raw.nickname.trim() ? raw.nickname.trim() : null,
    phone_last4: typeof raw?.phone_last4 === "string" && /^\d{4}$/.test(raw.phone_last4) ? raw.phone_last4 : null,
    exp: Number.isFinite(Number(raw?.exp)) ? Number(raw.exp) : 0,
    updated_at: typeof raw?.updated_at === "string" && raw.updated_at ? raw.updated_at : null,
    quota: {
      search: sanitizeQuotaBucket(raw?.quota?.search),
      read: sanitizeQuotaBucket(raw?.quota?.read),
    },
    last_expiry_alert_date:
      typeof raw?.last_expiry_alert_date === "string" && raw.last_expiry_alert_date
        ? raw.last_expiry_alert_date
        : null,
  };
}

const UNAMBIGUOUS_QUOTA_PHRASES = [
  "额度已达上限",
  "额度不足",
  "超出额度",
  "额度已用完",
  "额度用尽",
  "配额已达上限",
  "配额已用完",
  "频率限制",
  "频率上限",
  "频率过高",
  "请求过于频繁",
  "超出频率",
];

// Upstream fields are read as numbers only when they are primitives: an object
// field in a malformed body must not throw and hide the rest of the body.
function primitiveNumber(v) {
  return typeof v === "number" || typeof v === "string" ? Number(v) || 0 : 0;
}

// The first present status code wins, as `a || b` did before (a "0" string
// counts as present), skipping only non-primitive fields.
function firstStatusCode(...values) {
  const v = values.find((x) => (typeof x === "number" || typeof x === "string") && x);
  return primitiveNumber(v);
}

// Never throws: a malformed upstream body must not replace the original error
// with a TypeError, so anything unexpected classifies as "none".
function classifyUpstreamError(errorOrInfo) {
  try {
    return classifyUpstreamErrorUnsafe(errorOrInfo);
  } catch {
    return "none";
  }
}

function classifyUpstreamErrorUnsafe(errorOrInfo) {
  if (!errorOrInfo) return "none";
  const httpStatus =
    typeof errorOrInfo.httpStatus === "number" || typeof errorOrInfo.httpStatus === "string"
      ? Number(errorOrInfo.httpStatus) || 0
      : 0;
  const body =
    errorOrInfo.upstreamBody && typeof errorOrInfo.upstreamBody === "object"
      ? errorOrInfo.upstreamBody
      : null;

  if (body) {
    const nestedErr = body.error && typeof body.error === "object" ? body.error : null;
    const statusCode = firstStatusCode(body.status_code, nestedErr?.status_code);
    const codeTokens = [
      body.status,
      body.error_code,
      body.code,
      nestedErr?.status,
      nestedErr?.error_code,
      nestedErr?.code,
    ]
      // Primitives only: an object field (malformed body) is skipped, not
      // converted, so it cannot hide the rest of the body's signals.
      .filter((v) => typeof v === "string" || typeof v === "number")
      .map((v) => String(v).trim().toLowerCase())
      .filter(Boolean);

    const msgFields = [
      body.message,
      body.msg,
      typeof body.error === "string" ? body.error : null,
      nestedErr?.message,
      nestedErr?.msg,
    ]
      .filter((v) => typeof v === "string" && v.trim())
      .map((v) => v.trim());
    const msgText = msgFields.join(" ");
    const msgLower = msgText.toLowerCase();

    if (
      codeTokens.some((c) => c === "invalid_arguments" || c.startsWith("e_bad_")) ||
      /^(?:e_bad_[a-z0-9_]*|e_query_sort|e_scope_conflict|e_cursor_[a-z0-9_]*|e_source_not_resolved)\b/i.test(
        msgText,
      ) ||
      msgText.includes("参数错误")
    ) {
      return "none";
    }

    if (
      httpStatus === 429 ||
      statusCode === 429 ||
      codeTokens.some((c) => c === "quota_exceeded" || c === "rate_limited")
    ) {
      return "quota";
    }

    if (msgText.includes("无查看权限或额度已达上限")) {
      return "ambiguous_retry";
    }

    if (
      /\btoo many requests\b/i.test(msgLower) ||
      UNAMBIGUOUS_QUOTA_PHRASES.some((phrase) => msgText.includes(phrase))
    ) {
      return "quota";
    }

    return "none";
  }

  // Fallback for synthetic Error/string without structured upstreamBody.
  const text = String(typeof errorOrInfo === "string" ? errorOrInfo : errorOrInfo.message || "").trim();
  const lower = text.toLowerCase();
  if (
    /^(?:e_bad_[a-z0-9_]*|e_query_sort|e_scope_conflict|e_cursor_[a-z0-9_]*|e_source_not_resolved)\b/i.test(
      text,
    ) ||
    text.includes("参数错误")
  ) {
    return "none";
  }
  if (httpStatus === 429 || /^E_UPSTREAM_HTTP:\s*HTTP\s+429\b/i.test(text)) {
    return "quota";
  }
  if (text.includes("无查看权限或额度已达上限")) {
    return "ambiguous_retry";
  }
  if (
    /\b(?:quota_exceeded|rate_limited|too many requests)\b/i.test(lower) ||
    UNAMBIGUOUS_QUOTA_PHRASES.some((phrase) => text.includes(phrase))
  ) {
    return "quota";
  }
  return "none";
}

function isQuotaOrRateLimitError(errorOrInfo) {
  return classifyUpstreamError(errorOrInfo) === "quota";
}

const DESKTOP_STATUS = {
  ready: {
    status: "ready",
    hint: "AlphaEngine Desktop 已就绪。",
  },
  app_not_running: {
    status: "app_not_running",
    error: "E_DESKTOP_NOT_RUNNING: AlphaEngine Desktop 未运行",
    hint: "启动 AlphaEngine，然后在 App 里打开任意模块页并保持开着。",
  },
  no_module_page: {
    status: "no_module_page",
    error: "E_DESKTOP_NO_MODULE_PAGE: AlphaEngine Desktop 在运行，但没有打开的模块页，读不到 token",
    hint: "登录态不用动。在 App 里打开任意会加载网页内容的模块页并保持开着，token 只随模块页的子进程下发。",
  },
  token_expired: {
    status: "token_expired",
    error: "E_DESKTOP_TOKEN_EXPIRED: AlphaEngine 登录态已过期",
    hint: "在 App 内重新登录，再打开一个模块页。",
  },
};

function createAccountManager({
  stateFile = defaultAccountsStateFile(),
  cooldownMs,
  nowMs = () => Date.now(),
  readDesktopSnapshot = desktopProcessSnapshot,
  keychain,
  sendOpsAlert = sendDefaultOpsAlert,
  logWarning = defaultLogWarning,
} = {}) {
  const effectiveCooldownMs = resolveQuotaCooldownMs(cooldownMs);
  const effectiveKeychain = keychain || createMacOsKeychain({ logWarning });
  const inFlightAlerts = new Set();
  const ephemeralSecrets = new Set();
  let memoryState = { version: 1, last_used_user_id: null, accounts: {} };
  const memoryClearMap = {};
  let warnedStateWriteFailure = false;
  let warnedMarkerDirFailure = false;

  function parseIsoMs(value) {
    if (typeof value !== "string" || !value) return null;
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  }

  function mergeQuotaBucket(diskBucket, incBucket, clearCutoffMs) {
    let diskMs = parseIsoMs(diskBucket?.exhausted_at);
    let incMs = parseIsoMs(incBucket?.exhausted_at);
    // A success that started after a mark proves it stale, whichever copy
    // (disk, or a state loaded before the clear) still carries it.
    if (Number.isFinite(clearCutoffMs)) {
      if (diskMs !== null && diskMs < clearCutoffMs) diskMs = null;
      if (incMs !== null && incMs < clearCutoffMs) incMs = null;
    }
    if (diskMs === null && incMs === null) return { exhausted_at: null };
    if (diskMs === null) return { exhausted_at: new Date(incMs).toISOString() };
    if (incMs === null) return { exhausted_at: new Date(diskMs).toISOString() };
    return { exhausted_at: new Date(Math.max(diskMs, incMs)).toISOString() };
  }

  function mergeAccountRecords(diskRec, incRec, clearBucketsForUser) {
    if (!diskRec) return sanitizeAccountRecord(incRec.user_id, incRec);
    if (!incRec) return sanitizeAccountRecord(diskRec.user_id, diskRec);
    const diskUpdatedMs = parseIsoMs(diskRec.updated_at) ?? 0;
    const incUpdatedMs = parseIsoMs(incRec.updated_at) ?? 0;
    const preferInc = incUpdatedMs >= diskUpdatedMs;
    const newer = preferInc ? incRec : diskRec;
    const older = preferInc ? diskRec : incRec;

    const diskAlert = diskRec.last_expiry_alert_date || "";
    const incAlert = incRec.last_expiry_alert_date || "";
    const mergedAlert = (diskAlert >= incAlert ? diskAlert : incAlert) || null;
    const laterUpdatedMs = Math.max(diskUpdatedMs, incUpdatedMs);

    return sanitizeAccountRecord(incRec.user_id || diskRec.user_id, {
      user_id: incRec.user_id || diskRec.user_id,
      nickname: newer.nickname || older.nickname || null,
      phone_last4: newer.phone_last4 || older.phone_last4 || null,
      exp: Math.max(Number(diskRec.exp || 0), Number(incRec.exp || 0)),
      updated_at: laterUpdatedMs > 0 ? new Date(laterUpdatedMs).toISOString() : null,
      quota: {
        search: mergeQuotaBucket(diskRec.quota?.search, incRec.quota?.search, clearBucketsForUser?.search),
        read: mergeQuotaBucket(diskRec.quota?.read, incRec.quota?.read, clearBucketsForUser?.read),
      },
      last_expiry_alert_date: mergedAlert,
    });
  }

  function mergeStates(baseState, incState, clearMap) {
    const accounts = {};
    const userIds = new Set([
      ...Object.keys(baseState?.accounts || {}),
      ...Object.keys(incState?.accounts || {}),
    ]);
    for (const uid of userIds) {
      accounts[uid] = mergeAccountRecords(
        baseState?.accounts?.[uid],
        incState?.accounts?.[uid],
        clearMap?.[uid],
      );
    }
    return {
      version: 1,
      last_used_user_id: incState?.last_used_user_id || baseState?.last_used_user_id || null,
      accounts,
    };
  }

  function cloneCleanState(state) {
    const cleanAccounts = {};
    for (const [uid, record] of Object.entries(state?.accounts || {})) {
      if (uid) cleanAccounts[uid] = sanitizeAccountRecord(uid, record);
    }
    return {
      version: 1,
      last_used_user_id: typeof state?.last_used_user_id === "string" ? state.last_used_user_id : null,
      accounts: cleanAccounts,
    };
  }

  function readDiskState() {
    try {
      const raw = readFileSync(stateFile, "utf8");
      const parsed = JSON.parse(raw);
      return cloneCleanState(parsed);
    } catch {
      return null;
    }
  }

  function loadState() {
    const disk = readDiskState();
    if (!disk) return cloneCleanState(memoryState);
    const merged = mergeStates(disk, memoryState, memoryClearMap);
    memoryState = merged;
    return cloneCleanState(merged);
  }

  let unsavedState = false;

  function saveState(state, clearMap) {
    if (clearMap) {
      for (const [uid, buckets] of Object.entries(clearMap)) {
        memoryClearMap[uid] = { ...(memoryClearMap[uid] || {}) };
        for (const [bucket, cutoffMs] of Object.entries(buckets || {})) {
          if (Number.isFinite(cutoffMs)) {
            memoryClearMap[uid][bucket] = Math.max(memoryClearMap[uid][bucket] || 0, cutoffMs);
          }
        }
      }
    }
    const cleanIncoming = cloneCleanState(state);
    const disk = readDiskState();
    // Every write carries all clears this process has made, not only this
    // call's: a clear whose own write failed must not be undone by a later
    // write that merges the stale timestamp back from disk. A clear only drops
    // marks older than its cutoff, so newer marks survive.
    const merged = disk ? mergeStates(disk, cleanIncoming, memoryClearMap) : cleanIncoming;
    memoryState = cloneCleanState(merged);
    // Set until a write succeeds, so a record kept only in memory after a
    // failed write is written once the file is writable again.
    unsavedState = true;
    try {
      const dir = path.dirname(stateFile);
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      const tmpFile = `${stateFile}.tmp.${process.pid}.${randomUUID()}`;
      writeFileSync(tmpFile, `${JSON.stringify(merged, null, 2)}\n`, { mode: 0o600 });
      chmodSync(tmpFile, 0o600);
      renameSync(tmpFile, stateFile);
      chmodSync(stateFile, 0o600);
      unsavedState = false;
      return true;
    } catch (err) {
      if (!warnedStateWriteFailure) {
        warnedStateWriteFailure = true;
        logWarning(
          `[alphaengine-mcp] failed to persist account state (${err?.code || "write_error"}), continuing with in-memory state`,
        );
      }
      return false;
    }
  }

  function syncAccounts() {
    const currentNowMs = nowMs();
    const nowIso = new Date(currentNowMs).toISOString();
    const { appRunning, candidates } = readDesktopSnapshot();
    const state = loadState();
    let stateDirty = false;

    const appByUser = new Map();
    const anonymousAppCandidates = [];
    for (const candidate of candidates) {
      if (candidate.token) ephemeralSecrets.add(candidate.token);
      if (candidate.refreshToken) ephemeralSecrets.add(candidate.refreshToken);
      const uid = String(candidate.userId || "").trim();
      if (!uid) {
        anonymousAppCandidates.push(candidate);
        continue;
      }
      const prev = appByUser.get(uid);
      if (!prev || candidate.exp > prev.exp) {
        appByUser.set(uid, candidate);
      }
    }

    const keychainUsers = new Set();
    const resolvedAppEntries = [];
    for (const [uid, appCandidate] of appByUser.entries()) {
      const existingKc = effectiveKeychain.load(uid);
      if (existingKc?.token) {
        ephemeralSecrets.add(existingKc.token);
        keychainUsers.add(uid);
      }

      // Never let a stale renderer process overwrite a newer valid token in Keychain.
      let chosen = appCandidate;
      if (existingKc?.token && existingKc.exp > appCandidate.exp) {
        chosen = {
          ...existingKc,
          nickname: existingKc.nickname || appCandidate.nickname,
          phoneLast4: existingKc.phoneLast4 || appCandidate.phoneLast4,
        };
      } else if (!existingKc?.token || appCandidate.exp > existingKc.exp) {
        if (effectiveKeychain.save(uid, appCandidate)) {
          keychainUsers.add(uid);
        }
      }

      resolvedAppEntries.push({
        ...chosen,
        inDesktopApp: true,
        inKeychain: keychainUsers.has(uid),
      });

      const existing = state.accounts[uid] || sanitizeAccountRecord(uid, {});
      const candidate = sanitizeAccountRecord(uid, {
        ...existing,
        nickname: chosen.nickname || existing.nickname,
        phone_last4: chosen.phoneLast4 || existing.phone_last4,
        exp: Math.max(existing.exp || 0, chosen.exp || 0),
      });
      // Only a real change rewrites the record: stamping updated_at on every
      // request would rewrite the shared state file each call and widen the
      // window in which another process's fresh quota mark can be lost.
      if (!state.accounts[uid] || JSON.stringify(existing) !== JSON.stringify(candidate)) {
        state.accounts[uid] = sanitizeAccountRecord(uid, { ...candidate, updated_at: nowIso });
        stateDirty = true;
      }
    }

    const keychainOnly = [];
    for (const uid of Object.keys(state.accounts)) {
      if (appByUser.has(uid)) continue;
      const loaded = effectiveKeychain.load(uid);
      if (loaded?.token) {
        ephemeralSecrets.add(loaded.token);
        keychainUsers.add(uid);
        const existing = state.accounts[uid];
        const refreshed = sanitizeAccountRecord(uid, {
          ...existing,
          nickname: loaded.nickname || existing.nickname,
          phone_last4: loaded.phoneLast4 || existing.phone_last4,
          exp: Math.max(existing.exp || 0, loaded.exp || 0),
        });
        if (JSON.stringify(existing) !== JSON.stringify(refreshed)) {
          state.accounts[uid] = refreshed;
          stateDirty = true;
        }
        keychainOnly.push({
          ...loaded,
          nickname: refreshed.nickname,
          phoneLast4: refreshed.phone_last4,
          exp: loaded.exp || refreshed.exp,
          updatedAtMs: Date.parse(refreshed.updated_at || "") || 0,
          inDesktopApp: false,
          inKeychain: true,
        });
      }
    }

    // A retry after a failed write carries this process's pending clears too,
    // so a cleared exhausted_at is not merged back from the stale file.
    if (stateDirty || unsavedState) saveState(state);

    const appOrdered = [
      ...resolvedAppEntries,
      ...anonymousAppCandidates.map((entry) => ({
        ...entry,
        inDesktopApp: true,
        inKeychain: false,
      })),
    ].sort((a, b) => b.exp - a.exp);

    keychainOnly.sort((a, b) => b.exp - a.exp || b.updatedAtMs - a.updatedAtMs);

    const orderedAccounts = [...appOrdered, ...keychainOnly];
    return {
      nowMs: currentNowMs,
      nowSec: Math.floor(currentNowMs / 1000),
      appRunning,
      desktopCandidates: candidates,
      orderedAccounts,
      state,
    };
  }

  function isInCooldown(state, userId, quotaType, currentNowMs) {
    if (!userId) return false;
    const exhaustedAt = state.accounts[userId]?.quota?.[quotaType]?.exhausted_at;
    if (!exhaustedAt) return false;
    const exhaustedMs = Date.parse(exhaustedAt);
    if (!Number.isFinite(exhaustedMs)) return false;
    return currentNowMs - exhaustedMs < effectiveCooldownMs;
  }

  function markQuotaExhausted(userId, quotaType, currentNowMs) {
    if (!userId) return;
    if (memoryClearMap[userId]) {
      delete memoryClearMap[userId][quotaType];
    }
    const state = loadState();
    const record = state.accounts[userId] || sanitizeAccountRecord(userId, {});
    record.quota[quotaType] = { exhausted_at: new Date(currentNowMs).toISOString() };
    state.accounts[userId] = record;
    saveState(state);
  }

  function clearQuotaExhausted(userId, quotaType, requestStartMs) {
    if (!userId) return;
    const state = loadState();
    const record = state.accounts[userId];
    let dirty = false;
    let clearMap;
    const exhaustedAt = record?.quota?.[quotaType]?.exhausted_at;
    if (exhaustedAt) {
      const exhaustedMs = Date.parse(exhaustedAt);
      // Clear only marks recorded strictly BEFORE this request started.
      if (!Number.isFinite(exhaustedMs) || exhaustedMs < requestStartMs) {
        record.quota[quotaType] = { exhausted_at: null };
        clearMap = { [userId]: { [quotaType]: requestStartMs } };
        dirty = true;
      }
    }
    if (state.last_used_user_id !== userId) {
      state.last_used_user_id = userId;
      dirty = true;
    }
    if (dirty) saveState(state, clearMap);
  }

  function selectCandidates(snapshot, quotaType) {
    const validAccounts = snapshot.orderedAccounts.filter((acc) => acc.exp > snapshot.nowSec + 30);
    const available = validAccounts.filter(
      (acc) => !isInCooldown(snapshot.state, acc.userId, quotaType, snapshot.nowMs),
    );
    return { validAccounts, available };
  }

  function collectAllSecrets(snapshot) {
    for (const acc of snapshot?.orderedAccounts || []) {
      if (acc.token) ephemeralSecrets.add(acc.token);
      if (acc.refreshToken) ephemeralSecrets.add(acc.refreshToken);
    }
    return [...ephemeralSecrets];
  }

  function statusSummary() {
    const snapshot = syncAccounts();
    const { nowMs: currentNowMs, nowSec, appRunning, desktopCandidates, orderedAccounts, state } = snapshot;
    const { validAccounts, available: availableSearch } = selectCandidates(snapshot, "search");
    const { available: availableRead } = selectCandidates(snapshot, "read");
    const primaryValid = validAccounts[0] || null;
    const activeSearchUserId = availableSearch[0]?.userId || null;
    const activeReadUserId = availableRead[0]?.userId || null;
    const desktopUserId = orderedAccounts.find((acc) => acc.inDesktopApp)?.userId || null;
    const currentUserId =
      activeSearchUserId ||
      activeReadUserId ||
      (state.last_used_user_id && validAccounts.some((acc) => acc.userId === state.last_used_user_id)
        ? state.last_used_user_id
        : null) ||
      primaryValid?.userId ||
      null;

    let statusKey;
    let customHint = null;
    if (primaryValid) {
      statusKey = "ready";
      if (!primaryValid.inDesktopApp) {
        customHint = appRunning
          ? "AlphaEngine Desktop 未打开模块页，当前使用钥匙串中已保存的账号令牌。"
          : "AlphaEngine Desktop 未运行，当前使用钥匙串中已保存的账号令牌。";
      }
    } else if (orderedAccounts.length > 0) {
      statusKey = "token_expired";
    } else {
      statusKey = !appRunning ? "app_not_running" : desktopCandidates.length === 0 ? "no_module_page" : "token_expired";
    }

    const baseStatus = DESKTOP_STATUS[statusKey];
    const accounts = orderedAccounts.map((acc) => {
      const record = state.accounts[acc.userId] || sanitizeAccountRecord(acc.userId, {});
      const searchExhaustedAt = record.quota.search.exhausted_at;
      const readExhaustedAt = record.quota.read.exhausted_at;
      const searchExhaustedMs = searchExhaustedAt ? Date.parse(searchExhaustedAt) : NaN;
      const readExhaustedMs = readExhaustedAt ? Date.parse(readExhaustedAt) : NaN;
      const searchInCooldown =
        Number.isFinite(searchExhaustedMs) && currentNowMs - searchExhaustedMs < effectiveCooldownMs;
      const readInCooldown =
        Number.isFinite(readExhaustedMs) && currentNowMs - readExhaustedMs < effectiveCooldownMs;
      return {
        user_id: acc.userId || null,
        nickname: acc.nickname || null,
        phone_last4: acc.phoneLast4 || null,
        token_expires_at: acc.exp ? new Date(acc.exp * 1000).toISOString() : null,
        token_seconds_remaining: acc.exp ? Math.max(0, acc.exp - nowSec) : 0,
        token_expired: !(acc.exp > nowSec + 30),
        is_current: Boolean(currentUserId && acc.userId === currentUserId),
        in_desktop_app: acc.inDesktopApp,
        in_keychain: acc.inKeychain,
        search_quota: {
          status: searchInCooldown ? "exhausted" : "available",
          label: searchInCooldown ? "已用完" : "可用",
          exhausted_at: searchExhaustedAt || null,
          cooldown_until: searchInCooldown ? new Date(searchExhaustedMs + effectiveCooldownMs).toISOString() : null,
        },
        read_quota: {
          status: readInCooldown ? "exhausted" : "available",
          label: readInCooldown ? "已用完" : "可用",
          exhausted_at: readExhaustedAt || null,
          cooldown_until: readInCooldown ? new Date(readExhaustedMs + effectiveCooldownMs).toISOString() : null,
        },
      };
    });

    return {
      ...baseStatus,
      ...(customHint ? { hint: customHint } : {}),
      app_running: appRunning,
      desktop_renderer_processes: desktopCandidates.length,
      authenticated: Boolean(primaryValid),
      current_user_id: currentUserId,
      desktop_user_id: desktopUserId,
      active_search_user_id: activeSearchUserId,
      active_read_user_id: activeReadUserId,
      token_expires_at: primaryValid ? new Date(primaryValid.exp * 1000).toISOString() : null,
      token_seconds_remaining: primaryValid ? Math.max(0, primaryValid.exp - nowSec) : 0,
      accounts,
      _snapshot: snapshot,
    };
  }

  function alertMarkerDir() {
    return path.join(path.dirname(stateFile), "alphaengine_alert_markers");
  }

  function alertMarkerPath(userId, todayStr) {
    const safeUser = encodeURIComponent(String(userId)).replace(/[^A-Za-z0-9._-]/g, "_");
    return path.join(alertMarkerDir(), `${safeUser}_${todayStr}.lock`);
  }

  function pruneOldAlertMarkers(todayStr) {
    try {
      const dir = alertMarkerDir();
      for (const name of readdirSync(dir)) {
        if (!name.endsWith(".lock")) continue;
        if (!name.endsWith(`_${todayStr}.lock`)) {
          try {
            unlinkSync(path.join(dir, name));
          } catch {
            // Best-effort cleanup.
          }
        }
      }
    } catch {
      // Ignore missing/unreadable marker dir.
    }
  }

  async function checkExpiringAccountsAndAlert(snapshotInput) {
    const snapshot = snapshotInput || syncAccounts();
    const { nowMs: currentNowMs, nowSec, orderedAccounts } = snapshot;
    const todayStr = shanghaiDateKey(currentNowMs);
    const alerted = [];

    for (const acc of orderedAccounts) {
      if (!acc.userId || !(acc.exp > 0)) continue;
      const remainingSeconds = acc.exp - nowSec;
      if (remainingSeconds >= EXPIRY_ALERT_WINDOW_SECONDS) continue;
      // Stop daily reminders once an account has been expired for more than 7 days.
      if (remainingSeconds < -EXPIRY_ALERT_GRACE_SECONDS) continue;

      const latestState = loadState();
      if (latestState.accounts[acc.userId]?.last_expiry_alert_date === todayStr) continue;

      const dedupKey = `${acc.userId}:${todayStr}`;
      if (inFlightAlerts.has(dedupKey)) continue;
      inFlightAlerts.add(dedupKey);

      let markerClaimed = false;
      const markerPath = alertMarkerPath(acc.userId, todayStr);
      try {
        try {
          mkdirSync(alertMarkerDir(), { recursive: true, mode: 0o700 });
          const fd = openSync(markerPath, "wx", 0o600);
          closeSync(fd);
          markerClaimed = true;
        } catch (err) {
          if (err?.code === "EEXIST") {
            continue;
          }
          if (!warnedMarkerDirFailure) {
            warnedMarkerDirFailure = true;
            logWarning(
              `[alphaengine-mcp] unable to create daily alert marker (${err?.code || "fs_error"}), falling back to in-memory dedup`,
            );
          }
        }

        const label = accountDisplayLabel(acc);
        const message = `请在 AlphaEngine App 里登录账号 ${label}`;
        let deliveryOutcome = "failed";
        try {
          const result = await sendOpsAlert({
            code: "alphaengine_token_expiring",
            detail: label,
            message,
            userId: acc.userId,
          });
          if (result === true || result?.ok === true || result?.outcome === "sent") {
            deliveryOutcome = "sent";
          } else if (result?.outcome === "uncertain") {
            deliveryOutcome = "uncertain";
          }
        } catch (err) {
          if (err?.name === "TimeoutError" || err?.name === "AbortError" || err?.code === "ETIMEDOUT") {
            deliveryOutcome = "uncertain";
          }
        }

        if (deliveryOutcome === "sent") {
          const freshState = loadState();
          const record = freshState.accounts[acc.userId] || sanitizeAccountRecord(acc.userId, acc);
          record.last_expiry_alert_date = todayStr;
          freshState.accounts[acc.userId] = record;
          saveState(freshState);
          if (markerClaimed) pruneOldAlertMarkers(todayStr);
          alerted.push({ user_id: acc.userId, label, date: todayStr });
        } else if (deliveryOutcome === "uncertain") {
          // Gateway timed out (delivery result unknown): keep today's marker to prevent duplicate alerts.
          if (markerClaimed) pruneOldAlertMarkers(todayStr);
        } else if (markerClaimed) {
          try {
            unlinkSync(markerPath);
          } catch {
            // Best-effort cleanup only on definitive complete failure.
          }
        }
      } finally {
        inFlightAlerts.delete(dedupKey);
      }
    }
    return alerted;
  }

  async function executeWithFailover(quotaType, invokeWithToken) {
    const requestStartMs = nowMs();
    const statusInfo = statusSummary();
    const snapshot = statusInfo._snapshot;
    const secrets = collectAllSecrets(snapshot);

    void checkExpiringAccountsAndAlert(snapshot).catch(() => {});

    const { validAccounts, available } = selectCandidates(snapshot, quotaType);
    if (!validAccounts.length) {
      throw new Error(`${statusInfo.error}。${statusInfo.hint}`);
    }
    if (!available.length) {
      const quotaLabel = quotaType === "read" ? "阅读额度" : "检索额度";
      throw new Error(`E_UPSTREAM_API: 额度已达上限（所有已保存 AlphaEngine 账号当前${quotaLabel}均处于冷却期，quota_exceeded）`);
    }

    const primary = available[0];
    try {
      const result = await invokeWithToken(primary.token, primary, secrets);
      clearQuotaExhausted(primary.userId, quotaType, requestStartMs);
      return result;
    } catch (primaryErr) {
      const safePrimaryErr = primaryErr instanceof Error ? primaryErr : new Error(String(primaryErr));
      safePrimaryErr.message = redactSecrets(safePrimaryErr.message, secrets);

      const errKind = classifyUpstreamError(safePrimaryErr);
      if (errKind === "none") {
        throw safePrimaryErr;
      }

      if (errKind === "quota") {
        markQuotaExhausted(primary.userId, quotaType, nowMs());
      }

      // Re-evaluate retry candidates against current marks and 30s expiry buffer.
      const retrySnapshot = syncAccounts();
      const retrySecrets = collectAllSecrets(retrySnapshot);
      const { available: retryAvailable } = selectCandidates(retrySnapshot, quotaType);
      const fallback = retryAvailable.find((acc) => acc.userId && acc.userId !== primary.userId);
      if (!fallback) {
        throw safePrimaryErr;
      }

      try {
        const retryResult = await invokeWithToken(fallback.token, fallback, retrySecrets);
        clearQuotaExhausted(fallback.userId, quotaType, requestStartMs);
        return retryResult;
      } catch (fallbackErr) {
        const safeFallbackErr = fallbackErr instanceof Error ? fallbackErr : new Error(String(fallbackErr));
        safeFallbackErr.message = redactSecrets(safeFallbackErr.message, retrySecrets);
        const fallbackKind = classifyUpstreamError(safeFallbackErr);
        if (fallbackKind === "quota") {
          markQuotaExhausted(fallback.userId, quotaType, nowMs());
          // Return the original error when both accounts fail.
          throw safePrimaryErr;
        }
        if (errKind === "ambiguous_retry") {
          throw safePrimaryErr;
        }
        throw safeFallbackErr;
      }
    }
  }

  return {
    syncAccounts,
    statusSummary,
    checkExpiringAccountsAndAlert,
    executeWithFailover,
    loadState,
    getKnownSecrets: () => [...ephemeralSecrets],
  };
}

const defaultAccountManager = createAccountManager();

async function fetchWithToken(apiPath, options = {}, token, knownSecrets = []) {
  const { quotaType: _quotaType, ...fetchOptions } = options;
  const secrets = [token, ...knownSecrets].filter(Boolean);
  // Additional fixed, read-only dictionary endpoints live under kmpadmin/kmpstock.
  const url = apiPath.startsWith("/api/v1/") ? `https://www.alphaengine.top${apiPath}` : `${API_BASE}${apiPath}`;
  const response = await fetch(url, {
    ...fetchOptions,
    headers: {
      Accept: "application/json, text/plain, */*",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
      Authorization: `Bearer ${token}`,
      Origin: "https://www.alphaengine.top",
      Referer: "https://www.alphaengine.top/",
      "User-Agent": `AlphaEngine-MCP/${VERSION}`,
      ...(fetchOptions.body ? { "Content-Type": "application/json" } : {}),
      ...(fetchOptions.headers || {}),
    },
    signal: fetchOptions.signal || AbortSignal.timeout(120000),
  });
  const responseText = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(responseText);
  } catch {
    const err = new Error(redactSecrets(`E_UPSTREAM_RESPONSE: HTTP ${response.status}, non-JSON response`, secrets));
    err.httpStatus = response.status;
    throw err;
  }
  const bodyStatusCode = firstStatusCode(parsed?.status_code, parsed?.error?.status_code);
  if (!response.ok || bodyStatusCode >= 400) {
    const effectiveStatus = bodyStatusCode >= 400 ? bodyStatusCode : response.status;
    const snippet = formatSanitizedErrorSnippet(parsed, secrets, 600);
    const err = new Error(`E_UPSTREAM_HTTP: HTTP ${effectiveStatus}: ${snippet}`);
    err.httpStatus = effectiveStatus;
    err.upstreamBody = sanitizeErrorValue(parsed, secrets);
    throw err;
  }
  if (parsed?.success === false || (parsed?.code && ![0, 200, "0", "200"].includes(parsed.code))) {
    const rawMsg = parsed.message || parsed.msg;
    const snippet =
      typeof rawMsg === "string" && rawMsg
        ? redactSecrets(rawMsg, secrets).slice(0, 600)
        : formatSanitizedErrorSnippet(parsed, secrets, 600);
    const err = new Error(`E_UPSTREAM_API: ${snippet}`);
    err.httpStatus = response.status;
    err.upstreamBody = sanitizeErrorValue(parsed, secrets);
    throw err;
  }
  if (parsed && typeof parsed === "object") {
    const structuredKind = classifyUpstreamError({ httpStatus: response.status, upstreamBody: parsed });
    if (structuredKind !== "none") {
      const snippet = formatSanitizedErrorSnippet(parsed, secrets, 600);
      const err = new Error(`E_UPSTREAM_API: ${snippet}`);
      err.httpStatus = response.status;
      err.upstreamBody = sanitizeErrorValue(parsed, secrets);
      throw err;
    }
  }
  return parsed?.data ?? parsed?.body ?? parsed;
}

async function apiRequest(apiPath, options = {}, manager = defaultAccountManager) {
  const quotaType = options.quotaType || resolveEndpointQuotaType(apiPath);
  return manager.executeWithFailover(quotaType, (token, _acc, secrets) =>
    fetchWithToken(apiPath, options, token, secrets),
  );
}

function requireDatePair(dateFrom, dateTo) {
  if (Boolean(dateFrom) !== Boolean(dateTo)) {
    throw new Error("E_BAD_DATE_RANGE: date_from 和 date_to 必须成对提供");
  }
  for (const value of [dateFrom, dateTo]) {
    if (value && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(`${value}T00:00:00Z`)) || new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value)) {
      throw new Error(`E_BAD_DATE: ${value}，格式应为 YYYY-MM-DD`);
    }
  }
  if (dateFrom && dateFrom > dateTo) throw new Error("E_BAD_DATE_RANGE: date_from 不能晚于 date_to");
}

function dateToMillis(value, endOfDay = false) {
  if (!value) return null;
  const suffix = endOfDay ? "T23:59:59.999+08:00" : "T00:00:00.000+08:00";
  return Date.parse(`${value}${suffix}`);
}

function normalizeText(value) {
  return String(value || "")
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase("en-US")
    .replace(/\s+/g, " ");
}

function asArray(value) {
  if (value === undefined || value === null || value === "") return [];
  return Array.isArray(value) ? value : [value];
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function truncate(value, maxChars) {
  if (typeof value !== "string") return value;
  return value.length > maxChars ? `${value.slice(0, maxChars)}…` : value;
}

function cleanValue(value, maxChars = 3000) {
  if (typeof value !== "string") return value;
  if (value.startsWith("data:image/") || value.length > 100000) return undefined;
  return truncate(value, maxChars);
}

function flattenInstitutions(data) {
  const institutions = [];
  if (Array.isArray(data?.focus)) institutions.push(...data.focus);
  for (const group of data?.general || []) {
    if (Array.isArray(group?.institution)) institutions.push(...group.institution);
  }
  const byId = new Map();
  for (const item of institutions) {
    const id = item?.institution_id || item?.id;
    const name = item?.institution_name || item?.name;
    if (id && name) byId.set(id, { institution_id: id, institution_name: name });
  }
  return [...byId.values()];
}

async function institutionCatalog(request = apiRequest) {
  const previous = cached("institutions");
  if (previous) return previous;
  return setCached("institutions", flattenInstitutions(await request("/summary/institution/list")));
}

async function resolveSourceNames(sourceNames, request = apiRequest) {
  const requested = unique(asArray(sourceNames).map(String));
  if (!requested.length) return { ids: [], names: [] };
  const catalog = await institutionCatalog(request);
  const ids = [];
  const names = [];
  for (const requestedName of requested) {
    const canonicalName = SOURCE_ALIASES[requestedName] || requestedName;
    const normalized = normalizeText(canonicalName);
    const exact = catalog.filter((item) => normalizeText(item.institution_name) === normalized);
    if (exact.length) {
      exact.forEach((item) => ids.push(item.institution_id));
      names.push(...exact.map((item) => item.institution_name));
      continue;
    }
    const partial = catalog.filter((item) => normalizeText(item.institution_name).includes(normalized));
    if (partial.length === 1) {
      ids.push(partial[0].institution_id);
      names.push(partial[0].institution_name);
      continue;
    }
    const hint = partial.slice(0, 8).map((item) => item.institution_name);
    throw new Error(
      `E_SOURCE_NOT_RESOLVED: 无法唯一匹配来源“${requestedName}”` +
        (hint.length ? `；候选：${hint.join("、")}` : "；可先调用 list_filters(filter_type=sources)"),
    );
  }
  return { ids: unique(ids), names: unique(names) };
}

function flattenTypeTree(nodes, depth = 0, output = []) {
  for (const node of nodes || []) {
    output.push({
      type_id: node.type_id,
      type_name: node.type_name,
      type_name_en: node.type_name_en,
      parent_type_id: node.parent_type_id,
      depth,
    });
    flattenTypeTree(node.child, depth + 1, output);
  }
  return output;
}

async function typeCatalog(code, request = apiRequest) {
  const safeCode = String(code || "composite");
  if (!/^[A-Za-z0-9_-]+$/.test(safeCode)) throw new Error("E_BAD_CODE: 非法文档 code");
  const key = `types:${safeCode}`;
  const previous = cached(key);
  if (previous) return previous;
  const tree = await request(`/summary/type/list/${encodeURIComponent(safeCode)}`);
  return setCached(key, flattenTypeTree(tree));
}

function expandDocumentCodes(categories, rawCodes) {
  const codes = [...asArray(rawCodes).map(String)];
  for (const category of asArray(categories).map(String)) {
    const definition = CATEGORY_DEFS[category];
    if (!definition) {
      throw new Error(
        `E_BAD_CATEGORY: ${category}；可选值：${Object.keys(CATEGORY_DEFS).join(", ")}`,
      );
    }
    codes.push(...definition.codes);
  }
  return unique(codes);
}

function parseCursor(cursor) {
  if (cursor === undefined || cursor === null || cursor === "") {
    return { localToken: null, upstreamCursor: null };
  }
  if (Array.isArray(cursor)) return { localToken: null, upstreamCursor: cursor };
  if (typeof cursor === "string") {
    if (cursor.startsWith("ae1:")) return { localToken: cursor, upstreamCursor: null };
    try {
      const parsed = JSON.parse(cursor);
      if (Array.isArray(parsed)) return { localToken: null, upstreamCursor: parsed };
    } catch {
      // handled below
    }
  }
  throw new Error("E_BAD_CURSOR: cursor 应为 search_library 返回的字符串，或兼容的上游数组游标");
}

function pruneSearchCursorStates() {
  const now = Date.now();
  for (const [token, state] of searchCursorStates) {
    if (now - state.createdAt > SEARCH_CURSOR_TTL_MS) searchCursorStates.delete(token);
  }
}

function saveSearchCursorState(state) {
  pruneSearchCursorStates();
  const token = `ae1:${randomUUID()}`;
  searchCursorStates.set(token, { ...state, createdAt: Date.now() });
  return token;
}

function takeSearchCursorState(token, signature) {
  pruneSearchCursorStates();
  const state = searchCursorStates.get(token);
  if (!state) throw new Error("E_CURSOR_EXPIRED: 搜索游标不存在或已超过 15 分钟，请重新检索");
  if (state.signature !== signature) {
    throw new Error("E_CURSOR_FILTER_MISMATCH: 翻页时 query 与筛选条件必须保持不变");
  }
  return state;
}

function searchSignature(filters) {
  return createHash("sha256").update(JSON.stringify(filters)).digest("hex");
}

function itemTimeMillis(item) {
  const value = item?.rank_date || item?.publish_time;
  if (!value) return 0;
  const normalized = String(value).includes("T") ? String(value) : String(value).replace(" ", "T");
  const withZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized) ? normalized : `${normalized}+08:00`;
  return Date.parse(withZone) || 0;
}

function itemSources(item) {
  return unique(
    asArray(item?.institution_name || item?.institution)
      .flatMap((value) => (typeof value === "string" ? [value] : [])),
  );
}

function itemMarkets(item) {
  const explicit = asArray(item?.market_list).map(String);
  const companyList = asArray(item?.company_list)
    .map((company) => company?.market || company?.market_type)
    .filter(Boolean);
  return unique([...explicit, ...companyList]);
}

function sourceAttribution(item) {
  const source = itemSources(item).join(" / ") || "AlphaEngine";
  const date = String(item?.publish_time || item?.rank_date || "").slice(0, 10) || "日期未知";
  const title = item?.title || item?.search_title || "未命名文档";
  const docId = item?.doc_id || item?.id || item?.document_id || "unknown";
  return {
    markdown: `**${source}**, “${title}” (${date}; AlphaEngine doc_id: ${docId})`,
  };
}

function compactSearchResult(item, includeSnippet) {
  const result = {
    doc_id: item?.doc_id || item?.id || item?.document_id,
    title: item?.title || item?.search_title,
    publish_time: item?.publish_time,
    rank_date: item?.rank_date,
    sources: itemSources(item),
    document_code: item?.code,
    type_id: item?.type_id || item?.document_type_id,
    type_name: item?.type_name || item?.type_show_name || item?.type_full_name,
    publish_date: String(item?.publish_time || "").slice(0, 10),
    companies: unique(asArray(item?.company_name).map(String)),
    industries: unique(asArray(item?.industry_name).map(String)),
    markets: itemMarkets(item),
    source_attribution: sourceAttribution(item),
  };
  // Keep provenance and classification separate. An institutional meeting still
  // returns code=summary; institution_name describes the broker, not its owner.
  for (const key of ["summary_id", "title_cn", "institution_name", "institution_id", "document_type_id",
    "document_type_name", "type_show_name", "type_full_id", "type_full_name", "first_type_name",
    "institution_meeting_flag", "personal_meeting_flag", "owner_institution_id", "belong_wechat_group",
    "appointment_person_id", "doc_icon", "page_num", "author_list", "company_code", "company_show_name",
    "market_list", "rank_date", "read", "detail", "state", "realtime", "sensitive_permission", "label"] ) {
    if (Object.hasOwn(item, key)) result[key] = item[key];
  }
  if (includeSnippet !== false) {
    result.snippet = truncate(item?.doc_introduce || item?.digest || "", 1800);
  }
  return Object.fromEntries(
    Object.entries(result).filter(([, value]) => value !== undefined && value !== ""),
  );
}

function resultMatches(item, filters) {
  const publishDate = String(item?.publish_time || "").slice(0, 10);
  if (filters.dateFrom && publishDate && publishDate < filters.dateFrom) return false;
  if (filters.dateTo && publishDate && publishDate > filters.dateTo) return false;
  if (filters.scope === "institution" && item?.institution_meeting_flag !== true) return false;
  if (filters.scope === "personal" && item?.personal_meeting_flag !== true) return false;
  if (filters.codes.length && !filters.codes.includes("composite") && !filters.codes.includes(item?.code)) return false;
  if (filters.typeIds.length) {
    const typeId = item?.type_id || item?.document_type_id;
    const ancestors = String(item?.type_full_id || "").split(":");
    if (!filters.typeIds.some((id) => id === typeId || ancestors.includes(id))) return false;
  }
  if (filters.sourceNames.length) {
    const sources = itemSources(item).map(normalizeText);
    if (!filters.sourceNames.some((name) => sources.includes(normalizeText(name)))) return false;
  }
  if (filters.markets.length) {
    const markets = itemMarkets(item);
    if (markets.length && !filters.markets.some((market) => markets.includes(market))) return false;
  }
  return true;
}

async function searchLibrary(args, request = apiRequest) {
  const query = String(args.query || "").trim();
  requireDatePair(args.date_from, args.date_to);
  const limit = Math.max(1, Math.min(Number(args.limit || 10), 50));
  const typeIds = unique(asArray(args.type_ids).map(String));
  const requestedCodes = expandDocumentCodes(args.document_categories, args.document_codes);
  const scopeCodes = { institutionMeeting: "institution", personalMeeting: "personal" };
  const requestedScopes = unique([args.meeting_scope === "all" ? null : args.meeting_scope,
    ...requestedCodes.map((code) => scopeCodes[code])]);
  if (requestedScopes.length > 1) throw new Error("E_SCOPE_CONFLICT: 机构与个人会议请分别检索");
  const scope = requestedScopes[0] || "all";
  if (!["all", "institution", "personal"].includes(scope)) throw new Error("E_BAD_SCOPE: meeting_scope 应为 all、institution 或 personal");
  let codes = requestedCodes.filter((code) => !scopeCodes[code]);
  if (scope !== "all" && codes.some((code) => !["summary", "composite"].includes(code))) {
    throw new Error("E_SCOPE_CONFLICT: 机构/个人会议范围不能与非会议文档类别混用");
  }
  if (scope !== "all") codes = ["summary"];
  const scopeCode = Object.keys(scopeCodes).find((code) => scopeCodes[code] === scope);
  const sourceResolution = await resolveSourceNames(args.source_names, request);
  const sourceIds = unique([...sourceResolution.ids, ...asArray(args.source_ids).map(String)]);
  const sourceNames = sourceResolution.names;
  const markets = unique(asArray(args.markets).map(String));
  const effectiveCode = scopeCode || (codes.length === 1 ? codes[0] : "composite");
  const requestCodes = scopeCode ? [scopeCode] : codes;
  const documentTypeList = requestCodes.length
    ? requestCodes.map((code) => ({ code, type_ids: typeIds }))
    : [{ code: "composite", type_ids: typeIds }];
  const sort = args.sort || (query ? "relevance" : "newest");
  if (query && sort === "newest") throw new Error("E_QUERY_SORT: 有关键词的主题检索请用 sort=relevance；无关键词列表可用 newest");
  if (!["relevance", "newest"].includes(sort)) throw new Error("E_BAD_SORT: sort 应为 relevance 或 newest");
  const orderBy = sort === "newest" ? "-time" : "comprehensive";
  const filters = {
    query,
    date_from: args.date_from || null,
    date_to: args.date_to || null,
    source_names: sourceNames,
    source_ids: sourceIds,
    document_categories: asArray(args.document_categories),
    document_codes: codes,
    type_ids: typeIds,
    markets,
    industry_names: unique(asArray(args.industry_names).map(String)),
    optional_only: Boolean(args.optional_only),
    meeting_scope: scope,
    search_in: args.search_in || "full_text",
    company_names: unique(asArray(args.company_names).map(String)),
    company_short_names: unique(asArray(args.company_short_names).map(String)),
    stock_group_ids: unique(asArray(args.stock_group_ids).map(String)),
    first_regions: unique(asArray(args.first_regions).map(String)),
    second_regions: unique(asArray(args.second_regions).map(String)),
    own_reservations_only: Boolean(args.own_reservations_only),
    sort,
  };
  if (!["full_text", "title"].includes(filters.search_in)) throw new Error("E_BAD_SEARCH_IN: search_in 应为 full_text 或 title");
  if (filters.own_reservations_only && scope !== "institution") throw new Error("E_BAD_SCOPE: own_reservations_only 仅用于机构会议");
  const signature = searchSignature(filters);
  const parsedCursor = parseCursor(args.cursor);
  let cursor = parsedCursor.upstreamCursor;
  let upstreamTotal = null;
  let upstreamHasMore = false;
  let buffer = [];
  let seen = new Set();
  let mustFetchInitialPage = true;
  let distinctIds = [];
  let upstreamPages = 0;

  if (parsedCursor.localToken) {
    const state = takeSearchCursorState(parsedCursor.localToken, signature);
    cursor = state.upstreamCursor;
    upstreamTotal = state.upstreamTotal;
    upstreamHasMore = state.upstreamHasMore;
    buffer = [...state.pending];
    seen = new Set(state.seen);
    distinctIds = state.distinctIds || [];
    mustFetchInitialPage = false;
  }

  for (let page = 0; page < 5 && buffer.length < limit; page += 1) {
    if (!mustFetchInitialPage && (!upstreamHasMore || !cursor)) break;
    const payload = {
      query,
      code: effectiveCode,
      type_id: typeIds,
      institution_id: sourceIds,
      doc_type_list: documentTypeList,
      industry_name: unique(asArray(args.industry_names).map(String)),
      query_type: filters.search_in === "title" ? "1" : "2",
      company_short_name: filters.company_short_names,
      company_name: filters.company_names,
      market_list: markets,
      stock_group_ids: filters.stock_group_ids,
      is_optional: Boolean(args.optional_only),
      start_time: dateToMillis(args.date_from, false),
      end_time: dateToMillis(args.date_to, true),
      size: Math.max(limit, 20),
      search_after: cursor,
      order_by: orderBy,
      distinct_ids: distinctIds,
      first_region: filters.first_regions,
      second_region: filters.second_regions,
      ...(filters.own_reservations_only ? { own_subscribe_flag: true } : {}),
    };
    const data = await request("/summary/search/list/new", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    upstreamPages += 1;
    const results = data?.results || data?.list || (Array.isArray(data) ? data : []);
    upstreamTotal ??= data?.total ?? results.length;
    for (const item of results) {
      const docId = item?.doc_id || item?.id || item?.document_id;
      if (docId && seen.has(docId)) continue;
      if (!resultMatches(item, {
        dateFrom: args.date_from,
        dateTo: args.date_to,
        codes,
        typeIds,
        sourceNames,
        markets,
        scope,
      })) continue;
      if (docId) seen.add(docId);
      buffer.push(item);
    }
    const nextUpstreamCursor = data?.search_after ?? data?.searchAfter ?? null;
    upstreamHasMore = Boolean(data?.has_next_page ?? data?.hasNextPage);
    if (upstreamHasMore && (!nextUpstreamCursor || JSON.stringify(nextUpstreamCursor) === JSON.stringify(cursor))) {
      throw new Error("E_UPSTREAM_CURSOR: 上游声明还有数据但未推进游标；不能把部分结果当作全量");
    }
    cursor = nextUpstreamCursor;
    distinctIds = results.map((item) => item.id || item.doc_id).filter(Boolean);
    mustFetchInitialPage = false;
    if (!upstreamHasMore || !cursor) break;
  }

  // Keep upstream rank ordering and pagination boundaries intact. Re-sorting a
  // single page by publish_time can move newer institutional recordings behind old ones.
  const collected = buffer.splice(0, limit);
  const hasMore = buffer.length > 0 || Boolean(upstreamHasMore && cursor);
  const nextCursor = hasMore
    ? saveSearchCursorState({
        signature,
        pending: buffer,
        upstreamCursor: cursor,
        upstreamHasMore,
        upstreamTotal,
        seen: [...seen],
        distinctIds,
      })
    : null;

  return {
    query,
    filters,
    total_upstream: upstreamTotal,
    returned: collected.length,
    results: collected.map((item) => compactSearchResult(item, args.include_snippets)),
    cursor: nextCursor,
    has_more: hasMore,
    upstream_pages_fetched: upstreamPages,
    pagination_note: "继续使用 cursor，直到 has_more=false；total_upstream 是上游匹配数，可能含被本地精确条件过滤的记录。",
    next_step: "对需要深入引用的结果调用 get_document(doc_id, offset, max_chars)。",
  };
}

function selectDocumentText(detail, field = "auto") {
  if (field !== "auto") return typeof detail?.[field] === "string" ? detail[field] : "";
  const candidates = [
    detail?.content,
    detail?.original_text,
    detail?.full_content,
    detail?.text,
    detail?.markdown,
  ];
  return candidates.find((value) => typeof value === "string" && value.length) || "";
}

async function fetchDocumentDetail(docId, mode = "auto", request = apiRequest) {
  const encoded = encodeURIComponent(docId);
  const cacheKey = `document:${mode}:${docId}`;
  const previous = cached(cacheKey);
  if (previous) return previous;
  if (mode === "private") {
    return setCached(cacheKey, await request(`/summary/private/detail/${encoded}`, { quotaType: "read" }));
  }
  if (mode === "public") {
    return setCached(
      cacheKey,
      await request(`/summary/detail/${encoded}/true?system=false`, { quotaType: "read" }),
    );
  }
  try {
    return setCached(
      cacheKey,
      await request(`/summary/detail/${encoded}/true?system=false`, { quotaType: "read" }),
    );
  } catch (publicError) {
    // A quota/permission/authentication refusal is not a reason to retry through
    // a different document route, and may charge another read.
    if (/额度|权限|登录|token|HTTP (?:401|403|429)/i.test(String(publicError.message))) throw publicError;
    try {
      return setCached(cacheKey, await request(`/summary/private/detail/${encoded}`, { quotaType: "read" }));
    } catch {
      throw publicError;
    }
  }
}

function documentMetadata(detail) {
  const metadata = {
    ...compactSearchResult(detail, false),
    doc_id: detail?.doc_id || detail?.id,
    title: detail?.title || detail?.search_title,
    publish_time: detail?.publish_time,
    rank_date: detail?.rank_date,
    sources: itemSources(detail),
    document_code: detail?.code,
    type_id: detail?.type_id || detail?.document_type_id,
    type_name: detail?.type_name || detail?.type_show_name || detail?.type_full_name,
    companies: unique(asArray(detail?.company_name).map(String)),
    industries: unique(asArray(detail?.industry_name).map(String)),
    authors: unique(asArray(detail?.author_list).map((author) =>
      typeof author === "string" ? author : author?.name || author?.author_name,
    )),
    file_format: detail?.file_format,
    page_num: detail?.page_num,
    digest: cleanValue(detail?.digest || detail?.digest_cn, 5000),
    main_point: cleanValue(detail?.main_point, 5000),
    question_answer: cleanValue(detail?.question_answer, 5000),
    attachment_available: Boolean(detail?.file_path || detail?.original_pdf_path || detail?.attachment_path),
    source_attribution: sourceAttribution(detail),
  };
  return Object.fromEntries(
    Object.entries(metadata).filter(([, value]) => value !== undefined && value !== ""),
  );
}

async function getDocument(args) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  const mode = args.mode || "auto";
  if (!["auto", "public", "private"].includes(mode)) {
    throw new Error("E_BAD_MODE: mode 应为 auto、public 或 private");
  }
  const contentField = args.content_field || "auto";
  if (!["auto", "original_text", "content", "digest", "digest_cn", "main_point", "question_answer"].includes(contentField)) {
    throw new Error("E_BAD_CONTENT_FIELD: 不支持的正文字段");
  }
  const detail = await fetchDocumentDetail(String(args.doc_id), mode);
  const text = selectDocumentText(detail, contentField);
  const availableFields = Object.fromEntries(["content", "original_text", "full_content", "text", "markdown", "digest", "digest_cn", "main_point", "question_answer"]
    .filter((key) => typeof detail?.[key] === "string" && detail[key].length).map((key) => [key, detail[key].length]));
  const offset = Math.max(0, Number(args.offset || 0));
  const maxChars = Math.max(1, Math.min(Number(args.max_chars || 12000), 30000));
  const textChunk = text.slice(offset, offset + maxChars);
  return {
    metadata: documentMetadata(detail),
    content_field: contentField,
    available_content_fields: availableFields,
    ...(text ? {} : { warning: "所选正文字段为空；请查看 available_content_fields，不能据此认定整篇文档为空。" }),
    content_chars: text.length,
    content_sha256: text ? createHash("sha256").update(text).digest("hex") : null,
    offset,
    returned_chars: textChunk.length,
    text: textChunk,
    next_offset: offset + textChunk.length < text.length ? offset + textChunk.length : null,
    complete: offset === 0 && textChunk.length === text.length,
  };
}

function collectCandidateUrls(value, keyPath = "", output = []) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => collectCandidateUrls(item, `${keyPath}[${index}]`, output));
  } else if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      collectCandidateUrls(child, keyPath ? `${keyPath}.${key}` : key, output);
    }
  } else if (
    typeof value === "string" &&
    /^https?:\/\//i.test(value) &&
    /(file|path|url|download|pdf|attachment)/i.test(keyPath)
  ) {
    output.push({ key: keyPath, url: value });
  }
  return output;
}

function extensionFor(contentType, url, bytes) {
  if (/pdf/i.test(contentType) || bytes.subarray(0, 5).toString("ascii") === "%PDF-") return ".pdf";
  if (/wordprocessingml/i.test(contentType) || /\.docx(?:$|\?)/i.test(url)) return ".docx";
  if (/msword/i.test(contentType) || /\.doc(?:$|\?)/i.test(url)) return ".doc";
  if (/spreadsheetml/i.test(contentType) || /\.xlsx(?:$|\?)/i.test(url)) return ".xlsx";
  if (/ms-excel/i.test(contentType) || /\.xls(?:$|\?)/i.test(url)) return ".xls";
  if (/presentationml/i.test(contentType) || /\.pptx(?:$|\?)/i.test(url)) return ".pptx";
  if (/ms-powerpoint/i.test(contentType) || /\.ppt(?:$|\?)/i.test(url)) return ".ppt";
  if (/text\/plain/i.test(contentType) || /\.txt(?:$|\?)/i.test(url)) return ".txt";
  return ".bin";
}

function documentFileCandidates(value) {
  return collectCandidateUrls(value).filter((item) =>
    /\.(?:pdf|docx?|xlsx?|pptx?|txt)(?:$|\?)/i.test(item.url) ||
    (/original_pdf_path|file_path/i.test(item.key) && !/\.(?:mp[34]|wav|m4a|mov|png|jpe?g|gif|webp)(?:$|\?)/i.test(item.url)));
}

async function attachmentRows(docId, request = apiRequest) {
  const data = await request(`/summary/attachment/list/${encodeURIComponent(docId)}`);
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.list)) return data.list;
  throw new Error("E_UPSTREAM_ATTACHMENTS: 附件列表格式异常");
}

async function listAttachments(args, request = apiRequest) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  const rows = await attachmentRows(args.doc_id, request);
  return { doc_id: args.doc_id, returned: rows.length, attachments: rows.map((row, index) => ({
    attachment_index: index, ...safeMetadata(row), downloadable_document: documentFileCandidates(row).length > 0,
  })), next_step: "下载选定文档附件用 download_document(doc_id, attachment_index)；不返回签名地址。" };
}

async function downloadDocument(args, request = apiRequest) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  if (args.attachment_index !== undefined && (!Number.isInteger(args.attachment_index) || args.attachment_index < 0)) throw new Error("E_BAD_ATTACHMENT: attachment_index 须为非负整数");
  const isAttachment = args.attachment_index !== undefined;
  const detail = isAttachment ? (await attachmentRows(String(args.doc_id), request))[args.attachment_index]
    : await fetchDocumentDetail(String(args.doc_id), args.mode || "auto", request);
  if (!detail) throw new Error("E_BAD_ATTACHMENT: 附件索引不存在，请重新 list_attachments");
  const candidates = documentFileCandidates(detail);
  const candidate =
    candidates.find((item) => /original_pdf_path|file_path/i.test(item.key) && /\.pdf(?:$|\?)/i.test(item.url)) ||
    candidates.find((item) => /\.pdf(?:$|\?)/i.test(item.url)) ||
    candidates[0];
  if (!candidate) throw new Error("E_FILE_NOT_FOUND: 文档详情没有可下载的原始文件地址");
  const response = await fetch(candidate.url, {
    headers: { "User-Agent": `AlphaEngine-MCP/${VERSION}` },
    redirect: "follow",
    signal: AbortSignal.timeout(120000),
  });
  if (!response.ok) throw new Error(`E_FILE_HTTP: HTTP ${response.status}`);
  const declaredSize = Number(response.headers.get("content-length") || 0);
  if (declaredSize > MAX_DOWNLOAD_BYTES) {
    throw new Error(`E_FILE_TOO_LARGE: 文件 ${declaredSize} bytes，超过 100MB 上限`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > MAX_DOWNLOAD_BYTES) throw new Error("E_FILE_TOO_LARGE: 文件超过 100MB 上限");
  const contentType = response.headers.get("content-type") || "application/octet-stream";
  const extension = extensionFor(contentType, candidate.url, bytes);
  const safeId = String(args.doc_id).replace(/[^A-Za-z0-9_-]/g, "_");
  const directory = path.resolve(WORKSPACE, "temp/alphaengine-mcp-downloads");
  await mkdir(directory, { recursive: true });
  const outputPath = path.join(directory, `${safeId}${isAttachment ? `-attachment-${args.attachment_index}` : ""}${extension}`);
  await writeFile(outputPath, bytes, { mode: 0o600 });
  return {
    doc_id: String(args.doc_id),
    title: detail?.title,
    local_path: outputPath,
    bytes: bytes.length,
    content_type: contentType,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    valid_pdf_signature: bytes.subarray(0, 5).toString("ascii") === "%PDF-",
    source_attribution: sourceAttribution(detail),
  };
}

function filterPage(items, args, key) {
  const offset = Math.max(0, Number(args.offset || 0));
  const limit = Math.max(1, Math.min(Number(args.limit || 50), 200));
  const selected = items.slice(offset, offset + limit);
  return { [key]: selected, total: items.length, offset, returned: selected.length,
    next_offset: offset + selected.length < items.length ? offset + selected.length : null };
}

function flattenDictionary(nodes, fields, depth = 0, output = []) {
  for (const node of nodes || []) {
    output.push({ ...Object.fromEntries(fields.filter((key) => Object.hasOwn(node, key)).map((key) => [key, node[key]])), depth });
    flattenDictionary(node.child, fields, depth + 1, output);
  }
  return output;
}

async function listFilters(args, request = apiRequest) {
  const filterType = args.filter_type || "categories";
  const requestedQuery = String(args.query || "");
  const query = normalizeText(SOURCE_ALIASES[requestedQuery] || requestedQuery);
  const limit = Math.max(1, Math.min(Number(args.limit || 50), 200));
  if (filterType === "categories") {
    return {
      categories: Object.entries(CATEGORY_DEFS).map(([id, definition]) => ({
        id,
        label: definition.label,
        document_codes: definition.codes,
      })),
    };
  }
  if (filterType === "sources") {
    const catalog = await institutionCatalog(request);
    return filterPage(catalog.filter((item) => !query || normalizeText(`${item.institution_name} ${item.institution_id}`).includes(query)), args, "sources");
  }
  if (filterType === "document_types") {
    const types = await typeCatalog(args.document_code || "composite", request);
    return {
      document_code: args.document_code || "composite",
      ...filterPage(types
        .filter((item) =>
          !query ||
          normalizeText(item.type_name).includes(query) ||
          normalizeText(item.type_name_en).includes(query) ||
          normalizeText(item.type_id).includes(query),
        ), args, "document_types"),
    };
  }
  if (filterType === "upstream_categories") {
    const items = await request("/summary/search/category");
    return filterPage(items.filter((item) => !query || normalizeText(`${item.key} ${item.value} ${item.value_en}`).includes(query)), args, "upstream_categories");
  }
  if (filterType === "industries") {
    const items = await request(`/summary/industry/list?code=${encodeURIComponent(args.document_code || "composite")}`);
    const flat = flattenDictionary(items, ["industry_id", "industry_name", "industry_name_en"]);
    return filterPage(flat.filter((item) => !query || normalizeText(JSON.stringify(item)).includes(query)), args, "industries");
  }
  if (filterType === "companies") {
    if (!requestedQuery.trim()) throw new Error("E_BAD_ARGS: 公司词典须提供 query（公司名称或代码）");
    const items = await request(`/api/v1/kmpadmin/dic/company/list?keyword=${encodeURIComponent(requestedQuery.trim())}`);
    return filterPage(items, args, "companies");
  }
  if (["watchlist_groups", "watchlist_stocks"].includes(filterType)) {
    if (filterType === "watchlist_stocks" && !args.group_id) throw new Error("E_BAD_ARGS: watchlist_stocks 须提供 watchlist_groups 返回的 group_id");
    const apiPath = filterType === "watchlist_groups" ? "/api/v1/kmpstock/stock/self/group/list"
      : `/api/v1/kmpstock/stock/self/stock/list/all/?groupId=${encodeURIComponent(args.group_id)}`;
    const items = await request(apiPath);
    if (!Array.isArray(items)) throw new Error("E_UPSTREAM_RESPONSE: 自选列表格式异常");
    return filterPage(items.filter((item) => !query || normalizeText(JSON.stringify(item)).includes(query)).map(safeMetadata), args, filterType);
  }
  if (filterType === "regions") {
    const flat = flattenDictionary(await request("/region/list"), ["id", "first_region", "second_region", "region_en"]);
    return filterPage(flat.filter((item) => !query || normalizeText(JSON.stringify(item)).includes(query)), args, "regions");
  }
  throw new Error("E_BAD_FILTER_TYPE: 不支持的词典，请查看 list_filters schema");
}

const RECORDING_STATES = { 1: "未开始", 2: "录制中", 3: "录制失败", 4: "转录中", 5: "转录成功" };
function recordingType(args) {
  const scope = args.meeting_scope || "institution";
  if (!["institution", "personal"].includes(scope)) throw new Error("E_BAD_SCOPE: 录制任务须选择 institution 或 personal");
  return scope === "institution" ? "institutionMeeting" : "personalMeeting";
}

// Metadata may contain signed media links or meeting passcodes; do not expose them.
function safeMetadata(value) {
  if (Array.isArray(value)) return value.map(safeMetadata);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !/(token|authorization|cookie|password|passcode|secret|logo)/i.test(key))
    .map(([key, child]) => [key, safeMetadata(child)]));
  if (typeof value === "string" && /^https?:\/\//i.test(value)) return "[link omitted]";
  return value;
}

async function listRecordingTasks(args, request = apiRequest) {
  const type = recordingType(args);
  requireDatePair(args.date_from, args.date_to);
  const page = Math.max(1, Number(args.page || 1));
  const size = Math.max(1, Math.min(Number(args.limit || 20), 100));
  const payload = { type, page_num: page, size, own_subscribe_flag: Boolean(args.own_reservations_only),
    optional_flag: Boolean(args.optional_only), query: String(args.query || ""),
    transcribe_state: args.states || [], institution_id: args.source_ids || [], industry_id: args.industry_ids || [],
    start_time: dateToMillis(args.date_from), end_time: dateToMillis(args.date_to, true) };
  const data = await request("/summary/search/booked/list", { method: "POST", body: JSON.stringify(payload) });
  const rows = (data.results || []).map((row) => ({ ...safeMetadata(row), state_name: RECORDING_STATES[row.transcribe_state] || "未知" }));
  const current = data.current_page_num ?? page;
  // The paged frontend uses total/current_page_num. has_next_page belongs to the
  // separate time-anchor cursor mode and is false even on page 1 of 1,879 tasks.
  const total = data.total;
  const more = Number.isFinite(total) ? current * size < total : rows.length === size;
  if (more && !rows.length) throw new Error("E_UPSTREAM_PAGE: 任务总数还有数据但当前页为空");
  return { meeting_scope: args.meeting_scope || "institution", total, page: current, limit: size, returned: rows.length,
    results: rows, has_more: more, next_page: more ? current + 1 : null,
    state_definitions: RECORDING_STATES,
    note: "录制预约任务（含未开始/失败/处理中），与已入库会议文档数量不同；正文用 search_library 的机构/个人会议范围查找。" };
}

async function getRecordingStatus(args, request = apiRequest, manager = defaultAccountManager) {
  const type = recordingType(args);
  // A quota error ends the pair at once (whatever the other request is
  // doing) so the failover marks this account and switches; otherwise both
  // settle and the error reported is quota, then ambiguous, then the first.
  const kindOf = classifyUpstreamError;
  const runPair = (invoke) =>
    new Promise((resolve, reject) => {
      const calls = [
        invoke(`/summary/search/booked/count/list?type=${type}`),
        invoke("/summary/search/booked/preview", { method: "POST", body: JSON.stringify({ type }) }),
      ];
      for (const call of calls) {
        call.catch((err) => {
          if (kindOf(err) === "quota") reject(err);
        });
      }
      Promise.allSettled(calls).then((settled) => {
        const failures = settled.filter((r) => r.status === "rejected").map((r) => r.reason);
        if (!failures.length) return resolve(settled.map((r) => r.value));
        reject(
          failures.find((err) => kindOf(err) === "quota") ||
            failures.find((err) => kindOf(err) !== "none") ||
            failures[0],
        );
      });
    });
  const [counts, preview] =
    request === apiRequest
      ? await manager.executeWithFailover("search", (token, _acc, secrets) =>
          runPair((apiPath, opts = {}) => fetchWithToken(apiPath, opts, token, secrets)),
        )
      : await runPair(request);
  return { meeting_scope: args.meeting_scope || "institution", counts, preview: safeMetadata(preview),
    state_definitions: RECORDING_STATES, note: "counts 保留上游原字段；完整任务用 list_recording_tasks 分页查询。" };
}

async function getRecordingTask(args, request = apiRequest) {
  if (!args.roadshow_id) throw new Error("E_BAD_ARGS: roadshow_id 必填，取自 list_recording_tasks");
  const type = recordingType(args);
  const suffix = args.wechat_group ? `?wechatGroup=${encodeURIComponent(args.wechat_group)}` : "";
  return { meeting_scope: args.meeting_scope || "institution", roadshow_id: args.roadshow_id,
    detail: safeMetadata(await request(`/summary/search/reservation/info/${type}/${encodeURIComponent(args.roadshow_id)}${suffix}`)) };
}

async function getDocumentInfo(args, request = apiRequest) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  return { doc_id: args.doc_id, ...safeMetadata(await request(`/summary/doc/base?doc_id=${encodeURIComponent(args.doc_id)}`)),
    note: "仅基础类型/已读状态，不读取全文；完整文本使用 get_document。" };
}

async function authStatus(_args = {}, manager = defaultAccountManager) {
  const { _snapshot, ...publicStatus } = manager.statusSummary();
  void manager.checkExpiringAccountsAndAlert(_snapshot).catch(() => {});
  return {
    ...publicStatus,
    api_base: API_BASE,
    note: "Token 优先从运行中的 AlphaEngine Desktop 读取并按账号保存在 macOS 钥匙串；不写入明文状态文件、不写日志、不返回。",
  };
}

const TOOLS = {
  search_library: {
    description:
      "检索 AlphaEngine 投研文档库及本机构/个人录制会议。关键词用 relevance；无关键词列表用 newest。按 cursor 翻到 has_more=false 才算完成；来源券商与录制归属分开。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "全文关键词，如公司名、产品名或主题" },
        date_from: { type: "string", description: "起始发布日期 YYYY-MM-DD，须与 date_to 同时提供" },
        date_to: { type: "string", description: "结束发布日期 YYYY-MM-DD，须与 date_from 同时提供" },
        source_names: {
          type: "array",
          items: { type: "string" },
          description: "来源机构名称，如 Morgan Stanley、中信证券",
        },
        source_ids: { type: "array", items: { type: "string" } },
        document_categories: {
          type: "array",
          items: { type: "string", enum: Object.keys(CATEGORY_DEFS) },
          description: "友好文档分类；只搜卖方点评使用 sell_side_comment",
        },
        document_codes: {
          type: "array",
          items: { type: "string" },
          description: "AlphaEngine 原始大类 code，如 comment、chinaReport、foreignReport",
        },
        type_ids: {
          type: "array",
          items: { type: "string" },
          description: "细分文档类型 ID；可通过 list_filters(document_types) 查询",
        },
        markets: { type: "array", items: { type: "string", enum: ["A", "HK", "US"] } },
        industry_names: { type: "array", items: { type: "string" } },
        meeting_scope: { type: "string", enum: ["all", "institution", "personal"], default: "all", description: "institution=本机构录制，personal=本人录制；不按券商来源判断归属" },
        search_in: { type: "string", enum: ["full_text", "title"], default: "full_text" },
        company_names: { type: "array", items: { type: "string" }, description: "公司词典 company_name；list_filters(companies) 可按名称或代码解析" },
        company_short_names: { type: "array", items: { type: "string" } },
        stock_group_ids: { type: "array", items: { type: "string" } },
        first_regions: { type: "array", items: { type: "string" } },
        second_regions: { type: "array", items: { type: "string" } },
        own_reservations_only: { type: "boolean", default: false, description: "仅自己预约的机构会议；查整个机构请省略" },
        optional_only: { type: "boolean", default: false },
        sort: { type: "string", enum: ["relevance", "newest"], description: "有 query 默认 relevance；空 query 默认 newest，保留上游 rank_date 顺序" },
        cursor: {
          description:
            "直接传入上次 search_library 返回的 opaque cursor 字符串（ae1:...）；兼容旧版上游数组游标及其 JSON 字符串",
          anyOf: [{ type: "array" }, { type: "string" }, { type: "null" }],
        },
        limit: { type: "integer", minimum: 1, maximum: 50, default: 10 },
        include_snippets: { type: "boolean", default: true },
      },
      additionalProperties: false,
    },
    call: searchLibrary,
  },
  get_document: {
    description:
      "按 doc_id 读取完整文档正文。正文较长时用 next_offset 连续分段读取，content_sha256 用于校验各段属于同一版本。",
    inputSchema: {
      type: "object",
      properties: {
        doc_id: { type: "string" },
        mode: { type: "string", enum: ["auto", "public", "private"], default: "auto" },
        offset: { type: "integer", minimum: 0, default: 0 },
        max_chars: { type: "integer", minimum: 1, maximum: 30000, default: 12000 },
        content_field: { type: "string", enum: ["auto", "original_text", "content", "digest", "digest_cn", "main_point", "question_answer"], default: "auto", description: "正文/转录/摘要/要点/问答可分别分页；auto 保持既有正文优先顺序" },
      },
      required: ["doc_id"],
      additionalProperties: false,
    },
    call: getDocument,
  },
  download_document: {
    description:
      "下载原始 PDF/Office 文档到 OpenClaw workspace 临时目录；不暴露签名 URL。文本点评可能没有附件。",
    inputSchema: {
      type: "object",
      properties: {
        doc_id: { type: "string" },
        mode: { type: "string", enum: ["auto", "public", "private"], default: "auto" },
        attachment_index: { type: "integer", minimum: 0, description: "可选，list_attachments 返回的附件序号；省略则下载原始文档" },
      },
      required: ["doc_id"],
      additionalProperties: false,
    },
    call: downloadDocument,
  },
  list_filters: {
    description: "查询友好分类、当前账户上游分类、来源机构、细分类型、行业、公司、地区和自选股分组/成员。词典用 offset/next_offset 翻页；show 为上游账户可见状态。",
    inputSchema: {
      type: "object",
      properties: {
        filter_type: {
          type: "string",
          enum: ["categories", "upstream_categories", "sources", "document_types", "industries", "companies", "regions", "watchlist_groups", "watchlist_stocks"],
          default: "categories",
        },
        query: { type: "string", description: "名称或 ID 的模糊匹配词" },
        document_code: { type: "string", default: "composite" },
        group_id: { type: "string", description: "watchlist_stocks 所需的分组 ID，来自 watchlist_groups.id；可用于 search_library.stock_group_ids" },
        limit: { type: "integer", minimum: 1, maximum: 200, default: 50 },
        offset: { type: "integer", minimum: 0, default: 0 },
      },
      additionalProperties: false,
    },
    call: listFilters,
  },
  get_document_info: {
    description: "只查文档基础类型和已读状态，不取全文，便于先检查专家库类型再决定是否读取。",
    inputSchema: { type: "object", properties: { doc_id: { type: "string" } }, required: ["doc_id"], additionalProperties: false },
    call: getDocumentInfo,
  },
  list_attachments: {
    description: "列出文档补充附件及下载序号，不暴露签名地址；下载仍走 download_document。",
    inputSchema: { type: "object", properties: { doc_id: { type: "string" } }, required: ["doc_id"], additionalProperties: false },
    call: listAttachments,
  },
  list_recording_tasks: {
    description: "分页列出机构/个人会议预约及录制任务，含未开始、录制中、失败、转录中和成功；不是已入库纪要列表。使用 next_page 翻页。",
    inputSchema: { type: "object", properties: {
      meeting_scope: { type: "string", enum: ["institution", "personal"], default: "institution" },
      page: { type: "integer", minimum: 1, default: 1 },
      limit: { type: "integer", minimum: 1, maximum: 100, default: 20 },
      query: { type: "string" },
      states: { type: "array", items: { type: "integer", enum: [1, 2, 3, 4, 5] }, description: "1未开始/2录制中/3录制失败/4转录中/5转录成功" },
      date_from: { type: "string", description: "会议日期 YYYY-MM-DD，与 date_to 成对" },
      date_to: { type: "string" },
      source_ids: { type: "array", items: { type: "string" } },
      industry_ids: { type: "array", items: { type: "string" } },
      own_reservations_only: { type: "boolean", default: false },
      optional_only: { type: "boolean", default: false },
    }, additionalProperties: false },
    call: listRecordingTasks,
  },
  get_recording_status: {
    description: "查询机构/个人会议预约及处理状态计数和正在处理的会议预览，不取全文。",
    inputSchema: { type: "object", properties: {
      meeting_scope: { type: "string", enum: ["institution", "personal"], default: "institution" },
    }, additionalProperties: false },
    call: getRecordingStatus,
  },
  get_recording_task: {
    description: "按 list_recording_tasks 返回的 roadshow_id 查预约/录制信息；同会议多个微信群时传 wechat_group 消歧。",
    inputSchema: { type: "object", properties: {
      meeting_scope: { type: "string", enum: ["institution", "personal"], default: "institution" },
      roadshow_id: { type: "string" }, wechat_group: { type: "string" },
    }, required: ["roadshow_id"], additionalProperties: false },
    call: getRecordingTask,
  },
  auth_status: {
    description:
      "检查 AlphaEngine Desktop 与已保存多账号状态（含昵称、手机号后四位、过期时间、检索/阅读额度与当前账号）；不会返回 token。status 区分 ready / app_not_running / no_module_page / token_expired。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    call: authStatus,
  },
};

function rpcSuccess(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function handleRpc(message) {
  if (!message || message.jsonrpc !== "2.0") return rpcError(message?.id ?? null, -32600, "Invalid Request");
  const { id, method, params } = message;
  if (method === "notifications/initialized" || method?.startsWith("notifications/")) return null;
  if (method === "initialize") {
    return rpcSuccess(id, {
      protocolVersion: params?.protocolVersion || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "alphaengine-mcp", version: VERSION },
    });
  }
  if (method === "ping") return rpcSuccess(id, {});
  if (method === "tools/list") {
    return rpcSuccess(id, {
      tools: Object.entries(TOOLS).map(([name, tool]) => ({
        name,
        description: tool.description,
        inputSchema: tool.inputSchema,
      })),
    });
  }
  if (method === "tools/call") {
    const tool = TOOLS[params?.name];
    if (!tool) return rpcError(id, -32601, `Tool not found: ${params?.name || ""}`);
    try {
      const result = await tool.call(params?.arguments || {});
      return rpcSuccess(id, {
        content: [{ type: "text", text: JSON.stringify(result) }],
        structuredContent: result,
        isError: false,
      });
    } catch (error) {
      return rpcSuccess(id, {
        content: [
          {
            type: "text",
            text: redactSecrets(String(error.message || error), defaultAccountManager.getKnownSecrets()),
          },
        ],
        isError: true,
      });
    }
  }
  return rpcError(id, -32601, `Method not found: ${method}`);
}

function writeJson(response, status, body) {
  const text = body === null ? "" : JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(text),
    "Cache-Control": "no-store",
  });
  response.end(text);
}

async function readJsonBody(request) {
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 2 * 1024 * 1024) throw new Error("Request body too large");
  }
  return JSON.parse(body || "{}");
}

function startHttp() {
  const server = http.createServer(async (request, response) => {
    try {
      if (request.method === "GET" && request.url === "/health") {
        const status = await authStatus();
        return writeJson(response, 200, {
          ok: true,
          service: "alphaengine-mcp",
          version: VERSION,
          ...status,
        });
      }
      if (request.method !== "POST" || request.url !== "/mcp") {
        return writeJson(response, 404, { error: "Not found" });
      }
      const body = await readJsonBody(request);
      if (Array.isArray(body)) {
        const results = (await Promise.all(body.map(handleRpc))).filter(Boolean);
        return writeJson(response, results.length ? 200 : 204, results.length ? results : null);
      }
      const result = await handleRpc(body);
      return writeJson(response, result ? 200 : 204, result);
    } catch (error) {
      return writeJson(response, 400, rpcError(null, -32700, String(error.message || error)));
    }
  });
  server.listen(PORT, HOST, () => {
    process.stderr.write(`[alphaengine-mcp] listening on http://${HOST}:${PORT}/mcp (v${VERSION})\n`);
  });
  const shutdown = () => server.close(() => process.exit(0));
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

function startStdio() {
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on("line", async (line) => {
    if (!line.trim()) return;
    let response;
    try {
      response = await handleRpc(JSON.parse(line));
    } catch (error) {
      response = rpcError(null, -32700, String(error.message || error));
    }
    if (response) process.stdout.write(`${JSON.stringify(response)}\n`);
  });
}

function isValidDocumentId(id) { return typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$/.test(id); }

// Local ingestion reuses the authenticated reader; importing must not start a server.
export { isValidDocumentId, fetchDocumentDetail, searchLibrary, listFilters, listRecordingTasks, getRecordingStatus, getRecordingTask,
  getDocumentInfo, listAttachments, selectDocumentText, documentFileCandidates, extensionFor, compactSearchResult,
  documentMetadata, resultMatches, requireDatePair, TOOLS, handleRpc,
  createAccountManager, createMacOsKeychain, isValidKeychainAccount, quoteSecurityInteractiveArg,
  classifyUpstreamError, isQuotaOrRateLimitError, resolveEndpointQuotaType, ENDPOINT_QUOTA_RULES,
  formatSanitizedErrorSnippet, sanitizeErrorValue, fetchWithToken, apiRequest, downloadDocument,
  authStatus, redactSecrets, sendDefaultOpsAlert };
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--stdio")) startStdio();
  else startHttp();
}
