import { useState, type ReactElement, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { mediumTime } from '@/lib/format';
import { Button } from '@/components/ui/button';
import PulseDot from '@/components/ui/pulse-dot';
import type { MarketTarget } from '../utils/api';
import { armedTargets } from '../session/marketWatchEvents';
import type { MarketWatchState } from '../hooks/utils/streamEventHandlers';
import { conditionLabel, targetConditionLabel, targetStatusLabel } from './marketTargetLabels';

const TERTIARY = { color: 'var(--color-text-tertiary)' as const };

// Locale-aware clock (matches the prior `toLocaleTimeString()` shape: h:m:s).

/** A titled panel section. Phase 1 renders only the market-watch section; later
 *  status sections (todos, skills, background tasks) append as siblings. The
 *  pulse marks a section with something live in it. */
function StatusSection({
  title,
  caption,
  live = true,
  children,
}: {
  title: string;
  caption?: string;
  live?: boolean;
  children: ReactNode;
}): ReactElement {
  return (
    <section className="mb-4">
      <div className="mb-2 flex items-center gap-2">
        {live && <PulseDot />}
        <span className="text-xs font-semibold uppercase tracking-wide" style={TERTIARY}>
          {title}
        </span>
        {caption && (
          <span className="ml-auto text-[0.6875rem]" style={TERTIARY}>
            {caption}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

/** The streamed quote block, treated as opaque preformatted text: the first line
 *  is the backend's "As of …" header (muted caption), the rest are quote rows
 *  with tabular figures. No parsing — prices are never pulled out of it. */
function QuoteBlock({ content }: { content: string }): ReactElement {
  const newlineIdx = content.indexOf('\n');
  const header = newlineIdx === -1 ? content : content.slice(0, newlineIdx);
  const rows = newlineIdx === -1 ? '' : content.slice(newlineIdx + 1);
  return (
    <div className="mt-3">
      <div className="text-[0.6875rem]" style={TERTIARY}>
        {header}
      </div>
      {rows && (
        <div
          className="mt-1.5 whitespace-pre-wrap wrap-break-word tabular-nums text-xs leading-relaxed"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {rows}
        </div>
      )}
    </div>
  );
}

/** One target: its condition, where it stands, and a cancel while it is armed. */
function TargetRow({
  target,
  now,
  onCancel,
}: {
  target: MarketTarget;
  now: number;
  onCancel?: ((targetId: string) => Promise<void>) | null;
}): ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const [cancelling, setCancelling] = useState(false);
  const [failed, setFailed] = useState(false);

  const cancel = () => {
    if (!onCancel) return;
    setCancelling(true);
    setFailed(false);
    onCancel(target.target_id).then(
      () => setCancelling(false),
      () => {
        setCancelling(false);
        setFailed(true);
      },
    );
  };

  return (
    <li className="flex items-start gap-3 py-2" data-target-status={target.status}>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {target.symbol}
          </span>
          <span className="tabular-nums" style={{ color: 'var(--color-text-secondary)' }}>
            {targetConditionLabel(target, t, locale)}
          </span>
        </div>
        <div className="mt-0.5 text-xs tabular-nums" style={TERTIARY}>
          {targetStatusLabel(target, t, locale, now)}
        </div>
        {failed && (
          <div className="mt-0.5 text-xs" style={{ color: 'var(--color-loss)' }} role="alert">
            {t('chat.marketWatch.cancelTargetFailed')}
          </div>
        )}
      </div>
      {target.status === 'armed' && onCancel && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 shrink-0 px-2 text-xs"
          style={{ color: 'var(--color-text-secondary)' }}
          disabled={cancelling}
          onClick={cancel}
          aria-label={t('chat.marketWatch.cancelTargetLabel', {
            symbol: target.symbol,
            condition: conditionLabel(target.kind, target.value, locale),
          })}
        >
          {cancelling ? t('chat.marketWatch.cancellingTarget') : t('chat.marketWatch.cancelTarget')}
        </Button>
      )}
    </li>
  );
}

export interface StatusPanelProps {
  marketWatch?: MarketWatchState | null;
  /** Cancels an armed target; rejects when the request fails. Omitted, the
   *  targets show without a cancel. */
  onCancelTarget?: ((targetId: string) => Promise<void>) | null;
}

/**
 * The file panel's Status tab body: the live market watch (symbols + the quote
 * block streamed by `market_watch_update` events), then the thread's price
 * targets: armed ones and those settled in the last day.
 * Structured so later status surfaces can be appended as more sections.
 */
export default function StatusPanel({ marketWatch, onCancelTarget }: StatusPanelProps): ReactElement {
  const { t } = useTranslation();
  const locale = useLocale();
  const symbols = marketWatch?.symbols ?? [];
  const targets = marketWatch?.targets ?? [];
  const content = marketWatch?.content;
  const timestamp = marketWatch?.timestamp;
  const armedCount = armedTargets(targets).length;
  // Only an armed target's time left moves.
  const now = useNow(60_000, armedCount > 0);

  const updatedCaption =
    timestamp != null
      ? t('chat.marketWatch.panelUpdated', {
          time: mediumTime(timestamp * 1000, locale),
          defaultValue: 'Updated {{time}}',
        })
      : undefined;

  return (
    <div className="h-full overflow-y-auto px-4 py-4">
      {/* With only targets, the empty watch list would read as nothing watched. */}
      {(symbols.length > 0 || targets.length === 0) && (
        <StatusSection
          title={t('chat.marketWatch.panelTitle', { defaultValue: 'Market watch' })}
          caption={updatedCaption}
        >
          {symbols.length === 0 ? (
            <p className="text-sm" style={TERTIARY}>
              {t('chat.marketWatch.panelEmpty', { defaultValue: 'No tickers are being watched.' })}
            </p>
          ) : (
            <>
              <div className="flex flex-wrap gap-1.5">
                {symbols.map((symbol) => (
                  <span
                    key={symbol}
                    className="inline-flex items-center rounded-md px-2 py-0.5 text-sm font-medium"
                    style={{
                      background: 'var(--color-bg-subtle)',
                      color: 'var(--color-text-primary)',
                    }}
                  >
                    {symbol}
                  </span>
                ))}
              </div>
              {content ? (
                <QuoteBlock content={content} />
              ) : (
                <p className="mt-3 text-xs" style={TERTIARY}>
                  {t('chat.marketWatch.panelWaiting', {
                    defaultValue: 'Live prices appear here while the agent is working.',
                  })}
                </p>
              )}
            </>
          )}
        </StatusSection>
      )}
      {targets.length > 0 && (
        <StatusSection title={t('chat.marketWatch.targetsTitle')} live={armedCount > 0}>
          <ul className="divide-y divide-(--color-border-muted)">
            {targets.map((target) => (
              <TargetRow key={target.target_id} target={target} now={now} onCancel={onCancelTarget} />
            ))}
          </ul>
        </StatusSection>
      )}
    </div>
  );
}
