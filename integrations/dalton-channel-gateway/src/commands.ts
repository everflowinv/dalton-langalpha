export type SessionCommandTargetKind =
  | "home"
  | "workspace_id"
  | "exact_name"
  | "shortcut_name";

export type ParsedSlashCommand =
  | {
      kind: "bind";
      code: string;
      canonicalCommand: string;
    }
  | {
      kind: "unbind";
      canonicalCommand: string;
    }
  | {
      kind: "session";
      cmd: "workspace_list" | "workspace_switch" | "new" | "stop";
      targetKind?: SessionCommandTargetKind;
      workspaceId?: string;
      workspaceQuery?: string;
      canonicalCommand: string;
    }
  | {
      kind: "invalid";
      reason:
        | "empty_command"
        | "multiline_command"
        | "missing_bind_code"
        | "unexpected_arguments";
      message: string;
    };

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Trim outer whitespace from candidate command text after the channel adapter
 * has already removed any structured bot mention. Preserves all internal
 * characters (including `@`, `#`, and spaces inside workspace names).
 */
export function normalizeCommandCandidateText(
  rawText: string,
  _chatType: "dm" | "group" = "dm",
): string {
  return (rawText ?? "").trim();
}

/**
 * Parse an inbound message into a structured slash command, or return `null`
 * if the message is normal conversation text (does not begin with `/`).
 *
 * Supported commands:
 * - `/bind <code>`
 * - `/unbind`
 * - `/workspace` or `/workspace list`
 * - `/workspace home`
 * - `/workspace <uuid>`
 * - `/workspace <exact workspace name>`
 * - `/main`
 * - `/new`
 * - `/stop`
 * - `/<exact workspace name>` (shortcut for switching to a workspace by exact outer-trimmed name)
 */
export function parseSlashCommand(
  rawText: string,
  chatType: "dm" | "group" = "dm",
): ParsedSlashCommand | null {
  const candidate = normalizeCommandCandidateText(rawText, chatType);
  if (!candidate.startsWith("/")) {
    return null;
  }

  const afterSlash = candidate.slice(1);
  if (/[\r\n]/.test(afterSlash)) {
    return {
      kind: "invalid",
      reason: "multiline_command",
      message:
        "Slash commands must be a single line. Use `/workspace` to list workspaces, `/main` for Chief of Staff, or `/new` to start a new thread.",
    };
  }

  if (afterSlash.length === 0 || /^\s/.test(afterSlash)) {
    return {
      kind: "invalid",
      reason: "empty_command",
      message:
        "Unknown command. Available commands: `/main`, `/<workspace name>`, `/workspace`, `/new`, `/stop`, `/bind <CODE>`, `/unbind`.",
    };
  }

  const fullBody = afterSlash.trim();
  const tokenMatch = /^(\S+)(?:\s+([\s\S]*))?$/u.exec(fullBody);
  if (!tokenMatch) {
    return {
      kind: "invalid",
      reason: "empty_command",
      message:
        "Unknown command. Available commands: `/main`, `/<workspace name>`, `/workspace`, `/new`, `/stop`, `/bind <CODE>`, `/unbind`.",
    };
  }

  const head = tokenMatch[1]!.toLowerCase();
  const rest = (tokenMatch[2] ?? "").trim();

  if (head === "bind") {
    if (!rest) {
      return {
        kind: "invalid",
        reason: "missing_bind_code",
        message:
          "Usage: `/bind <6-CHAR-CODE>` (generate a bind code in Web Settings → Channels).",
      };
    }
    return {
      kind: "bind",
      code: rest,
      canonicalCommand: `/bind ${rest}`,
    };
  }

  if (head === "unbind") {
    if (rest) {
      return {
        kind: "invalid",
        reason: "unexpected_arguments",
        message: `Usage: \`/unbind\` (takes no arguments). To switch to a workspace named "${fullBody}", use \`/workspace ${fullBody}\`.`,
      };
    }
    return {
      kind: "unbind",
      canonicalCommand: "/unbind",
    };
  }

  if (head === "main") {
    if (rest) {
      return {
        kind: "invalid",
        reason: "unexpected_arguments",
        message: `Usage: \`/main\` (takes no arguments). To switch to a workspace named "${fullBody}", use \`/workspace ${fullBody}\`.`,
      };
    }
    return {
      kind: "session",
      cmd: "workspace_switch",
      targetKind: "home",
      workspaceQuery: "home",
      canonicalCommand: "/main",
    };
  }

  if (head === "new") {
    if (rest) {
      return {
        kind: "invalid",
        reason: "unexpected_arguments",
        message: `Usage: \`/new\` (takes no arguments). To switch to a workspace named "${fullBody}", use \`/workspace ${fullBody}\`.`,
      };
    }
    return {
      kind: "session",
      cmd: "new",
      canonicalCommand: "/new",
    };
  }

  if (head === "stop") {
    if (rest) {
      return {
        kind: "invalid",
        reason: "unexpected_arguments",
        message: `Usage: \`/stop\` (takes no arguments). To switch to a workspace named "${fullBody}", use \`/workspace ${fullBody}\`.`,
      };
    }
    return {
      kind: "session",
      cmd: "stop",
      canonicalCommand: "/stop",
    };
  }

  if (head === "workspace") {
    if (!rest || rest.toLowerCase() === "list") {
      return {
        kind: "session",
        cmd: "workspace_list",
        canonicalCommand: "/workspace",
      };
    }
    if (rest.toLowerCase() === "home") {
      return {
        kind: "session",
        cmd: "workspace_switch",
        targetKind: "home",
        workspaceQuery: "home",
        canonicalCommand: "/workspace home",
      };
    }
    if (UUID_RE.test(rest)) {
      const normalizedId = rest.toLowerCase();
      return {
        kind: "session",
        cmd: "workspace_switch",
        targetKind: "workspace_id",
        workspaceId: normalizedId,
        workspaceQuery: normalizedId,
        canonicalCommand: `/workspace ${normalizedId}`,
      };
    }
    return {
      kind: "session",
      cmd: "workspace_switch",
      targetKind: "exact_name",
      workspaceQuery: rest,
      canonicalCommand: `/workspace ${rest}`,
    };
  }

  return {
    kind: "session",
    cmd: "workspace_switch",
    targetKind: "shortcut_name",
    workspaceQuery: fullBody,
    canonicalCommand: `/${fullBody}`,
  };
}
