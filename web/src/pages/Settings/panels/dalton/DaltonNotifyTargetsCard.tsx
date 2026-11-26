import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ToggleSwitch } from '@/components/ui/switch';
import ConfirmDialog from '@/pages/Dashboard/components/ConfirmDialog';
import { useCapabilities } from '@/hooks/useCapabilities';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { modelPrefs, splitPreferenceWrite } from '@/lib/modelPreferences';
import { queryKeys } from '@/lib/queryKeys';
import { listAutomations } from '@/pages/Automations/utils/api';
import {
  fetchDaltonChannelBindings,
  sendDaltonNotifyTest,
} from '@/pages/Settings/utils/daltonApi';
import { resolveNotifyRouteEntry } from './AutomationNotifyTargets';
import type { Automation } from '@/types/automation';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

const FEISHU_USER_RE = /^feishu:user:ou_[A-Za-z0-9_-]+$/;
const DISCORD_USER_RE = /^discord:user:[0-9]+$/;
const WEIXIN_USER_RE = /^weixin:user:[^\s:]{1,255}$/;
const GROUP_TARGET_RE = /^(feishu:chat:|discord:channel:)/i;

/** Personal DM targets allowed under the currently enabled channel capabilities. */
function isValidManualTargetForCapabilities(
  raw: string,
  caps: { feishu: boolean; discord: boolean; weixin: boolean },
): boolean {
  const cleaned = raw.trim();
  if (caps.feishu && FEISHU_USER_RE.test(cleaned)) return true;
  if (caps.discord && DISCORD_USER_RE.test(cleaned)) return true;
  if (caps.weixin && WEIXIN_USER_RE.test(cleaned)) return true;
  return false;
}

/** A label is a plain printable name, at most 40 chars (matching server validation). */
function cleanDaltonNotifyLabel(raw: string): string {
  return raw.trim().replace(/[\p{Cc}\p{Cf}]/gu, '').slice(0, 40);
}

const CATEGORY_ROUTE_KEYS = new Set([
  'earnings_t0',
  'earnings_t1',
  'daily_brief',
  'trade_alert',
  'data_task',
  'default',
]);

function isCategoryOrFallbackRouteKey(key: string): boolean {
  const normalized = key.trim().toLowerCase();
  return normalized.startsWith('category:') || CATEGORY_ROUTE_KEYS.has(normalized);
}

const AUTOMATIONS_PAGE_LIMIT = 100;

function extractDataTaskScope(auto: Pick<Automation, 'name'>): string {
  const name = auto.name.trim();
  if (name.startsWith('data:')) {
    return name.slice('data:'.length).trim();
  }
  return name;
}

function resolveWinningRouteForTaskCount(
  auto: Automation,
  routes: Record<string, string[]>,
): { list: string[]; winningKey: string | null } | null {
  if (auto.dalton_notify_category === 'data_task') {
    const scope = extractDataTaskScope(auto);
    const id = (auto.automation_id || '').trim();
    const candidates = [
      id ? `automation:${id}` : '',
      scope ? `data:${scope}` : '',
      auto.name.trim(),
      auto.name.trim().toLowerCase(),
      'data_task',
      'default',
    ];
    for (const key of candidates) {
      if (key && Array.isArray(routes[key])) {
        return { list: routes[key] as string[], winningKey: key };
      }
    }
    return null;
  }
  const entry = resolveNotifyRouteEntry(auto, routes);
  if (!entry) return null;
  const id = (auto.automation_id || '').trim();
  const name = auto.name.trim();
  const category = auto.dalton_notify_category ?? '';
  let winningKey: string | null = null;
  if (category === 'custom') {
    winningKey = id ? `automation:${id}` : null;
  } else {
    const candidates = [
      id ? `automation:${id}` : '',
      name,
      name.toLowerCase(),
      `category:${category}`,
      category,
    ];
    for (const key of candidates) {
      if (key && Array.isArray(routes[key])) {
        winningKey = key;
        break;
      }
    }
  }
  return { list: entry.list, winningKey };
}

/**
 * Count distinct tasks whose effective winning route explicitly delivers to `target`.
 *
 * When the full `automations` list is available, resolves each task's single
 * winning route with the same precedence as dispatch (`automation:<id>` > display
 * name > category, and `data:<scope>` for data tasks) and deduplicates `data:`
 * keys against already-counted tasks so no task is counted twice. When
 * `automations` is unavailable, only strictly unambiguous `automation:<id>` /
 * `data:<scope>` keys yield a number; any category fallback key or legacy
 * display-name key alongside an ID key returns `null` so the dialog shows the
 * positive non-numeric copy instead of risking double-counting.
 */
export function countDistinctWeixinRouteTasks(
  routes: Record<string, string[]> | null | undefined,
  target: string,
  automations: Automation[] | null | undefined,
): number | null {
  if (!routes || typeof routes !== 'object') return 0;
  if (Array.isArray(automations)) {
    const seenIds = new Set<string>();
    const coveredDataKeys = new Set<string>();
    const matchedTaskKeys = new Set<string>();
    for (const auto of automations) {
      const id = (auto.automation_id || '').trim();
      if (id && seenIds.has(id)) continue;
      if (id) seenIds.add(id);
      if (auto.dalton_notify_category === 'data_task') {
        const scope = extractDataTaskScope(auto);
        if (scope) coveredDataKeys.add(`data:${scope.toLowerCase()}`);
      }
      const resolved = resolveWinningRouteForTaskCount(auto, routes);
      if (resolved?.winningKey?.trim().toLowerCase().startsWith('data:')) {
        coveredDataKeys.add(resolved.winningKey.trim().toLowerCase());
      }
      if (resolved && resolved.list.includes(target)) {
        matchedTaskKeys.add(id ? `automation:${id}` : auto.name.trim().toLowerCase());
      }
    }
    for (const [key, list] of Object.entries(routes)) {
      if (!Array.isArray(list) || !list.includes(target)) continue;
      const cleanedKey = key.trim();
      if (cleanedKey.startsWith('automation:')) {
        const id = cleanedKey.slice('automation:'.length).trim();
        if (id && !seenIds.has(id)) {
          matchedTaskKeys.add(`automation:${id}`);
        }
      } else if (cleanedKey.startsWith('data:')) {
        const normalizedDataKey = cleanedKey.toLowerCase();
        if (!coveredDataKeys.has(normalizedDataKey)) {
          matchedTaskKeys.add(normalizedDataKey);
        }
      }
    }
    return matchedTaskKeys.size;
  }

  const dedupedKeys = new Set<string>();
  let hasIdKey = false;
  let hasLegacyNameKey = false;
  for (const [key, list] of Object.entries(routes)) {
    if (!Array.isArray(list) || !list.includes(target)) continue;
    if (isCategoryOrFallbackRouteKey(key)) {
      return null;
    }
    const normalized = key.trim().toLowerCase();
    if (normalized.startsWith('automation:')) {
      hasIdKey = true;
    } else if (!normalized.startsWith('data:')) {
      hasLegacyNameKey = true;
    }
    dedupedKeys.add(normalized);
  }
  if (hasIdKey && hasLegacyNameKey) {
    return null;
  }
  return dedupedKeys.size;
}

export interface DaltonNotifyTargetsCardProps {
  /** Only admins in Team Channels & Ops can manually enter raw target IDs. */
  allowManualInput?: boolean;
}

interface BoundCandidate {
  platform: 'feishu' | 'discord' | 'weixin';
  target: string;
  label: string;
  accountId: string;
}

/**
 * Push notification switches & linked-account recipient toggles.
 * Replaces raw `feishu:user:ou_xxx` text entry for members by deriving bound accounts
 * from `fetchDaltonChannelBindings()`.
 */
export function DaltonNotifyTargetsCard({
  allowManualInput = false,
}: DaltonNotifyTargetsCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasCapability } = useCapabilities();
  const canFeishu = hasCapability('channel_feishu');
  const canDiscord = hasCapability('channel_discord');
  const canWeixin = hasCapability('channel_weixin');
  const { preferences, isLoading: prefsLoading, isLoaded } = usePreferences();
  const updatePrefs = useUpdatePreferences();

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: canFeishu || canDiscord || canWeixin,
    retry: false,
  });

  const [manualInput, setManualInput] = useState('');
  const [labelDrafts, setLabelDrafts] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState(false);
  const [pendingDisable, setPendingDisable] = useState<BoundCandidate | null>(null);
  const [testNotice, setTestNotice] = useState<{
    tone: 'success' | 'error';
    message: string;
  } | null>(null);

  const mp = modelPrefs(preferences);
  // Backend defaults dalton_notify_enabled to False when unset or null (src/dalton/llm/model_prefs.py).
  const masterEnabled = mp.dalton_notify_enabled === true;
  const targets = useMemo(
    () => (Array.isArray(mp.dalton_notify_targets) ? mp.dalton_notify_targets : []),
    [mp.dalton_notify_targets],
  );
  const labels = useMemo(
    () => mp.dalton_notify_target_labels ?? {},
    [mp.dalton_notify_target_labels],
  );

  const boundCandidates = useMemo<BoundCandidate[]>(() => {
    const bindings = bindingsQuery.data?.bindings;
    const wx = bindingsQuery.data?.weixin_account;
    if (!bindings && !wx) return [];
    const list: BoundCandidate[] = [];
    if (canFeishu && bindings?.feishu?.linked && bindings.feishu.platform_user_id) {
      list.push({
        platform: 'feishu',
        target: `feishu:user:${bindings.feishu.platform_user_id}`,
        label: bindings.feishu.platform_username || bindings.feishu.platform_user_id,
        accountId: bindings.feishu.platform_user_id,
      });
    }
    if (canDiscord && bindings?.discord?.linked && bindings.discord.platform_user_id) {
      list.push({
        platform: 'discord',
        target: `discord:user:${bindings.discord.platform_user_id}`,
        label: bindings.discord.platform_username || bindings.discord.platform_user_id,
        accountId: bindings.discord.platform_user_id,
      });
    }
    if (canWeixin) {
      const wxActive =
        wx != null
          ? wx.status === 'active' &&
            wx.channel_enabled !== false &&
            wx.member_allowed !== false &&
            Boolean(wx.platform_user_id)
          : Boolean(
              bindings?.weixin?.linked &&
                bindings.weixin.platform_user_id &&
                bindings.weixin.status !== 'pending_verification',
            );
      const wxUserId = wx?.platform_user_id || bindings?.weixin?.platform_user_id || null;
      if (wxActive && wxUserId) {
        const masked = wx?.masked_ilink_user_id || bindings?.weixin?.masked_ilink_user_id;
        list.push({
          platform: 'weixin',
          target: `weixin:user:${wxUserId}`,
          label:
            wx?.ilink_username ||
            wx?.platform_username ||
            bindings?.weixin?.platform_username ||
            masked ||
            wxUserId,
          accountId: masked || wxUserId,
        });
      }
    }
    return list;
  }, [bindingsQuery.data?.bindings, bindingsQuery.data?.weixin_account, canFeishu, canDiscord, canWeixin]);

  const boundTargetSet = useMemo(
    () => new Set(boundCandidates.map((c) => c.target)),
    [boundCandidates],
  );

  const customTargets = useMemo(
    () => targets.filter((item) => !boundTargetSet.has(item)),
    [targets, boundTargetSet],
  );

  const saveTargets = async (nextTargets: string[]) => {
    setFormError(null);
    setSavedNotice(false);
    try {
      await updatePrefs.mutateAsync(
        splitPreferenceWrite({ dalton_notify_targets: nextTargets }),
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      setSavedNotice(true);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setFormError(
        typeof detail === 'string' && detail.trim()
          ? detail
          : t('dalton.notifyTargets.saveFailed'),
      );
    }
  };

  const handleMasterToggle = async (checked: boolean) => {
    setFormError(null);
    setSavedNotice(false);
    try {
      await updatePrefs.mutateAsync(
        splitPreferenceWrite({ dalton_notify_enabled: checked }),
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      setSavedNotice(true);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setFormError(
        typeof detail === 'string' && detail.trim()
          ? detail
          : t('dalton.notifyTargets.saveFailed'),
      );
    }
  };

  const handleCandidateToggle = async (item: BoundCandidate, checked: boolean) => {
    if (!checked) {
      setPendingDisable(item);
      return;
    }
    const next = Array.from(new Set([...targets, item.target]));
    await saveTargets(next);
  };

  const confirmDisableCandidate = async () => {
    if (!pendingDisable) return;
    const targetToRemove = pendingDisable.target;
    setPendingDisable(null);
    await saveTargets(targets.filter((tItem) => tItem !== targetToRemove));
  };

  const handleManualAdd = async () => {
    const cleaned = manualInput.trim();
    if (GROUP_TARGET_RE.test(cleaned)) {
      setFormError(t('dalton.notifyTargets.groupTargetHint'));
      return;
    }
    if (
      !isValidManualTargetForCapabilities(cleaned, {
        feishu: canFeishu,
        discord: canDiscord,
        weixin: canWeixin,
      })
    ) {
      setFormError(
        t(
          canWeixin
            ? 'dalton.notifyTargets.manualAddErrorWithWeixin'
            : 'dalton.notifyTargets.manualAddError',
        ),
      );
      return;
    }
    if (targets.includes(cleaned)) {
      setManualInput('');
      setFormError(null);
      return;
    }
    const next = Array.from(new Set([...targets, cleaned]));
    await saveTargets(next);
    setManualInput('');
  };

  const commitLabel = async (target: string) => {
    const saved = labels[target] ?? '';
    const draftLabel = labelDrafts[target] ?? saved;
    const cleaned = cleanDaltonNotifyLabel(draftLabel);
    setLabelDrafts((prev) => ({ ...prev, [target]: cleaned }));
    if (cleaned === saved) return;
    setFormError(null);
    setSavedNotice(false);
    try {
      await updatePrefs.mutateAsync(
        splitPreferenceWrite({
          dalton_notify_target_labels: { ...labels, [target]: cleaned || null },
        }),
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      setSavedNotice(true);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setFormError(
        typeof detail === 'string' && detail.trim()
          ? detail
          : t('dalton.notifyTargets.saveFailed'),
      );
    }
  };

  const renderAliasInput = (target: string) => (
    <Input
      value={labelDrafts[target] ?? labels[target] ?? ''}
      disabled={updatePrefs.isPending}
      aria-label={t('dalton.notifyTargets.aliasAria', { target })}
      placeholder={t('dalton.notifyTargets.aliasPlaceholder')}
      data-testid={`dalton-notify-label-${target}`}
      onChange={(e) =>
        setLabelDrafts((prev) => ({ ...prev, [target]: e.target.value }))
      }
      onBlur={() => void commitLabel(target)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur();
      }}
      className="mt-1 h-9 md:h-7 w-full md:w-52 max-w-full text-xs"
    />
  );

  const handleRemoveTarget = async (target: string) => {
    await saveTargets(targets.filter((item) => item !== target));
  };

  const testMutation = useMutation({
    mutationFn: (target: string) => sendDaltonNotifyTest(target),
    onSuccess: (res, target) => {
      if (res.ok) {
        setTestNotice({
          tone: 'success',
          message: t('dalton.notifyTargets.testSuccess', { target }),
        });
      } else {
        setTestNotice({
          tone: 'error',
          message: res.detail || t('dalton.notifyTargets.testFailed'),
        });
      }
    },
    onError: () => {
      setTestNotice({ tone: 'error', message: t('dalton.notifyTargets.testFailed') });
    },
  });

  const cachedAutomations = useMemo<Automation[] | null>(() => {
    if (pendingDisable?.platform !== 'weixin') return null;
    const entries = queryClient.getQueriesData<{
      data?: { automations?: Automation[] };
      automations?: Automation[];
    }>({
      queryKey: queryKeys.automations.all,
    });
    for (const [, value] of entries) {
      if (!value) continue;
      if (Array.isArray(value.automations)) return value.automations;
      if (Array.isArray(value.data?.automations)) return value.data.automations;
    }
    return null;
  }, [queryClient, pendingDisable]);

  const automationsQuery = useQuery({
    queryKey: queryKeys.automations.list({ limit: AUTOMATIONS_PAGE_LIMIT, allPages: true }),
    queryFn: async () => {
      try {
        const all: Automation[] = [];
        let offset = 0;
        while (offset < 1000) {
          const res = await listAutomations({ limit: AUTOMATIONS_PAGE_LIMIT, offset });
          const page = res.data?.automations;
          if (!Array.isArray(page)) return null;
          all.push(...page);
          const total = typeof res.data?.total === 'number' ? res.data.total : all.length;
          if (all.length >= total || page.length < AUTOMATIONS_PAGE_LIMIT) {
            return all;
          }
          offset += page.length;
        }
        return all;
      } catch {
        return null;
      }
    },
    enabled:
      !allowManualInput &&
      canWeixin &&
      pendingDisable?.platform === 'weixin' &&
      cachedAutomations === null,
    retry: false,
    staleTime: 60_000,
  });

  const knownAutomations = cachedAutomations ?? automationsQuery.data ?? null;

  if (!canFeishu && !canDiscord && !canWeixin) {
    return null;
  }

  const pendingWeixinRouteCount =
    pendingDisable?.platform === 'weixin'
      ? countDistinctWeixinRouteTasks(
          mp.dalton_notify_routes,
          pendingDisable.target,
          knownAutomations,
        )
      : 0;

  const confirmDisableMessage =
    pendingDisable?.platform === 'weixin'
      ? pendingWeixinRouteCount !== null && pendingWeixinRouteCount > 0
        ? t('dalton.notifyTargets.disableBoundWeixinConfirmWithCount', {
            label: pendingDisable.label,
            count: pendingWeixinRouteCount,
          })
        : t('dalton.notifyTargets.disableBoundWeixinConfirm', {
            label: pendingDisable.label,
          })
      : t('dalton.notifyTargets.disableBoundConfirm', {
          label: pendingDisable?.label ?? '',
        });

  return (
    <DaltonSettingsCard
      id={allowManualInput ? 'dalton-notify-targets-admin' : 'dalton-notify-targets'}
      testId={allowManualInput ? 'dalton-notify-targets-admin-card' : 'dalton-notify-targets-card'}
      title={
        allowManualInput
          ? t('dalton.notifyTargets.manualAddTitle')
          : t('dalton.notifyTargets.title')
      }
      description={
        allowManualInput
          ? t(
              canWeixin
                ? 'dalton.notifyTargets.manualAddDescWithWeixin'
                : 'dalton.notifyTargets.manualAddDesc',
            )
          : t('dalton.notifyTargets.description')
      }
      layout="padded"
      bodyClassName="space-y-4"
      action={
        !allowManualInput && !prefsLoading && isLoaded ? (
          <DaltonStatusBadge
            dot
            tone={masterEnabled ? 'success' : 'neutral'}
            label={
              masterEnabled
                ? t('dalton.notifyTargets.masterOn')
                : t('dalton.notifyTargets.masterOff')
            }
          />
        ) : undefined
      }
    >
      {prefsLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : !isLoaded ? (
        <DaltonInlineNotice tone="error">{t('dalton.notifyTargets.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          {!allowManualInput && (
            <>
              <div
                className="rounded-md border"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  backgroundColor: 'var(--color-bg-elevated)',
                }}
              >
                <SettingRow
                  id="dalton-notify-master-toggle"
                  label={t('dalton.notifyTargets.masterSwitchLabel')}
                  description={t(
                    canWeixin
                      ? 'dalton.notifyTargets.masterSwitchDescWithWeixin'
                      : 'dalton.notifyTargets.masterSwitchDesc',
                  )}
                >
                  <ToggleSwitch
                    id="dalton-notify-master-toggle-switch"
                    ariaLabel={t('dalton.notifyTargets.masterSwitchLabel')}
                    checked={masterEnabled}
                    disabled={updatePrefs.isPending}
                    onChange={() => void handleMasterToggle(!masterEnabled)}
                  />
                </SettingRow>
              </div>

              <div className="space-y-2">
                <div>
                  <h4
                    className="text-xs font-medium"
                    style={{ color: 'var(--color-text-primary)' }}
                  >
                    {t('dalton.notifyTargets.boundAccountsTitle')}
                  </h4>
                  <p
                    className="text-xs mt-0.5"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {t(
                      canWeixin
                        ? 'dalton.notifyTargets.boundAccountsDescWithWeixin'
                        : 'dalton.notifyTargets.boundAccountsDesc',
                    )}
                  </p>
                </div>

                {bindingsQuery.isLoading ? (
                  <DaltonLoadingRow label={t('dalton.common.loading')} />
                ) : boundCandidates.length === 0 ? (
                  <DaltonInlineNotice tone="info" testId="dalton-notify-no-bound-accounts">
                    {t(
                      canWeixin
                        ? 'dalton.notifyTargets.noBoundAccountsWithWeixin'
                        : 'dalton.notifyTargets.noBoundAccounts',
                    )}
                  </DaltonInlineNotice>
                ) : (
                  <DaltonKeyValueList>
                    {boundCandidates.map((item) => {
                      const active = targets.includes(item.target);
                      const isTesting =
                        testMutation.isPending && testMutation.variables === item.target;
                      return (
                        <DaltonItemRow
                          key={item.target}
                          testId={`dalton-notify-bound-${item.platform}`}
                          title={
                            <span>
                              {item.platform === 'feishu'
                                ? t('dalton.notifyTargets.platformFeishu')
                                : item.platform === 'discord'
                                  ? t('dalton.notifyTargets.platformDiscord')
                                  : t('dalton.notifyTargets.platformWeixin')}
                              {' · '}
                              {item.label}
                              {labels[item.target] && (
                                <span
                                  className="ml-1.5 text-xs font-normal"
                                  style={{ color: 'var(--color-text-secondary)' }}
                                >
                                  ({labels[item.target]})
                                </span>
                              )}
                            </span>
                          }
                          subtitle={renderAliasInput(item.target)}
                          meta={<span className="font-mono max-md:break-all">{item.accountId}</span>}
                          trailing={
                            <div className="flex flex-wrap items-center gap-2">
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-8 gap-1 text-xs"
                                disabled={isTesting}
                                onClick={() => testMutation.mutate(item.target)}
                                data-testid={`dalton-notify-test-${item.platform}`}
                              >
                                <Send className="h-3.5 w-3.5" />
                                <span>
                                  {isTesting
                                    ? t('dalton.notifyTargets.testing')
                                    : t('dalton.notifyTargets.testButton')}
                                </span>
                              </Button>
                              <ToggleSwitch
                                checked={active}
                                disabled={updatePrefs.isPending}
                                ariaLabel={item.label}
                                onChange={() =>
                                  void handleCandidateToggle(item, !active)
                                }
                              />
                            </div>
                          }
                        />
                      );
                    })}
                  </DaltonKeyValueList>
                )}
              </div>
            </>
          )}

          {allowManualInput && (
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={manualInput}
                onChange={(e) => setManualInput(e.target.value)}
                placeholder={t(
                  canWeixin
                    ? 'dalton.notifyTargets.manualAddPlaceholderWithWeixin'
                    : 'dalton.notifyTargets.manualAddPlaceholder',
                )}
                data-testid="dalton-notify-manual-input"
                className="h-9 md:h-8 w-full md:w-72 text-xs font-mono"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                disabled={updatePrefs.isPending}
                onClick={() => void handleManualAdd()}
                data-testid="dalton-notify-manual-add"
              >
                {t('dalton.notifyTargets.manualAddButton')}
              </Button>
            </div>
          )}

          {(allowManualInput ? targets : customTargets).length > 0 && (
            <div className="space-y-2">
              {!allowManualInput && (
                <div>
                  <h4
                    className="text-xs font-medium"
                    style={{ color: 'var(--color-text-primary)' }}
                  >
                    {t('dalton.notifyTargets.customTargetsTitle')}
                  </h4>
                  <p
                    className="text-xs mt-0.5"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {t('dalton.notifyTargets.customTargetsDesc')}
                  </p>
                </div>
              )}
              <DaltonKeyValueList>
                {(allowManualInput ? targets : customTargets).map((target) => {
                  const isTesting =
                    testMutation.isPending && testMutation.variables === target;
                  return (
                    <DaltonItemRow
                      key={target}
                      testId={`dalton-notify-custom-${target}`}
                      title={
                        labels[target] ? (
                          <span className="text-xs font-medium break-words">{labels[target]}</span>
                        ) : (
                          <code className="text-xs font-mono max-md:break-all">{target}</code>
                        )
                      }
                      subtitle={renderAliasInput(target)}
                      meta={<code className="font-mono max-md:break-all">{target}</code>}
                      trailing={
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-8 gap-1 text-xs"
                            disabled={isTesting}
                            onClick={() => testMutation.mutate(target)}
                          >
                            <Send className="h-3.5 w-3.5" />
                            <span>
                              {isTesting
                                ? t('dalton.notifyTargets.testing')
                                : t('dalton.notifyTargets.testButton')}
                            </span>
                          </Button>
                          <DaltonActionConfirmButton
                            label={t('dalton.notifyTargets.removeLabel')}
                            confirmTitle={t('dalton.notifyTargets.removeTitle')}
                            confirmMessage={t('dalton.notifyTargets.removeConfirm', {
                              target,
                            })}
                            confirmLabel={t('dalton.notifyTargets.removeLabel')}
                            busy={updatePrefs.isPending}
                            onConfirm={() => handleRemoveTarget(target)}
                            testId={`dalton-notify-remove-${target}`}
                          />
                        </div>
                      }
                    />
                  );
                })}
              </DaltonKeyValueList>
            </div>
          )}

          {formError && <DaltonInlineNotice tone="error">{formError}</DaltonInlineNotice>}
          {savedNotice && (
            <DaltonInlineNotice tone="success">{t('dalton.notifyTargets.saved')}</DaltonInlineNotice>
          )}
          {testNotice && (
            <DaltonInlineNotice tone={testNotice.tone}>{testNotice.message}</DaltonInlineNotice>
          )}
        </>
      )}

      <ConfirmDialog
        open={Boolean(pendingDisable)}
        title={t('dalton.notifyTargets.disableBoundTitle')}
        message={confirmDisableMessage}
        confirmLabel={t('dalton.common.confirm')}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setPendingDisable(null);
        }}
        onConfirm={() => void confirmDisableCandidate()}
      />
    </DaltonSettingsCard>
  );
}
