import type { ReactElement } from 'react';
import { useTranslation } from 'react-i18next';
import { BellRing } from 'lucide-react';
import { useLocale } from '@/hooks/useLocale';
import type { MarketTarget } from '../../utils/api';
import type { MarketAlert } from '../../session/marketWatchEvents';
import { marketAlertLabel } from '../marketTargetLabels';

interface MarketAlertRowProps {
  alert: MarketAlert;
  /** The thread's targets, which fill in what a notice left out. */
  targets?: readonly MarketTarget[] | null;
}

/**
 * A price alert in the transcript, in the divider vocabulary the other system
 * events use: where an alert landed inside a running turn, and above a turn an
 * alert started. Never a user bubble, since nobody typed it.
 */
export function MarketAlertRow({ alert, targets }: MarketAlertRowProps): ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const label = marketAlertLabel(alert, targets, t, locale);
  return (
    <div className="my-1" data-market-alert={alert.source}>
      <div className="flex items-center gap-3 py-2">
        <div className="min-w-4 flex-1" style={{ borderTop: '1px solid var(--color-border-muted)' }} />
        <span className="inline-flex min-w-0 items-center gap-1.5 text-xs tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
          <BellRing aria-hidden="true" className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
          <span>{label}</span>
        </span>
        <div className="min-w-4 flex-1" style={{ borderTop: '1px solid var(--color-border-muted)' }} />
      </div>
    </div>
  );
}
