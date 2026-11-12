// How a modification-history entry is shown: newest first, with a short change line.
import type { AutomationFieldChange, AutomationHistoryEntry } from '@/types/automation';

const FIELD_KEYS: Record<string, string> = {
  name: 'automation.fieldName',
  description: 'automation.fieldDescription',
  desired_status: 'automation.fieldDesiredStatus',
  trigger_type: 'automation.fieldTriggerType',
  cron_expression: 'automation.fieldCron',
  cron_expr: 'automation.fieldCron',
  once_run_at: 'automation.fieldOnceRunAt',
  trigger_config: 'automation.fieldTriggerConfig',
  timezone: 'automation.fieldTimezone',
  instruction: 'automation.fieldInstruction',
  workspace_id: 'automation.fieldWorkspace',
  llm_model: 'automation.fieldModel',
  additional_context: 'automation.fieldContext',
  thread_strategy: 'automation.fieldThread',
  conversation_thread_id: 'automation.fieldThreadId',
  max_failures: 'automation.fieldMaxFailures',
  notify_enabled: 'automation.fieldNotify',
  notify_routes: 'automation.fieldNotifyRoutes',
  delivery_config: 'automation.fieldDelivery',
  enabled: 'automation.fieldEnabled',
  paused_by_user: 'automation.fieldPaused',
  cos_mode: 'automation.fieldCosMode',
  notify_storage_mode: 'automation.fieldNotifyStorage',
  status: 'automation.fieldStatus',
};

const SUMMARY_FIELDS = [
  'name',
  'description',
  'cron_expression',
  'cron_expr',
  'timezone',
  'instruction',
  'status',
  'enabled',
  'desired_status',
  'paused_by_user',
] as const;

export interface HistoryChangeLine {
  field: string;
  fieldKey: string | null;
  oldValue: string;
  newValue: string;
}

export interface SnapshotLine {
  field: string;
  fieldKey: string | null;
  value: string;
}

/** Locale key for a known snapshot field. Unknown fields keep their stored name. */
export function fieldLabelKey(field: string): string | null {
  return FIELD_KEYS[field] ?? null;
}

export function historyActionKey(action: string): string | null {
  switch (action) {
    case 'create':
      return 'automation.actionCreate';
    case 'update':
      return 'automation.actionUpdate';
    case 'rollback':
      return 'automation.actionRollback';
    case 'delete':
      return 'automation.actionDelete';
    case 'restore':
      return 'automation.actionRestore';
    case 'resume':
      return 'automation.actionResume';
    case 'pause':
      return 'automation.actionPause';
    case 'maintenance_pause_stale_binding':
      return 'automation.actionMaintenancePause';
    case 'seed':
      return 'automation.actionSeed';
    default:
      return null;
  }
}

export function formatChangeValue(value: unknown): string {
  if (value == null || value === '') return '—';
  if (typeof value === 'string') return value.length > 80 ? `${value.slice(0, 77)}…` : value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    const text = JSON.stringify(value);
    return text.length > 80 ? `${text.slice(0, 77)}…` : text;
  } catch {
    return '—';
  }
}

export function historyEntriesDesc(entries: readonly AutomationHistoryEntry[] | null | undefined): AutomationHistoryEntry[] {
  return [...(entries ?? [])].sort((left, right) => right.version - left.version);
}

export function changeLines(changes: Record<string, AutomationFieldChange> | null | undefined): HistoryChangeLine[] {
  if (!changes) return [];
  return Object.entries(changes).map(([field, change]) => ({
    field,
    fieldKey: fieldLabelKey(field),
    oldValue: formatChangeValue(change?.old),
    newValue: formatChangeValue(change?.new),
  }));
}

/** A few human fields from the version being restored, for the confirm dialog. */
export function snapshotLines(snapshot: Record<string, unknown> | null | undefined): SnapshotLine[] {
  if (!snapshot) return [];
  const lines: SnapshotLine[] = [];
  for (const field of SUMMARY_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(snapshot, field)) continue;
    lines.push({ field, fieldKey: fieldLabelKey(field), value: formatChangeValue(snapshot[field]) });
    if (lines.length >= 6) break;
  }
  return lines;
}
