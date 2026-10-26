/* eslint-disable react-refresh/only-export-components */
import React, { useState } from 'react';
import { Activity, AlertTriangle, ChevronDown, ChevronUp, Clock, PauseCircle, Wrench } from 'lucide-react';
import { useUpdateWorkspaceTracking, useWorkspaceTracking } from './useWorkspaceTracking';
import {
  fetchWorkspaceTracking,
  updateWorkspaceTracking,
  type WorkspaceTrackingResponse,
  type WorkspaceTrackingTask,
} from '../../utils/api/workspaces';

export {
  fetchWorkspaceTracking,
  updateWorkspaceTracking,
  useUpdateWorkspaceTracking,
  useWorkspaceTracking,
  type WorkspaceTrackingResponse,
  type WorkspaceTrackingTask,
};

export const LABEL_ENABLED_ZH = '自动跟踪：开启（盯盘每 30 分钟 · 内部资料北京时间 11:00 扫描，Core 每交易日、Watch 每周 · 涨跌 ±4% 触发）';
export const LABEL_DISABLED_ZH = '自动跟踪：关闭';
export const LABEL_INCOMPLETE_ZH = '自动跟踪：不完整';

function formatNextRunAt(task: WorkspaceTrackingTask): string {
  if (!task.exists || task.status === 'missing') {
    return '缺失';
  }
  if (task.status === 'paused') {
    return '已暂停';
  }
  if (task.trigger_type === 'price') {
    return '实时行情触发 (±4%)';
  }
  if (!task.next_run_at) {
    return '待排期';
  }
  const dt = new Date(task.next_run_at);
  if (Number.isNaN(dt.getTime())) {
    return task.next_run_at;
  }
  return dt.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

export function WorkspaceTrackingToggle({
  workspaceId,
  readOnly = false,
  compact = false,
}: {
  workspaceId: string;
  readOnly?: boolean;
  compact?: boolean;
}): React.ReactElement | null {
  const [expanded, setExpanded] = useState(false);

  const { data, isLoading, isError } = useWorkspaceTracking(workspaceId);
  const mutation = useUpdateWorkspaceTracking(workspaceId);

  if (!workspaceId || isError || (isLoading && !data)) {
    return null;
  }

  const status = data?.status ?? 'disabled';
  if (status === 'not_applicable') {
    return null;
  }
  const isEnabled = status === 'enabled';
  const isIncomplete = status === 'incomplete';
  const canEdit = data?.can_edit ?? !readOnly;
  const tasks = data?.tasks ?? [];

  const statusText =
    status === 'enabled'
      ? LABEL_ENABLED_ZH
      : status === 'incomplete'
        ? LABEL_INCOMPLETE_ZH
        : LABEL_DISABLED_ZH;

  const handleToggle = () => {
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(!isEnabled);
  };

  const handlePauseAll = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(false);
  };

  const handleRepair = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(true);
  };

  return (
    <div
      className="flex flex-col items-center relative"
      data-testid="workspace-tracking-toggle"
      data-tracking-status={status}
    >
      <div
        className={`inline-flex items-center gap-2 rounded-full border transition-colors ${
          compact ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-1.5 text-xs'
        }`}
        style={{
          borderColor: isIncomplete
            ? 'rgba(245, 158, 11, 0.45)'
            : isEnabled
              ? 'rgba(16, 185, 129, 0.35)'
              : 'var(--color-border-muted)',
          backgroundColor: isIncomplete
            ? 'rgba(245, 158, 11, 0.08)'
            : isEnabled
              ? 'rgba(16, 185, 129, 0.08)'
              : 'var(--color-bg-elevated, transparent)',
          color: 'var(--color-text-secondary)',
        }}
      >
        {isIncomplete ? (
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-500" />
        ) : (
          <Activity
            className={`h-3.5 w-3.5 shrink-0 ${
              isEnabled ? 'text-emerald-500' : 'opacity-60'
            }`}
          />
        )}

        <span
          className={compact ? 'max-w-[260px] truncate' : ''}
          title={statusText}
          data-testid="workspace-tracking-label"
        >
          {compact && isEnabled ? '自动跟踪：开启' : statusText}
        </span>

        {isIncomplete && canEdit && (
          <>
            <button
              type="button"
              onClick={handlePauseAll}
              disabled={mutation.isPending}
              data-testid="workspace-tracking-pause-all"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-zinc-500/20 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-500/30 transition-colors disabled:opacity-50"
            >
              <PauseCircle className="h-3 w-3" />
              <span>全部暂停</span>
            </button>
            <button
              type="button"
              onClick={handleRepair}
              disabled={mutation.isPending}
              data-testid="workspace-tracking-repair"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/20 text-amber-600 dark:text-amber-400 hover:bg-amber-500/30 transition-colors disabled:opacity-50"
            >
              <Wrench className="h-3 w-3" />
              <span>修复</span>
            </button>
          </>
        )}

        <button
          type="button"
          role="switch"
          aria-checked={isEnabled}
          aria-label="自动跟踪开关"
          disabled={!canEdit || mutation.isPending}
          onClick={handleToggle}
          data-testid="workspace-tracking-switch"
          className={`relative inline-flex h-4 w-7 shrink-0 rounded-full transition-colors duration-150 ${
            !canEdit ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
          } ${isEnabled ? 'bg-emerald-500' : 'bg-zinc-400/50 dark:bg-zinc-600'}`}
        >
          <span
            className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform duration-150 mt-0.5 ${
              isEnabled ? 'translate-x-3.5' : 'translate-x-0.5'
            }`}
          />
        </button>

        {tasks.length > 0 && (
          <button
            type="button"
            onClick={() => setExpanded((prev) => !prev)}
            aria-expanded={expanded}
            aria-label="展开任务下次运行时间"
            data-testid="workspace-tracking-expand"
            className="p-0.5 rounded hover:bg-foreground/10 transition-colors"
            title="查看各自动任务下次运行时间"
          >
            {expanded ? (
              <ChevronUp className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" />
            )}
          </button>
        )}
      </div>

      {expanded && tasks.length > 0 && (
        <div
          data-testid="workspace-tracking-tasks"
          className={`mt-2 rounded-lg border p-2.5 text-xs flex flex-col gap-1.5 ${
            compact
              ? 'absolute top-full right-0 z-30 min-w-[300px] shadow-lg'
              : 'w-full max-w-[520px]'
          }`}
          style={{
            borderColor: 'var(--color-border-muted)',
            backgroundColor: 'var(--color-bg-page)',
            color: 'var(--color-text-secondary)',
          }}
        >
          {tasks.map((task) => (
            <div
              key={task.name}
              className="flex items-center justify-between gap-3 py-0.5"
              data-testid={`tracking-task-${task.name}`}
            >
              <div className="flex items-center gap-1.5 min-w-0">
                <span
                  className={`inline-block size-1.5 rounded-full shrink-0 ${
                    task.status === 'active'
                      ? 'bg-emerald-500'
                      : task.status === 'paused'
                        ? 'bg-amber-500'
                        : 'bg-rose-500'
                  }`}
                />
                <span className="font-mono text-[11px] truncate">{task.name}</span>
              </div>
              <div
                className="flex items-center gap-1 shrink-0 text-[11px]"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                <Clock className="h-3 w-3 opacity-70" />
                <span>下次运行：{formatNextRunAt(task)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
