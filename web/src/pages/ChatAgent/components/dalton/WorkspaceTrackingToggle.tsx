/* eslint-disable react-refresh/only-export-components */
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { Activity, ChevronDown, Clock, PauseCircle, Wrench } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { ToggleSwitch } from '@/components/ui/switch';
// Direct module imports: the primitives barrel also re-exports the
// confirm dialog, which would pull its animation library onto first load.
import { TOOLBAR_CHIP_CLASS, toolbarChipStyle, type DaltonToolbarChipTone } from '@/components/dalton/primitives/DaltonToolbarPill';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { cn } from '@/lib/utils';
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

function formatTrackingTaskName(name: string, t: TFunction): string {
  if (name.startsWith('dalton-sector-')) {
    if (name.endsWith('-agenda')) return t('dalton.tracking.taskSectorAgenda');
    if (name.endsWith('-delta')) return t('dalton.tracking.taskSectorDelta');
  }
  if (name.endsWith('-watch')) return t('dalton.tracking.taskWatch');
  if (name.endsWith('-delta')) return t('dalton.tracking.taskDelta');
  if (name.endsWith('-move-up')) return t('dalton.tracking.taskMoveUp');
  if (name.endsWith('-move-down')) return t('dalton.tracking.taskMoveDown');
  return name;
}

function formatNextRunAt(
  task: WorkspaceTrackingTask,
  t: TFunction,
  locale: string,
): string {
  if (!task.exists || task.status === 'missing') {
    return t('dalton.tracking.statusMissing');
  }
  if (task.status === 'paused') {
    return t('dalton.tracking.statusPaused');
  }
  if (task.trigger_type === 'price') {
    return t('dalton.tracking.triggerPrice');
  }
  if (!task.next_run_at) {
    return t('dalton.tracking.statusPending');
  }
  return formatTimestamp(task.next_run_at, locale) || task.next_run_at;
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
  const { t } = useTranslation();
  const locale = useLocale();
  const [open, setOpen] = useState(false);

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

  const tone: DaltonToolbarChipTone = isIncomplete
    ? 'warning'
    : isEnabled
      ? 'success'
      : 'neutral';

  const dotColor = isIncomplete
    ? 'var(--color-warning)'
    : isEnabled
      ? 'var(--color-success)'
      : 'var(--color-text-quaternary)';

  const shortLabel = isIncomplete
    ? t('dalton.tracking.incompleteShort')
    : isEnabled
      ? t('dalton.tracking.enabledShort')
      : t('dalton.tracking.disabledShort');

  const detailText =
    status === 'disabled'
      ? t('dalton.tracking.disabledDetail')
      : t('dalton.tracking.enabledDetail');

  const handleToggle = () => {
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(!isEnabled);
  };

  const handlePauseAll = () => {
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(false);
  };

  const handleRepair = () => {
    if (!canEdit || mutation.isPending) return;
    mutation.mutate(true);
  };

  return (
    <div
      className="inline-flex items-center relative"
      data-testid="workspace-tracking-toggle"
      data-tracking-status={status}
      data-compact={compact ? 'true' : undefined}
    >
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            data-testid="workspace-tracking-expand"
            data-compact={compact ? 'true' : undefined}
            aria-expanded={open}
            aria-label={compact ? `${shortLabel} — ${t('dalton.tracking.expandAriaLabel')}` : t('dalton.tracking.expandAriaLabel')}
            title={detailText}
            className={cn(
              TOOLBAR_CHIP_CLASS,
              'press cursor-pointer hover:bg-(--color-bg-subtle)',
              compact && 'tap-target gap-1 px-2',
            )}
            style={toolbarChipStyle(tone)}
          >
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 rounded-full shrink-0"
              style={{ backgroundColor: dotColor }}
            />
            {compact ? (
              <Activity className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            ) : (
              <>
                <span data-testid="workspace-tracking-label">{shortLabel}</span>
                <ChevronDown className="h-3 w-3 shrink-0 opacity-70" />
              </>
            )}
          </button>
        </PopoverTrigger>

        <PopoverContent
          align="end"
          sideOffset={6}
          className="w-80 max-w-[calc(100vw-24px)] p-3 text-xs space-y-2.5"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full shrink-0"
                style={{ backgroundColor: dotColor }}
              />
              <span
                className="font-medium truncate"
                style={{ color: 'var(--color-text-primary)' }}
              >
                {shortLabel}
              </span>
            </div>
            <ToggleSwitch
              checked={isEnabled}
              onChange={handleToggle}
              disabled={!canEdit || mutation.isPending}
              testId="workspace-tracking-switch"
              ariaLabel={t('dalton.tracking.switchAriaLabel')}
            />
          </div>

          <p
            className="text-[11px] leading-relaxed"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {detailText}
          </p>

          {isIncomplete && canEdit && (
            <div className="flex items-center gap-2 pt-0.5">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={mutation.isPending}
                onClick={handlePauseAll}
                data-testid="workspace-tracking-pause-all"
                className="h-7 px-2.5 text-xs gap-1"
              >
                <PauseCircle className="h-3.5 w-3.5" />
                <span>{t('dalton.tracking.pauseAll')}</span>
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={mutation.isPending}
                onClick={handleRepair}
                data-testid="workspace-tracking-repair"
                className="h-7 px-2.5 text-xs gap-1"
              >
                <Wrench className="h-3.5 w-3.5" />
                <span>{t('dalton.tracking.repair')}</span>
              </Button>
            </div>
          )}

          {tasks.length > 0 && (
            <div
              data-testid="workspace-tracking-tasks"
              className="rule-t pt-2 flex flex-col gap-1.5"
            >
              {tasks.map((task) => {
                const taskDot =
                  task.status === 'active'
                    ? 'var(--color-success)'
                    : task.status === 'paused'
                      ? 'var(--color-warning)'
                      : 'var(--color-loss)';
                return (
                  <div
                    key={task.name}
                    className="flex items-center justify-between gap-3 py-0.5"
                    data-testid={`tracking-task-${task.name}`}
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span
                        aria-hidden="true"
                        className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
                        style={{ backgroundColor: taskDot }}
                      />
                      <span
                        className="truncate"
                        style={{ color: 'var(--color-text-secondary)' }}
                        title={task.name}
                      >
                        {formatTrackingTaskName(task.name, t)}
                      </span>
                    </div>
                    <div
                      className="flex items-center gap-1 shrink-0 text-[11px] tabular-nums"
                      style={{ color: 'var(--color-text-tertiary)' }}
                    >
                      <Clock className="h-3 w-3 opacity-70" />
                      <span>
                        {t('dalton.tracking.nextRunPrefix', {
                          time: formatNextRunAt(task, t, locale),
                        })}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
