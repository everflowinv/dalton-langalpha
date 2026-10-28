import { useEffect, useId, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router';
import { TradingLevelAgreedOn, TradingLevelName } from '@/components/trading/TradingLevelText';
import { RadioChoice } from '@/components/ui/choice';
import { useTradingPermissionChoice } from '@/hooks/useTradingPermissionChoice';
import { TRADING_LEVELS, TRADING_LEVEL_INFO, TRADING_PERMISSION_ANCHOR } from '@/lib/tradingPermission';
import { cn } from '@/lib/utils';
import type { TradingLevel } from '@/types/api';

/**
 * The user's trading permission: how far the agent may go with orders at their
 * brokerages. What a pick does, and the agreement behind the two levels that
 * skip approval, is `useTradingPermissionChoice`'s; this draws it for Settings.
 */
export function TradingPermissionSection() {
  const { t } = useTranslation();
  const { hash } = useLocation();
  const headingId = useId();
  const descId = useId();
  const groupName = useId();
  const sectionRef = useRef<HTMLElement>(null);
  const { permission, shown, pendingLevel, agreedDate, select, dialog } = useTradingPermissionChoice();
  const loaded = permission.data !== undefined;

  // A deep link lands on the section once it has its height. Before that the
  // cards are not drawn, and a scroll to a short page stops short of it.
  useEffect(() => {
    if (!loaded || hash !== `#${TRADING_PERMISSION_ANCHOR}`) return;
    sectionRef.current?.scrollIntoView?.({ block: 'start' });
  }, [loaded, hash]);

  return (
    <section
      ref={sectionRef}
      id={TRADING_PERMISSION_ANCHOR}
      aria-labelledby={headingId}
      className="scroll-mt-4 space-y-3"
    >
      <div>
        <div className="flex items-baseline justify-between gap-3">
          <h3 id={headingId} className="text-[0.8125rem] font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('settings.tradingPermission.title')}
          </h3>
          {pendingLevel && (
            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('common.saving')}
            </span>
          )}
        </div>
        <p id={descId} className="mt-0.5 text-xs leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('settings.tradingPermission.desc')}
        </p>
      </div>

      {/* Also over cards already drawn: a reread after a save whose answer was
          lost keeps the old level on screen, and the save may have landed. */}
      {permission.isError && (
        <div className="flex items-center gap-2 text-xs">
          <span style={{ color: 'var(--color-loss)' }}>{t('settings.tradingPermission.loadFailed')}</span>
          <button
            type="button"
            onClick={() => void permission.refetch()}
            className="underline underline-offset-2"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('common.retry')}
          </button>
        </div>
      )}

      {permission.isLoading && (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('common.loading')}</p>
      )}

      {loaded && (
        <div role="radiogroup" aria-labelledby={headingId} aria-describedby={descId} className="flex flex-col gap-2">
          {TRADING_LEVELS.map((level) => (
            <LevelCard
              key={level}
              name={groupName}
              level={level}
              checked={shown === level}
              agreedDate={agreedDate}
              onSelect={select}
            />
          ))}
        </div>
      )}

      {dialog}
    </section>
  );
}

function LevelCard({
  name,
  level,
  checked,
  agreedDate,
  onSelect,
}: {
  name: string;
  level: TradingLevel;
  checked: boolean;
  /** When the current agreement was accepted; drawn only on the chosen card
   *  of a level that needed it. */
  agreedDate: string | null;
  onSelect: (level: TradingLevel) => void;
}) {
  const { t } = useTranslation();
  const labelId = useId();
  const descId = useId();
  const info = TRADING_LEVEL_INFO[level];

  // Named by its label alone, so the description is read as one rather than
  // as part of the name.
  return (
    <RadioChoice
      name={name}
      value={level}
      checked={checked}
      onSelect={onSelect}
      aria-labelledby={labelId}
      aria-describedby={descId}
      markClassName="mt-0.5"
      className={cn(
        'items-start gap-3 rounded-lg px-3 py-2.5 transition-colors',
        checked ? 'bg-(--color-bg-tag)' : 'bg-(--color-bg-card) hover:bg-(--color-bg-hover)',
      )}
      style={{ border: `1px solid ${checked ? 'var(--color-text-primary)' : 'var(--color-border-muted)'}` }}
    >
      <span className="min-w-0 flex-1">
        {/* No glyph per level here, so the warning is drawn on the name. */}
        <TradingLevelName level={level} id={labelId} flagWarning className="text-[0.8125rem] font-medium" />
        <span id={descId} className="mt-0.5 block text-xs leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
          {t(info.descriptionKey)}
        </span>
        <TradingLevelAgreedOn
          level={level}
          chosen={checked}
          agreedDate={agreedDate}
          className="text-(--color-text-quaternary)"
        />
      </span>
    </RadioChoice>
  );
}
