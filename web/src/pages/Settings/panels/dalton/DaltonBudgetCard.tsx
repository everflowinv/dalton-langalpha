import { useEffect, useMemo, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import TimezonePicker from '@/components/TimezonePicker';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useLocale } from '@/hooks/useLocale';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { useAllWorkspaces } from '@/hooks/useAllWorkspaces';
import { formatTimestamp } from '@/lib/format';
import {
  useDaltonBudget,
  useDaltonBudgetToday,
  useDeleteDaltonTaskCap,
  useUpdateDaltonBudget,
  useUpsertDaltonTaskCap,
} from '../../hooks/useOpenClaw';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueGrid,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

function formatUsd(n: number | null | undefined): string {
  const val = typeof n === 'number' && Number.isFinite(n) ? n : 0;
  return `$${val.toFixed(2)}`;
}

/**
 * Personal daily USD budget & per-workspace task cap settings card.
 * Scoped to the current signed-in user per `/api/v1/dalton/budget*` backend routes.
 */
export function DaltonBudgetCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const { isPm, callerTier } = useTeamCredentials();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);

  const budgetQuery = useDaltonBudget(true);
  const todayQuery = useDaltonBudgetToday(true);
  const workspacesQuery = useAllWorkspaces({ includeHome: false });
  const updateBudget = useUpdateDaltonBudget();
  const upsertCap = useUpsertDaltonTaskCap();
  const deleteCap = useDeleteDaltonTaskCap();

  const [dailyCapInput, setDailyCapInput] = useState('');
  const [defaultTaskCapInput, setDefaultTaskCapInput] = useState('');
  const [timezoneInput, setTimezoneInput] = useState('Asia/Shanghai');

  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState('');
  const [wsCapInput, setWsCapInput] = useState('');
  const [capError, setCapError] = useState<string | null>(null);
  const [budgetSaved, setBudgetSaved] = useState(false);
  const [budgetError, setBudgetError] = useState<string | null>(null);
  const [runsOpen, setRunsOpen] = useState(false);

  const settings = budgetQuery.data;
  const today = todayQuery.data;
  const workspaces = useMemo(() => {
    const items = workspacesQuery.data?.workspaces ?? [];
    return items.filter((w) => !w.is_home);
  }, [workspacesQuery.data?.workspaces]);

  useEffect(() => {
    if (!settings) return;
    setDailyCapInput(
      settings.daily_cap_usd != null ? String(settings.daily_cap_usd) : '0',
    );
    setDefaultTaskCapInput(
      settings.default_task_cap_usd != null ? String(settings.default_task_cap_usd) : '',
    );
    setTimezoneInput(settings.timezone || 'Asia/Shanghai');
  }, [settings]);

  const handleSaveBudget = async () => {
    setBudgetSaved(false);
    setBudgetError(null);
    const parsedDaily = Number(dailyCapInput.trim() || '0');
    if (!Number.isFinite(parsedDaily) || parsedDaily < 0) {
      setBudgetError(t('dalton.budget.errors.invalidCap'));
      return;
    }
    let parsedTaskCap: number | null = null;
    if (defaultTaskCapInput.trim() !== '') {
      const n = Number(defaultTaskCapInput.trim());
      if (!Number.isFinite(n) || n <= 0) {
        setBudgetError(t('dalton.budget.errors.invalidTaskCap'));
        return;
      }
      parsedTaskCap = n;
    }
    try {
      await updateBudget.mutateAsync({
        daily_cap_usd: parsedDaily,
        default_task_cap_usd: parsedTaskCap,
        timezone: timezoneInput || 'Asia/Shanghai',
      });
      setBudgetSaved(true);
    } catch {
      setBudgetError(t('dalton.budget.errors.saveFailed'));
    }
  };

  const handleUpsertCap = async () => {
    setCapError(null);
    if (!selectedWorkspaceId) {
      setCapError(t('dalton.budget.errors.selectWorkspace'));
      return;
    }
    const rawCap = wsCapInput.trim();
    if (!rawCap) {
      setCapError(t('dalton.budget.errors.emptyCap'));
      return;
    }
    const parsed = Number(rawCap);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setCapError(t('dalton.budget.errors.invalidTaskCap'));
      return;
    }
    try {
      await upsertCap.mutateAsync({
        scopeKind: 'workspace',
        scopeId: selectedWorkspaceId,
        capUsd: parsed,
      });
      setWsCapInput('');
    } catch {
      setCapError(t('dalton.budget.errors.saveFailed'));
    }
  };

  const handleDeleteCap = async (scopeId: string) => {
    setCapError(null);
    try {
      await deleteCap.mutateAsync({ scopeKind: 'workspace', scopeId });
    } catch {
      setCapError(t('dalton.budget.errors.deleteFailed'));
    }
  };

  const gateEnabled = today?.budget_gate_enabled !== false;
  const runs = today?.runs ?? [];
  const taskCaps = settings?.task_caps ?? [];
  const capCount = settings?.daily_cap_usd ?? today?.cap_usd ?? 0;

  return (
    <DaltonSettingsCard
      id="dalton-budget"
      testId="dalton-budget-card"
      title={t('dalton.budget.title')}
      description={t('dalton.budget.description')}
      layout="padded"
      bodyClassName="space-y-5"
      action={
        <div className="flex items-center gap-2">
          {today && (
            <DaltonStatusBadge
              dot
              tone={gateEnabled ? 'success' : 'warning'}
              label={
                gateEnabled
                  ? t('dalton.budget.gateOnLabel')
                  : t('dalton.budget.gateOffLabel')
              }
            />
          )}
          <DaltonRefreshButton
            busy={budgetQuery.isFetching || todayQuery.isFetching}
            onClick={() => {
              void budgetQuery.refetch();
              void todayQuery.refetch();
            }}
          />
        </div>
      }
    >
      {budgetQuery.isLoading && todayQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : budgetQuery.isError ? (
        <DaltonInlineNotice tone="error">{t('dalton.common.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          <DaltonKeyValueGrid
            columns={2}
            items={[
              {
                testId: 'dalton-budget-cash-today',
                label: t('dalton.budget.todayCashLabel'),
                value: `${formatUsd(today?.spent_usd)} / ${formatUsd(capCount)}`,
                hint: t('dalton.budget.todayCashNote'),
              },
              {
                testId: 'dalton-budget-sub-today',
                label: t('dalton.budget.todaySubEquivLabel'),
                value: formatUsd(today?.subscription_notional_usd),
              },
            ]}
          />

          {today && !gateEnabled && (
            <DaltonInlineNotice tone="warning" testId="dalton-budget-gate-off">
              {isAdmin ? t('dalton.budget.gateOffAdminDesc') : t('dalton.budget.gateOffMemberDesc')}
            </DaltonInlineNotice>
          )}

          <div
            className="rounded-md border divide-y"
            style={{
              borderColor: 'var(--color-border-muted)',
              backgroundColor: 'var(--color-bg-elevated)',
            }}
          >
            <SettingRow
              id="dalton-budget-daily-cap"
              label={t('dalton.budget.dailyCapLabel')}
              description={t('dalton.budget.dailyCapHint')}
              htmlFor="dalton-budget-daily-cap-input"
            >
              <Input
                id="dalton-budget-daily-cap-input"
                type="number"
                min="0"
                step="0.5"
                value={dailyCapInput}
                onChange={(e) => {
                  setDailyCapInput(e.target.value);
                  setBudgetSaved(false);
                }}
                className="w-36 h-8 text-xs tabular-nums"
              />
            </SettingRow>

            <SettingRow
              id="dalton-budget-task-cap"
              label={t('dalton.budget.defaultTaskCapLabel')}
              description={t('dalton.budget.defaultTaskCapHint')}
              htmlFor="dalton-budget-task-cap-input"
            >
              <Input
                id="dalton-budget-task-cap-input"
                type="number"
                min="0"
                step="0.5"
                placeholder="—"
                value={defaultTaskCapInput}
                onChange={(e) => {
                  setDefaultTaskCapInput(e.target.value);
                  setBudgetSaved(false);
                }}
                className="w-36 h-8 text-xs tabular-nums"
              />
            </SettingRow>

            <SettingRow
              id="dalton-budget-timezone"
              label={t('dalton.budget.timezoneLabel')}
            >
              <TimezonePicker
                value={timezoneInput}
                onChange={(tz) => {
                  setTimezoneInput(tz);
                  setBudgetSaved(false);
                }}
                className="w-56"
              />
            </SettingRow>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('dalton.budget.byokCashNote')}
            </p>
            <div className="flex items-center gap-2">
              {budgetSaved && (
                <DaltonStatusBadge tone="success" label={t('dalton.budget.saved')} />
              )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                disabled={updateBudget.isPending}
                onClick={() => void handleSaveBudget()}
                data-testid="dalton-budget-save"
              >
                {updateBudget.isPending ? t('dalton.common.saving') : t('dalton.common.save')}
              </Button>
            </div>
          </div>

          {budgetError && (
            <DaltonInlineNotice tone="error">{budgetError}</DaltonInlineNotice>
          )}

          {/* Per-workspace task cap overrides using workspace dropdown */}
          <div className="space-y-2.5 pt-2 border-t" style={{ borderColor: 'var(--color-border-muted)' }}>
            <div>
              <h4 className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('dalton.budget.workspaceCapsTitle')}
              </h4>
              <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('dalton.budget.workspaceCapsDesc')}
              </p>
            </div>

            {workspaces.length === 0 ? (
              <DaltonInlineNotice tone="info">
                {t('dalton.budget.noWorkspacesAvailable')}
              </DaltonInlineNotice>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  value={selectedWorkspaceId}
                  onChange={(e) => setSelectedWorkspaceId(e.target.value)}
                  aria-label={t('dalton.budget.workspaceSelectPlaceholder')}
                  data-testid="dalton-budget-workspace-select"
                  className="w-56 text-xs"
                >
                  <option value="">{t('dalton.budget.workspaceSelectPlaceholder')}</option>
                  {workspaces.map((ws) => (
                    <option key={ws.workspace_id} value={ws.workspace_id}>
                      {ws.name}
                    </option>
                  ))}
                </Select>

                <Input
                  type="number"
                  min="0"
                  step="0.5"
                  placeholder={t('dalton.budget.workspaceCapPlaceholder')}
                  value={wsCapInput}
                  onChange={(e) => setWsCapInput(e.target.value)}
                  data-testid="dalton-budget-workspace-cap-input"
                  className="h-8 w-44 text-xs tabular-nums"
                />

                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 text-xs"
                  disabled={upsertCap.isPending}
                  onClick={() => void handleUpsertCap()}
                  data-testid="dalton-budget-workspace-cap-save"
                >
                  {t('dalton.budget.addOrUpdateCap')}
                </Button>
              </div>
            )}

            {capError && <DaltonInlineNotice tone="error">{capError}</DaltonInlineNotice>}

            {taskCaps.length > 0 && (
              <DaltonKeyValueList>
                {taskCaps.map((cap) => {
                  const matchedWs = workspaces.find((w) => w.workspace_id === cap.scope_id);
                  const displayName = cap.scope_name || matchedWs?.name || cap.scope_id;
                  return (
                    <DaltonItemRow
                      key={`${cap.scope_kind}:${cap.scope_id}`}
                      title={displayName}
                      trailing={
                        <div className="flex items-center gap-2">
                          <span
                            className="text-xs font-medium tabular-nums"
                            style={{ color: 'var(--color-text-secondary)' }}
                          >
                            {t('dalton.budget.capUnit', { amount: cap.cap_usd.toFixed(2) })}
                          </span>
                          <DaltonActionConfirmButton
                            label={t('dalton.budget.removeCapLabel')}
                            confirmTitle={t('dalton.budget.removeCapTitle')}
                            confirmMessage={t('dalton.budget.removeCapConfirm', {
                              name: displayName,
                            })}
                            confirmLabel={t('dalton.common.delete')}
                            busy={deleteCap.isPending}
                            onConfirm={() => handleDeleteCap(cap.scope_id)}
                            testId={`dalton-budget-cap-delete-${cap.scope_id}`}
                          />
                        </div>
                      }
                    />
                  );
                })}
              </DaltonKeyValueList>
            )}
          </div>

          {/* Collapsible today spend ledger */}
          <div className="pt-2 border-t" style={{ borderColor: 'var(--color-border-muted)' }}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 px-1.5 text-xs gap-1"
              onClick={() => setRunsOpen((v) => !v)}
              data-testid="dalton-budget-runs-toggle"
            >
              <ChevronDown
                className={`h-3.5 w-3.5 transition-transform ${runsOpen ? 'rotate-180' : ''}`}
              />
              <span>{t('dalton.budget.runsTitle', { count: runs.length })}</span>
            </Button>

            <Disclosure open={runsOpen}>
              <div className="mt-2">
                <DaltonKeyValueList
                  isEmpty={runs.length === 0}
                  empty={t('dalton.budget.runsEmpty')}
                >
                  {runs.map((run) => {
                    const matchedWs = workspaces.find((w) => w.workspace_id === run.workspace_id);
                    const statusLabel =
                      run.state === 'open'
                        ? t('dalton.budget.statusActive')
                        : run.state === 'released'
                          ? t('dalton.budget.statusRolledBack')
                          : t('dalton.budget.statusFinalized');
                    const statusTone =
                      run.state === 'open'
                        ? 'accent'
                        : run.state === 'settled'
                          ? 'success'
                          : 'neutral';
                    return (
                      <DaltonItemRow
                        key={run.run_ref}
                        title={
                          <span className="text-xs font-mono">
                            {matchedWs?.name ?? run.run_ref.slice(0, 12)}
                          </span>
                        }
                        meta={formatTimestamp(run.created_at, locale)}
                        trailing={
                          <div className="flex items-center gap-2">
                            <DaltonStatusBadge tone={statusTone} label={statusLabel} />
                            <span className="text-xs tabular-nums font-medium">
                              {formatUsd(run.charge_usd)}
                            </span>
                          </div>
                        }
                      />
                    );
                  })}
                </DaltonKeyValueList>
              </div>
            </Disclosure>
          </div>
        </>
      )}
    </DaltonSettingsCard>
  );
}
