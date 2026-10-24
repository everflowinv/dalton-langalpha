import React, { useCallback, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  HelpCircle,
  Loader2,
  PauseCircle,
  Play,
  RefreshCw,
  Wrench,
  XCircle,
} from 'lucide-react';
import { ToggleSwitch } from '@/components/ui/switch';
import {
  useDaltonSystemJobs,
  useRunDaltonSystemJobNow,
  useToggleDaltonSystemJob,
} from '../../hooks/useOpenClaw';
import {
  type DaltonSystemJobItem,
  type DaltonSystemJobScope,
  type DaltonSystemJobStatus,
} from '../../utils/daltonApi';

const SCOPE_LABELS: Record<DaltonSystemJobScope, { title: string; subtitle: string }> = {
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
    subtitle: '宿主机 launchd 运维状态展示（数据库备份、日志轮转、后端看门狗）',
  },
};

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

export function DaltonSystemJobsCard(): React.ReactElement {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ type: 'info' | 'error'; text: string } | null>(null);

  const { data, isLoading, isFetching, refetch, error: queryError } = useDaltonSystemJobs();
  const toggleMutation = useToggleDaltonSystemJob();
  const runNowMutation = useRunDaltonSystemJobNow();

  const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
  const loading = isLoading || isFetching;

  const handleToggle = useCallback(
    async (job: DaltonSystemJobItem, nextEnabled: boolean) => {
      if (!job.can_toggle || busyKey) return;
      setBusyKey(job.key);
      setBanner(null);
      try {
        const res = await toggleMutation.mutateAsync({ key: job.key, enabled: nextEnabled });
        if (res?.ok === true) {
          setBanner({
            type: 'info',
            text: `${job.title || job.key} 已${nextEnabled ? '开启' : '关闭'}`,
          });
        } else {
          setBanner({
            type: 'error',
            text: res?.error || res?.message || '切换任务状态失败',
          });
        }
      } catch (err: unknown) {
        const respData = (
          err as { response?: { data?: { detail?: string; error?: string; message?: string } } }
        )?.response?.data;
        const msg =
          respData?.detail ||
          respData?.error ||
          respData?.message ||
          (err instanceof Error ? err.message : '切换任务状态失败');
        setBanner({ type: 'error', text: msg });
      } finally {
        setBusyKey(null);
      }
    },
    [busyKey, toggleMutation],
  );

  const handleRunNow = useCallback(
    async (job: DaltonSystemJobItem) => {
      if (!job.can_run_now || busyKey) return;
      setBusyKey(job.key);
      setBanner(null);
      try {
        const res = await runNowMutation.mutateAsync(job.key);
        if (res?.ok === true) {
          setBanner({
            type: 'info',
            text:
              res.message ||
              `已异步触发「${job.title || job.key}」，请稍后在 ${res.where_to_check || '当前面板'} 查看结果`,
          });
        } else {
          setBanner({
            type: 'error',
            text: res?.error || res?.message || '触发立即运行失败',
          });
        }
      } catch (err: unknown) {
        const respData = (
          err as { response?: { data?: { detail?: string; error?: string; message?: string } } }
        )?.response?.data;
        const msg =
          respData?.detail ||
          respData?.error ||
          respData?.message ||
          (err instanceof Error ? err.message : '触发立即运行失败');
        setBanner({ type: 'error', text: msg });
      } finally {
        setBusyKey(null);
      }
    },
    [busyKey, runNowMutation],
  );

  const effectiveBanner =
    banner ||
    (queryError
      ? {
          type: 'error' as const,
          text: queryError instanceof Error ? queryError.message : '加载系统自动任务失败',
        }
      : null);

  const scopes: DaltonSystemJobScope[] = ['company', 'home', 'host', 'ops'];

  return (
    <div
      className="rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-system-jobs-card"
    >
      <div className="flex items-center justify-between gap-2 pb-3">
        <div>
          <h4 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
            自动任务
          </h4>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            统一查看、开关与立即触发系统内所有自动运行任务（公司跟踪、Home 全局、宿主机归档、运维监控）
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
            const meta = SCOPE_LABELS[scope];
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
                      const isBusy = busyKey === job.key;
                      const isChecked = job.status === 'on';
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
                              {job.schedule && (
                                <span className="text-[11px] text-muted-foreground">
                                  ({job.schedule})
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

                            {job.log_tail && (
                              <pre className="mt-1 max-h-24 overflow-auto rounded bg-muted/60 p-1.5 font-mono text-[10px] text-muted-foreground">
                                {job.log_tail}
                              </pre>
                            )}
                          </div>

                          <div className="flex shrink-0 items-center gap-2 pt-1 sm:pt-0">
                            {job.scope !== 'ops' && job.status === 'partial' && job.can_toggle && (
                              <>
                                <button
                                  type="button"
                                  disabled={isBusy}
                                  onClick={() => void handleToggle(job, false)}
                                  data-testid={`system-job-pause-all-${job.key}`}
                                  className="inline-flex items-center gap-1 rounded bg-zinc-500/15 px-2 py-1 text-[11px] font-medium text-zinc-700 hover:bg-zinc-500/25 disabled:opacity-50 dark:text-zinc-300"
                                >
                                  <PauseCircle className="h-3 w-3" />
                                  全部暂停
                                </button>
                                <button
                                  type="button"
                                  disabled={isBusy}
                                  onClick={() => void handleToggle(job, true)}
                                  data-testid={`system-job-repair-${job.key}`}
                                  className="inline-flex items-center gap-1 rounded bg-amber-500/15 px-2 py-1 text-[11px] font-medium text-amber-700 hover:bg-amber-500/25 disabled:opacity-50 dark:text-amber-300"
                                >
                                  <Wrench className="h-3 w-3" />
                                  修复
                                </button>
                              </>
                            )}

                            {job.scope !== 'ops' && job.can_run_now && (
                              <button
                                type="button"
                                disabled={isBusy}
                                onClick={() => void handleRunNow(job)}
                                className="inline-flex items-center gap-1 rounded border border-border/60 bg-background px-2 py-1 text-[11px] font-medium text-foreground hover:bg-muted/60 disabled:opacity-50"
                                data-testid={`system-job-run-${job.key}`}
                              >
                                {isBusy ? (
                                  <Loader2 className="h-3 w-3 animate-spin" />
                                ) : (
                                  <Play className="h-3 w-3" />
                                )}
                                立即运行
                              </button>
                            )}

                            {job.scope !== 'ops' && job.can_toggle ? (
                              <ToggleSwitch
                                checked={isChecked}
                                disabled={isBusy}
                                onChange={() => void handleToggle(job, !isChecked)}
                                ariaLabel={`切换 ${job.title || job.key}`}
                              />
                            ) : (
                              <span className="text-[11px] text-muted-foreground">只读</span>
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
