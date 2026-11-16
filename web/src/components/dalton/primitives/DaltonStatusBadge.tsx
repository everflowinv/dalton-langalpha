import type { CSSProperties, ReactNode } from 'react';
import { Badge, type BadgeProps } from '@/components/ui/badge';
import { Loader } from '@/components/ui/loader';
import { cn } from '@/lib/utils';

export type DaltonStatusTone = 'success' | 'warning' | 'danger' | 'neutral' | 'accent';

const TONE_COLOR_VAR: Record<DaltonStatusTone, string> = {
  success: 'var(--color-success)',
  warning: 'var(--color-warning)',
  danger: 'var(--color-loss)',
  neutral: 'var(--color-text-tertiary)',
  accent: 'var(--color-accent-primary)',
};

const TONE_BADGE_VARIANT: Record<DaltonStatusTone, BadgeProps['variant']> = {
  success: 'success',
  warning: 'warning',
  danger: 'destructive',
  neutral: 'muted',
  accent: 'info',
};

export interface DaltonStatusBadgeProps {
  tone?: DaltonStatusTone;
  label: ReactNode;
  dot?: boolean;
  className?: string;
  testId?: string;
}

/** Token-backed status badge built on upstream Badge without raw colors or dark:* overrides. */
export function DaltonStatusBadge({
  tone = 'neutral',
  label,
  dot = false,
  className,
  testId,
}: DaltonStatusBadgeProps) {
  const colorVar = TONE_COLOR_VAR[tone];
  const style: CSSProperties = {
    color: tone === 'neutral' ? 'var(--color-text-secondary)' : colorVar,
    borderColor: `color-mix(in srgb, ${colorVar} 30%, transparent)`,
    backgroundColor: `color-mix(in srgb, ${colorVar} 10%, transparent)`,
  };

  return (
    <Badge
      variant={TONE_BADGE_VARIANT[tone]}
      data-testid={testId}
      style={style}
      className={cn(
        'gap-1.5 border px-2 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap',
        className,
      )}
    >
      {dot && (
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 rounded-full shrink-0"
          style={{ backgroundColor: colorVar }}
        />
      )}
      <span>{label}</span>
    </Badge>
  );
}

export type DaltonNoticeTone = 'error' | 'warning' | 'info' | 'success';

const NOTICE_COLOR_VAR: Record<DaltonNoticeTone, string> = {
  error: 'var(--color-loss)',
  warning: 'var(--color-warning)',
  info: 'var(--color-accent-primary)',
  success: 'var(--color-success)',
};

export interface DaltonInlineNoticeProps {
  tone?: DaltonNoticeTone;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
  testId?: string;
  role?: 'status' | 'alert';
}

/** Inline notice banner using upstream semantic CSS variables. */
export function DaltonInlineNotice({
  tone = 'info',
  children,
  action,
  className,
  testId,
  role,
}: DaltonInlineNoticeProps) {
  const colorVar = NOTICE_COLOR_VAR[tone];
  const isAlert = tone === 'error' || tone === 'warning';
  return (
    <div
      role={role ?? (isAlert ? 'alert' : 'status')}
      data-testid={testId}
      className={cn(
        'flex flex-wrap items-start justify-between gap-2 rounded-md border px-3 py-2 text-xs leading-5',
        className,
      )}
      style={{
        borderColor: `color-mix(in srgb, ${colorVar} 32%, transparent)`,
        backgroundColor:
          tone === 'info'
            ? 'var(--color-bg-elevated)'
            : `color-mix(in srgb, ${colorVar} 8%, transparent)`,
        color:
          tone === 'info'
            ? 'var(--color-text-secondary)'
            : colorVar,
      }}
    >
      <div className="min-w-0 flex-1 break-words">{children}</div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export interface DaltonLoadingRowProps {
  label?: string;
  size?: number;
  className?: string;
}

/** Inline loading row backed by upstream ASCII Loader (replaces Loader2 spinners). */
export function DaltonLoadingRow({
  label,
  size = 14,
  className,
}: DaltonLoadingRowProps) {
  return (
    <div
      className={cn('inline-flex items-center gap-2 text-xs py-1', className)}
      style={{ color: 'var(--color-text-tertiary)' }}
    >
      <span aria-hidden="true" className="inline-flex">
        <Loader size={size} label="" />
      </span>
      {label && <span>{label}</span>}
    </div>
  );
}
