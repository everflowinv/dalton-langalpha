import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { SettingRow } from '../../../components/SettingsLayout';

export interface DaltonKeyValueListProps {
  children: ReactNode;
  className?: string;
  empty?: ReactNode;
  isEmpty?: boolean;
}

/** Bordered list container using upstream .settings-card hairline dividers. */
export function DaltonKeyValueList({
  children,
  className,
  empty,
  isEmpty = false,
}: DaltonKeyValueListProps) {
  if (isEmpty && empty) {
    return (
      <div
        className={cn('settings-card px-3.5 py-3 text-xs', className)}
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        {empty}
      </div>
    );
  }
  return (
    <div className={cn('settings-card', className)}>
      {children}
    </div>
  );
}

export interface DaltonItemRowProps {
  leading?: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  children?: ReactNode;
  className?: string;
  testId?: string;
}

/** Standard list item row built on upstream SettingRow, with optional expanded detail body. */
export function DaltonItemRow({
  leading,
  title,
  subtitle,
  meta,
  trailing,
  children,
  className,
  testId,
}: DaltonItemRowProps) {
  const composedLabel = leading ? (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="shrink-0">{leading}</span>
      <span className="inline-flex flex-wrap items-center gap-2">{title}</span>
    </span>
  ) : (
    <span className="inline-flex flex-wrap items-center gap-2">{title}</span>
  );

  const composedDesc =
    subtitle || meta ? (
      <span className="block space-y-0.5">
        {subtitle && <span className="block break-words">{subtitle}</span>}
        {meta && (
          <span
            className="block text-[11px] leading-4"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {meta}
          </span>
        )}
      </span>
    ) : undefined;

  return (
    <div data-testid={testId} className={className}>
      <SettingRow label={composedLabel} description={composedDesc}>
        {trailing ? <div className="flex shrink-0 items-center gap-1.5">{trailing}</div> : undefined}
      </SettingRow>
      {children && (
        <div
          className="px-[18px] pb-3.5 pt-2.5 border-t"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          {children}
        </div>
      )}
    </div>
  );
}

export interface DaltonKeyValueItem {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  testId?: string;
}

export interface DaltonKeyValueGridProps {
  items: DaltonKeyValueItem[];
  columns?: 2 | 3 | 4;
  className?: string;
}

/** Structured read-only metric / fact grid matching upstream .settings-card + Quiet Workspace typography. */
export function DaltonKeyValueGrid({
  items,
  columns = 3,
  className,
}: DaltonKeyValueGridProps) {
  const gridCols =
    columns === 2
      ? 'grid-cols-1 sm:grid-cols-2'
      : columns === 4
        ? 'grid-cols-2 sm:grid-cols-4'
        : 'grid-cols-1 sm:grid-cols-3';

  return (
    <div className={cn('settings-card grid gap-3 px-4 py-3', gridCols, className)}>
      {items.map((item, idx) => (
        <div key={idx} data-testid={item.testId} className="min-w-0 space-y-0.5">
          <div
            className="text-[11px] font-medium uppercase tracking-wider"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {item.label}
          </div>
          <div
            className="text-sm font-medium tabular-nums truncate"
            style={{ color: 'var(--color-text-primary)' }}
          >
            {item.value}
          </div>
          {item.hint && (
            <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              {item.hint}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
