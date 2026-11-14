/**
 * Loopback ops alerts for the host watchdog.
 *
 * The backend may be down, so this route does not call it and does not write
 * a delivery row. Targets and the two fixed sentences come from a host file.
 * Text is sent with the plain-text Feishu API, never the card upgrade.
 */

import { readFile } from "node:fs/promises";

const LOOPBACK = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);
const ALERT_CODES = new Set(["backend_down", "backend_recovered", "backup_failed"]);
const ALLOWED_BODY_KEYS = new Set(["code", "detail"]);
const ALLOWED_CONFIG_KEYS = new Set(["targets", "messages"]);
const ALLOWED_MESSAGE_KEYS = new Set(["backend_down", "backend_recovered", "backup_failed"]);
const REQUIRED_MESSAGE_KEYS = new Set(["backend_down", "backend_recovered"]);
const DEFAULT_BACKUP_FAILED_TEMPLATE = "Dalton 备份失败：{detail}";
const MAX_BACKUP_DETAIL_CHARS = 300;

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
  const rawBackupFailed = parsed.messages.backup_failed;
  if (rawBackupFailed !== undefined) {
    if (typeof rawBackupFailed !== "string" || rawBackupFailed.trim().length === 0) {
      return { ok: false };
    }
    messages.backup_failed = rawBackupFailed;
  } else {
    messages.backup_failed = DEFAULT_BACKUP_FAILED_TEMPLATE;
  }
  return { ok: true, targets, messages };
}

export function sanitizeBackupAlertDetail(raw: string): string {
  // Replace line breaks/tabs with spaces and strip remaining ASCII control characters.
  const withoutControls = raw
    .replace(/[\r\n\t]+/g, " ")
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x1f\x7f]/g, "")
    .trim();
  return Array.from(withoutControls).slice(0, MAX_BACKUP_DETAIL_CHARS).join("");
}

function renderBackupFailedMessage(template: string, rawDetail: string): string {
  const detail = sanitizeBackupAlertDetail(rawDetail) || "未知原因";
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
        message: "code must be backend_down, backend_recovered, or backup_failed.",
      },
    };
  }
  if (code !== "backup_failed" && options.body.detail !== undefined) {
    return {
      status: 400,
      data: {
        ok: false,
        code: "invalid_request",
        message: "detail is only supported for backup_failed.",
      },
    };
  }
  if (
    code === "backup_failed" &&
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
  const text =
    code === "backup_failed"
      ? renderBackupFailedMessage(
          config.messages.backup_failed ?? DEFAULT_BACKUP_FAILED_TEMPLATE,
          typeof options.body.detail === "string" ? options.body.detail : "",
        )
      : (config.messages[code] ?? "");
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
