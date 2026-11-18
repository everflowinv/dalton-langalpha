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
  "alphaengine_login_required",
  "alphaengine_module_page_required",
]);
const DETAIL_ALERT_CODES = new Set(["backup_failed", "model_unavailable"]);
const ALLOWED_BODY_KEYS = new Set(["code", "detail"]);
const ALLOWED_CONFIG_KEYS = new Set(["targets", "messages"]);
const ALLOWED_MESSAGE_KEYS = new Set([
  "backend_down",
  "backend_recovered",
  "backup_failed",
  "model_unavailable",
  "alphaengine_login_required",
  "alphaengine_module_page_required",
]);
const REQUIRED_MESSAGE_KEYS = new Set(["backend_down", "backend_recovered"]);
const OPTIONAL_MESSAGE_KEYS = new Set([
  "alphaengine_login_required",
  "alphaengine_module_page_required",
]);
const DEFAULT_DETAIL_TEMPLATES: Record<string, string> = {
  backup_failed: "Dalton 备份失败：{detail}",
  model_unavailable: "Dalton 模型可用性告警：{detail}",
};
const MAX_BACKUP_DETAIL_CHARS = 300;
const MAX_MODEL_UNAVAILABLE_DETAIL_CHARS = 600;

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
          "code must be backend_down, backend_recovered, backup_failed, model_unavailable, alphaengine_login_required, or alphaengine_module_page_required.",
      },
    };
  }
  if (!DETAIL_ALERT_CODES.has(code) && options.body.detail !== undefined) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message: "detail is only supported for backup_failed or model_unavailable.",
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
  if (typeof options.sendPlainText !== "function") {
    return invalidConfig();
  }
  const config = await loadOpsAlertConfig(options.configPath);
  if (!config.ok) {
    return invalidConfig();
  }
  let text: string;
  if (DETAIL_ALERT_CODES.has(code)) {
    const maxChars =
      code === "model_unavailable" ? MAX_MODEL_UNAVAILABLE_DETAIL_CHARS : MAX_BACKUP_DETAIL_CHARS;
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
  for (const target of config.targets) {
    try {
      const sent = await options.sendPlainText(target, text);
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
      code,
      results,
    },
  };
}
