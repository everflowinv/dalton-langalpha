/**
 * Loopback ops alerts for the host watchdog.
 *
 * The backend may be down, so this route does not call it and does not write
 * a delivery row. Targets and the two fixed sentences come from a host file.
 * Text is sent with the plain-text Feishu API, never the card upgrade.
 */

import { readFile } from "node:fs/promises";

const LOOPBACK = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);
const ALERT_CODES = new Set([
  "backend_down",
  "backend_recovered",
  "backup_failed",
  "model_unavailable",
  "model_released",
  "monitor_publish_failed",
  "alphaengine_login_required",
  "alphaengine_module_page_required",
  "alphaengine_token_expiring",
  "redis_diag_p1",
  "redis_diag_p2",
  "redis_diag_p3",
]);
const DETAIL_ALERT_CODES = new Set([
  "backup_failed",
  "model_unavailable",
  "model_released",
  "monitor_publish_failed",
  "alphaengine_token_expiring",
  "redis_diag_p1",
  "redis_diag_p2",
  "redis_diag_p3",
]);
const ALLOWED_BODY_KEYS = new Set(["code", "detail", "provider", "models"]);
const ALLOWED_CONFIG_KEYS = new Set(["targets", "messages"]);
const ALLOWED_MESSAGE_KEYS = new Set([
  "backend_down",
  "backend_recovered",
  "backup_failed",
  "model_unavailable",
  "model_released",
  "monitor_publish_failed",
  "alphaengine_login_required",
  "alphaengine_module_page_required",
  "alphaengine_token_expiring",
  "redis_diag_p1",
  "redis_diag_p2",
  "redis_diag_p3",
]);
const REQUIRED_MESSAGE_KEYS = new Set(["backend_down", "backend_recovered"]);
const OPTIONAL_MESSAGE_KEYS = new Set([
  "alphaengine_login_required",
  "alphaengine_module_page_required",
]);
const DEFAULT_DETAIL_TEMPLATES: Record<string, string> = {
  backup_failed: "Dalton 备份失败：{detail}",
  model_unavailable: "Dalton 模型可用性告警：{detail}",
  model_released: "订阅已开放 {provider} 的 {models}，核对档位后将其设为可见并去掉 awaiting_release 标记",
  monitor_publish_failed: "Dalton 看板发布告警：{detail}",
  alphaengine_token_expiring: "请在 AlphaEngine App 里登录账号 {detail}",
  redis_diag_p1: "Dalton Redis P1 告警：{detail}",
  redis_diag_p2: "Dalton Redis P2 退化告警：{detail}",
  redis_diag_p3: "Dalton Redis P3 预警：{detail}",
};
const MAX_BACKUP_DETAIL_CHARS = 300;
const MAX_MODEL_UNAVAILABLE_DETAIL_CHARS = 600;
const MAX_REDIS_DIAG_DETAIL_CHARS = 600;

export const REDIS_DIAG_P1_DEDUP_WINDOW_MS = 5 * 60 * 1000;
export const REDIS_DIAG_P2_COOLDOWN_MS = 30 * 60 * 1000;
export const REDIS_DIAG_P3_COOLDOWN_MS = 30 * 60 * 1000;
export const REDIS_DIAG_LARGE_PAYLOAD_BYTES = 512 * 1024;
export const REDIS_DIAG_P2_RECOVERED_THRESHOLD = 5;
export const REDIS_DIAG_P2_POOL_UTIL_THRESHOLD = 0.8;
export const REDIS_DIAG_P2_LOOP_LAG_MS_THRESHOLD = 1500;
export const REDIS_DIAG_P2_FORK_USEC_THRESHOLD = 200_000;
export const REDIS_DIAG_P2_SLOWLOG_US_THRESHOLD = 100_000;

export interface OpsAlertSender {
  (targetAddress: string, text: string): Promise<{ messageId: string }>;
}

export interface OpsAlertResult {
  status: number;
  data: Record<string, unknown>;
}

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return typeof val === "object" && val !== null && !Array.isArray(val);
}

export function isLoopbackAddress(address: string | undefined): boolean {
  return typeof address === "string" && LOOPBACK.has(address);
}

function invalidConfig(): OpsAlertResult {
  return {
    status: 503,
    data: {
      ok: false,
      code: "ops_alerts_unconfigured",
      message: "Ops alert configuration is missing or invalid.",
    },
  };
}

function isFeishuTarget(value: string): boolean {
  if (value.startsWith("feishu:user:")) {
    return value.slice("feishu:user:".length).trim().length > 0;
  }
  if (value.startsWith("feishu:chat:")) {
    return value.slice("feishu:chat:".length).trim().length > 0;
  }
  return false;
}

async function loadOpsAlertConfig(configPath: string | undefined): Promise<
  | { ok: true; targets: string[]; messages: Record<string, string> }
  | { ok: false }
> {
  const path = (configPath ?? "").trim();
  if (!path) {
    return { ok: false };
  }
  let parsed: unknown;
  try {
    const raw = await readFile(path, "utf8");
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return { ok: false };
  }
  if (!isPlainRecord(parsed)) {
    return { ok: false };
  }
  const extra = Object.keys(parsed).filter((key) => !ALLOWED_CONFIG_KEYS.has(key));
  if (extra.length > 0 || !Array.isArray(parsed.targets) || !isPlainRecord(parsed.messages)) {
    return { ok: false };
  }
  const messageExtra = Object.keys(parsed.messages).filter(
    (key) => !ALLOWED_MESSAGE_KEYS.has(key),
  );
  if (messageExtra.length > 0 || parsed.targets.length === 0) {
    return { ok: false };
  }
  const targets: string[] = [];
  for (const item of parsed.targets) {
    if (typeof item !== "string" || !isFeishuTarget(item.trim())) {
      return { ok: false };
    }
    targets.push(item.trim());
  }
  const messages: Record<string, string> = {};
  for (const code of REQUIRED_MESSAGE_KEYS) {
    const text = parsed.messages[code];
    if (typeof text !== "string" || text.trim().length === 0) {
      return { ok: false };
    }
    messages[code] = text;
  }
  for (const [code, defaultTemplate] of Object.entries(DEFAULT_DETAIL_TEMPLATES)) {
    const rawDetailTemplate = parsed.messages[code];
    if (rawDetailTemplate !== undefined) {
      if (typeof rawDetailTemplate !== "string" || rawDetailTemplate.trim().length === 0) {
        return { ok: false };
      }
      messages[code] = rawDetailTemplate;
    } else {
      messages[code] = defaultTemplate;
    }
  }
  for (const code of OPTIONAL_MESSAGE_KEYS) {
    const raw = parsed.messages[code];
    if (raw !== undefined) {
      if (typeof raw !== "string" || raw.trim().length === 0) {
        return { ok: false };
      }
      messages[code] = raw;
    }
  }
  return { ok: true, targets, messages };
}

export function sanitizeAlertDetail(raw: string, maxChars: number): string {
  // Replace line breaks/tabs with spaces and strip remaining ASCII control characters.
  const withoutControls = raw
    .replace(/[\r\n\t]+/g, " ")
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x1f\x7f]/g, "")
    .trim();
  return Array.from(withoutControls).slice(0, maxChars).join("");
}

export function sanitizeBackupAlertDetail(raw: string): string {
  return sanitizeAlertDetail(raw, MAX_BACKUP_DETAIL_CHARS);
}

function renderDetailAlertMessage(template: string, rawDetail: string, maxChars: number): string {
  const detail = sanitizeAlertDetail(rawDetail, maxChars) || "未知原因";
  if (template.includes("{detail}")) {
    return template.replaceAll("{detail}", () => detail);
  }
  return `${template}：${detail}`;
}

function renderModelReleasedMessage(
  template: string,
  rawProvider: unknown,
  rawModels: unknown,
  rawDetail: unknown,
  maxChars: number,
): string {
  const detailStr = typeof rawDetail === "string" ? rawDetail : "";
  const colonIdx = detailStr.indexOf(":");
  const fallbackProvider = colonIdx > 0 ? detailStr.slice(0, colonIdx) : "";
  const fallbackModels = colonIdx > 0 ? detailStr.slice(colonIdx + 1) : detailStr;
  const provider =
    sanitizeAlertDetail(typeof rawProvider === "string" ? rawProvider : fallbackProvider, 120) ||
    "未知订阅";
  const models =
    sanitizeAlertDetail(typeof rawModels === "string" ? rawModels : fallbackModels, maxChars) ||
    "未知模型";
  if (template.includes("{provider}") || template.includes("{models}")) {
    return template
      .replaceAll("{provider}", () => provider)
      .replaceAll("{models}", () => models);
  }
  const combined = detailStr || `${provider}: ${models}`;
  return renderDetailAlertMessage(template, combined, maxChars);
}

export async function handleOpsAlert(options: {
  body: unknown;
  remoteAddress: string | undefined;
  configPath: string | undefined;
  sendPlainText: OpsAlertSender | undefined;
}): Promise<OpsAlertResult> {
  if (!isLoopbackAddress(options.remoteAddress)) {
    return {
      status: 403,
      data: {
        ok: false,
        code: "not_loopback",
        message: "Ops alerts accept only a loopback client.",
      },
    };
  }
  if (!isPlainRecord(options.body)) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message: "Request body must be a JSON object.",
      },
    };
  }
  const extra = Object.keys(options.body).filter((key) => !ALLOWED_BODY_KEYS.has(key));
  const code = typeof options.body.code === "string" ? options.body.code.trim() : "";
  if (extra.length > 0 || !ALERT_CODES.has(code)) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message:
          "code must be backend_down, backend_recovered, backup_failed, model_unavailable, model_released, monitor_publish_failed, alphaengine_login_required, alphaengine_module_page_required, alphaengine_token_expiring, redis_diag_p1, redis_diag_p2, or redis_diag_p3.",
      },
    };
  }
  if (!DETAIL_ALERT_CODES.has(code) && options.body.detail !== undefined) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message:
          "detail is only supported for backup_failed, model_unavailable, model_released, monitor_publish_failed, alphaengine_token_expiring, redis_diag_p1, redis_diag_p2, or redis_diag_p3.",
      },
    };
  }
  if (
    code !== "model_released" &&
    (options.body.provider !== undefined || options.body.models !== undefined)
  ) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message: "provider and models are only supported for model_released.",
      },
    };
  }
  if (
    DETAIL_ALERT_CODES.has(code) &&
    options.body.detail !== undefined &&
    typeof options.body.detail !== "string"
  ) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message: "detail must be a string.",
      },
    };
  }
  if (
    code === "model_released" &&
    ((options.body.provider !== undefined && typeof options.body.provider !== "string") ||
      (options.body.models !== undefined && typeof options.body.models !== "string"))
  ) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message: "provider and models must be strings.",
      },
    };
  }
  if (typeof options.sendPlainText !== "function") {
    return invalidConfig();
  }
  const config = await loadOpsAlertConfig(options.configPath);
  if (!config.ok) {
    return invalidConfig();
  }
  let text: string;
  if (code === "model_released") {
    text = renderModelReleasedMessage(
      config.messages[code] ?? DEFAULT_DETAIL_TEMPLATES[code],
      options.body.provider,
      options.body.models,
      options.body.detail,
      MAX_MODEL_UNAVAILABLE_DETAIL_CHARS,
    );
  } else if (DETAIL_ALERT_CODES.has(code)) {
    const maxChars =
      code === "model_unavailable" || code === "monitor_publish_failed"
        ? MAX_MODEL_UNAVAILABLE_DETAIL_CHARS
        : code.startsWith("redis_diag_")
          ? MAX_REDIS_DIAG_DETAIL_CHARS
          : MAX_BACKUP_DETAIL_CHARS;
    text = renderDetailAlertMessage(
      config.messages[code] ?? DEFAULT_DETAIL_TEMPLATES[code],
      typeof options.body.detail === "string" ? options.body.detail : "",
      maxChars,
    );
  } else {
    const configured = config.messages[code];
    if (typeof configured !== "string" || configured.trim().length === 0) {
      return invalidConfig();
    }
    text = configured;
  }
  const results: Array<Record<string, unknown>> = [];
  let anyFailed = false;
  let anySucceeded = false;
  for (const target of config.targets) {
    try {
      const sent = await options.sendPlainText(target, text);
      anySucceeded = true;
      results.push({
        target,
        ok: true,
        message_id: sent.messageId,
      });
    } catch (err) {
      anyFailed = true;
      results.push({
        target,
        ok: false,
        error: err instanceof Error ? err.name : "Error",
      });
    }
  }
  if (anyFailed) {
    return {
      status: 502,
      data: {
        ok: false,
        any_succeeded: anySucceeded,
        code,
        message: "One or more ops alert sends failed.",
        results,
      },
    };
  }
  return {
    status: 200,
    data: {
      ok: true,
      any_succeeded: anySucceeded,
      code,
      results,
    },
  };
}

export interface RedisDiagClusterSummary {
  active_worker_count?: number;
  total_recovered_writes_5m?: number;
  total_transport_lost_5m?: number;
  total_transport_lost_ever?: number;
  last_transport_lost_at?: string | null;
  max_pool_utilization?: number;
  max_loop_lag_5s_ms?: number;
  max_loop_lag_60s_ms?: number;
  max_heartbeat_age_ms?: number;
  max_payload_bytes_5m?: number;
  max_wire_ms?: number;
  max_acquire_ms?: number;
}

export interface RedisDiagWorkerSummary {
  worker_id?: string;
  counts?: { transport_lost_total?: number };
}

export interface RedisDiagServerSummary {
  stats?: { latest_fork_usec?: number };
  slowlog?: Array<{ id?: number; start_time?: number; duration_us?: number; command?: string }>;
}

export interface RedisDiagSummary {
  cluster?: RedisDiagClusterSummary;
  server?: RedisDiagServerSummary | null;
  workers?: RedisDiagWorkerSummary[];
}

export interface RedisDiagAlertDedupState {
  p1LastEventKey?: string;
  p1LastEver?: number;
  p1LastTransportLostAt?: string;
  p1LastSentAtMs?: number;
  p1WorkerTotals?: Record<string, number>;
  p2LastSentAtMs?: number;
  p2LastSlowlogId?: number;
  p2LastSlowlogStartTime?: number;
  p3LastSentAtMs?: number;
}

export interface RedisDiagEvaluatedAlert {
  severity: "P1" | "P2" | "P3";
  code: "redis_diag_p1" | "redis_diag_p2" | "redis_diag_p3";
  reason: string;
  detail: string;
}

/**
 * Evaluate cluster-aggregated Redis diagnostic alerts with event-window P1 dedup
 * and 30-minute P2/P3 cooldown suppression.
 */
export function evaluateRedisDiagAlerts(
  summary: RedisDiagSummary,
  state: RedisDiagAlertDedupState = {},
  nowMs: number = Date.now(),
): { alerts: RedisDiagEvaluatedAlert[]; nextState: RedisDiagAlertDedupState } {
  const cluster = summary.cluster ?? {};
  const server = summary.server ?? {};
  const workers = Array.isArray(summary.workers) ? summary.workers : [];
  const nextState: RedisDiagAlertDedupState = { ...state };
  const alerts: RedisDiagEvaluatedAlert[] = [];

  const activeWorkerTotals: Record<string, number> = {};
  for (const w of workers) {
    const wid = typeof w?.worker_id === "string" ? w.worker_id.trim() : "";
    if (!wid) {
      continue;
    }
    activeWorkerTotals[wid] = Number(w?.counts?.transport_lost_total ?? 0) || 0;
  }

  const tl5m = Number(cluster.total_transport_lost_5m ?? 0) || 0;
  const tlEver = Number(cluster.total_transport_lost_ever ?? 0) || 0;
  const tlAt = typeof cluster.last_transport_lost_at === "string" ? cluster.last_transport_lost_at : "";
  if (tl5m >= 1) {
    const eventKey = tlEver > 0 || tlAt ? `${tlEver}:${tlAt}` : `5m:${tl5m}`;
    const prevKey = state.p1LastEventKey ?? "";
    const prevEver = Number(state.p1LastEver ?? 0) || 0;
    const prevTlAt = state.p1LastTransportLostAt ?? "";
    const prevSentAt = Number(state.p1LastSentAtMs ?? 0) || 0;
    const prevWorkerTotals = state.p1WorkerTotals ?? {};
    const withinWindow = prevSentAt > 0 && nowMs - prevSentAt < REDIS_DIAG_P1_DEDUP_WINDOW_MS;
    const hasWorkers = Object.keys(activeWorkerTotals).length > 0;
    const workerNewIncident = Object.entries(activeWorkerTotals).some(
      ([wid, cnt]) => cnt > 0 && cnt > (Number(prevWorkerTotals[wid] ?? 0) || 0),
    );
    const tsNewIncident = Boolean(tlAt && prevTlAt && tlAt > prevTlAt);
    const clusterNewIncident = !hasWorkers && tlEver > prevEver;
    const isNewIncident =
      workerNewIncident ||
      tsNewIncident ||
      clusterNewIncident ||
      (eventKey !== prevKey && !withinWindow);
    if (prevSentAt === 0 || isNewIncident) {
      alerts.push({
        severity: "P1",
        code: "redis_diag_p1",
        reason: "transport_lost",
        detail: `集群 5 分钟内发生 transport_lost=${tl5m} 次（active_workers=${cluster.active_worker_count ?? 0}, max_wire_ms=${cluster.max_wire_ms ?? 0}, max_acquire_ms=${cluster.max_acquire_ms ?? 0}, max_loop_lag_5s_ms=${cluster.max_loop_lag_5s_ms ?? 0}）`,
      });
      nextState.p1LastEventKey = eventKey;
      nextState.p1LastEver = tlEver;
      nextState.p1LastTransportLostAt = tlAt;
      nextState.p1LastSentAtMs = nowMs;
      nextState.p1WorkerTotals = activeWorkerTotals;
    }
  }

  const p2Reasons: string[] = [];
  const recovered5m = Number(cluster.total_recovered_writes_5m ?? 0) || 0;
  if (recovered5m >= REDIS_DIAG_P2_RECOVERED_THRESHOLD) {
    p2Reasons.push(`recovered_5m=${recovered5m}`);
  }
  const poolUtil = Number(cluster.max_pool_utilization ?? 0) || 0;
  if (poolUtil >= REDIS_DIAG_P2_POOL_UTIL_THRESHOLD) {
    p2Reasons.push(`pool_util=${Math.round(poolUtil * 100)}%`);
  }
  const maxLag = Math.max(
    Number(cluster.max_loop_lag_5s_ms ?? 0) || 0,
    Number(cluster.max_loop_lag_60s_ms ?? 0) || 0,
    Number(cluster.max_heartbeat_age_ms ?? 0) || 0,
  );
  if (maxLag >= REDIS_DIAG_P2_LOOP_LAG_MS_THRESHOLD) {
    p2Reasons.push(`loop_lag_ms=${Math.round(maxLag * 10) / 10}`);
  }
  const latestForkUsec = Number(server?.stats?.latest_fork_usec ?? 0) || 0;
  if (latestForkUsec >= REDIS_DIAG_P2_FORK_USEC_THRESHOLD) {
    p2Reasons.push(`latest_fork_usec=${latestForkUsec}`);
  }
  const prevSlowId = state.p2LastSlowlogId ?? -1;
  const prevSlowTs = Number(state.p2LastSlowlogStartTime ?? 0) || 0;
  const slowEntries = Array.isArray(server?.slowlog)
    ? server.slowlog.filter((e) => {
        if ((Number(e?.duration_us ?? 0) || 0) < REDIS_DIAG_P2_SLOWLOG_US_THRESHOLD) {
          return false;
        }
        const ets = Number(e?.start_time ?? 0) || 0;
        if (typeof e?.id === "number") {
          return prevSlowId < 0 || e.id > prevSlowId || (ets > 0 && prevSlowTs > 0 && ets > prevSlowTs);
        }
        return (prevSlowId < 0 && prevSlowTs === 0) || ets > prevSlowTs;
      })
    : [];
  if (slowEntries.length > 0) {
    const maxSlowMs = Math.max(...slowEntries.map((e) => Math.round(((Number(e?.duration_us ?? 0) || 0) / 1000) * 10) / 10));
    p2Reasons.push(`slowlog_ge_100ms=${slowEntries.length}(max=${maxSlowMs}ms)`);
  }
  if (p2Reasons.length > 0) {
    const prevP2 = Number(state.p2LastSentAtMs ?? 0) || 0;
    if (prevP2 === 0 || nowMs - prevP2 >= REDIS_DIAG_P2_COOLDOWN_MS) {
      alerts.push({
        severity: "P2",
        code: "redis_diag_p2",
        reason: p2Reasons.join(","),
        detail: `Redis 写入链路退化：${p2Reasons.join("，")}`,
      });
      nextState.p2LastSentAtMs = nowMs;
      if (slowEntries.length > 0) {
        let latest = slowEntries[0]!;
        for (const e of slowEntries) {
          const curTs = Number(e?.start_time ?? 0) || 0;
          const bestTs = Number(latest?.start_time ?? 0) || 0;
          const curId = typeof e?.id === "number" ? e.id : -1;
          const bestId = typeof latest?.id === "number" ? latest.id : -1;
          if (curTs > bestTs || (curTs === bestTs && curId > bestId)) {
            latest = e;
          }
        }
        if (typeof latest.id === "number") {
          nextState.p2LastSlowlogId = latest.id;
        }
        const latestTs = Number(latest.start_time ?? 0) || 0;
        if (latestTs > prevSlowTs) {
          nextState.p2LastSlowlogStartTime = latestTs;
        }
      }
    }
  }

  const maxPayload = Number(cluster.max_payload_bytes_5m ?? 0) || 0;
  if (maxPayload >= REDIS_DIAG_LARGE_PAYLOAD_BYTES) {
    const prevP3 = Number(state.p3LastSentAtMs ?? 0) || 0;
    if (prevP3 === 0 || nowMs - prevP3 >= REDIS_DIAG_P3_COOLDOWN_MS) {
      alerts.push({
        severity: "P3",
        code: "redis_diag_p3",
        reason: "large_payload",
        detail: `检测到超大单帧载荷 payload_bytes=${maxPayload}（阈值 ${REDIS_DIAG_LARGE_PAYLOAD_BYTES}）`,
      });
      nextState.p3LastSentAtMs = nowMs;
    }
  }

  return { alerts, nextState };
}

export async function dispatchRedisDiagSummaryAlerts(options: {
  summary: RedisDiagSummary;
  state?: RedisDiagAlertDedupState;
  nowMs?: number;
  remoteAddress: string | undefined;
  configPath: string | undefined;
  sendPlainText: OpsAlertSender | undefined;
}): Promise<{
  results: Array<{ alert: RedisDiagEvaluatedAlert; result: OpsAlertResult }>;
  nextState: RedisDiagAlertDedupState;
}> {
  const evaluated = evaluateRedisDiagAlerts(options.summary, options.state ?? {}, options.nowMs);
  const results: Array<{ alert: RedisDiagEvaluatedAlert; result: OpsAlertResult }> = [];
  const nextState: RedisDiagAlertDedupState = { ...(options.state ?? {}) };
  for (const alert of evaluated.alerts) {
    const result = await handleOpsAlert({
      body: { code: alert.code, detail: alert.detail },
      remoteAddress: options.remoteAddress,
      configPath: options.configPath,
      sendPlainText: options.sendPlainText,
    });
    results.push({ alert, result });
    if (result.status === 200) {
      if (alert.severity === "P1") {
        nextState.p1LastEventKey = evaluated.nextState.p1LastEventKey;
        nextState.p1LastEver = evaluated.nextState.p1LastEver;
        nextState.p1LastTransportLostAt = evaluated.nextState.p1LastTransportLostAt;
        nextState.p1LastSentAtMs = evaluated.nextState.p1LastSentAtMs;
        nextState.p1WorkerTotals = evaluated.nextState.p1WorkerTotals;
      } else if (alert.severity === "P2") {
        nextState.p2LastSentAtMs = evaluated.nextState.p2LastSentAtMs;
        if (evaluated.nextState.p2LastSlowlogId !== undefined) {
          nextState.p2LastSlowlogId = evaluated.nextState.p2LastSlowlogId;
        }
        if (evaluated.nextState.p2LastSlowlogStartTime !== undefined) {
          nextState.p2LastSlowlogStartTime = evaluated.nextState.p2LastSlowlogStartTime;
        }
      } else if (alert.severity === "P3") {
        nextState.p3LastSentAtMs = evaluated.nextState.p3LastSentAtMs;
      }
    }
  }
  return { results, nextState };
}
