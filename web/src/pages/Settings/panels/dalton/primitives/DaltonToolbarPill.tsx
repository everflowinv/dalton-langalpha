import type { ReactNode } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

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
