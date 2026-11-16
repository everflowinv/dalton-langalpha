import type { CSSProperties, ReactNode } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export type DaltonToolbarChipTone = 'neutral' | 'success' | 'warning' | 'danger';

const CHIP_TONE_VAR: Record<Exclude<DaltonToolbarChipTone, 'neutral'>, string> = {
  success: 'var(--color-success)',
  warning: 'var(--color-warning)',
  danger: 'var(--color-loss)',
};

export function toolbarChipStyle(tone: DaltonToolbarChipTone = 'neutral'): CSSProperties {
  if (tone === 'neutral') {
    return {
      borderColor: 'var(--color-shell-edge, var(--color-border-muted))',
      backgroundColor: 'var(--color-bg-elevated)',
      color: 'var(--color-text-secondary)',
    };
  }
  const colorVar = CHIP_TONE_VAR[tone];
  return {
    borderColor: `color-mix(in srgb, ${colorVar} 34%, transparent)`,
    backgroundColor: `color-mix(in srgb, ${colorVar} 10%, transparent)`,
    color: colorVar,
  };
}

export const TOOLBAR_CHIP_CLASS =
  'inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs whitespace-nowrap transition-colors';

export interface DaltonToolbarChipProps {
  tone?: DaltonToolbarChipTone;
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
  ariaExpanded?: boolean;
  testId?: string;
  className?: string;
}

/** Lightweight single-line toolbar pill for ChatView / ThreadGallery header controls. */
export function DaltonToolbarChip({
  tone = 'neutral',
  children,
  onClick,
  disabled = false,
  title,
  ariaLabel,
  ariaExpanded,
  testId,
  className,
}: DaltonToolbarChipProps) {
  const style = toolbarChipStyle(tone);
  if (!onClick) {
    return (
      <div
        role="status"
        data-testid={testId}
        title={title}
        className={cn(TOOLBAR_CHIP_CLASS, className)}
        style={style}
      >
        {children}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
      aria-expanded={ariaExpanded}
      data-testid={testId}
      className={cn(
        TOOLBAR_CHIP_CLASS,
        'press cursor-pointer hover:bg-(--color-bg-subtle) disabled:cursor-not-allowed disabled:opacity-60',
        className,
      )}
      style={style}
    >
      {children}
    </button>
  );
}

export interface DaltonToolbarPillProps {
  id?: string;
  label: ReactNode;
  active?: boolean;
  count?: number | string;
  onClick: () => void;
  disabled?: boolean;
  testId?: string;
  className?: string;
}

/** Selectable tab pill backed by upstream Button + Badge primitives. */
export function DaltonToolbarPill({
  id,
  label,
  active = false,
  count,
  onClick,
  disabled = false,
  testId,
  className,
}: DaltonToolbarPillProps) {
  return (
    <Button
      id={id}
      type="button"
      role="tab"
      aria-selected={active}
      variant={active ? 'default' : 'outline'}
      size="sm"
      disabled={disabled}
      onClick={onClick}
      data-testid={testId}
      className={cn('h-8 gap-1.5 rounded-full px-3 text-xs font-medium', className)}
    >
      <span>{label}</span>
      {count !== undefined && (
        <Badge
          variant="muted"
          className="px-1.5 py-0 text-[11px] tabular-nums"
          style={
            active
              ? {
                  backgroundColor: 'color-mix(in srgb, currentColor 18%, transparent)',
                  color: 'inherit',
                }
              : undefined
          }
        >
          {count}
        </Badge>
      )}
    </Button>
  );
}
