import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/Disclosure';
import { useLocale } from '@/hooks/useLocale';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { formatTimestamp } from '@/lib/format';
import type { DaltonSystemJobItem, DaltonSystemJobScope } from '@/pages/Settings/utils/daltonApi';
import { useDaltonSystemJobs } from '../../hooks/useOpenClaw';
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
} from '@/components/dalton/primitives';

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
 * Read-only by design: running or pausing company jobs lives on Automations →
 * company tasks, not here. `host` and `ops` scopes are shown only to admins
 * (never based on the client-side advanced-mode toggle).
 */
export function DaltonSystemJobsCard({
  scopes,
}: {
  scopes?: readonly string[];
} = {}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { isPm, callerTier } = useTeamCredentials();
  const { hasCapability } = useCapabilities();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);
  const showOpsJobs = isAdmin && hasCapability('data_jobs');

  const jobsQuery = useDaltonSystemJobs(true);

  const [expandedLogs, setExpandedLogs] = useState<Set<string>>(new Set());

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
    () => (showOpsJobs ? jobs.filter((job) => OPS_SCOPES.has(job.scope)) : []),
    [showOpsJobs, jobs],
  );

  const toggleLog = (key: string) => {
    setExpandedLogs((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderJobList = (items: DaltonSystemJobItem[], emptyText: string) => (
    <DaltonKeyValueList isEmpty={items.length === 0} empty={emptyText}>
      {items.map((job) => {
        const logOpen = expandedLogs.has(job.key);

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
        <div className="flex flex-wrap items-center gap-2 min-w-0">
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
