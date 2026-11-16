import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, Play } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { ToggleSwitch } from '@/components/ui/switch';
import { useLocale } from '@/hooks/useLocale';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { formatTimestamp } from '@/lib/format';
import type { DaltonSystemJobItem, DaltonSystemJobScope } from '@/pages/Settings/utils/daltonApi';
import {
  useDaltonSystemJobs,
  useRunDaltonSystemJobNow,
  useToggleDaltonSystemJob,
} from '../../hooks/useOpenClaw';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonNavButton,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
  type DaltonStatusTone,
} from './primitives';

const OPS_SCOPES = new Set<DaltonSystemJobScope>(['host', 'ops']);

function jobStatusTone(item: DaltonSystemJobItem): DaltonStatusTone {
  if (item.running) return 'accent';
  if (item.status === 'on') return 'success';
  if (item.status === 'partial') return 'warning';
  return 'neutral';
}

function jobStatusLabel(
  item: DaltonSystemJobItem,
  t: (k: string) => string,
): string {
  if (item.running) return t('dalton.systemJobs.statusRunning');
  if (item.status === 'on') return t('dalton.systemJobs.statusSuccess');
  if (item.status === 'partial') return t('dalton.systemJobs.statusFailed');
  return t('dalton.systemJobs.statusIdle');
}

function scopeLabel(scope: DaltonSystemJobScope, t: (k: string, opts?: Record<string, unknown>) => string): string {
  return t(`dalton.systemJobs.scopeLabels.${scope}`, { defaultValue: scope });
}

/**
 * Team automated system jobs card inside Team Management -> Team Channels & Ops.
 * Strict permission isolation: `host` and `ops` scopes are rendered ONLY when admin
 * (never based on any client-side advanced-mode toggle), while `data`/research scopes
 * follow backend-returned `can_run_now`/`can_toggle` capabilities.
 */
export function DaltonSystemJobsCard({
  scopes,
}: {
  scopes?: readonly string[];
} = {}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { isPm, callerTier } = useTeamCredentials();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);

  const jobsQuery = useDaltonSystemJobs(true);
  const toggleJob = useToggleDaltonSystemJob();
  const runJob = useRunDaltonSystemJobNow();

  const [expandedLogs, setExpandedLogs] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);

  const jobs = useMemo(() => {
    const raw = jobsQuery.data?.jobs ?? [];
    if (!scopes || scopes.length === 0) return raw;
    const allow = new Set(scopes);
    return raw.filter((j) => allow.has(j.scope));
  }, [jobsQuery.data?.jobs, scopes]);

  const teamJobs = useMemo(
    () => jobs.filter((job) => !OPS_SCOPES.has(job.scope)),
    [jobs],
  );
  const opsJobs = useMemo(
    () => (isAdmin ? jobs.filter((job) => OPS_SCOPES.has(job.scope)) : []),
    [isAdmin, jobs],
  );

  const toggleLog = (key: string) => {
    setExpandedLogs((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleToggle = async (job: DaltonSystemJobItem, enabled: boolean) => {
    setNotice(null);
    try {
      await toggleJob.mutateAsync({
        key: job.key,
        enabled,
      });
    } catch {
      setNotice({ tone: 'error', message: t('dalton.common.saveFailed') });
    }
  };

  const handleRunNow = async (job: DaltonSystemJobItem) => {
    setNotice(null);
    try {
      await runJob.mutateAsync(job.key);
      setNotice({
        tone: 'success',
        message: t('dalton.systemJobs.triggeredSuccess', {
          name: job.title || job.key,
        }),
      });
    } catch {
      setNotice({ tone: 'error', message: t('dalton.systemJobs.triggeredFailed') });
    }
  };

  const renderJobList = (items: DaltonSystemJobItem[], emptyText: string) => (
    <DaltonKeyValueList isEmpty={items.length === 0} empty={emptyText}>
      {items.map((job) => {
        const logOpen = expandedLogs.has(job.key);
        const isToggling =
          toggleJob.isPending && toggleJob.variables?.key === job.key;
        const isRunningNow =
          runJob.isPending && runJob.variables === job.key;
        const isOpsScope = OPS_SCOPES.has(job.scope);
        const canControl = (isOpsScope ? isAdmin : true) && job.can_run_now;
        const canToggle = (isOpsScope ? isAdmin : true) && job.can_toggle;

        return (
          <DaltonItemRow
            key={job.key}
            testId={`dalton-system-job-${job.key}`}
            title={
              <span className="flex flex-wrap items-center gap-2">
                <span>{job.title || job.key}</span>
                <DaltonStatusBadge tone="neutral" label={scopeLabel(job.scope, t)} />
                <DaltonStatusBadge
                  dot
                  tone={jobStatusTone(job)}
                  label={jobStatusLabel(job, t)}
                />
              </span>
            }
            subtitle={job.description}
            meta={
              <span className="flex flex-wrap items-center gap-3">
                {job.schedule && <span>{job.schedule}</span>}
                <span>
                  {job.last_run_at
                    ? t('dalton.systemJobs.lastRun', {
                        time: formatTimestamp(job.last_run_at, locale),
                      })
                    : t('dalton.systemJobs.neverRun')}
                </span>
                {job.next_run_at && (
                  <span>
                    {t('dalton.systemJobs.nextRun', {
                      time: formatTimestamp(job.next_run_at, locale),
                    })}
                  </span>
                )}
              </span>
            }
            trailing={
              <div className="flex items-center gap-2">
                {job.log_tail && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-8 gap-1 px-2 text-xs"
                    onClick={() => toggleLog(job.key)}
                  >
                    {logOpen ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                    <span>
                      {logOpen
                        ? t('dalton.systemJobs.hideLogs')
                        : t('dalton.systemJobs.showLogs')}
                    </span>
                  </Button>
                )}
                {canControl && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8 gap-1 text-xs"
                    disabled={isRunningNow || job.running}
                    onClick={() => void handleRunNow(job)}
                    data-testid={`dalton-system-job-run-${job.key}`}
                  >
                    <Play className="h-3 w-3" />
                    <span>
                      {isRunningNow
                        ? t('dalton.systemJobs.triggering')
                        : t('dalton.systemJobs.triggerNow')}
                    </span>
                  </Button>
                )}
                {canToggle && (
                  <ToggleSwitch
                    checked={job.enabled !== false}
                    disabled={isToggling}
                    ariaLabel={job.title || job.key}
                    onChange={() => void handleToggle(job, job.enabled === false)}
                  />
                )}
              </div>
            }
          >
            {job.log_tail && (
              <Disclosure open={logOpen}>
                <pre
                  className="mt-1 overflow-x-auto rounded border p-2.5 text-[11px] font-mono leading-5 whitespace-pre-wrap"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    backgroundColor: 'var(--color-bg-card)',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  {job.log_tail}
                </pre>
              </Disclosure>
            )}
          </DaltonItemRow>
        );
      })}
    </DaltonKeyValueList>
  );

  return (
    <DaltonSettingsCard
      id="dalton-system-jobs"
      testId="dalton-system-jobs-card"
      title={t('dalton.systemJobs.title')}
      description={t('dalton.systemJobs.description')}
      layout="padded"
      bodyClassName="space-y-5"
      action={
        <div className="flex items-center gap-2">
          <DaltonNavButton
            to="/automations"
            label={t('dalton.systemJobs.openAutomations')}
            testId="dalton-system-jobs-automations-link"
          />
          <DaltonRefreshButton
            busy={jobsQuery.isFetching}
            onClick={() => void jobsQuery.refetch()}
            testId="dalton-system-jobs-refresh"
          />
        </div>
      }
    >
      {jobsQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : jobsQuery.isError ? (
        <DaltonInlineNotice tone="error">{t('dalton.systemJobs.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          {notice && <DaltonInlineNotice tone={notice.tone}>{notice.message}</DaltonInlineNotice>}

          <div className="space-y-2" data-testid="dalton-system-jobs-team-section">
            <div>
              <h4 className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('dalton.systemJobs.teamSectionTitle')}
              </h4>
              <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('dalton.systemJobs.teamSectionDesc')}
              </p>
            </div>
            {renderJobList(teamJobs, t('dalton.systemJobs.empty'))}
          </div>

          {isAdmin && opsJobs.length > 0 && (
            <div
              className="space-y-2 pt-3 border-t"
              style={{ borderColor: 'var(--color-border-muted)' }}
              data-testid="dalton-system-jobs-ops-section"
            >
              <div>
                <h4 className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {t('dalton.systemJobs.opsSectionTitle')}
                </h4>
                <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('dalton.systemJobs.opsSectionDesc')}
                </p>
              </div>
              {renderJobList(opsJobs, t('dalton.systemJobs.empty'))}
            </div>
          )}
        </>
      )}
    </DaltonSettingsCard>
  );
}
