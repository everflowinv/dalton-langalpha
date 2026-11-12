import React, { useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  HelpCircle,
  Loader2,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import { useDaltonSystemJobs } from '../../hooks/useOpenClaw';
import {
  type DaltonSystemJobItem,
  type DaltonSystemJobScope,
  type DaltonSystemJobStatus,
} from '../../utils/daltonApi';

// 行业层 P4：sector scope 的文案走 i18n（新增文案的约定），四个原有 scope
// 的文案保持原样不动。
type NonSectorScope = Exclude<DaltonSystemJobScope, 'sector'>;

type TranslationFn = ReturnType<typeof useTranslation>['t'];

const SCOPE_LABELS: Record<NonSectorScope, { title: string; subtitle: string }> = {
  company: {
    title: '公司自动跟踪',
    subtitle: '每家覆盖公司的四项标准自主跟踪任务（盯盘 / 内部资料增量 / 涨跌 ±4% 触发）',
  },
  home: {
    title: '全局任务（Home）',
    subtitle: '财报日历扫描、研究总监每日/每周复盘、数据地图每日体检',
  },
  host: {
    title: '宿主机任务',
    subtitle: '宿主机桥接层托管的 OpenClaw 团队对话每日归档',
  },
  ops: {
    title: '运维任务（只读）',
    subtitle: '宿主机 launchd 运维状态展示（数据库备份、后端看门狗）',
  },
};

const DEFAULT_SCOPES: DaltonSystemJobScope[] = ['company', 'home', 'host', 'ops'];

const SCOPE_SUMMARY_NAMES: Record<NonSectorScope, string> = {
  company: '公司跟踪',
  home: 'Home 全局',
  host: '宿主机归档',
  ops: '运维监控',
};

function scopeGroupLabels(
  scope: DaltonSystemJobScope,
  t: TranslationFn,
): { title: string; subtitle: string } {
  if (scope === 'sector') {
    return {
      title: t('dalton.systemJobs.sectorTitle'),
      subtitle: t('dalton.systemJobs.sectorSubtitle'),
    };
  }
  return SCOPE_LABELS[scope];
}

function scopeSummaryName(scope: DaltonSystemJobScope, t: TranslationFn): string {
  return scope === 'sector' ? t('dalton.systemJobs.sectorSummary') : SCOPE_SUMMARY_NAMES[scope];
}

function formatTimeZh(iso: string | null | undefined): string {
  if (!iso) return '暂无';
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return String(iso);
  return dt.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function formatDailyScheduleFromNextRun(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return null;
  const hh = String(dt.getHours()).padStart(2, '0');
  const mm = String(dt.getMinutes()).padStart(2, '0');
  return `每日 ${hh}:${mm}`;
}

function shouldShowJobLog(job: DaltonSystemJobItem): boolean {
  if (!job.log_tail) return false;
  if (job.exit_code != null) {
    return job.exit_code !== 0 && job.exit_code !== 2;
  }
  return Boolean(job.last_result && job.last_result.includes('失败'));
}

function StatusBadge({ status }: { status: DaltonSystemJobStatus }) {
  if (status === 'on') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
        <CheckCircle2 className="h-3 w-3" />
        开启
      </span>
    );
  }
  if (status === 'partial') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-600 dark:text-amber-400">
        <AlertTriangle className="h-3 w-3" />
        不完整
      </span>
    );
  }
  if (status === 'off') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
        <XCircle className="h-3 w-3" />
        关闭
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
      <HelpCircle className="h-3 w-3" />
      未知
    </span>
  );
}

export function DaltonSystemJobsCard({
  scopes = DEFAULT_SCOPES,
}: {
  scopes?: DaltonSystemJobScope[];
} = {}): React.ReactElement {
  const { t } = useTranslation();
  const [expandedLogKeys, setExpandedLogKeys] = useState<Record<string, boolean>>({});

  const { data, isLoading, isFetching, refetch, error: queryError } = useDaltonSystemJobs();

  const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
  const loading = isLoading || isFetching;
  const effectiveBanner = queryError
    ? {
        type: 'error' as const,
        text: queryError instanceof Error ? queryError.message : '加载系统自动任务失败',
      }
    : null;

  return (
    <div
      className="rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-system-jobs-card"
    >
      <Link
        to="/automations?scope=company"
        data-testid="system-jobs-open-company"
        className="mb-3 flex w-full items-center justify-center rounded-md px-3 py-2 text-sm font-semibold"
        style={{
          backgroundColor: 'var(--color-btn-primary-bg)',
          color: 'var(--color-btn-primary-text)',
        }}
      >
        前往 自动化 → 公司公共任务
      </Link>
      <div className="flex items-center justify-between gap-2 pb-3">
        <div>
          <h4 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            自动任务
          </h4>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {`只读查看系统底层定时任务的运行状态（${scopes.map((s) => scopeSummaryName(s, t)).join('、')}）`}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refetch()}
          disabled={loading}
          className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-background px-2.5 py-1 text-xs font-medium hover:bg-muted/60 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      {effectiveBanner && (
        <div
          className={`mb-3 rounded-md px-3 py-2 text-xs ${
            effectiveBanner.type === 'error'
              ? 'border border-destructive/40 bg-destructive/10 text-destructive'
              : 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
          }`}
        >
          {effectiveBanner.text}
        </div>
      )}

      {loading && jobs.length === 0 ? (
        <div className="flex items-center justify-center py-6 text-xs text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          正在加载自动任务清单...
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {scopes.map((scope) => {
            const groupJobs = jobs.filter((j) => j.scope === scope);
            const meta = scopeGroupLabels(scope, t);
            return (
              <div
                key={scope}
                className="rounded-md border border-border/50 bg-background/40 p-3"
                data-testid={`system-jobs-group-${scope}`}
              >
                <div className="mb-2 flex items-center justify-between">
                  <div>
                    <div className="text-xs font-semibold text-foreground">{meta.title}</div>
                    <div className="text-[11px] text-muted-foreground">{meta.subtitle}</div>
                  </div>
                  <span className="text-[11px] text-muted-foreground">{groupJobs.length} 项</span>
                </div>

                {groupJobs.length === 0 ? (
                  <div className="py-2 text-xs text-muted-foreground">暂无任务</div>
                ) : (
                  <div className="divide-y divide-border/40">
                    {groupJobs.map((job) => {
                      const displaySchedule =
                        job.schedule ||
                        (job.key === 'conversation-archive' || job.kind === 'conversation_archive'
                          ? formatDailyScheduleFromNextRun(job.next_run_at)
                          : null);
                      const showLog = shouldShowJobLog(job);
                      const isLogExpanded = Boolean(expandedLogKeys[job.key]);
                      return (
                        <div
                          key={job.key}
                          className="flex flex-col gap-1.5 py-2.5 first:pt-1 last:pb-1 sm:flex-row sm:items-center sm:justify-between"
                          data-testid={`system-job-row-${job.key}`}
                        >
                          <div className="min-w-0 flex-1 space-y-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-xs font-medium text-foreground">
                                {job.title || job.key}
                              </span>
                              <StatusBadge status={job.status} />
                              {displaySchedule && (
                                <span className="text-[11px] text-muted-foreground">
                                  ({displaySchedule})
                                </span>
                              )}
                            </div>

                            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                              <span className="inline-flex items-center gap-1">
                                <Clock className="h-3 w-3" />
                                上次运行：{formatTimeZh(job.last_run_at)}
                                {job.last_result ? ` · 结果：${job.last_result}` : ''}
                              </span>
                              <span>下次运行：{formatTimeZh(job.next_run_at)}</span>
                            </div>

                            {job.config && (
                              <div className="text-[11px] text-muted-foreground">
                                抽取配置（只读）：模型{' '}
                                <code className="rounded bg-muted px-1 py-0.5">
                                  {job.config.extractor_model || job.config.model}
                                </code>{' '}
                                · Prompt 版本{' '}
                                <code className="rounded bg-muted px-1 py-0.5">
                                  {job.config.prompt_version}
                                </code>{' '}
                                · 并发度{' '}
                                <code className="rounded bg-muted px-1 py-0.5">
                                  {job.config.extract_concurrency ?? job.config.concurrency}
                                </code>
                              </div>
                            )}

                            {showLog && (
                              <div className="pt-0.5">
                                <button
                                  type="button"
                                  onClick={() =>
                                    setExpandedLogKeys((prev) => ({
                                      ...prev,
                                      [job.key]: !prev[job.key],
                                    }))
                                  }
                                  data-testid={`system-job-log-toggle-${job.key}`}
                                  className="inline-flex items-center gap-1 rounded border border-border/50 bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                                >
                                  {isLogExpanded ? '收起日志' : '查看日志'}
                                </button>
                                {isLogExpanded && (
                                  <pre
                                    data-testid={`system-job-log-${job.key}`}
                                    className="mt-1 max-h-96 max-h-[24lh] overflow-auto rounded bg-muted/60 p-1.5 font-mono text-[10px] leading-4 text-muted-foreground"
                                  >
                                    {job.log_tail}
                                  </pre>
                                )}
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
