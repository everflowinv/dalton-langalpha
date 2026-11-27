import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ChevronDown, ChevronRight, RefreshCw, RotateCcw, Save } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { apiErrorDetailMessage, apiErrorStatus } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { ToggleSwitch } from '@/components/ui/switch';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';
import { queryKeys } from '@/lib/queryKeys';
import {
  WATCHLIST_TIERS,
  previewWatchlistTierPolicy,
  reconcileWatchlist,
  resetWatchlistTierPolicy,
  updateWatchlistTierPolicy,
  useWatchlistTierPolicies,
  type WatchlistCronPreset,
  type WatchlistPreviewResponse,
  type WatchlistTier,
  type WatchlistTierPolicy,
} from '@/lib/dalton/watchlist';

const DEFAULT_WATCH_PRESETS: readonly WatchlistCronPreset[] = [
  { id: 'every_30m', cron: '*/30 6-20 * * 1-5', label_zh: '每 30 分钟（交易日 6:00–20:00）', label_en: 'Every 30 min (Mon–Fri 6:00–20:00)' },
  { id: 'hourly', cron: '0 6-20 * * 1-5', label_zh: '每小时（交易日 6:00–20:00）', label_en: 'Hourly (Mon–Fri 6:00–20:00)' },
  { id: 'every_2h', cron: '0 6-20/2 * * 1-5', label_zh: '每 2 小时（交易日 6:00–20:00）', label_en: 'Every 2 hours (Mon–Fri 6:00–20:00)' },
  { id: 'premarket_daily', cron: '30 7 * * 1-5', label_zh: '每日盘前一次（交易日 07:30）', label_en: 'Once pre-market (Mon–Fri 07:30)' },
];

const DEFAULT_DELTA_PRESETS: readonly WatchlistCronPreset[] = [
  { id: 'daily_trading', cron: '0 11 * * 2-6', label_zh: '每交易日一次（周二至周六 11:00）', label_en: 'Daily after trading (Tue–Sat 11:00)' },
  { id: 'twice_weekly', cron: '0 11 * * 3,6', label_zh: '每周两次（周三、周六 11:00）', label_en: 'Twice weekly (Wed & Sat 11:00)' },
  { id: 'weekly', cron: '0 11 * * 6', label_zh: '每周一次（周六 11:00）', label_en: 'Weekly (Sat 11:00)' },
];

const MOVE_THRESHOLD_PRESETS = [4, 6, 8] as const;
const EARNINGS_STAGE_ORDER = ['tm3', 't0', 'tp1'] as const;
const COMMON_TIMEZONES = [
  'Asia/Shanghai',
  'America/New_York',
  'Asia/Hong_Kong',
  'UTC',
  'Europe/London',
  'Asia/Tokyo',
] as const;

const FALLBACK_POLICIES: Record<WatchlistTier, WatchlistTierPolicy> = {
  core: {
    tier: 'core',
    policy_version: 1,
    watch_enabled: true,
    watch_cron: '*/30 6-20 * * 1-5',
    delta_enabled: true,
    delta_cron: '0 11 * * 2-6',
    delta_timezone: 'Asia/Shanghai',
    move_enabled: true,
    move_threshold_pct: 4,
    earnings_stages: ['tm3', 't0', 'tp1'],
  },
  watch: {
    tier: 'watch',
    policy_version: 1,
    watch_enabled: true,
    watch_cron: '30 7 * * 1-5',
    delta_enabled: true,
    delta_cron: '0 11 * * 6',
    delta_timezone: 'Asia/Shanghai',
    move_enabled: true,
    move_threshold_pct: 4,
    earnings_stages: ['t0'],
  },
  radar: {
    tier: 'radar',
    policy_version: 1,
    watch_enabled: false,
    watch_cron: '30 7 * * 1-5',
    delta_enabled: false,
    delta_cron: '0 11 * * 6',
    delta_timezone: 'Asia/Shanghai',
    move_enabled: false,
    move_threshold_pct: 6,
    earnings_stages: [],
  },
};

function isFiveFieldCron(expr: string): boolean {
  const parts = expr.trim().split(/\s+/).filter(Boolean);
  return parts.length === 5;
}

function policiesEqual(a: WatchlistTierPolicy, b: WatchlistTierPolicy): boolean {
  return (
    a.watch_enabled === b.watch_enabled &&
    a.watch_cron.trim() === b.watch_cron.trim() &&
    a.delta_enabled === b.delta_enabled &&
    a.delta_cron.trim() === b.delta_cron.trim() &&
    a.delta_timezone.trim() === b.delta_timezone.trim() &&
    a.move_enabled === b.move_enabled &&
    Number(a.move_threshold_pct) === Number(b.move_threshold_pct) &&
    JSON.stringify([...a.earnings_stages].sort()) === JSON.stringify([...b.earnings_stages].sort())
  );
}

interface PendingPolicyPreview {
  action: 'set_tier_policy' | 'reset_tier_policy';
  tier: WatchlistTier;
  overrideUserPause: boolean;
  preview: WatchlistPreviewResponse;
}

interface NoticeState {
  tone: 'success' | 'warning' | 'error' | 'info';
  message: string;
  partialJobId?: string | null;
}

/** Personal three-tier watchlist monitoring policy configuration card (`#dalton-tier-policies`). */
export function DaltonTierPolicyCard(): React.ReactElement {
  const { t, i18n } = useTranslation();
  const isZh = (i18n.language || '').toLowerCase().startsWith('zh');
  const queryClient = useQueryClient();

  const { data, isLoading, isError, refetch } = useWatchlistTierPolicies(true);
  const [selectedTier, setSelectedTier] = useState<WatchlistTier>('core');
  const [drafts, setDrafts] = useState<Record<WatchlistTier, WatchlistTierPolicy>>(FALLBACK_POLICIES);
  const [watchAdvancedOpen, setWatchAdvancedOpen] = useState(false);
  const [deltaAdvancedOpen, setDeltaAdvancedOpen] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [commitBusy, setCommitBusy] = useState(false);
  const [retryingJob, setRetryingJob] = useState(false);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [pendingPreview, setPendingPreview] = useState<PendingPolicyPreview | null>(null);
  const [modalError, setModalError] = useState<string | null>(null);

  const serverPolicies = useMemo<Record<WatchlistTier, WatchlistTierPolicy>>(
    () => ({
      core: data?.policies?.core ?? FALLBACK_POLICIES.core,
      watch: data?.policies?.watch ?? FALLBACK_POLICIES.watch,
      radar: data?.policies?.radar ?? FALLBACK_POLICIES.radar,
    }),
    [data?.policies],
  );

  useEffect(() => {
    if (data?.policies) {
      setDrafts({
        core: { ...data.policies.core, earnings_stages: [...data.policies.core.earnings_stages] },
        watch: { ...data.policies.watch, earnings_stages: [...data.policies.watch.earnings_stages] },
        radar: { ...data.policies.radar, earnings_stages: [...data.policies.radar.earnings_stages] },
      });
    }
  }, [data?.policies]);

  const watchPresets = data?.presets?.watch_cron?.length
    ? data.presets.watch_cron
    : DEFAULT_WATCH_PRESETS;
  const deltaPresets = data?.presets?.delta_cron?.length
    ? data.presets.delta_cron
    : DEFAULT_DELTA_PRESETS;

  const activeDraft = drafts[selectedTier];
  const activeSaved = serverPolicies[selectedTier];
  const isDirty = !policiesEqual(activeDraft, activeSaved);

  const watchPresetValue = watchPresets.some((p) => p.cron === activeDraft.watch_cron.trim())
    ? activeDraft.watch_cron.trim()
    : '__custom__';
  const deltaPresetValue = deltaPresets.some((p) => p.cron === activeDraft.delta_cron.trim())
    ? activeDraft.delta_cron.trim()
    : '__custom__';

  const timezoneOptions = useMemo(() => {
    const set = new Set<string>(COMMON_TIMEZONES);
    if (activeDraft.delta_timezone) set.add(activeDraft.delta_timezone);
    return Array.from(set);
  }, [activeDraft.delta_timezone]);

  const patchDraft = useCallback(
    (patch: Partial<WatchlistTierPolicy>) => {
      setNotice(null);
      setDrafts((prev) => ({
        ...prev,
        [selectedTier]: {
          ...prev[selectedTier],
          ...patch,
        },
      }));
    },
    [selectedTier],
  );

  const toggleEarningsStage = useCallback(
    (stage: string) => {
      setNotice(null);
      setDrafts((prev) => {
        const cur = new Set(prev[selectedTier].earnings_stages);
        if (cur.has(stage)) cur.delete(stage);
        else cur.add(stage);
        const ordered = EARNINGS_STAGE_ORDER.filter((s) => cur.has(s));
        return {
          ...prev,
          [selectedTier]: {
            ...prev[selectedTier],
            earnings_stages: ordered,
          },
        };
      });
    },
    [selectedTier],
  );

  const runPreview = useCallback(
    async (
      action: 'set_tier_policy' | 'reset_tier_policy',
      tier: WatchlistTier,
      overrideUserPause = false,
    ) => {
      setNotice(null);
      setModalError(null);

      if (action === 'set_tier_policy') {
        const d = drafts[tier];
        if (!isFiveFieldCron(d.watch_cron)) {
          setNotice({ tone: 'error', message: t('settings:dalton.tierPolicies.errors.invalidWatchCron') });
          return;
        }
        if (!isFiveFieldCron(d.delta_cron)) {
          setNotice({ tone: 'error', message: t('settings:dalton.tierPolicies.errors.invalidDeltaCron') });
          return;
        }
        const pct = Number(d.move_threshold_pct);
        if (!Number.isFinite(pct) || pct < 1 || pct > 30) {
          setNotice({ tone: 'error', message: t('settings:dalton.tierPolicies.errors.invalidThreshold') });
          return;
        }
      }

      setPreviewBusy(true);
      try {
        const d = drafts[tier];
        const preview = await previewWatchlistTierPolicy({
          tier,
          action,
          ...(action === 'set_tier_policy'
            ? {
                watch_enabled: d.watch_enabled,
                watch_cron: d.watch_cron.trim(),
                delta_enabled: d.delta_enabled,
                delta_cron: d.delta_cron.trim(),
                delta_timezone: d.delta_timezone.trim() || 'Asia/Shanghai',
                move_enabled: d.move_enabled,
                move_threshold_pct: Number(d.move_threshold_pct),
                earnings_stages: d.earnings_stages,
              }
            : {}),
          override_user_pause: overrideUserPause,
        });
        if (!preview.ok || preview.precheck_error) {
          setNotice({
            tone: 'error',
            message: preview.precheck_error || t('settings:dalton.tierPolicies.errors.previewFailed'),
          });
          return;
        }
        setPendingPreview({ action, tier, overrideUserPause, preview });
      } catch (err) {
        setNotice({
          tone: 'error',
          message: apiErrorDetailMessage(err) || t('settings:dalton.tierPolicies.errors.previewFailed'),
        });
      } finally {
        setPreviewBusy(false);
      }
    },
    [drafts, t],
  );

  const handleConfirmCommit = useCallback(async () => {
    if (!pendingPreview || !pendingPreview.preview.snapshot_id) return;
    setCommitBusy(true);
    setModalError(null);
    try {
      const payload = {
        snapshot_id: pendingPreview.preview.snapshot_id,
        expected_policy_version: pendingPreview.preview.expected_policy_version ?? undefined,
        expected_roster_hash: pendingPreview.preview.expected_roster_hash ?? undefined,
        override_user_pause: pendingPreview.overrideUserPause,
      };
      const res =
        pendingPreview.action === 'reset_tier_policy'
          ? await resetWatchlistTierPolicy(pendingPreview.tier, payload)
          : await updateWatchlistTierPolicy(pendingPreview.tier, payload);

      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlistTierPolicies() }),
        queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() }),
      ]);
      setPendingPreview(null);
      if (res.reconcile_status === 'partial') {
        setNotice({
          tone: 'warning',
          message: t('settings:dalton.tierPolicies.partialSaved'),
          partialJobId: res.job_id ?? null,
        });
      } else {
        setNotice({
          tone: 'success',
          message:
            pendingPreview.action === 'reset_tier_policy'
              ? t('settings:dalton.tierPolicies.resetSuccess')
              : t('settings:dalton.tierPolicies.saveSuccess'),
        });
      }
    } catch (err) {
      if (apiErrorStatus(err) === 409) {
        setModalError(t('settings:dalton.tierPolicies.errors.scopeChanged'));
      } else {
        setModalError(apiErrorDetailMessage(err) || t('settings:dalton.tierPolicies.errors.saveFailed'));
      }
    } finally {
      setCommitBusy(false);
    }
  }, [pendingPreview, queryClient, t]);

  const handleRetryPartial = useCallback(async () => {
    if (retryingJob) return;
    setRetryingJob(true);
    try {
      const res = await reconcileWatchlist(notice?.partialJobId ?? undefined);
      if (res?.status === 'completed') {
        setNotice({ tone: 'success', message: t('settings:dalton.tierPolicies.reconcileDone') });
      } else {
        setNotice({
          tone: 'warning',
          message: t('settings:dalton.tierPolicies.partialSaved'),
          partialJobId: notice?.partialJobId,
        });
      }
    } catch (err) {
      setNotice({
        tone: 'error',
        message: apiErrorDetailMessage(err) || t('settings:dalton.tierPolicies.errors.saveFailed'),
      });
    } finally {
      setRetryingJob(false);
    }
  }, [notice?.partialJobId, retryingJob, t]);

  const tierLabel = (tier: WatchlistTier) => t(`settings:dalton.tierPolicies.tiers.${tier}`);
  const presetLabel = (p: WatchlistCronPreset) => (isZh ? p.label_zh : p.label_en);

  return (
    <>
      <DaltonSettingsCard
        id="dalton-tier-policies"
        testId="dalton-tier-policy-card"
        title={t('settings:dalton.tierPoliciesLabel')}
        description={t('settings:dalton.tierPoliciesDesc')}
        layout="padded"
        action={
          <div className="flex items-center gap-1.5">
            <DaltonStatusBadge
              tone={isDirty ? 'warning' : 'neutral'}
              label={
                isDirty
                  ? t('settings:dalton.tierPolicies.unsavedBadge')
                  : `v${activeSaved.policy_version}`
              }
            />
          </div>
        }
        below={
          notice ? (
            <div className="mt-2">
              <DaltonInlineNotice tone={notice.tone} testId="dalton-tier-policy-notice">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span>{notice.message}</span>
                  {notice.tone === 'warning' && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={retryingJob}
                      onClick={() => void handleRetryPartial()}
                    >
                      <RefreshCw className="h-3 w-3 mr-1" />
                      {t('settings:dalton.tierPolicies.retryReconcile')}
                    </Button>
                  )}
                </div>
              </DaltonInlineNotice>
            </div>
          ) : undefined
        }
      >
        {isLoading ? (
          <DaltonLoadingRow />
        ) : isError ? (
          <div className="flex items-center justify-between py-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            <span>{t('settings:dalton.tierPolicies.errors.loadFailed')}</span>
            <Button type="button" size="sm" variant="outline" onClick={() => void refetch()}>
              {t('common.retry')}
            </Button>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Tier selector tabs */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="inline-flex rounded-lg border p-0.5" style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-bg-page)' }} role="tablist">
                {WATCHLIST_TIERS.map((tier) => {
                  const active = selectedTier === tier;
                  return (
                    <button
                      key={tier}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      data-testid={`tier-policy-tab-${tier}`}
                      onClick={() => {
                        setSelectedTier(tier);
                        setNotice(null);
                      }}
                      className="px-3 py-1.5 rounded-md text-xs font-medium transition-colors"
                      style={{
                        backgroundColor: active ? 'var(--color-btn-primary-bg)' : 'transparent',
                        color: active ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
                      }}
                    >
                      {tierLabel(tier)}
                    </button>
                  );
                })}
              </div>
              <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings:dalton.tierPolicies.effectiveScopeHint')}
              </span>
            </div>

            {/* 1. Intraday Watch */}
            <div className="rounded-lg border p-3 space-y-2.5" style={{ borderColor: 'var(--color-border-muted)' }}>
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {t('settings:dalton.tierPolicies.watch.title')}
                  </div>
                  <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings:dalton.tierPolicies.watch.desc')}
                  </div>
                </div>
                <ToggleSwitch
                  checked={activeDraft.watch_enabled}
                  onChange={() => patchDraft({ watch_enabled: !activeDraft.watch_enabled })}
                  aria-label={t('settings:dalton.tierPolicies.watch.title')}
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  aria-label={t('settings:dalton.tierPolicies.watch.presetLabel')}
                  data-testid="tier-policy-watch-preset"
                  value={watchPresetValue}
                  disabled={!activeDraft.watch_enabled}
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === '__custom__') {
                      setWatchAdvancedOpen(true);
                    } else {
                      patchDraft({ watch_cron: val });
                    }
                  }}
                  className="text-xs rounded-md border px-2.5 py-1.5 min-w-[220px]"
                  style={{
                    backgroundColor: 'var(--color-bg-input)',
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  {watchPresets.map((p) => (
                    <option key={p.id} value={p.cron}>
                      {presetLabel(p)}
                    </option>
                  ))}
                  <option value="__custom__">{t('settings:dalton.tierPolicies.customCronOption')}</option>
                </select>
                <button
                  type="button"
                  data-testid="tier-policy-watch-advanced-toggle"
                  onClick={() => setWatchAdvancedOpen((v) => !v)}
                  className="inline-flex items-center gap-1 text-xs hover:underline"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {watchAdvancedOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                  {t('settings:dalton.tierPolicies.advancedCron')}
                </button>
              </div>
              {watchAdvancedOpen && (
                <div className="space-y-1 pt-1">
                  <input
                    type="text"
                    aria-label={t('settings:dalton.tierPolicies.watch.cronInputAria')}
                    data-testid="tier-policy-watch-cron-input"
                    value={activeDraft.watch_cron}
                    onChange={(e) => patchDraft({ watch_cron: e.target.value })}
                    placeholder="*/30 6-20 * * 1-5"
                    className="w-full max-w-xs font-mono text-xs rounded-md border px-2.5 py-1.5"
                    style={{
                      backgroundColor: 'var(--color-bg-input)',
                      borderColor: 'var(--color-border-muted)',
                      color: 'var(--color-text-primary)',
                    }}
                  />
                  <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings:dalton.tierPolicies.watch.dstHint')}
                  </div>
                </div>
              )}
            </div>

            {/* 2. Thesis Delta */}
            <div className="rounded-lg border p-3 space-y-2.5" style={{ borderColor: 'var(--color-border-muted)' }}>
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {t('settings:dalton.tierPolicies.delta.title')}
                  </div>
                  <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings:dalton.tierPolicies.delta.desc')}
                  </div>
                </div>
                <ToggleSwitch
                  checked={activeDraft.delta_enabled}
                  onChange={() => patchDraft({ delta_enabled: !activeDraft.delta_enabled })}
                  aria-label={t('settings:dalton.tierPolicies.delta.title')}
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  aria-label={t('settings:dalton.tierPolicies.delta.presetLabel')}
                  data-testid="tier-policy-delta-preset"
                  value={deltaPresetValue}
                  disabled={!activeDraft.delta_enabled}
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === '__custom__') {
                      setDeltaAdvancedOpen(true);
                    } else {
                      patchDraft({ delta_cron: val });
                    }
                  }}
                  className="text-xs rounded-md border px-2.5 py-1.5 min-w-[220px]"
                  style={{
                    backgroundColor: 'var(--color-bg-input)',
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  {deltaPresets.map((p) => (
                    <option key={p.id} value={p.cron}>
                      {presetLabel(p)}
                    </option>
                  ))}
                  <option value="__custom__">{t('settings:dalton.tierPolicies.customCronOption')}</option>
                </select>
                <button
                  type="button"
                  data-testid="tier-policy-delta-advanced-toggle"
                  onClick={() => setDeltaAdvancedOpen((v) => !v)}
                  className="inline-flex items-center gap-1 text-xs hover:underline"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {deltaAdvancedOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                  {t('settings:dalton.tierPolicies.advancedCron')}
                </button>
              </div>
              {deltaAdvancedOpen && (
                <div className="space-y-1.5 pt-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      type="text"
                      aria-label={t('settings:dalton.tierPolicies.delta.cronInputAria')}
                      data-testid="tier-policy-delta-cron-input"
                      value={activeDraft.delta_cron}
                      onChange={(e) => patchDraft({ delta_cron: e.target.value })}
                      placeholder="0 11 * * 2-6"
                      className="w-48 font-mono text-xs rounded-md border px-2.5 py-1.5"
                      style={{
                        backgroundColor: 'var(--color-bg-input)',
                        borderColor: 'var(--color-border-muted)',
                        color: 'var(--color-text-primary)',
                      }}
                    />
                    <select
                      aria-label={t('settings:dalton.tierPolicies.delta.timezoneAria')}
                      data-testid="tier-policy-delta-timezone"
                      value={activeDraft.delta_timezone}
                      onChange={(e) => patchDraft({ delta_timezone: e.target.value })}
                      className="text-xs rounded-md border px-2.5 py-1.5"
                      style={{
                        backgroundColor: 'var(--color-bg-input)',
                        borderColor: 'var(--color-border-muted)',
                        color: 'var(--color-text-primary)',
                      }}
                    >
                      {timezoneOptions.map((tz) => (
                        <option key={tz} value={tz}>
                          {tz}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings:dalton.tierPolicies.delta.dstHint')}
                  </div>
                </div>
              )}
            </div>

            {/* 3. Price Move Alert */}
            <div className="rounded-lg border p-3 space-y-2.5" style={{ borderColor: 'var(--color-border-muted)' }}>
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                    {t('settings:dalton.tierPolicies.move.title')}
                  </div>
                  <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings:dalton.tierPolicies.move.desc')}
                  </div>
                </div>
                <ToggleSwitch
                  checked={activeDraft.move_enabled}
                  onChange={() => patchDraft({ move_enabled: !activeDraft.move_enabled })}
                  aria-label={t('settings:dalton.tierPolicies.move.title')}
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {MOVE_THRESHOLD_PRESETS.map((pct) => {
                  const selected = Number(activeDraft.move_threshold_pct) === pct;
                  return (
                    <button
                      key={pct}
                      type="button"
                      disabled={!activeDraft.move_enabled}
                      onClick={() => patchDraft({ move_threshold_pct: pct })}
                      className="px-2.5 py-1 rounded-md border text-xs font-mono disabled:opacity-50"
                      style={{
                        backgroundColor: selected ? 'var(--color-btn-primary-bg)' : 'var(--color-bg-page)',
                        color: selected ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
                        borderColor: 'var(--color-border-muted)',
                      }}
                    >
                      ±{pct}%
                    </button>
                  );
                })}
                <div className="inline-flex items-center gap-1">
                  <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>±</span>
                  <input
                    type="number"
                    min={1}
                    max={30}
                    step={0.5}
                    aria-label={t('settings:dalton.tierPolicies.move.thresholdAria')}
                    data-testid="tier-policy-move-threshold"
                    disabled={!activeDraft.move_enabled}
                    value={activeDraft.move_threshold_pct}
                    onChange={(e) => {
                      const val = parseFloat(e.target.value);
                      patchDraft({ move_threshold_pct: Number.isNaN(val) ? 0 : val });
                    }}
                    className="w-20 font-mono text-xs rounded-md border px-2 py-1"
                    style={{
                      backgroundColor: 'var(--color-bg-input)',
                      borderColor: 'var(--color-border-muted)',
                      color: 'var(--color-text-primary)',
                    }}
                  />
                  <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>%</span>
                </div>
              </div>
            </div>

            {/* 4. Earnings Auto-Write */}
            <div className="rounded-lg border p-3 space-y-2" style={{ borderColor: 'var(--color-border-muted)' }}>
              <div>
                <div className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {t('settings:dalton.tierPolicies.earnings.title')}
                </div>
                <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings:dalton.tierPolicies.earnings.desc')}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                {EARNINGS_STAGE_ORDER.map((stage) => {
                  const checked = activeDraft.earnings_stages.includes(stage);
                  return (
                    <label
                      key={stage}
                      className="inline-flex items-center gap-1.5 text-xs cursor-pointer"
                      style={{ color: 'var(--color-text-primary)' }}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        data-testid={`tier-policy-stage-${stage}`}
                        onChange={() => toggleEarningsStage(stage)}
                      />
                      <span>{t(`settings:dalton.tierPolicies.earnings.stages.${stage}`)}</span>
                    </label>
                  );
                })}
              </div>
            </div>

            {/* Footer actions */}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <Button
                type="button"
                size="sm"
                variant="outline"
                data-testid="tier-policy-reset-btn"
                disabled={previewBusy}
                onClick={() => void runPreview('reset_tier_policy', selectedTier, false)}
              >
                <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
                {t('settings:dalton.tierPolicies.resetDefault')}
              </Button>
              <Button
                type="button"
                size="sm"
                data-testid="tier-policy-save-btn"
                disabled={!isDirty || previewBusy}
                onClick={() => void runPreview('set_tier_policy', selectedTier, false)}
              >
                <Save className="h-3.5 w-3.5 mr-1.5" />
                {previewBusy
                  ? t('settings:dalton.tierPolicies.previewing')
                  : t('settings:dalton.tierPolicies.saveChanges')}
              </Button>
            </div>
          </div>
        )}
      </DaltonSettingsCard>

      {/* Server-issued snapshot confirmation modal */}
      {pendingPreview && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t('settings:dalton.tierPolicies.confirmModal.title')}
          data-testid="tier-policy-confirm-modal"
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'var(--color-bg-overlay)' }}
          onClick={() => !commitBusy && setPendingPreview(null)}
        >
          <div
            className="w-full max-w-md rounded-xl border p-5 shadow-xl space-y-3"
            style={{
              backgroundColor: 'var(--color-bg-card)',
              borderColor: 'var(--color-border-muted)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
              {pendingPreview.preview.card_title || t('settings:dalton.tierPolicies.confirmModal.title')}
            </h3>
            {pendingPreview.preview.card_description && (
              <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {pendingPreview.preview.card_description}
              </p>
            )}

            <div
              className="rounded-md border p-2.5 text-xs space-y-1.5"
              style={{ borderColor: 'var(--color-border-muted)', backgroundColor: 'var(--color-bg-page)' }}
            >
              <div style={{ color: 'var(--color-text-primary)' }}>
                {t('settings:dalton.tierPolicies.confirmModal.impactSummary', {
                  companies: pendingPreview.preview.target_tickers.length,
                  workspaces: pendingPreview.preview.affected_workspaces.length,
                })}
              </div>
              <div className="flex flex-wrap gap-2 text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                <span>
                  {t('chat.watchlistConfirm.summary.create', {
                    count: pendingPreview.preview.task_summary.create_count ?? 0,
                  })}
                </span>
                <span>
                  {t('chat.watchlistConfirm.summary.update', {
                    count: pendingPreview.preview.task_summary.update_count ?? 0,
                  })}
                </span>
                <span>
                  {t('chat.watchlistConfirm.summary.resume', {
                    count: pendingPreview.preview.task_summary.resume_count ?? 0,
                  })}
                </span>
                <span>
                  {t('chat.watchlistConfirm.summary.pause', {
                    count: pendingPreview.preview.task_summary.pause_count ?? 0,
                  })}
                </span>
              </div>
            </div>

            <label
              className="flex items-center gap-2 text-xs cursor-pointer"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              <input
                type="checkbox"
                checked={pendingPreview.overrideUserPause}
                disabled={previewBusy || commitBusy}
                onChange={(e) =>
                  void runPreview(pendingPreview.action, pendingPreview.tier, e.target.checked)
                }
              />
              <span>{t('settings:dalton.tierPolicies.confirmModal.overrideUserPause')}</span>
            </label>

            {modalError && (
              <div className="flex items-start justify-between gap-2 text-xs" style={{ color: 'var(--color-warning)' }}>
                <div className="flex items-start gap-1.5">
                  <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                  <span>{modalError}</span>
                </div>
                <button
                  type="button"
                  className="underline shrink-0"
                  onClick={() =>
                    void runPreview(
                      pendingPreview.action,
                      pendingPreview.tier,
                      pendingPreview.overrideUserPause,
                    )
                  }
                >
                  {t('settings:dalton.tierPolicies.confirmModal.refreshPreview')}
                </button>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-1">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={commitBusy}
                onClick={() => setPendingPreview(null)}
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="button"
                size="sm"
                data-testid="tier-policy-confirm-btn"
                disabled={commitBusy || previewBusy}
                onClick={() => void handleConfirmCommit()}
              >
                {commitBusy
                  ? t('settings:dalton.tierPolicies.saving')
                  : t('settings:dalton.tierPolicies.confirmModal.confirm')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default DaltonTierPolicyCard;
