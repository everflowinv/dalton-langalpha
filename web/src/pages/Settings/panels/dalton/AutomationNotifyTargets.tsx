import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Check, ChevronDown } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { queryKeys } from '@/lib/queryKeys';
import { modelPrefs, splitPreferenceWrite } from '@/lib/modelPreferences';
import { fetchDaltonFeishuGroups, type DaltonFeishuGroup } from '@/pages/Settings/utils/daltonApi';
import type { Automation } from '@/types/automation';

interface AutomationNotifyTargetsProps {
  automation: Pick<Automation, 'name' | 'dalton_notify_category'>;
}

type NotifyRoutes = Record<string, string[] | null>;

function isChatTarget(target: string): boolean {
  return target.startsWith('feishu:chat:');
}

/** The route entry a task resolves to, with the backend's own key precedence:
 *  exact name, lowercased name, `category:<cat>`, bare category. `null` means
 *  no route (personal targets only). An entry that exists but is not a list is
 *  skipped, exactly as `resolve_notify_route_targets` skips it. */
export function resolveNotifyRouteEntry(
  automation: Pick<Automation, 'name' | 'dalton_notify_category'>,
  routes: NotifyRoutes,
): { list: string[]; source: 'task' | 'category' } | null {
  const category = automation.dalton_notify_category;
  if (!category) return null;
  const name = automation.name.trim();
  const candidates: Array<[string, 'task' | 'category']> = [
    [name, 'task'],
    [name.toLowerCase(), 'task'],
    [`category:${category}`, 'category'],
    [category, 'category'],
  ];
  for (const [key, source] of candidates) {
    if (key && Array.isArray(routes[key])) {
      return { list: routes[key] as string[], source };
    }
  }
  return null;
}

/** What the collapsed selector shows for the currently selected subset. */
export function notifyTargetsSelectionLabel(
  selectedPersonal: string[] | null,
  selectedChats: string[],
  labels: Record<string, string>,
  groupName: (target: string) => string,
  noneLabel: string,
  defaultLabel: string,
): string {
  if (selectedPersonal === null && selectedChats.length === 0) return defaultLabel;
  if (selectedPersonal !== null && selectedPersonal.length === 0 && selectedChats.length === 0) {
    // An explicit empty route: the task delivers nowhere.
    return noneLabel;
  }
  // Full keys first (a label's key is the whole target string), then the
  // group's own name, and only last the bare id.
  const names = [...(selectedPersonal ?? []), ...selectedChats].map(
    (t) => labels[t] || groupName(t) || t.replace(/^feishu:chat:/, ''),
  );
  if (names.length === 0) return defaultLabel;
  if (names.length <= 2) return names.join('、');
  return `${names.length} 个目标`;
}

export function AutomationNotifyTargets({
  automation,
}: AutomationNotifyTargetsProps): React.ReactElement | null {
  const { preferences, isLoading, isLoaded } = usePreferences();
  const updatePreferences = useUpdatePreferences();
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  const prefsReady = Boolean(isLoaded) && !isLoading;
  const prefs = modelPrefs(preferences);
  const masterEnabled = prefs.dalton_notify_enabled === true;
  const targets = Array.isArray(prefs.dalton_notify_targets)
    ? prefs.dalton_notify_targets
    : [];
  const labels = prefs.dalton_notify_target_labels ?? {};
  const routes = (prefs.dalton_notify_routes ?? {}) as NotifyRoutes;

  // The route this task resolves to (task key first, then category), the same
  // precedence the server applies at delivery time.
  const routeEntry = resolveNotifyRouteEntry(automation, routes);
  const routeList = routeEntry ? routeEntry.list : [];
  const inheritsCategory = routeEntry?.source === 'category';
  // Stale caches may name deleted personal targets; they drop out here as on
  // the server. An explicit empty route ([] = deliver nowhere) stays a list —
  // only a missing entry means "no route".
  const selectedPersonal: string[] | null = routeEntry
    ? routeList.filter((t) => targets.includes(t))
    : null;
  const selectedChats: string[] = routeEntry ? routeList.filter((t) => isChatTarget(t)) : [];

  // The group list is itself the PM gate: the endpoint answers 403 for a
  // non-admin, and no auth context is read here — a render without
  // AuthProvider (upstream page tests) degrades to personal-targets-only
  // instead of throwing. It is fetched when the selector opens, when a saved
  // route names groups (the collapsed label needs their names), or when there
  // is no personal target at all (groups are then the only thing to select).
  // The request needs a pushable task and the switches on, but not the
  // groups themselves: an admin with no personal targets is only known to be
  // selectable once the fetch answers, so `groups` stays out of this gate.
  const mayQuery =
    Boolean(automation.dalton_notify_category) &&
    prefsReady &&
    masterEnabled &&
    (open || selectedChats.length > 0 || targets.length === 0);
  const groupsQuery = useQuery({
    queryKey: queryKeys.dalton.feishuGroups(),
    queryFn: fetchDaltonFeishuGroups,
    enabled: mayQuery,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const groups: DaltonFeishuGroup[] = groupsQuery.data?.groups ?? [];
  const groupNames = new Map(groups.map((g) => [g.chat_id, g.name]));
  const groupName = (target: string) =>
    groupNames.get(target.replace(/^feishu:chat:/, '')) ?? '';
  // A saved group the bot has left is not in the discovery list, but it still
  // rides in the route — it must stay visible and removable here.
  const staleChats = selectedChats.filter((t) => !groupName(t));

  if (!automation.dalton_notify_category) {
    // Not a pushable task: no toggle beside it, so no target selector either.
    return null;
  }

  // A saved route (even an emptied one) keeps the selector alive with no
  // personal targets and no groups: it has to stay openable to be cleared.
  const canInteract =
    prefsReady &&
    masterEnabled &&
    (targets.length > 0 || groups.length > 0 || routeEntry !== null);

  if (!canInteract) {
    return null;
  }

  const toggleTarget = (target: string) => {
    const isChat = isChatTarget(target);
    // With no route of its own (or a category one), the task currently
    // reaches every personal target — that is the baseline being edited.
    const basePersonal = selectedPersonal ?? targets;
    const baseChats = selectedChats;
    const nextPersonal = isChat
      ? basePersonal
      : basePersonal.includes(target)
        ? basePersonal.filter((t) => t !== target)
        : [...basePersonal, target];
    const nextChats = isChat
      ? baseChats.includes(target)
        ? baseChats.filter((t) => t !== target)
        : [...baseChats, target]
      : baseChats;
    // Every edit saves an explicit task-level list, never null: null would
    // fall back to the category route (or all personal targets) and undo the
    // edit. An emptied list stays [] — "deliver nowhere".
    void updatePreferences.mutateAsync(
      splitPreferenceWrite({
        dalton_notify_routes: {
          [automation.name]: [...nextPersonal, ...nextChats],
        },
      }),
    );
  };

  const row = (
    key: string,
    title: string,
    sub: string,
    checked: boolean,
    onToggle: () => void,
    testName: string,
  ) => (
    <button
      key={key}
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={testName}
      onClick={onToggle}
      className="flex items-start gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-popover"
    >
      <span
        aria-hidden="true"
        className="mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm border"
        style={{
          borderColor: 'var(--color-border-muted)',
          backgroundColor: checked ? 'var(--color-accent-primary)' : 'transparent',
        }}
      >
        {checked && <Check className="h-3 w-3 text-white" />}
      </span>
      <span className="flex min-w-0 flex-col">
        <span style={{ color: 'var(--color-text-primary)' }}>{title}</span>
        <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
          {sub}
        </span>
      </span>
    </button>
  );

  return (
    <div
      className="flex shrink-0 items-center"
      data-testid={`dalton-notify-targets-${automation.name}`}
    >
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={`推送目标 ${automation.name}`}
            className="inline-flex items-center gap-0.5 text-xs"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {notifyTargetsSelectionLabel(
              selectedPersonal,
              selectedChats,
              labels,
              groupName,
              t('dalton.notify.targetsNone'),
              '仅个人目标',
            )}
            <ChevronDown className="h-3 w-3" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-64 p-1.5">
          <p className="px-2 py-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            推送到哪些目标（不选群则只推个人）
          </p>
          {inheritsCategory && (
            <p
              className="px-2 pb-1 text-[11px]"
              style={{ color: 'var(--color-text-tertiary)' }}
              data-testid="dalton-notify-inherits-category"
            >
              {t('dalton.notify.targetsInheritCategory')}
            </p>
          )}
          <p className="px-2 pt-1 pb-0.5 text-[11px] font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
            个人目标
          </p>
          <div className="flex flex-col">
            {targets.map((target) =>
              row(
                target,
                labels[target] || '未命名目标',
                target,
                // No route of its own means every personal target is reached;
                // the checks say what delivery actually does.
                selectedPersonal === null ? true : selectedPersonal.includes(target),
                () => toggleTarget(target),
                `个人目标 ${target}`,
              ),
            )}
          </div>
          {(groups.length > 0 || staleChats.length > 0) && (
            <>
              <p className="px-2 pt-2 pb-0.5 text-[11px] font-semibold" style={{ color: 'var(--color-text-secondary)' }}>
                飞书群
              </p>
              <div className="flex flex-col">
                {groups.map((group) => {
                  const target = `feishu:chat:${group.chat_id}`;
                  return row(
                    target,
                    group.name || '未命名群',
                    target,
                    selectedChats.includes(target),
                    () => toggleTarget(target),
                    `飞书群 ${group.name || group.chat_id}`,
                  );
                })}
                {staleChats.map((target) => {
                  // Not in the discovery list anymore, but still in the saved
                  // route: keep it visible and removable, or the route could
                  // never be edited again (the server would 400 the group it
                  // no longer knows).
                  const id = target.replace(/^feishu:chat:/, '');
                  return row(
                    target,
                    `${id}（${t('dalton.notify.targetsGroupLeft')}）`,
                    target,
                    true,
                    () => toggleTarget(target),
                    `飞书群 ${id}`,
                  );
                })}
              </div>
            </>
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
