import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';
import { payerLabel, type Payer } from '@/lib/modelPayer';

/** The small tag set beside a model or a setting's name. */
export function NameTag({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cn('flex-none rounded px-[5px] py-px text-[0.625rem] font-medium leading-normal', className)}
      style={{ backgroundColor: 'var(--color-bg-tag)', color: 'var(--color-text-tertiary)' }}
    >
      {children}
    </span>
  );
}

/** Who pays for a turn on a model, set beside its name. Nothing for a model
 *  with no payer, so a caller passes whatever its map holds. */
export function PayerBadge({ payer, className }: {
  payer: Payer | undefined;
  className?: string;
}) {
  const { t } = useTranslation();
  if (!payer) return null;
  return <NameTag className={className}>{payerLabel(t, payer)}</NameTag>;
}
