import { auth } from "./auth.js";
import { automations } from "./automations.js";
import type { Command } from "./command.js";
import { computers } from "./computers.js";
import { drive } from "./drive.js";
import { driveMemo } from "./driveMemo.js";
import { models } from "./models.js";
import { skills } from "./skills.js";
import { threads } from "./threads.js";
import { workspaces } from "./workspaces.js";

export const COMMANDS: readonly Command[] = [...auth, ...computers, ...workspaces, ...threads, ...automations, ...drive, ...driveMemo, ...skills, ...models];

const DRIVE_COMMANDS = new Set<Command>(drive);
const DRIVE_MEMO_COMMANDS = new Set<Command>(driveMemo);

function isDisabledFlag(raw: string | undefined): boolean {
  const v = raw?.trim().toLowerCase();
  return v === "0" || v === "false" || v === "no" || v === "off";
}

/**
 * DALTON-HOOK(drive-kill-switch): filter the CLI command list by deployment
 * capabilities advertised into the sandbox environment (`LANGALPHA_DRIVE_ENABLED`,
 * `LANGALPHA_DRIVE_MEMOS_ENABLED`). Outside a sandbox (unset), all commands stay
 * available for standalone CLI usage and web grammar generation.
 */
export function availableCommands(env: NodeJS.ProcessEnv = process.env): readonly Command[] {
  const driveDisabled = isDisabledFlag(env.LANGALPHA_DRIVE_ENABLED);
  const memosDisabled = driveDisabled || isDisabledFlag(env.LANGALPHA_DRIVE_MEMOS_ENABLED);
  if (!driveDisabled && !memosDisabled) return COMMANDS;
  return COMMANDS.filter((c) => {
    if (memosDisabled && DRIVE_MEMO_COMMANDS.has(c)) return false;
    if (driveDisabled && DRIVE_COMMANDS.has(c)) return false;
    return true;
  });
}
