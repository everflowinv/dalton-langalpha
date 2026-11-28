import type { Automation } from '@/types/automation';

const BUILTIN_NOTIFY_CATEGORIES = new Set([
  'earnings_t0',
  'earnings_tp1',
  'earnings_tm3',
  'price_move',
  'director_weekly',
  'earnings_calendar',
  'data_task',
]);

const BUILTIN_DALTON_KINDS = new Set([
  'earnings_t0',
  'earnings_tp1',
  'earnings_tm3',
  'price_move',
  'director_weekly',
  'calendar',
  'data_task',
]);

const EARN_AUTO_RE = /^dalton-[a-z0-9.]+-earn-[a-z0-9._-]+-(?:t0|tp1|tm3)$/i;
const MOVE_AUTO_RE = /^dalton-[a-z0-9.]+-move-(?:up|down)$/i;
const EXCLUDED_AUTO_RE = /^dalton-(?:[a-z0-9.]+-(?:watch|delta)|director-daily|data-health)$/i;

/**
 * True when an automation is one of Dalton's built-in `dalton-*` tasks whose
 * notifications are managed by Dalton notification settings rather than
 * upstream `delivery_config.methods`.
 */
export function isBuiltinDaltonNotifyTask(
  name: string | null | undefined,
  automation?: Pick<Automation, 'dalton_notify_category' | 'metadata'> | null,
): boolean {
  const cleaned = (name ?? '').trim().toLowerCase();
  if (cleaned && EXCLUDED_AUTO_RE.test(cleaned)) {
    return false;
  }
  const category = automation?.dalton_notify_category;
  if (category && BUILTIN_NOTIFY_CATEGORIES.has(category)) {
    return true;
  }
  if (
    cleaned === 'dalton-director-weekly' ||
    cleaned === 'dalton-earnings-calendar' ||
    EARN_AUTO_RE.test(cleaned) ||
    MOVE_AUTO_RE.test(cleaned)
  ) {
    return true;
  }
  const rawKind =
    automation?.metadata && typeof automation.metadata === 'object'
      ? (automation.metadata as Record<string, unknown>).dalton_kind
      : undefined;
  if (typeof rawKind === 'string' && BUILTIN_DALTON_KINDS.has(rawKind.trim().toLowerCase())) {
    return true;
  }
  return false;
}
