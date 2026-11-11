// DALTON-HOOK(company-automations): scope and engine decide which actions a task offers.
import { apiErrorStatus } from '@/api/errors';
import type { Automation, AutomationRead, ScriptAutomationRead } from '@/types/automation';

export type ManageTask = Automation | AutomationRead;

export function isForbiddenError(error: unknown): boolean {
  return apiErrorStatus(error) === 403;
}

/** True for a script read. An older agent row has no `engine_kind` and stays an agent. */
export function isScriptTask(task: ManageTask | null | undefined): task is ScriptAutomationRead {
  if (task == null || !('engine_kind' in task)) return false;
  return task.engine_kind === 'script';
}

export function isCompanyTask(task: ManageTask | null | undefined): boolean {
  return !!task && 'scope' in task && task.scope === 'company';
}

/** The detail's `config_version` when it can be sent as `expected_config_version`. */
export function configVersionOf(task: { config_version?: unknown } | null | undefined): number | null {
  const value = task?.config_version;
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 ? value : null;
}

/** Agent rows, including a list row that has no `engine_kind`. Scripts are left out. */
export function asAutomations(tasks: readonly ManageTask[]): Automation[] {
  const agents: Automation[] = [];
  for (const task of tasks) {
    if (!isScriptTask(task)) agents.push(task);
  }
  return agents;
}

/** Script configuration status. The latest run's status is not a substitute. */
export function scriptStatusLabelKey(task: ScriptAutomationRead): string | null {
  if (task.paused_by_user || task.status === 'paused') return 'automation.statePaused';
  if (task.status === 'disabled' || task.enabled === false) return 'automation.stateDisabled';
  if (task.status === 'active' || task.enabled) return 'automation.statusActive';
  return null;
}

/** Translation key for a known run status. Unknown words are shown unchanged. */
export function runStatusKey(status: string): string | null {
  switch (status) {
    case 'succeeded':
      return 'automation.runSucceeded';
    case 'timed_out':
    case 'timeout':
      return 'automation.runTimedOut';
    case 'failed':
      return 'automation.runFailed';
    case 'running':
      return 'automation.runRunning';
    case 'pending':
      return 'automation.runQueued';
    case 'completed':
      return 'automation.runCompleted';
    case 'skipped':
      return 'automation.runSkipped';
    case 'waiting':
      return 'automation.runWaiting';
    default:
      return null;
  }
}
